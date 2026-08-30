"""CDP agent — pure library for crawling a single blog.

This module is a pure CDP library. It accepts navigation/fetch callbacks
from the worker and does NOT own tab lifecycle. On CDP failure, it raises
TabDeadError — the caller (worker) decides whether to retry.

Functions:
    crawl_blog(navigate_fn, fetch_page_fn, username, ...) → dict
        Crawl a single blog. Raises TabDeadError on CDP failure.
    _new_tab_url(browser_ws, target_url) → (ws_url, target_id)
        Internal helper — creates a tab. Used by worker.
    close_tab(browser_ws, target_id) → None
        Internal helper — closes a tab. Used by worker.
    detect_login_wall / detect_dead / detect_end_of_posts → bool
        Pure detection functions.
    compute_page_metrics(html, source_blog) → dict
        Pure extraction wrapper.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from cdp_use import CDPClient
from cdp_wrapper import TabDeadError, cdp_send

from cache import (
    CACHE_DIR,
    append_log,
    index_status,
    load_entry,
    save_entry,
)
from config import (
    CDP_COMMAND_TIMEOUT,
    CONTENT_WAIT_TIMEOUT,
    DEAD_PHRASES,
    DELAY_MAX,
    DELAY_MIN,
    END_PHRASES,
    LOGIN_WALL_PHRASES,
)
from extractor import check_limit, extract_from_html
from eventlog import info as ev, warn as ev_warn, error as ev_err

logger = logging.getLogger("tumblr-agent")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LoginWallDetected(Exception):
    """Raised when a login wall is detected. Worker halts the pipeline."""


# ---------------------------------------------------------------------------
# Internal helpers — used by worker for tab lifecycle
# ---------------------------------------------------------------------------


def _extract_browser_ws(browser_ws: str) -> str:
    """Given a browser HTTP endpoint, return the browser WebSocket debugger URL."""
    import urllib.request

    base = browser_ws.replace("ws://", "http://").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/version", timeout=5) as resp:
            info = json.loads(resp.read())
        return info.get("webSocketDebuggerUrl", "")
    except Exception as exc:
        raise RuntimeError(
            f"Cannot get browser WebSocket URL from {browser_ws}: {exc}"
        ) from exc


async def _new_tab_url(browser_ws: str, target_url: str) -> tuple[str, str]:
    """Create a new CDP tab and return (page_ws_url, target_id)."""
    browser_ws_url = _extract_browser_ws(browser_ws)
    if not browser_ws_url:
        raise RuntimeError("Browser has no WebSocket debugger URL")

    create_client = CDPClient(browser_ws_url)
    await create_client.start()
    try:
        result = await create_client.send.Target.createTarget(
            params={"url": target_url}
        )
        target_id = result.get("targetId")
    except Exception as exc:
        await create_client.stop()
        raise RuntimeError(
            f"Target.createTarget failed for {target_url}: {exc}"
        ) from exc
    finally:
        await create_client.stop()

    if not target_id:
        raise RuntimeError(f"Target.createTarget returned no targetId for {target_url}")

    base = browser_ws.replace("ws://", "http://").rstrip("/")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        try:
            import urllib.request as _urllib

            with _urllib.urlopen(f"{base}/json/list", timeout=5) as resp:
                targets = json.loads(resp.read())
            for t in targets:
                if t.get("type") == "page" and t.get("id") == target_id:
                    ws_url = t.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url, target_id
        except Exception:  # noqa: BLE001
            pass

    try:
        await close_tab(browser_ws, target_id)
    except Exception:
        pass
    raise RuntimeError(
        f"Timed out waiting for new tab {target_url} (targetId={target_id})"
    )


async def close_tab(browser_ws: str, target_id: str) -> None:
    """Close a Chrome tab by its targetId.

    Tries CDP Target.closeTarget first, then falls back to the HTTP
    /json/close/{id} endpoint — which works even when the tab's CDP
    connection is dead.
    """
    import urllib.request

    # HTTP fallback always works (doesn't need the tab's CDP WS)
    try:
        base = browser_ws.replace("ws://", "http://").rstrip("/")
        urllib.request.urlopen(
            f"{base}/json/close/{target_id}", timeout=5
        )
        logger.info("Closed tab %s via HTTP", target_id)
        return
    except Exception:
        pass

    browser_ws_url = _extract_browser_ws(browser_ws)
    if not browser_ws_url:
        logger.warning(
            "Cannot close tab %s — browser has no WebSocket debugger URL", target_id
        )
        return

    client = CDPClient(browser_ws_url)
    await client.start()
    try:
        await client.send.Target.closeTarget(params={"targetId": target_id})
        logger.info("Closed tab targetId=%s", target_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to close tab %s: %s", target_id, exc)
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------


async def fetch_page_html(
    client: CDPClient,
    username: str,
    offset: int,
    timeout_ms: int = 30000,
) -> tuple[str, str]:
    """Navigate to tumblr.com/<username>?offset=<N> and return (html, final_url).

    Raises TabDeadError on CDP failure — worker owns recovery.
    """
    url = f"https://www.tumblr.com/{username}?offset={offset}"

    try:
        await cdp_send(client, "Page.navigate", {"url": url, "timeout": timeout_ms})
    except Exception as exc:
        logger.warning("Page.navigate failed for %s offset %d: %s", username, offset, exc)
        raise TabDeadError(f"Page.navigate failed: {exc}") from exc

    # ---- IMMEDIATE DEAD CHECK (user directive 2026-08-28) ----------------
    # Tumblr redirects dead/deactivated blogs to /blog-explorer or shows a
    # dead-phrase page. Without this we burn 10-30s scrolling + waiting on
    # the dead page before the crawl loop's detect_dead() fires. Grab the
    # URL + a snippet of text right after navigate and bail fast if dead.
    try:
        res = await cdp_send(
            client,
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({url: location.href, text: (document.body ? document.body.innerText : '').slice(0, 800)})",
                "returnByValue": True,
            },
        )
        snap = json.loads(res.get("result", {}).get("value", "{}"))
        snap_url = snap.get("url", "")
        snap_text = (snap.get("text") or "").lower()
        if "blog-explorer" in snap_url.lower():
            logger.info("DEAD (early): %s offset %d — blog-explorer redirect", username, offset)
            return "", snap_url
        for phrase in DEAD_PHRASES:
            if phrase in snap_text:
                logger.info("DEAD (early): %s offset %d — phrase '%s'", username, offset, phrase)
                return "", snap_url
    except Exception:
        pass  # If the quick check fails, fall through to normal flow
    # ----------------------------------------------------------------------
    SCROLL_DEADLINE = time.monotonic() + 30.0
    prev_cells = -1
    stable_rounds = 0
    while time.monotonic() < SCROLL_DEADLINE:
        try:
            await cdp_send(
                client,
                "Runtime.evaluate",
                {
                    "expression": (
                        "(function() {"
                        "  window.scrollTo(0, document.body.scrollHeight);"
                        "  return document.querySelectorAll('[data-cell-id]').length;"
                        "})()"
                    ),
                    "returnByValue": True,
                },
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)
        try:
            res = await cdp_send(
                client,
                "Runtime.evaluate",
                {
                    "expression": "document.querySelectorAll('[data-cell-id]').length",
                    "returnByValue": True,
                },
            )
            cell_count = int(res.get("result", {}).get("value", 0) or 0)
        except Exception:
            cell_count = 0
        if cell_count == prev_cells:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
            prev_cells = cell_count

    deadline = time.monotonic() + CONTENT_WAIT_TIMEOUT
    last_text = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        try:
            result = await cdp_send(
                client,
                "Runtime.evaluate",
                {
                    "expression": "document.body ? document.body.innerText : ''",
                    "returnByValue": True,
                },
            )
            new_text = result.get("result", {}).get("value", "")
            if new_text:
                last_text = new_text
                if len(new_text) > 100:
                    break
        except Exception:
            pass

    if len(last_text) <= 100:
        logger.warning(
            "Content wait timed out for %s offset %d after %.0fs",
            username,
            offset,
            CONTENT_WAIT_TIMEOUT,
        )

    try:
        result = await cdp_send(
            client,
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({html: document.documentElement.outerHTML, url: location.href})",
                "returnByValue": True,
            },
        )
        payload = result.get("result", {}).get("value", "{}")
        try:
            data = json.loads(payload)
            html = data.get("html", "")
            final_url = data.get("url", "")
        except Exception:
            html = payload
            final_url = ""
        return html, final_url
    except Exception as exc:
        logger.error("Failed to get page HTML for %s: %s", username, exc)
        raise TabDeadError(f"Runtime.evaluate failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Detection helpers (pure functions)
# ---------------------------------------------------------------------------


def detect_login_wall(page_text: str, html: str = "", url: str = "") -> bool:
    """Return True if the page is a login wall / human verification challenge.

    The ONLY reliable signal is the URL: Tumblr redirects to /login or
    /signup when the session is unauthenticated. Scanning page HTML for
    keywords (e.g. "recaptcha") causes false positives — that word
    appears in analytics/tracking scripts on every page, including
    authenticated ones. So we check the URL only.
    """
    u = url.lower()
    if "login" in u or "signup" in u:
        return True
    if "content_warning_wall" in u:
        return True
    return False


def detect_login_wall_detail(page_text: str, html: str = "", url: str = "") -> tuple[bool, str]:
    """Return (is_wall, reason) — like detect_login_wall but explains the match."""
    u = url.lower()
    if "login" in u:
        return True, "url:login"
    if "signup" in u:
        return True, "url:signup"
    if "content_warning_wall" in u:
        return True, "url:content_warning_wall"
    return False, ""


def detect_dead(page_text: str) -> bool:
    """Return True if the blog appears dead / private / gone."""
    combined = page_text.lower()
    for phrase in DEAD_PHRASES:
        if phrase in combined:
            return True
    return False


def detect_end_of_posts(page_text: str, html: str) -> bool:
    """Return True if the page signals no more posts are available."""
    combined = (page_text + html).lower()
    for phrase in END_PHRASES:
        if phrase in combined:
            return True
    return False


# ---------------------------------------------------------------------------
# Metrics wrapper (pure function)
# ---------------------------------------------------------------------------


def compute_page_metrics(html: str, source_blog: str | None) -> dict[str, Any]:
    """Run the canonical extractor on page HTML and return per-page metrics."""
    return extract_from_html(html, source_blog)


# ---------------------------------------------------------------------------
# Main crawl loop — PURE LIBRARY, accepts navigation callbacks, raises TabDeadError
# ---------------------------------------------------------------------------


async def crawl_blog(
    browser_ws: str,
    navigate_fn: Callable[[str, int], Awaitable[tuple[str, str]]],
    fetch_page_fn: Callable[[int], Awaitable[tuple[str, str]]],
    username: str,
    tier: int,
    *,
    unique_limit: int = 100,
    total_limit: int = 250,
    post_limit: int = 250,
    delay_min: float = DELAY_MIN,
    delay_max: float = DELAY_MAX,
    source_blog: str | None = None,
    cache_dir: Path | None = None,
    on_page: callable | None = None,
    on_progress: callable | None = None,
) -> dict[str, Any]:
    """Crawl a single blog from start to stop condition.

    PURE LIBRARY: accepts navigation/fetch callbacks from worker.
    Does NOT own tab lifecycle. Raises TabDeadError on CDP failure — worker owns recovery.

    Args:
        browser_ws: Browser HTTP endpoint (for tab recovery by worker).
        navigate_fn: (username, offset) -> (html, final_url) — navigates to page.
        fetch_page_fn: offset -> (html, final_url) — fetches page at offset (worker tab).
        username: Blog username to crawl.
        tier: Tier label for cache organization.
        unique_limit/total_limit/post_limit: Stop conditions.
        delay_min/delay_max: Random delay range between page fetches.
        source_blog: The blog that led us to this username.
        cache_dir: Override for cache root.
        on_page: Callback(username, page_usernames, tier) per page.

    Returns:
        Results dict with cumulative totals and per-page breakdown.

    Raises:
        TabDeadError: CDP connection died. Worker must close + reopen tab.
        LoginWallDetected: Login wall detected. Worker must halt pipeline.
    """
    cache_root = cache_dir or CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)

    all_usernames: list[str] = []
    all_occurrences: list[dict] = []
    per_page: list[dict] = []
    total_count = 0
    unique_count = 0
    posts_processed = 0
    dead = False
    dead_reason = ""

    # First page: use navigate_fn (navigates to blog URL)
    # Subsequent pages: use fetch_page_fn
    first_page = True
    for offset in range(0, post_limit, 20):
        if unique_count >= unique_limit or total_count >= total_limit or posts_processed >= post_limit:
            break

        try:
            if first_page:
                html, final_url = await navigate_fn(username, offset)
                first_page = False
            else:
                html, final_url = await fetch_page_fn(offset)
        except TabDeadError:
            raise
        except Exception as exc:
            logger.warning("Page fetch failed for %s offset %d: %s", username, offset, exc)
            raise TabDeadError(f"Page fetch failed: {exc}") from exc

        if detect_login_wall("", html, final_url):
            logger.warning("LOGIN/CONTENT WALL for %s at offset %d — halting", username, offset)
            raise LoginWallDetected(username)

        # Check for dead/end after content load
        page_text = ""
        try:
            # Extract text from HTML for detection
            import re
            page_text = re.sub(r"<[^>]+>", "", html)[:5000]
        except Exception:
            pass

        if detect_dead(page_text):
            logger.info("DEAD: %s at offset %d — no content", username, offset)
            dead = True
            dead_reason = "dead_phrase"
            break

        if detect_end_of_posts(page_text, html):
            logger.info("END: %s at offset %d — no more posts", username, offset)
            break

        result = compute_page_metrics(html, username)
        page_usernames = result.get("usernames", [])
        page_unique = result.get("unique_count", 0)
        page_total = result.get("total_count", 0)
        page_posts = result.get("posts_processed", 0)

        posts_processed += page_posts
        total_count += page_total
        unique_count += page_unique
        all_usernames.extend(page_usernames)
        all_occurrences.extend(result.get("occurrences", []))
        per_page.append({
            "offset": offset,
            "usernames": page_usernames,
            "unique": page_unique,
            "total": page_total,
            "posts": page_posts,
            "url": final_url,
        })

        if on_page:
            on_page(username, page_usernames, tier)
        if on_progress:
            on_progress(f"page_fetched:{username}:offset:{offset}:posts:{page_posts}:unique:{page_unique}")

        # Random delay between pages
        delay = random.uniform(delay_min, delay_max)
        logger.debug("Delay %.1fs before next page for %s", delay, username)
        await asyncio.sleep(delay)

        # Check stop conditions
        if unique_count >= unique_limit or total_count >= total_limit or posts_processed >= post_limit:
            break

    return {
        "username": username,
        "tier": tier,
        "status": "ok" if not dead else "dead",
        "unique_count": unique_count,
        "total_count": total_count,
        "posts_processed": posts_processed,
        "usernames": all_usernames,
        "all_occurrences": all_occurrences,
        "per_page": per_page,
        "dead": dead,
        "dead_reason": dead_reason,
        "source_blog": source_blog,
    }
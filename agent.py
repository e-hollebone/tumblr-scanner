"""CDP agent — pure library for crawling a single blog.

This module is a pure CDP library. It accepts a connected CDPClient (or
WS URL) and does NOT own tab lifecycle. On CDP failure, it raises
TabDeadError — the caller (worker) decides whether to retry.

Functions:
    crawl_blog(browser_ws, ws_url, username, ...) → dict
        Crawl a single blog. Raises TabDeadError on CDP failure.
    probe_blog(browser_ws, username, ...) → dict
        Fetch page 0 only, return date probe result.
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
from typing import Any

from cdp_use import CDPClient

from cache import (
    CACHE_DIR,
    append_log,
    index_status,
    load_entry,
    save_entry,
)
from config import (
    DEAD_PHRASES,
    DELAY_MAX,
    DELAY_MIN,
    END_PHRASES,
    LOGIN_WALL_PHRASES,
)
from extractor import check_limit, extract_from_html

logger = logging.getLogger("tumblr-agent")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LoginWallDetected(Exception):
    """Raised when a login wall is detected. Worker halts the pipeline."""


class TabDeadError(Exception):
    """Raised when the CDP connection dies. Worker owns recovery (close + reopen tab)."""


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

    raise RuntimeError(
        f"Timed out waiting for new tab {target_url} (targetId={target_id})"
    )


async def close_tab(browser_ws: str, target_id: str) -> None:
    """Close a Chrome tab by its targetId."""
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
        await client.send.Page.navigate(params={"url": url, "timeout": timeout_ms})
    except Exception as exc:
        logger.warning("Page.navigate failed for %s offset %d: %s", username, offset, exc)
        raise TabDeadError(f"Page.navigate failed: {exc}") from exc

    TIMEOUT = 20.0
    deadline = time.monotonic() + TIMEOUT
    last_text = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        try:
            result = await client.send.Runtime.evaluate(
                params={
                    "expression": "document.body ? document.body.innerText : ''",
                    "returnByValue": True,
                }
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
            TIMEOUT,
        )

    try:
        result = await client.send.Runtime.evaluate(
            params={
                "expression": "JSON.stringify({html: document.documentElement.outerHTML, url: location.href})",
                "returnByValue": True,
            }
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
    """Return True if the page is a login wall / human verification challenge."""
    combined = (page_text + " " + html + " " + url).lower()
    for phrase in LOGIN_WALL_PHRASES:
        if phrase in combined:
            return True
    if "content_warning_wall" in url.lower():
        return True
    if "recaptcha" in (html + " " + url).lower():
        return True
    return False


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
# Probe mode — lightweight date check before full crawl
# ---------------------------------------------------------------------------


async def probe_blog(
    browser_ws: str,
    username: str,
    *,
    cache_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Probe a blog: fetch page 0 only, extract dates, check against index.

    Creates and closes its own tab (used by worker before delegating to
    crawl_blog). Returns dict with skip/page_date_max/page_date_min/usernames.
    """
    cache_root = cache_dir or CACHE_DIR
    tab_target_id: str | None = None

    try:
        target_url = f"https://www.tumblr.com/{username}"
        ws_url, tab_target_id = await _new_tab_url(browser_ws, target_url)
        client = CDPClient(ws_url)
        await client.start()

        try:
            html, final_url = await fetch_page_html(client, username, offset=0)
        except TabDeadError:
            logger.warning("Probe failed for %s: tab died", username)
            return {
                "skip": False,
                "page_date_max": None,
                "page_date_min": None,
                "usernames": [],
            }

        if detect_login_wall("", html, final_url):
            logger.warning("LOGIN/CONTENT WALL during probe for %s — halting", username)
            raise LoginWallDetected(username)

        result = compute_page_metrics(html, username)
        page_date_max = result.get("page_date_max")
        page_date_min = result.get("page_date_min")
        usernames = result.get("usernames", [])

        should_skip = False
        if index_path:
            status = index_status(index_path, username)
            should_skip = status == "fresh"

        return {
            "skip": should_skip,
            "page_date_max": page_date_max,
            "page_date_min": page_date_min,
            "usernames": usernames,
        }
    except LoginWallDetected:
        raise
    except Exception as exc:
        logger.warning("Probe failed for %s: %s", username, exc)
        return {
            "skip": False,
            "page_date_max": None,
            "page_date_min": None,
            "usernames": [],
        }
    finally:
        if tab_target_id:
            try:
                await close_tab(browser_ws, tab_target_id)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main crawl loop — PURE LIBRARY, accepts ws_url, raises TabDeadError
# ---------------------------------------------------------------------------


async def crawl_blog(
    browser_ws: str,
    ws_url: str,
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
) -> dict[str, Any]:
    """Crawl a single blog from start to stop condition.

    PURE LIBRARY: accepts a connected CDPClient WS URL. Does NOT own tab
    lifecycle. Raises TabDeadError on CDP failure — worker owns recovery.

    Args:
        browser_ws: Browser HTTP endpoint (for tab recovery by worker).
        ws_url: Page WS URL of the tab to reuse.
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
    tier_dir = cache_root / str(tier)

    existing = load_entry(tier_dir / f"{username}.json")

    page_size = 20

    all_usernames: list[str] = []
    unique_set: set[str] = set()
    per_page_results: list[dict[str, Any]] = []
    posts_processed = 0
    total_posts = 0
    status = "running"
    dead = False
    dead_reason: str | None = None

    offset = 0
    page_date_min: str | None = None

    client = CDPClient(ws_url)
    await client.start()

    try:
        while True:
            if check_limit(
                unique_count=len(unique_set),
                total_count=len(all_usernames),
                posts_count=total_posts,
                unique_limit=unique_limit,
                total_limit=total_limit,
                post_limit=post_limit,
            ):
                logger.info(
                    "Limit reached for %s: unique=%d total=%d posts=%d",
                    username,
                    len(unique_set),
                    len(all_usernames),
                    posts_processed,
                )
                status = "limit_reached"
                break

            # Fetch page — raises TabDeadError on failure
            page_html_str, final_url = await fetch_page_html(client, username, offset)

            if not page_html_str:
                try:
                    text_result = await client.send.Runtime.evaluate(
                        params={
                            "expression": "document.body ? document.body.innerText : ''",
                            "returnByValue": True,
                        }
                    )
                    page_text = text_result.get("result", {}).get("value", "")
                except Exception:
                    page_text = ""

                if detect_login_wall(page_text, "", final_url):
                    logger.warning("LOGIN WALL for %s (empty HTML) — halting", username)
                    status = "login_wall"
                    raise LoginWallDetected(username)

                if detect_dead(page_text):
                    matched = None
                    text_lower = page_text.lower()
                    for phrase in DEAD_PHRASES:
                        if phrase in text_lower:
                            matched = phrase
                            break
                    dead = True
                    dead_reason = (
                        f"phrase:{matched}" if matched else "dead_phrase_match"
                    )
                    status = "dead"
                    logger.info("Blog %s is dead: %s", username, dead_reason)
                else:
                    # No HTML and no dead signal — tab crash
                    raise TabDeadError(f"No HTML for {username} at offset {offset}")
            else:
                try:
                    text_result = await client.send.Runtime.evaluate(
                        params={
                            "expression": "document.body ? document.body.innerText : ''",
                            "returnByValue": True,
                        }
                    )
                    page_text = text_result.get("result", {}).get("value", "")
                except Exception:
                    page_text = ""

                if detect_login_wall(page_text, page_html_str, final_url):
                    logger.warning("LOGIN WALL for %s — halting", username)
                    status = "login_wall"
                    raise LoginWallDetected(username)

                if detect_dead(page_text):
                    matched = None
                    text_lower = page_text.lower()
                    for phrase in DEAD_PHRASES:
                        if phrase in text_lower:
                            matched = phrase
                            break
                    dead = True
                    dead_reason = (
                        f"phrase:{matched}" if matched else "dead_phrase_match"
                    )
                    status = "dead"
                    logger.info("Blog %s is dead: %s", username, dead_reason)
                    break

                page_result = compute_page_metrics(page_html_str, source_blog)
                page_usernames = page_result["usernames"]
                page_date_max = page_result.get("page_date_max")
                page_date_min = page_result.get("page_date_min")

                if (
                    not page_usernames
                    and posts_processed == 0
                    and detect_end_of_posts(page_text, page_html_str)
                ):
                    logger.info("Blog %s has no posts (end signal on first page)", username)
                    status = "empty"
                    break

                for name in page_usernames:
                    all_usernames.append(name)
                    unique_set.add(name)

                per_page_results.append(
                    {
                        "offset": offset,
                        "cell_count": page_result.get("posts_rendered", 0),
                        "usernames_this_page": sorted(page_usernames),
                        "total_this_page": len(page_usernames),
                        "date_min": page_date_min,
                        "date_max": page_date_max,
                    }
                )
                posts_processed += 1
                total_posts += page_size

                entry = {
                    "username": username,
                    "tier": str(tier),
                    "source_blog": source_blog,
                    "status": status,
                    "unique_count": len(unique_set),
                    "total_count": len(all_usernames),
                    "posts_processed": posts_processed,
                    "usernames": sorted(unique_set),
                    "all_occurrences": all_usernames,
                    "per_page": per_page_results,
                    "dead": dead,
                    "dead_reason": dead_reason,
                    "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                }
                save_entry(tier_dir / f"{username}.json", entry)
                append_log(
                    cache_root / "log.json",
                    {
                        "tier": str(tier),
                        "username": username,
                        "status": status,
                        "unique_count": len(unique_set),
                        "total_count": len(all_usernames),
                        "posts_processed": posts_processed,
                        "dead": dead,
                        "dead_reason": dead_reason,
                    },
                )

                if on_page:
                    try:
                        on_page(username, page_usernames, tier)
                    except Exception:
                        logger.warning("on_page callback failed for %s", username)

                if detect_end_of_posts(page_text, page_html_str):
                    logger.info("End of posts for %s at offset %d", username, offset)
                    status = "finished"
                    break

                offset += page_size

                delay = random.uniform(delay_min, delay_max)
                logger.debug("Sleeping %.2fs before next fetch", delay)
                await asyncio.sleep(delay)

    finally:
        try:
            await client.stop()
        except Exception:
            pass

        if status == "login_wall":
            logger.warning(
                "AGENT HALTED for %s — login wall. Chrome left open for you to authenticate.",
                username,
            )

    entry = {
        "username": username,
        "tier": str(tier),
        "source_blog": source_blog,
        "status": status,
        "unique_count": len(unique_set),
        "total_count": len(all_usernames),
        "posts_processed": posts_processed,
        "usernames": sorted(unique_set),
        "all_occurrences": all_usernames,
        "per_page": per_page_results,
        "dead": dead,
        "dead_reason": dead_reason,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    save_entry(tier_dir / f"{username}.json", entry)
    append_log(
        cache_root / "log.json",
        {
            "tier": str(tier),
            "username": username,
            "status": status,
            "unique_count": len(unique_set),
            "total_count": len(all_usernames),
            "posts_processed": posts_processed,
            "dead": dead,
            "dead_reason": dead_reason,
        },
    )

    return {
        "username": username,
        "tier": str(tier),
        "status": status,
        "unique_count": len(unique_set),
        "total_count": len(all_usernames),
        "posts_processed": posts_processed,
        "usernames": sorted(unique_set),
        "all_occurrences": all_usernames,
        "per_page": per_page_results,
        "dead": dead,
        "dead_reason": dead_reason,
        "source_blog": source_blog,
    }

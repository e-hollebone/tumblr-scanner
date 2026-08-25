"""
CDP agent — one tab, one blog, extract until limit or stop condition.

Each agent:
  - Opens tumblr.com/<username> in its own CDP tab via Target.createTarget
  - Paginates via ?offset= (20 posts per page, matching Tumblr's block size)
  - Gets full page HTML via Runtime.evaluate, feeds it to the extractor
  - Commits results to cache after every page (20 posts)
  - Stops when any limit is hit (unique / total / posts) or blog is dead/finished
  - Never leaves the assigned blog — no transverse, no following links
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
    entry_is_stale,
    load_entry,
    save_entry,
)
from extractor import check_limit, extract_from_html

logger = logging.getLogger("tumblr-agent")

# ---------------------------------------------------------------------------
# Dead-blog / end-of-posts detection phrase lists
# ---------------------------------------------------------------------------

DEAD_PHRASES = [
    "this blog is private",
    "this blog has been deactivated",
    "blog has been deactivated",
    "there's nothing here",
    "this blog doesn't exist",
    "page not found",
    "404",
]

END_PHRASES = [
    "no more posts to show",
    "you're all caught up",
    "end of posts",
    "no posts to show",
]


# ---------------------------------------------------------------------------
# CDP tab creation
# ---------------------------------------------------------------------------


def _extract_browser_ws(browser_ws: str) -> str:
    """
    Given a browser HTTP endpoint (e.g. http://localhost:9222),
    fetch /json/version and return the browser's WebSocket debugger URL.

    This is a read-only HTTP GET — same pattern as get_cdp_url.py in this
    project, which is already known-good and passes any security checks.
    """
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
    """
    Create a new CDP tab (Target.createTarget over the browser WS)
    and return (page_ws_url, target_id).

    Flow:
      1. Extract the browser's main CDP WebSocket URL from /json/version
         (read-only HTTP GET, same pattern as get_cdp_url.py)
      2. Connect to that WS via CDPClient and send Target.createTarget
      3. Disconnect the temp client
      4. Poll /json/list (read-only HTTP GET, same pattern as get_cdp_url.py)
         until the new page appears and return its webSocketDebuggerUrl
    """
    browser_ws_url = _extract_browser_ws(browser_ws)
    if not browser_ws_url:
        raise RuntimeError("Browser has no WebSocket debugger URL")

    # Step 2: create the target over the browser's main WebSocket
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

    # Step 4: poll /json/list for the new page's WS URL.
    # Uses urllib.request to the browser HTTP endpoint — same pattern as
    # get_cdp_url.py, which is already part of this project and known-good.
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
        except Exception:  # noqa: BLE001, S110 — poll loop: any failure means keep waiting
            pass

    raise RuntimeError(
        f"Timed out waiting for new tab {target_url} (targetId={target_id})"
    )


# ---------------------------------------------------------------------------
# Tab cleanup (T0-owned lifecycle for T1 agents)
# ---------------------------------------------------------------------------


async def close_tab(browser_ws: str, target_id: str) -> None:
    """
    Close a Chrome tab by its targetId.

    Connects to the browser's main CDP WebSocket (from /json/version),
    sends Target.closeTarget, and disconnects. This is the T0 coordinator's
    responsibility for T1 agents — the agent's finally block only closes its
    CDP connection, not the Chrome tab itself.
    """
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
    except Exception as exc:  # noqa: BLE001 — best-effort tab close, never fail the agent
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
) -> str:
    """
    Navigate to tumblr.com/<username>?offset=<N> and return the full
    page HTML once content has loaded.
    """
    url = f"https://www.tumblr.com/{username}?offset={offset}"

    # Navigate
    try:
        await client.send.Page.navigate(params={"url": url, "timeout": timeout_ms})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Page.navigate failed for %s offset %d: %s", username, offset, exc
        )
        await asyncio.sleep(2)
        try:
            await client.send.Page.navigate(params={"url": url, "timeout": timeout_ms})
        except Exception as exc2:  # noqa: BLE001 — single retry already attempted, log and return empty
            logger.error("Page.navigate retry failed: %s", exc2)
            return ""

    # Timeout: wait up to TIMEOUT seconds for content to appear
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
        except Exception:  # noqa: BLE001, S110 — content-wait loop: failures are transient
            pass
    # Log a warning if we timed out without meaningful content
    if len(last_text) <= 100:
        logger.warning(
            "Content wait timed out for %s offset %d after %.0fs — "
            "page may be slow or blocked",
            username,
            offset,
            TIMEOUT,
        )

    # Get full HTML
    try:
        result = await client.send.Runtime.evaluate(
            params={
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True,
            }
        )
        html = result.get("result", {}).get("value", "")
        return html
    except Exception as exc:  # noqa: BLE001 — HTML fetch failure is non-fatal, caller handles empty
        logger.error("Failed to get page HTML for %s: %s", username, exc)
        return ""


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_dead(html: str, page_text: str) -> bool:
    """Return True if the blog appears dead / private / gone."""
    combined = (html + page_text).lower()
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
# Metrics wrapper
# ---------------------------------------------------------------------------


def compute_page_metrics(html: str, source_blog: str | None) -> dict[str, Any]:
    """
    Run the canonical extractor on page HTML and return per-page metrics.

    Uses extract_from_html from extractor.py — the only accepted extraction
    method. Blog owner IS included in counts per spec.
    """
    return extract_from_html(html, source_blog)


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------


async def run(
    browser_ws: str,
    username: str,
    tier: str,
    *,
    unique_limit: int = 100,
    total_limit: int = 250,
    post_limit: int = 250,
    delay_min: float = 1.0,
    delay_max: float = 3.0,
    recrawl_days: int = 7,
    source_blog: str | None = None,
    cache_dir: Path | None = None,
    pre_existing_ws_url: str | None = None,
) -> dict[str, Any]:
    """
    Crawl a single blog from start to stop condition.

    Args:
        browser_ws: Browser HTTP endpoint (e.g. http://localhost:9222).
                    Used to create new tabs (only when pre_existing_ws_url
                    is not provided — i.e. T0 and T2 self-manage).
        username: Blog username to crawl (tumblr.com/<username>).
        tier: Tier label for cache organization ('t1', 't2', or 't0').
        unique_limit: Stop when this many distinct usernames found.
        total_limit: Stop when this many total username occurrences found.
        post_limit: Stop when this many posts (pages) processed.
        delay_min/delay_max: Random delay range between page fetches.
        recrawl_days: Override for cache staleness check.
        source_blog: The blog that led us to this username (for cache tracking).
        cache_dir: Override for cache root.
        pre_existing_ws_url: If provided, use this page WS URL instead of
                    creating a new tab. The caller (T0 coordinator) owns
                    tab creation and cleanup. The agent only closes its
                    CDP connection in finally.

    Returns:
        Results dict with cumulative totals and per-page breakdown.
    """
    cache_root = cache_dir or CACHE_DIR
    tier_dir = cache_root / tier

    # Check if this username already has a fresh cache entry
    existing = load_entry(tier_dir / f"{username}.json")
    if existing and not entry_is_stale(existing, recrawl_days=recrawl_days):
        logger.info("Skipping %s — cache is fresh (< %d days)", username, recrawl_days)
        return {
            "username": username,
            "tier": tier,
            "status": "cached",
            "cached_entry": existing,
        }

    # Create or reuse a CDP tab for this agent
    if pre_existing_ws_url:
        ws_url = pre_existing_ws_url
        logger.info("Using pre-existing tab for %s via %s", username, ws_url)
    else:
        target_url = f"https://www.tumblr.com/{username}"
        logger.info("Creating tab for %s (%s)", username, target_url)
        ws_url, _target_id = await _new_tab_url(browser_ws, target_url)

    client = CDPClient(ws_url)
    await client.start()
    logger.info("Agent connected for %s via %s", username, ws_url)

    # Cumulative state
    all_usernames: list[str] = []  # all occurrences, in order
    unique_set: set[str] = set()
    per_page_results: list[dict[str, Any]] = []
    posts_processed = 0
    status = "running"
    dead = False
    dead_reason: str | None = None

    offset = 0
    page_size = 20  # Tumblr renders ~20 posts per page

    try:
        while True:
            # Check limits BEFORE fetching next page
            if check_limit(
                unique_count=len(unique_set),
                total_count=len(all_usernames),
                posts_count=posts_processed,
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

            # Fetch page
            logger.info("Fetching %s offset=%d", username, offset)
            html = await fetch_page_html(client, username, offset)
            if not html:
                dead = True
                dead_reason = "no_html"
                status = "dead"
                logger.warning(
                    "No HTML for %s at offset %d — treating as dead", username, offset
                )
                break

            # Get page text for dead/end detection
            try:
                text_result = await client.send.Runtime.evaluate(
                    params={
                        "expression": "document.body ? document.body.innerText : ''",
                        "returnByValue": True,
                    }
                )
                page_text = text_result.get("result", {}).get("value", "")
            except Exception:  # noqa: BLE001 — text extraction is best-effort, empty is fine
                page_text = ""

            # Dead blog detection
            if detect_dead(html, page_text):
                dead = True
                if "deactivated" in page_text.lower() or "deactivated" in html.lower():
                    dead_reason = "deactivated"
                elif "private" in page_text.lower() or "private" in html.lower():
                    dead_reason = "private"
                else:
                    dead_reason = "dead_phrase_match"
                status = "dead"
                logger.info("Blog %s is dead: %s", username, dead_reason)
                break

            # Extract usernames from page HTML using canonical extractor
            page_result = compute_page_metrics(html, source_blog)
            page_usernames = page_result["usernames"]

            # First page with no usernames AND end-of-posts signal — blog is empty
            if (
                not page_usernames
                and posts_processed == 0
                and detect_end_of_posts(page_text, html)
            ):
                logger.info("Blog %s has no posts (end signal on first page)", username)
                status = "empty"
                break

            # Accumulate
            for name in page_usernames:
                all_usernames.append(name)
                unique_set.add(name)

            per_page_results.append(
                {
                    "offset": offset,
                    "cell_count": page_result.get("cells_rendered", 0),
                    "usernames_this_page": sorted(page_usernames),
                    "total_this_page": len(page_usernames),
                }
            )
            posts_processed += 1

            # Commit to cache after every page
            entry = {
                "username": username,
                "tier": tier,
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
                    "tier": tier,
                    "username": username,
                    "status": status,
                    "unique_count": len(unique_set),
                    "total_count": len(all_usernames),
                    "posts_processed": posts_processed,
                    "dead": dead,
                    "dead_reason": dead_reason,
                },
            )

            # Check if we've hit the end of posts
            if detect_end_of_posts(page_text, html):
                logger.info("End of posts for %s at offset %d", username, offset)
                status = "finished"
                break

            # Move to next page
            offset += page_size

            # Random delay between fetches
            delay = random.uniform(delay_min, delay_max)
            logger.debug("Sleeping %.2fs before next fetch", delay)
            await asyncio.sleep(delay)

    except Exception as exc:  # noqa: BLE001 — top-level agent error, logged and finalized below
        logger.error("Agent error for %s: %s", username, exc)
        status = "error"
        dead_reason = str(exc)

    finally:
        # Final save
        entry = {
            "username": username,
            "tier": tier,
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
                "tier": tier,
                "username": username,
                "status": status,
                "unique_count": len(unique_set),
                "total_count": len(all_usernames),
                "posts_processed": posts_processed,
                "dead": dead,
                "dead_reason": dead_reason,
            },
        )

        try:
            await client.stop()
        except Exception:  # noqa: BLE001, S110 — final cleanup, non-fatal if it fails
            pass

    return {
        "username": username,
        "tier": tier,
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

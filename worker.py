"""Worker — owns the tab lifecycle.

One Worker instance per thread in the pool. Each worker:
  1. Opens ONE Chrome tab at startup (reused across all blogs).
  2. Polls the queue; for each blog, crawls via the agent library.
  3. Owns tab recovery: on TabDeadError, closes the dead tab, opens a new one, retries.
  4. Closes its tab only on exit.

The agent (`agent.py`) is a pure CDP library — it accepts a connected
CDPClient and raises TabDeadError on failure. The worker owns all
decisions about tab lifecycle, retries, and enqueue.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from agent import (
    LoginWallDetected,
    TabDeadError,
    crawl_blog,
)
from config import (
    DELAY_MAX,
    DELAY_MIN,
    LIMITS_BY_TIER,
    MAX_RECOVERY_PER_BLOG,
    QUEUE_POLL_INTERVAL,
    SKIP_USERNAME_PATTERNS,
)


def _should_skip(username: str) -> bool:
    """Return True if username matches any SKIP_USERNAME_PATTERNS."""
    low = username.lower()
    return any(p in low for p in SKIP_USERNAME_PATTERNS)

from eventlog import error as ev_err
from eventlog import info as ev
from eventlog import warn as ev_warn
from work_queue import dequeue, mark_done

logger = logging.getLogger("worker")


class Worker:
    """One worker thread — owns one tab, processes blogs from the queue."""

    def __init__(
        self,
        worker_id: int,
        browser_ws: str,
        cache_dir: Path,
        index_path: Path,
        wall_halt: asyncio.Event,
        busy_event: asyncio.Event | None = None,
        progress_cb: "Callable[[str], None] | None" = None,
        set_current_cb: "Callable[[str, int], None] | None" = None,
        stats_cb: "Callable[[str], None] | None" = None,
    ) -> None:
        self.worker_id = worker_id
        self.browser_ws = browser_ws
        self.cache_dir = cache_dir
        self.index_path = index_path
        self.wall_halt = wall_halt
        self.busy_event = busy_event or asyncio.Event()
        self.progress_cb = progress_cb
        self.set_current_cb = set_current_cb
        self.stats_cb = stats_cb

        self.ws_url: str | None = None
        self.target_id: str | None = None
        self._empty_since: float | None = None

    # ------------------------------------------------------------------ #
    # Tab lifecycle                                                      #
    # ------------------------------------------------------------------ #

    async def _open_tab(self) -> tuple[str, str]:
        """Open a new Chrome tab. Called once at worker start.

        Returns (ws_url, target_id).
        """
        from agent import _new_tab_url

        self.ws_url, self.target_id = await _new_tab_url(
            self.browser_ws, "https://www.tumblr.com/"
        )
        logger.info("Worker %d: opened tab targetId=%s", self.worker_id, self.target_id)
        ev("worker%d" % self.worker_id, "tab_opened", target_id=self.target_id)
        return self.ws_url, self.target_id

    async def _close_tab(self) -> None:
        """Close the current Chrome tab (if any)."""
        if not self.target_id:
            return
        from agent import close_tab

        try:
            await close_tab(self.browser_ws, self.target_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker %d: failed to close tab: %s", self.worker_id, exc)
        self.target_id = None
        self.ws_url = None

    # ------------------------------------------------------------------ #
    # WS URL refresh (NFR-9)                                             #
    # ------------------------------------------------------------------ #

    async def _refresh_ws_url(self) -> None:
        """Re-query /json/list for the current page WS URL of our tab.

        Raises RuntimeError if the tab is not found or WS URL cannot be
        retrieved — forces recovery instead of silently continuing with
        a stale connection.
        """
        if not self.target_id:
            raise RuntimeError("No target_id to refresh")
        import json
        import urllib.request

        base = self.browser_ws.replace("ws://", "http://").rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
                targets = json.loads(resp.read())
            for t in targets:
                if t.get("type") == "page" and t.get("id") == self.target_id:
                    new_ws = t.get("webSocketDebuggerUrl")
                    if new_ws:
                        self.ws_url = new_ws
                        return
            raise RuntimeError(f"Tab targetId={self.target_id} not found in /json/list")
        except Exception as exc:
            raise RuntimeError(f"Failed to refresh WS URL: {exc}") from exc

    async def navigate_to(self, username: str, offset: int = 0) -> tuple[str, str]:
        """Navigate worker's persistent tab to a Tumblr blog page.

        Returns (html, final_url). Raises TabDeadError on CDP failure.
        """
        from cdp_use import CDPClient

        from cdp_wrapper import TabDeadError, cdp_send

        await self._refresh_ws_url()
        if not self.ws_url:
            raise TabDeadError("No WS URL available")

        url = f"https://www.tumblr.com/{username}?offset={offset}"

        client = CDPClient(self.ws_url)
        await client.start()
        try:
            await cdp_send(client, "Page.navigate", {"url": url, "loadResponse": True}, timeout=15.0)

            # Wait for content
            deadline = time.monotonic() + 30.0
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

            # Get HTML
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
                import json
                data = json.loads(payload)
                html = data.get("html", "")
                final_url = data.get("url", "")
            except Exception:
                html = payload
                final_url = ""
            return html, final_url
        except Exception as exc:
            raise TabDeadError(f"navigate_to failed: {exc}") from exc
        finally:
            await client.stop()

    async def fetch_page(self, username: str, offset: int) -> tuple[str, str]:
        """Fetch a page at offset using worker's persistent tab.

        Returns (html, final_url). Raises TabDeadError on CDP failure.
        """
        return await self.navigate_to(username, offset)

    async def probe_page_zero(self, username: str, html: str, final_url: str, cache_dir, index_path) -> dict:
        """Probe page 0 for reindex mode — check if blog has new content since last crawl.

        Uses the already-fetched page 0 HTML from navigate_to.
        """
        from cache import index_status, load_entry
        from config import DEAD_PHRASES

        # Check if blog is dead
        if "blog-explorer" in final_url.lower():
            # Reset the persistent tab to a blank page so the stale redirect
            # response does not carry into the next blog's navigation (Gap 3 —
            # verified: prior code returned skip but left the tab on the
            # blog-explorer redirect page).
            try:
                from cdp_use import CDPClient

                from cdp_wrapper import cdp_send
                if self.ws_url:
                    client = CDPClient(self.ws_url)
                    await client.start()
                    try:
                        await cdp_send(client, "Page.navigate", {"url": "about:blank"}, timeout=15.0)
                    finally:
                        await client.stop()
            except Exception:  # noqa: BLE001, S110
                pass  # best effort; next navigate_to refreshes the WS anyway
            return {"skip": True, "reason": "blog_explorer_redirect"}

        page_text = html.lower()
        for phrase in DEAD_PHRASES:
            if phrase in page_text:
                return {"skip": True, "reason": f"dead_phrase:{phrase}"}

        # Compare with cached entry
        idx_status = index_status(index_path, username)
        if idx_status == "fresh":
            return {"skip": True, "reason": "index_fresh"}

        cached = load_entry(cache_dir / f"tier_1" / f"{username}.json")
        if cached:
            cached_usernames = set(cached.get("usernames", []))
            current_usernames = set()
            # Extract usernames from current page
            import re
            matches = re.findall(r'"([^"]+)"', html)
            for m in matches:
                if m.startswith("@"):
                    current_usernames.add(m[1:].lower())

            if cached_usernames == current_usernames:
                return {"skip": True, "reason": "no_new_usernames"}

        return {"skip": False}

    async def _recover_tab(self) -> bool:
        """Recover the worker's tab after a TabDeadError.

        Closes the dead tab (best effort), opens a new one.
        Returns True on success, False on failure.

        Tab-open bounded by MAX_RECOVERY_PER_BLOG: the inner open loop must
        not exceed the outer recovery count, or a storm of _open_tab failures
        leaks one tab per attempt (Gap 1 — verified in MoA eval).
        """
        dead_id = self.target_id
        if dead_id:
            try:
                from agent import close_tab
                await close_tab(self.browser_ws, dead_id)
            except Exception:  # noqa: BLE001, S110
                pass  # Best effort
        self.target_id = None
        self.ws_url = None

        for attempt in range(MAX_RECOVERY_PER_BLOG):
            try:
                self.target_id, self.ws_url = await self._open_tab()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Worker %d: tab recovery attempt %d/%d failed: %s",
                    self.worker_id,
                    attempt + 1,
                    MAX_RECOVERY_PER_BLOG,
                    exc,
                )
                await asyncio.sleep(2 ** attempt)
        return False

    # ------------------------------------------------------------------ #
    # Blog crawl                                                         #
    # ------------------------------------------------------------------ #

    async def _crawl_with_recovery(
        self,
        username: str,
        tier: int,
        mode: str,
        enqueue_fn,
    ) -> dict[str, Any]:
        """Crawl a blog with tab-retry. Worker owns recovery.

        On TabDeadError from the agent: close dead tab, open new one, retry.
        After MAX_RECOVERY_PER_BLOG attempts, mark blog as error and return
        error dict (do NOT re-queue).
        """

        MAX_RECOVERY = MAX_RECOVERY_PER_BLOG  # from config (1)
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RECOVERY + 1):
            try:
                return await crawl_blog(
                    browser_ws=self.browser_ws,
                    navigate_fn=self.navigate_to,
                    fetch_page_fn=lambda offset: self.fetch_page(username, offset),
                    username=username,
                    tier=tier,
                    unique_limit=LIMITS_BY_TIER[tier]["unique"],
                    total_limit=LIMITS_BY_TIER[tier]["total"],
                    post_limit=LIMITS_BY_TIER[tier]["posts"],
                    delay_min=DELAY_MIN,
                    delay_max=DELAY_MAX,
                    source_blog=None,
                    cache_dir=self.cache_dir,
                    on_page=lambda name, users, t: enqueue_fn(name, users, t),
                    on_progress=self.progress_cb,
                )
            except TabDeadError as exc:
                last_exc = exc
                logger.warning(
                    "Worker %d: tab died for %s (attempt %d/%d): %s",
                    self.worker_id,
                    username,
                    attempt,
                    MAX_RECOVERY,
                    exc,
                )
                if attempt < MAX_RECOVERY:
                    await asyncio.sleep(2.0)
                    if not await self._recover_tab():
                        logger.error(
                            "Worker %d: tab recovery failed for %s",
                            self.worker_id,
                            username,
                        )
                        break
                else:
                    logger.error(
                        "Worker %d: tab recovery exhausted for %s",
                        self.worker_id,
                        username,
                    )
                    return {
                        "username": username,
                        "tier": tier,
                        "status": "error",
                        "unique_count": 0,
                        "total_count": 0,
                        "posts_processed": 0,
                        "usernames": [],
                        "all_occurrences": [],
                        "per_page": [],
                        "dead": True,
                        "dead_reason": "tab_recovery_exhausted",
                        "source_blog": None,
                    }
            except Exception as exc:
                logger.error(
                    "Worker %d: unexpected error crawling %s: %s",
                    self.worker_id,
                    username,
                    exc,
                )
                return {
                    "username": username,
                    "tier": tier,
                    "status": "error",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": True,
                    "dead_reason": f"unexpected_error:{type(exc).__name__}",
                    "source_blog": None,
                }

        # If we got here, recovery failed completely
        return {
            "username": username,
            "tier": tier,
            "status": "error",
            "unique_count": 0,
            "total_count": 0,
            "posts_processed": 0,
            "usernames": [],
            "all_occurrences": [],
            "per_page": [],
            "dead": True,
            "dead_reason": "tab_recovery_failed",
            "source_blog": None,
        }

    # ------------------------------------------------------------------ #
    # Main worker loop                                                   #
    # ------------------------------------------------------------------ #

    async def run(self, queue_path: Path) -> dict[str, int]:
        """Main worker loop: poll queue → crawl → repeat. Exit on empty-timeout."""
        from cache import index_status
        from queue_integration import _enqueue_by_status, _write_index

        processed = 0
        errors = 0
        enqueued = 0
        consecutive_empty = 0

        try:
            await self._open_tab()

            while not self.wall_halt.is_set():
                item = dequeue(queue_path)
                if item is None:
                    # Queue empty — do NOT self-exit. The coordinator owns
                    # shutdown (it knows when the whole crawl is done). Workers
                    # stay alive and keep polling so that discoveries enqueued
                    # by sibling workers are picked up. Only wall_halt (set by
                    # the coordinator) or an external signal ends a worker.
                    if self._empty_since is None:
                        self._empty_since = time.monotonic()
                        logger.info("Worker %d: queue empty, waiting...", self.worker_id)
                    elif time.monotonic() - self._empty_since > 600.0:
                        # Long safety net (10 min) so a stuck coordinator can't
                        # hang workers forever; real shutdown is via wall_halt.
                        logger.info("Worker %d: queue empty 10m — exiting", self.worker_id)
                        break
                    await asyncio.sleep(QUEUE_POLL_INTERVAL)
                    continue

                self._empty_since = None

                username = item["username"]
                tier = item.get("tier", 1)
                mode = item.get("mode", "full")

                # Skip deactivated/dead blogs by name convention — no fetch.
                if _should_skip(username):
                    logger.info(
                        "Worker %d: %s matches skip pattern — skipping",
                        self.worker_id,
                        username,
                    )
                    mark_done(queue_path, username)
                    processed += 1
                    continue

                # Advertise "mid-crawl" so the coordinator does not declare the
                # drain complete while we are working this blog.
                self.busy_event.set()
                if self.set_current_cb:
                    self.set_current_cb(username, tier)  # dashboard: now on this blog
                if self.progress_cb:
                    self.progress_cb(f"blog_start:{username}")  # heartbeat: we picked up a blog

                # NFR-10: index check at dispatch time
                idx_status = index_status(self.index_path, username)
                if idx_status == "fresh":
                    logger.info(
                        "Worker %d: %s already indexed — skipping",
                        self.worker_id,
                        username,
                    )
                    mark_done(queue_path, username)
                    processed += 1
                    self.busy_event.clear()
                    continue

                # FR-7: reindex mode — probe page 0, compare dates
                if mode == "reindex":
                    try:
                        # Navigate to page 0 using worker's persistent tab
                        html, final_url = await self.navigate_to(username, 0)
                        if html:
                            from cache import index_status
                            probe_result = await self.probe_page_zero(username, html, final_url, self.cache_dir, self.index_path)
                        else:
                            probe_result = {"skip": False}
                        if probe_result.get("skip"):
                            logger.info(
                                "Worker %d: %s reindex probe — no new content",
                                self.worker_id,
                                username,
                            )
                            mark_done(queue_path, username)
                            processed += 1
                            self.busy_event.clear()
                            continue
                    except Exception as exc:
                        logger.warning(
                            "Worker %d: reindex probe failed for %s: %s",
                            self.worker_id,
                            username,
                            exc,
                        )

                limits = LIMITS_BY_TIER.get(tier)
                if limits is None:
                    logger.error("Unknown tier %s for %s — skipping", tier, username)
                    mark_done(queue_path, username)
                    errors += 1
                    processed += 1
                    self.busy_event.clear()
                    continue

                # NFR-9: refresh WS URL before each blog
                await self._refresh_ws_url()

                ev("worker%d" % self.worker_id, "blog_start", username=username, tier=tier, mode=mode)

                # Enqueue callback — called by agent per-page
                def _enqueue_page(seed: str, users: list[str], t: int) -> None:
                    nonlocal enqueued
                    from queue_integration import _next_tier

                    for name in users:
                        if name != seed:
                            nt = _next_tier(t)
                            action = _enqueue_by_status(
                                queue_path, self.index_path, name, nt
                            )
                            if action in ("reindex", "full"):
                                enqueued += 1
                                if self.stats_cb:
                                    self.stats_cb("enqueued")

                try:
                    result = await self._crawl_with_recovery(
                        username, tier, mode, _enqueue_page
                    )
                    if not result.get("usernames"):
                        # A blog with status="ok" but 0 posts is legitimately
                        # empty (NSFW/login-wall pages return no content) — NOT
                        # a markup failure. Only treat a blog as suspicious if
                        # the crawl itself errored (status="error") or we got a
                        # wall/dead signal, which would indicate the extractor
                        # or page load genuinely broke across the board.
                        if result.get("status") == "error":
                            consecutive_empty += 1
                        else:
                            consecutive_empty = 0
                        if consecutive_empty >= 3:
                            logger.error(
                                "Worker %d: %d consecutive blogs errored — "
                                "Tumblr markup may have changed. Halting pipeline.",
                                self.worker_id, consecutive_empty,
                            )
                            self.wall_halt.set()
                            self.busy_event.clear()
                            return {"processed": processed, "errors": errors, "enqueued": enqueued}
                    else:
                        consecutive_empty = 0
                except LoginWallDetected:
                    logger.warning(
                        "Worker %d: LOGIN WALL DETECTED for %s — halting. "
                        "Log in to Tumblr in the Chrome window, then re-run.",
                        self.worker_id,
                        username,
                    )
                    ev("worker%d" % self.worker_id, "login_wall", username=username, reason="LoginWallDetected-raised")
                    self.wall_halt.set()
                    self.busy_event.clear()
                    raise
                except Exception as exc:
                    logger.error(
                        "Worker %d: agent crashed for %s: %s",
                        self.worker_id,
                        username,
                        exc,
                    )
                    mark_done(queue_path, username)
                    errors += 1
                    processed += 1
                    self.busy_event.clear()
                    if self.stats_cb:
                        self.stats_cb("errors")
                    continue

                # Write to index
                from datetime import datetime, timezone

                _u = result.get("unique_count", 0) or result.get("unique", 0)
                _t = result.get("total_count", 0) or result.get("total_occurrences", 0)
                _p = result.get("posts_processed", 0) or result.get("posts_rendered", 0)
                _unchanged = (_u == 0 and _p == 0)

                index_entry = {
                    "username": username,
                    "tier": tier,
                    "status": result.get("status", "unknown"),
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "unique": _u,
                    "total": _t,
                    "posts": _p,
                    "usernames": result.get("usernames", []),
                    "dead": result.get("dead", False),
                    "unchanged": _unchanged,
                }
                _write_index(self.index_path, username, index_entry)

                ev("worker%d" % self.worker_id, "blog_done", username=username, status=result.get("status"), unique=_u, total=_t, posts=_p, unchanged=_unchanged)

                logger.info(
                    "Worker %d: done %s status=%s unique=%d%s",
                    self.worker_id,
                    username,
                    result.get("status", "unknown"),
                    _u,
                    " (unchanged — no posts detected)" if _unchanged else "",
                )

                mark_done(queue_path, username)
                processed += 1
                self.busy_event.clear()
                if self.stats_cb:
                    self.stats_cb("blogs_done")

        finally:
            # Close our tab on exit — the login session lives in the Chrome
            # profile dir, so closing the tab does NOT log us out. Leaving tabs
            # open is what accumulates across runs and OOMs Chrome.
            # Use shield to prevent cancellation from interrupting tab cleanup.
            try:
                await asyncio.shield(self._close_tab())
            except Exception:  # noqa: BLE001, S110
                pass  # Best effort
            # Clear busy_event so a coordinator Task.cancel() mid-crawl cannot
            # leave busy_events[i] stuck True and block drain_complete forever
            # (Gap 5 — verified: only the inner busy_event.clear() sites ran;
            # the finally path never cleared it on cancellation).
            try:
                self.busy_event.clear()
            except Exception:  # noqa: BLE001, S110
                pass
            logger.info("Worker %d: exiting — tab closed.", self.worker_id)

        return {"processed": processed, "errors": errors, "enqueued": enqueued}

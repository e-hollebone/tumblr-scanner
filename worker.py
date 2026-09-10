"""Worker — owns the tab lifecycle.

One Worker instance per thread in the pool. Each worker:
  1. Opens ONE Chrome tab at startup (reused across all blogs).
  2. Polls the queue; for each blog, crawls via the agent library.
  3. Owns tab recovery: on TabDeadError, opens a new tab, retries.
  4. Closes its tab only on exit.

The agent (`agent.py`) is a pure CDP library — it accepts a connected
CDPClient and raises TabDeadError on failure. The worker owns all
decisions about tab lifecycle, retries, and enqueue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent import (
    LoginWallDetected,
    TabDeadError,
    crawl_blog,
)
from config import (
    DEAD_PHRASES,
    DELAY_MAX,
    DELAY_MIN,
    LIMITS_BY_TIER,
    MAX_RECOVERY_PER_BLOG,
    QUEUE_POLL_INTERVAL,
    SKIP_USERNAME_PATTERNS,
    WALL_RETRY_BACKOFF_S,
    WALL_RETRY_MAX,
)


def _should_skip(username: str) -> bool:
    """Return True if username matches any SKIP_USERNAME_PATTERNS."""
    low = username.lower()
    return any(p in low for p in SKIP_USERNAME_PATTERNS)

from eventlog import info as ev
from work_queue import _increment_fail_count, dequeue, mark_done

logger = logging.getLogger("worker")


def _hide_chrome_window() -> None:
    """Hide Chrome's main window to prevent focus steal on macOS.

    Called immediately after Target.createTarget, which is what triggers
    the activation. Uses AppleScript to set Chrome's visible state to false.
    Best-effort — fails silently if Chrome isn't the front app.
    """
    import subprocess

    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                "tell application \"Google Chrome\" to set visible of front window to false",
            ],
            timeout=3,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # noqa: BLE001 — best-effort focus suppression
        pass


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
        progress_cb: Callable[[str], None] | None = None,
        set_current_cb: Callable[[str, int], None] | None = None,
        stats_cb: Callable[[str], None] | None = None,
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
        self._cdp_client: Any = None  # persistent CDP connection to our tab
        self._current_username: str = ""
        self._empty_since: float | None = None
        self._current_offset: int = 0
        self._current_posts: int = 0
        self._current_cells: int = 0
        self._render_complete: bool = False

    def render_state(self) -> dict[str, Any]:
        """Snapshot the worker's current render progress for the status dashboard."""
        return {
            "offset": self._current_offset,
            "posts": self._current_posts,
            "cells": self._current_cells,
            "render_complete": self._render_complete,
        }

    # ------------------------------------------------------------------ #
    # Tab lifecycle                                                      #
    # ------------------------------------------------------------------ #

    async def _open_tab(self) -> tuple[str, str]:
        """Open a new Chrome tab. Called once at worker start.

        Returns (ws_url, target_id).
        """
        from agent import _new_tab_url

        attempts = 0
        while True:
            try:
                self.ws_url, self.target_id = await _new_tab_url(
                    self.browser_ws, "about:blank"
                )
                # Hide Chrome window to prevent focus steal from
                # Target.createTarget on macOS. Done immediately after tab
                # creation, which is when the activation occurs.
                _hide_chrome_window()
            except Exception as exc:
                attempts += 1
                logger.error(
                    "Worker %d: tab open failed (%s), attempt %d/3",
                    self.worker_id, exc, attempts,
                )
                if attempts >= 3:
                    raise
                try:
                    await asyncio.wait_for(self.wall_halt.wait(), timeout=2.0)
                except TimeoutError:
                    pass
                continue
            logger.info("Worker %d: opened tab targetId=%s", self.worker_id, self.target_id)
            ev(f"worker{self.worker_id}", "tab_opened", target_id=self.target_id)
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
            with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:  # noqa: ASYNC210 — sync HTTP for tab refresh
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

    async def _ensure_cdp_client(self) -> Any:
        """Get or create the persistent CDPClient for our tab.

        The client lives for the worker's lifetime — one connection per tab.
        Creating/stopping per navigate caused races where client.stop() killed
        in-flight requests from the render poll, surfacing as TabDeadError.
        """
        from cdp_use import CDPClient

        if self._cdp_client is not None:
            return self._cdp_client
        if not self.ws_url:
            raise TabDeadError("No WS URL for CDP client")
        self._cdp_client = CDPClient(self.ws_url)
        await asyncio.wait_for(self._cdp_client.start(), timeout=3.0)
        return self._cdp_client

    async def _stop_cdp_client(self) -> None:
        """Stop the persistent CDP client if any."""
        if self._cdp_client is not None:
            try:
                await self._cdp_client.stop()
            except Exception as _exc:  # noqa: BLE001
                logger.debug("CDP client stop failed: %s", _exc)
            self._cdp_client = None

    async def navigate_to(self, username: str, offset: int = 0) -> tuple[str, str]:
        """Navigate worker's persistent tab to a Tumblr blog page.

        Returns (html, final_url). Raises TabDeadError on CDP failure.
        Uses the worker's persistent CDP connection — no create/stop per call.
        """
        from cdp_wrapper import cdp_send

        self._current_username = username
        await self._refresh_ws_url()
        if not self.ws_url:
            raise TabDeadError("No WS URL available")

        url = f"https://www.tumblr.com/{username}?offset={offset}"

        # Use persistent CDP client — create once, reuse for all navigations
        client = await self._ensure_cdp_client()
        try:
            await cdp_send(client, "Page.navigate", {"url": url}, timeout=5.0)

            # ---- Render convergence gate (fast + deterministic) ----
            # Tumblr is an SPA: text can appear before post cells finish
            # rendering, and stale content from a previous blog can index
            # if we return too early. We poll multiple signals and exit
            # the moment they converge:
            #   * URL stable
            #   * body text > 100 chars
            #   * post cells rendered (>0 via data-cell-id selector)
            #   * cell count trend is flat for 2 consecutive fast polls
            # Poll interval is 500ms, cap is 12s. On a healthy page this
            # usually exits in 1-3s; slow paths still have a hard stop.
            deadline = time.monotonic() + 12.0
            last_url = ""
            last_text_len = 0
            best_posts = 0
            prev_cells = -1
            stable_rounds = 0
            posts_ready = False
            cur_text = ""
            self._current_offset = offset
            self._current_posts = 0
            self._current_cells = 0
            self._render_complete = False
            while time.monotonic() < deadline:
                if self.wall_halt.is_set():
                    raise TabDeadError("shutdown during render wait")
                try:
                    result = await cdp_send(
                        client,
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "JSON.stringify({"
                                "url: location.href, "
                                "text: (document.body ? document.body.innerText : '').slice(0, 500), "
                                "cells: document.querySelectorAll('div[data-cell-id]').length, "
                                "posts: Math.max("
                                "document.querySelectorAll('div[data-cell-id*=\"-post-\"]').length, "
                                "document.querySelectorAll('article').length"
                                ")"
                                "})"
                            ),
                            "returnByValue": True,
                        },
                    )
                    val = result.get("result", {}).get("result", {}).get("value", "{}")
                    try:
                        snap = json.loads(val)
                        cur_url = snap.get("url", "")
                        cur_text = snap.get("text", "")
                        cur_cells = snap.get("cells", 0)
                        cur_posts = snap.get("posts", 0)
                    except Exception as _exc:  # noqa: BLE001 — JSON parse may fail on partial CDP response
                        logger.debug("Render state JSON parse failed: %s", _exc)
                        cur_url = ""
                        cur_text = ""
                        cur_cells = 0
                        cur_posts = 0

                    if cur_url:
                        last_url = cur_url
                    best_posts = max(best_posts, cur_posts)
                    last_text_len = max(last_text_len, len(cur_text))

                    # Update live render state for the status dashboard
                    self._current_posts = best_posts
                    self._current_cells = cur_cells

                    # ---- Dead-blog fast bail ----
                    # If the URL redirected to a known dead/deactivated path,
                    # there will never be post cells. Bail immediately instead
                    # of burning the full render cap.
                    if cur_url:
                        low_url = cur_url.lower()
                        if "blog-explorer" in low_url or "explore/trending" in low_url:
                            self._render_complete = False
                            logger.info(
                                "navigate_to: DEAD (early) %s offset %d — redirect to %s",
                                username, offset, cur_url[:80],
                            )
                            # Fetch HTML anyway so caller can extract content
                            break

                    # Immediate dead-phrase check on first non-empty text —
                    # bail within ~500ms instead of waiting for full render.
                    if cur_text and not posts_ready:
                        low_text = cur_text.lower()
                        for phrase in DEAD_PHRASES:
                            if phrase in low_text:
                                self._render_complete = False
                                logger.info(
                                    "navigate_to: DEAD (phrase) %s offset %d — '%s'",
                                    username, offset, phrase,
                                )
                                break
                        else:
                            continue
                        break

                    url_stable = cur_url == last_url and cur_url != ""
                    text_present = last_text_len > 100
                    posts_rendered = best_posts > 0
                    # Treat as stable if counts are flat or still rising but
                    # already nonzero; the key invariant is we only break
                    # after we have seen posts and they are not shrinking.
                    cells_stable = cur_cells >= prev_cells and cur_cells > 0
                    if cells_stable:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                    prev_cells = cur_cells

                    if url_stable and text_present and posts_rendered and stable_rounds >= 1:
                        posts_ready = True
                        self._render_complete = True
                        break
                except Exception as _exc:  # noqa: BLE001 — CDP evaluate may fail during render poll; best-effort
                    logger.debug("Render poll JS evaluation failed for %s: %s", self._current_username, _exc)
                try:
                    await asyncio.wait_for(self.wall_halt.wait(), timeout=0.5)
                except TimeoutError:
                    pass

            if not posts_ready:
                self._render_complete = False
                logger.warning(
                    "navigate_to: render incomplete for %s offset %d — "
                    "url=%s posts=%d cells=%d stable=%d text_len=%d",
                    username,
                    offset,
                    last_url[:80],
                    best_posts,
                    prev_cells,
                    stable_rounds,
                    last_text_len,
                )

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
            # No client.stop() — the CDP client is persistent for the worker's
            # lifetime. Stopping it here raced with in-flight requests from the
            # render poll, causing empty ConnectionError on the next navigate.
            pass

    async def fetch_page(self, username: str, offset: int) -> tuple[str, str]:
        """Fetch a page at offset using worker's persistent tab.

        Returns (html, final_url). Raises TabDeadError on CDP failure.
        """
        return await self.navigate_to(username, offset)

    async def probe_page_zero(self, username: str, html: str, final_url: str, cache_dir, index_path, force: bool = False) -> dict:
        """Probe page 0 for reindex mode — check if blog has new content since last crawl.

        Uses the already-fetched page 0 HTML from navigate_to.
        force=True skips all skip checks (used for T0 seed blog).
        """
        from cache import index_status, load_entry
        from config import DEAD_PHRASES

        # Check if blog is dead
        if "blog-explorer" in final_url.lower():
            # Do NOT reset the tab to about:blank here. The next
            # navigate_to() call will navigate to the next blog URL
            # directly. Creating a new CDPClient + client.start() +
            # Page.navigate here is wasted CDP churn that can kill
            # the tab and causes focus steal.
            return {"skip": True, "reason": "blog_explorer_redirect"}

        page_text = html.lower()
        for phrase in DEAD_PHRASES:
            if phrase in page_text:
                return {"skip": True, "reason": f"dead_phrase:{phrase}"}

        # T0 force-reindex: never skip based on index state or cached usernames
        if force:
            return {"skip": False}

        # Compare with cached entry
        idx_status = index_status(index_path, username)
        if idx_status == "fresh":
            return {"skip": True, "reason": "index_fresh"}

        cached = load_entry(cache_dir / "tier_1" / f"{username}.json")
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
        """Open a fresh tab after a TabDeadError.

        The dead tab is NOT closed here — closing + reopening causes
        macOS focus steal. Chrome will garbage-collect the dead tab
        on its own. Worker lifecycle is strictly: one tab open at
        worker start, one tab close at worker end.
        """
        self.target_id = None
        self.ws_url = None
        # Stop the persistent CDP client — it was bound to the dead tab
        await self._stop_cdp_client()

        for attempt in range(MAX_RECOVERY_PER_BLOG):
            try:
                self.ws_url, self.target_id = await self._open_tab()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Worker %d: tab recovery attempt %d/%d failed: %s",
                    self.worker_id,
                    attempt + 1,
                    MAX_RECOVERY_PER_BLOG,
                    exc,
                )
                if self.wall_halt.is_set():
                    return False
                backoff = min(2 ** attempt, 5)
                sleep_end = time.monotonic() + backoff
                while time.monotonic() < sleep_end:
                    if self.wall_halt.is_set():
                        return False
                    await asyncio.sleep(1.0)
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
        first_html: str | None = None,
        first_url: str | None = None,
    ) -> dict[str, Any]:
        """Crawl a blog with tab-retry. Worker owns recovery.

        On TabDeadError from the agent: close dead tab, open new one, retry.
        After MAX_RECOVERY_PER_BLOG attempts, mark blog as error and return
        error dict (do NOT re-queue).
        """

        MAX_RECOVERY = MAX_RECOVERY_PER_BLOG  # from config (1)

        def _do_crawl():
            return crawl_blog(
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
                should_exit=lambda: self.wall_halt.is_set(),
                on_progress=self.progress_cb,
                first_html=first_html,
                first_url=first_url,
            )

        for attempt in range(1, MAX_RECOVERY + 1):
            # Fast abort: if shutdown was signaled, don't retry CDP operations
            if self.wall_halt.is_set():
                logger.info("Worker %d: shutdown signal during retry loop, aborting", self.worker_id)
                return {"username": username, "tier": tier, "status": "aborted", "unique": 0, "total": 0, "posts": 0, "usernames": [], "enqueued": 0, "dead": False}
            try:
                return await _do_crawl()
            except LoginWallDetected:
                # Fix A: a detected wall is a SOFT signal. Tumblr's rate-limit
                # / "are you human" interstitial routes through a /login URL,
                # which agent.detect_login_wall misreads as a hard login wall.
                # Retry the same blog from scratch up to WALL_RETRY_MAX times;
                # a transient interstitial clears on retry and the crawl
                # proceeds. Only after retries are exhausted do we accept it as
                # a genuine wall (re-raise -> worker loop halts + "log in").
                if attempt <= WALL_RETRY_MAX:
                    logger.warning(
                        "Worker %d: wall signal for %s (attempt %d/%d) — "
                        "retrying after %.0fs backoff (may be transient interstitial)",
                        self.worker_id,
                        username,
                        attempt,
                        WALL_RETRY_MAX,
                        WALL_RETRY_BACKOFF_S,
                    )
                    try:
                        await asyncio.wait_for(self.wall_halt.wait(), timeout=WALL_RETRY_BACKOFF_S)
                    except TimeoutError:
                        pass
                    if self.wall_halt.is_set():
                        logger.info("Worker %d: shutdown signal during wall retry, aborting", self.worker_id)
                        return {"username": username, "tier": tier, "status": "aborted", "unique": 0, "total": 0, "posts": 0, "usernames": [], "enqueued": 0, "dead": False}
                    continue
                logger.warning(
                    "Worker %d: wall confirmed for %s after %d retries — halting",
                    self.worker_id,
                    username,
                    WALL_RETRY_MAX,
                )
                raise
            except TabDeadError as exc:
                logger.warning(
                    "Worker %d: tab died for %s (attempt %d/%d): %s",
                    self.worker_id,
                    username,
                    attempt,
                    MAX_RECOVERY,
                    exc,
                )
                if attempt < MAX_RECOVERY:
                    # Check shutdown before sleeping + recovering
                    if self.wall_halt.is_set():
                        logger.info("Worker %d: shutdown signal after tab death, aborting", self.worker_id)
                        return {"username": username, "tier": tier, "status": "aborted", "unique": 0, "total": 0, "posts": 0, "usernames": [], "enqueued": 0, "dead": False}
                    try:
                        await asyncio.wait_for(self.wall_halt.wait(), timeout=2.0)
                    except TimeoutError:
                        pass
                    if self.wall_halt.is_set():
                        logger.info("Worker %d: shutdown signal during tab recovery, aborting", self.worker_id)
                        return {"username": username, "tier": tier, "status": "aborted", "unique": 0, "total": 0, "posts": 0, "usernames": [], "enqueued": 0, "dead": False}
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
                    # Fast-abort sleep: wake every 1s to check for shutdown signal
                    sleep_deadline = time.monotonic() + QUEUE_POLL_INTERVAL
                    while time.monotonic() < sleep_deadline:
                        if self.wall_halt.is_set():
                            logger.info("Worker %d: wall_halt during sleep, exiting", self.worker_id)
                            break
                        await asyncio.sleep(1.0)
                    if self.wall_halt.is_set():
                        break
                    continue

                # Abort immediately if shutdown was signaled between the while
                # check and dequeuing — don't start a new blog on Ctrl+C.
                if self.wall_halt.is_set():
                    logger.info("Worker %d: wall_halt set after dequeue — aborting", self.worker_id)
                    break

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
                logger.error("WORKER_TRACE: busy_event.set %s", username)
                if self.set_current_cb:
                    self.set_current_cb(username, tier)  # dashboard: now on this blog
                if self.progress_cb:
                    self.progress_cb(f"blog_start:{username}")  # heartbeat: we picked up a blog

                # NFR-10: index check at dispatch time — T0 (seed blog) is a
                # special case: always crawl it regardless of index state so
                # new posts since the last scan are picked up. T1/T2 keep the
                # existing fresh-skip optimization.
                if tier != 0:
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

                # FR-7: reindex mode — probe page 0, compare dates.
                # Reuse the fetched HTML instead of letting crawl_blog fetch
                # offset 0 again — avoids a redundant Page.navigate + render wait.
                probe_html, probe_url = "", ""
                if mode == "reindex":
                    try:
                        probe_html, probe_url = await self.navigate_to(username, 0)
                        if probe_html:
                            probe_result = await self.probe_page_zero(
                                username, probe_html, probe_url, self.cache_dir, self.index_path,
                                force=(tier == 0),
                            )
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
                            elif action == "overflow":
                                if self.stats_cb:
                                    self.stats_cb("queue_overflow")

                try:
                    result = await self._crawl_with_recovery(
                        username, tier, mode, _enqueue_page,
                        first_html=probe_html, first_url=probe_url,
                    )
                    if not result.get("usernames"):
                        # A blog with status="ok" but 0 posts is legitimately
                        # empty (NSFW/login-wall pages return no content) — NOT
                        # a markup failure. Only treat a blog as suspicious if
                        # the crawl itself errored (status="error") or we got a
                        # wall/dead signal, which would indicate the extractor
                        # or page load genuinely broke across the board.
                        if result.get("status") == "error" or result.get("dead"):
                            fail_count = _increment_fail_count(queue_path, username)
                            logger.warning(
                                "Worker %d: %s failed (dead=%s, fails=%d) — %s",
                                self.worker_id,
                                username,
                                result.get("dead", False),
                                fail_count,
                                "marking dead" if fail_count >= 2 else "requeuing",
                            )
                            if fail_count >= 2:
                                mark_done(queue_path, username)
                                errors += 1
                                processed += 1
                                self.busy_event.clear()
                                continue
                        else:
                            pass
                    else:
                        pass
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
                    logger.error("WORKER_TRACE: busy_event.clear after login_wall")
                    raise
                except Exception as exc:
                    logger.error(
                        "Worker %d: agent crashed for %s: %s",
                        self.worker_id,
                        username,
                        exc,
                    )
                    fail_count = _increment_fail_count(queue_path, username)
                    logger.warning(
                        "Worker %d: %s crashed (fails=%d) — %s",
                        self.worker_id,
                        username,
                        fail_count,
                        "marking dead" if fail_count >= 2 else "requeuing",
                    )
                    if fail_count >= 2:
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
            # Stop the persistent CDP client (if still connected)
            await self._stop_cdp_client()
            #
            # Close our tab on exit — the login session lives in the Chrome
            # profile dir, so closing the tab does NOT log us out. Leaving tabs
            # open is what accumulates across runs and OOMs Chrome.
            #
            # Fast shutdown: if wall_halt was set (SIGINT/drain complete), don't
            # use asyncio.shield and don't retry — just close via the HTTP
            # endpoint with a short timeout.
            if self.target_id:
                try:
                    from agent import close_tab
                    await asyncio.wait_for(
                        close_tab(self.browser_ws, self.target_id),
                        timeout=5.0,
                    )
                except Exception as _exc:  # noqa: BLE001
                    logger.debug("Worker %d: tab close failed during shutdown: %s", self.worker_id, _exc)
                self.target_id = None
                self.ws_url = None
            # Clear busy_event so a coordinator Task.cancel() mid-crawl cannot
            # leave busy_events[i] stuck True and block drain_complete forever
            try:
                self.busy_event.clear()
            except Exception:  # noqa: BLE001, S110
                pass
            logger.info("Worker %d: exiting — tab closed.", self.worker_id)

        return {"processed": processed, "errors": errors, "enqueued": enqueued}

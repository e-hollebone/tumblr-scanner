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
from typing import Any

from agent import (
    LoginWallDetected,
    TabDeadError,
    crawl_blog,
    probe_blog,
)
from config import DELAY_MAX, DELAY_MIN, LIMITS_BY_TIER, QUEUE_POLL_INTERVAL
from work_queue import dequeue, mark_done
from eventlog import info as ev, warn as ev_warn, error as ev_err

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
    ) -> None:
        self.worker_id = worker_id
        self.browser_ws = browser_ws
        self.cache_dir = cache_dir
        self.index_path = index_path
        self.wall_halt = wall_halt
        self.busy_event = busy_event or asyncio.Event()

        self.ws_url: str | None = None
        self.target_id: str | None = None
        self._empty_since: float | None = None

    # ------------------------------------------------------------------ #
    # Tab lifecycle                                                      #
    # ------------------------------------------------------------------ #

    async def _open_tab(self) -> None:
        """Open a new Chrome tab. Called once at worker start."""
        from agent import _new_tab_url

        self.ws_url, self.target_id = await _new_tab_url(
            self.browser_ws, "https://www.tumblr.com/"
        )
        logger.info("Worker %d: opened tab targetId=%s", self.worker_id, self.target_id)
        ev("worker%d" % self.worker_id, "tab_opened", target_id=self.target_id)

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

    async def _replace_tab(self) -> None:
        """Open a fresh tab (recovery). Does NOT close the dead tab — the
        operator owns tab lifecycle."""
        await self._open_tab()

    # ------------------------------------------------------------------ #
    # WS URL refresh (NFR-9)                                             #
    # ------------------------------------------------------------------ #

    async def _refresh_ws(self) -> None:
        """Re-query /json/list for the current page WS URL of our tab."""
        if not self.target_id:
            return
        import json
        import urllib.request

        base = self.browser_ws.replace("ws://", "http://").rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
                targets = json.loads(resp.read())
            for t in targets:
                if t.get("type") == "page" and t.get("id") == self.target_id:
                    new_ws = t.get("webSocketDebuggerUrl")
                    if new_ws and new_ws != self.ws_url:
                        self.ws_url = new_ws
                    return
        except Exception:  # noqa: BLE001
            pass  # Keep existing WS URL on refresh failure

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
        After MAX_RECOVERY_ATTEMPTS, re-raise as a non-fatal error dict.
        """

        MAX_TAB_RETRIES = 3
        last_exc: Exception | None = None

        for attempt in range(1, MAX_TAB_RETRIES + 1):
            try:
                return await crawl_blog(
                    browser_ws=self.browser_ws,
                    ws_url=self.ws_url,
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
                )
            except TabDeadError as exc:
                last_exc = exc
                logger.warning(
                    "Worker %d: tab died for %s (attempt %d/%d): %s",
                    self.worker_id,
                    username,
                    attempt,
                    MAX_TAB_RETRIES,
                    exc,
                )
                if attempt < MAX_TAB_RETRIES:
                    await asyncio.sleep(2.0)
                    await self._replace_tab()
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

        # Unreachable — for type-checker
        raise RuntimeError(f"unreachable: {last_exc}")

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

                # Advertise "mid-crawl" so the coordinator does not declare the
                # drain complete while we are working this blog.
                self.busy_event.set()

                username = item["username"]
                tier = item.get("tier", 1)
                mode = item.get("mode", "full")

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
                        probe_result = await probe_blog(
                            self.browser_ws,
                            username,
                            cache_dir=self.cache_dir,
                            index_path=self.index_path,
                        )
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
                await self._refresh_ws()

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

                try:
                    result = await self._crawl_with_recovery(
                        username, tier, mode, _enqueue_page
                    )
                    if not result.get("usernames"):
                        consecutive_empty += 1
                        if consecutive_empty >= 3:
                            logger.error(
                                "Worker %d: %d consecutive blogs yielded 0 usernames — "
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
                    continue

                # Write to index
                from datetime import datetime, timezone

                index_entry = {
                    "username": username,
                    "tier": tier,
                    "status": result.get("status", "unknown"),
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "unique": result.get("unique_count", 0),
                    "total": result.get("total_count", 0),
                    "posts": result.get("posts_processed", 0),
                    "usernames": result.get("usernames", []),
                    "dead": result.get("dead", False),
                }
                _write_index(self.index_path, username, index_entry)

                ev("worker%d" % self.worker_id, "blog_done", username=username, status=result.get("status"), unique=result.get("unique_count"), total=result.get("total_count"), posts=result.get("posts_processed"))

                logger.info(
                    "Worker %d: done %s status=%s unique=%d",
                    self.worker_id,
                    username,
                    result.get("status", "unknown"),
                    result.get("unique_count", 0),
                )

                mark_done(queue_path, username)
                processed += 1
                self.busy_event.clear()

        finally:
            # DO NOT close the tab. The operator owns tab lifecycle — workers
            # leave their tabs open for inspection. The operator closes them.
            logger.info(
                "Worker %d: exiting — tab left OPEN (targetId=%s).",
                self.worker_id,
                self.target_id,
            )

        return {"processed": processed, "errors": errors, "enqueued": enqueued}

"""Queue-mode pipeline — fresh Chrome, worker pool, parallel from first extraction.

Architecture:
  1. Fresh Chrome restart (dedicated profile, never touches user's Chrome).
  2. Seed blog is the FIRST queue item. Workers start immediately.
  3. Each worker owns ONE tab for its entire lifetime.
  4. Worker pulls a blog → crawls page-by-page → enqueues discoveries.
  5. The moment the seed worker finishes page 0 and enqueues depth-1 names,
     idle workers pick them up. No T0→T1→T2 gate.
  6. Parallelism starts at first extraction.

Workers poll the queue. When empty, they sleep briefly and retry. If the
queue stays empty past QUEUE_EMPTY_TIMEOUT, the worker exits.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent import LoginWallDetected
from cache import index_status
from chrome_lifecycle import restart_chrome
from config import (
    INDEX_PATH,
    QUEUE_OVERFLOW_THRESHOLD,
    QUEUE_PATH,
    WORKER_POOL_SIZE,
    WORKER_STALL_TIMEOUT,
)
from work_queue import cleanup as queue_cleanup
from work_queue import active_count, enqueue, pending_count, in_progress_count, queue_size
from eventlog import info as ev, warn as ev_warn, error as ev_err
from status_server import publish as _publish_status, start_status_server as _start_status_server

logger = logging.getLogger("queue-pipeline")


def _index_lock_path(path: Path) -> Path:
    """Dedicated lock file (never replaced) so flock serializes across writes."""
    return path.with_suffix(".lock")


def _read_index(path: Path) -> dict[str, Any]:
    """Read index.json or return empty dict. Uses LOCK_SH on a stable lock file."""
    if not path.exists():
        return {}
    lock_path = _index_lock_path(path)
    try:
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            finally:
                pass
    except (json.JSONDecodeError, OSError):
        return {}


def _write_index(path: Path, username: str, entry: dict[str, Any]) -> None:
    """Write or update a single entry in index.json. Uses LOCK_EX on a stable lock file."""
    import tempfile as _tf

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _index_lock_path(path)
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = {}
            try:
                with open(path, encoding="utf-8") as rf:
                    data = json.load(rf)
            except (json.JSONDecodeError, OSError):
                data = {}
            entry.setdefault("username", username)
            data[username] = entry
            fd, tmp_str = _tf.mkstemp(dir=str(path.parent), prefix=f"{path.stem}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                    json.dump(data, tmp_f, indent=2, ensure_ascii=False)
                    tmp_f.write("\n")
                os.replace(tmp_str, path)
            finally:
                if os.path.exists(tmp_str):
                    os.unlink(tmp_str)
        finally:
            pass


def _enqueue_by_status(
    queue_path: Path,
    index_path: Path,
    username: str,
    tier: int,
) -> str:
    """Enqueue a username based on its two-way index status.

    Returns the action taken: "reindex" (date probe enqueued) or
    "full" (full crawl enqueued). Names already in the index are
    enqueued as "reindex" (FR-7 date probe decides skip-or-crawl);
    names not in the index are enqueued as "full".
    """
    status = index_status(index_path, username)
    if queue_size(queue_path) >= QUEUE_OVERFLOW_THRESHOLD:
        logger.warning(
            "Queue overflow (%d >= %d) — skipping enqueue of %s",
            queue_size(queue_path), QUEUE_OVERFLOW_THRESHOLD, username,
        )
        return "fresh"
    if status == "stale":
        enqueue(queue_path, username, state="", tier=tier, mode="reindex")
        logger.info("Enqueued %s (tier=%s, mode=reindex)", username, tier)
        return "reindex"
    enqueue(queue_path, username, state="", tier=tier, mode="full")
    logger.info("Enqueued %s (tier=%s, mode=full)", username, tier)
    return "full"


def _next_tier(current: int) -> int:
    """Return the next tier for a discovered username (depth + 1)."""
    if current >= 2:
        return 2
    return current + 1


async def _drain_queue(
    queue_path: Path,
    index_path: Path,
    cache_dir: Path,
    browser_ws: str,
    pool_size: int = WORKER_POOL_SIZE,
    wall_halt: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Drain the queue using a pool of workers. Parallel from first extraction.

    Each worker owns ONE tab for its entire lifetime. Workers poll the queue;
    if empty, they sleep and retry. If the queue stays empty past the timeout,
    the worker exits. The pool runs until all workers exit.

    Args:
        wall_halt: caller-owned asyncio.Event used as the shutdown signal. If
            None, a fresh event is created (self-contained runs). The coordinator
            loop and every worker honor this event to release on shutdown.

    Returns drain stats.
    """
    processed = 0
    errors = 0
    total_enqueued = 0
    start = time.monotonic()
    # Use the caller-provided shutdown event if given (so an external signal
    # handler can halt the crawl); otherwise create a self-contained one.
    if wall_halt is None:
        wall_halt = asyncio.Event()

    logger.info(
        "Starting queue drain (queue_size=%d, workers=%d)",
        queue_size(queue_path),
        pool_size,
    )
    ev("coordinator", "drain_start", workers=pool_size, queue_size=queue_size(queue_path))

    # Live counters published to the dashboard (mutated by worker results
    # via the shared callback below). The coordinator only sees final
    # totals after gather(), so we track them live here.
    _live = {"blogs_done": 0, "errors": 0, "enqueued": 0}
    from threading import Lock as _Lock
    _live_lock = _Lock()

    def _on_blog_done_inc() -> None:
        with _live_lock:
            _live["blogs_done"] += 1

    # indexed count helper (reads index.json size)
    def _index_count(idx_path: Path) -> int:
        try:
            import json as _json
            if not idx_path.exists():
                return 0
            with open(idx_path, encoding="utf-8") as _f:
                return len(_json.load(_f))
        except Exception:
            return 0

    # Start the live status dashboard (operator watches it from Preview; no
    # need to ask the agent for updates). http://localhost:8788/
    try:
        _start_status_server()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not start status server: %s", exc)

    # Shared "worker busy" signals: coordinator must NOT declare the crawl
    # done while any worker is mid-crawl. active_count() alone can miss the
    # in_progress window between dequeue and mark_done (and that is exactly
    # what caused an early drain_complete in a prior run), so workers
    # advertise their crawl state explicitly via these events.
    busy_events = [asyncio.Event() for _ in range(pool_size)]

    # ACTIVE PROGRESS MONITOR (user directive 2026-08-28): busy_event only
    # tells the coordinator a worker THINKS it is busy. A worker stuck inside a
    # CDP await with no timeout holds busy_event forever while making zero
    # progress — the coordinator must not just trust busy_event, it must
    # verify progress. Each worker bumps progress_at[worker_id] on every page
    # fetch and at blog start. The coordinator force-restarts any worker that
    # holds busy_event but has not progressed in WORKER_STALL_TIMEOUT seconds.
    progress_at: dict[int, float] = {
        i: time.monotonic() for i in range(pool_size)
    }
    # Current blog each worker is on (set at blog_start) — for the live
    # dashboard. None = idle/not-yet-started.
    current_blog: dict[int, str | None] = {
        i: None for i in range(pool_size)
    }
    current_tier: dict[int, int | None] = {
        i: None for i in range(pool_size)
    }

    # Launch the worker pool — each Worker instance owns its own tab
    from worker import Worker

    def _make_progress_cb(wid: int) -> Callable[[str], None]:
        def _cb(msg: str) -> None:
            progress_at[wid] = time.monotonic()
        return _cb

    def _make_set_current_cb(wid: int) -> Callable[[str, int], None]:
        def _cb(username: str, tier: int) -> None:
            current_blog[wid] = username
            current_tier[wid] = tier
        return _cb

    def _make_stats_cb() -> Callable[[str], None]:
        def _cb(key: str) -> None:
            with _live_lock:
                if key in _live:
                    _live[key] += 1
        return _cb

    worker_tasks = [
        asyncio.create_task(
            Worker(
                worker_id=i,
                browser_ws=browser_ws,
                cache_dir=cache_dir,
                index_path=index_path,
                wall_halt=wall_halt,
                busy_event=busy_events[i],
                progress_cb=_make_progress_cb(i),
                set_current_cb=_make_set_current_cb(i),
                stats_cb=_make_stats_cb(),
            ).run(queue_path)
        )
        for i in range(pool_size)
    ]

    # Coordinator-owned shutdown: workers wait indefinitely for queue items
    # (they never self-kill on an empty queue). The coordinator decides when
    # the whole crawl is done — when the queue has been empty for a grace
    # period AND no worker is mid-crawl. Set wall_halt to release all workers.
    DRAIN_IDLE_GRACE = 15.0
    idle_since: float | None = None
    loop_count = 0
    workers_silent = False
    stalled_restarted: set[int] = set()
    while not wall_halt.is_set():
        await asyncio.sleep(2.0)
        loop_count += 1
        qsize = active_count(queue_path)
        crawling = any(e.is_set() for e in busy_events)
        pending = pending_count(queue_path)
        in_progress = in_progress_count(queue_path)
        if loop_count % 5 == 0:
            logger.error("DRAIN_TRACE: loop #%d qsize=%d pending=%d in_progress=%d crawling=%s busy=%s",
                         loop_count, qsize, pending, in_progress, crawling,
                         [e.is_set() for e in busy_events])

        # ---- ACTIVE PROGRESS MONITOR -------------------------------------
        # A worker holding busy_event but silent for > WORKER_STALL_TIMEOUT is
        # HUNG (a CDP await with no timeout). Do not wait passively — cancel the
        # stuck task and respawn a fresh worker so its tab/queue slot is reused.
        now = time.monotonic()
        workers_silent = all(
            not busy_events[i].is_set()
            and (now - progress_at[i]) >= DRAIN_IDLE_GRACE
            for i in range(pool_size)
        )
        for i in range(pool_size):
            if not busy_events[i].is_set():
                stalled_restarted.discard(i)
                continue
            silent = now - progress_at[i]
            if silent >= WORKER_STALL_TIMEOUT and i not in stalled_restarted:
                logger.error(
                    "STALL WATCHDOG: worker %d silent for %.0fs (no page_fetched) "
                    "— force-restarting task",
                    i, silent,
                )
                ev_err("coordinator", "worker_stall_restart",
                       worker_id=i, silent_seconds=round(silent, 1))
                old_task = worker_tasks[i]
                # Cancel the hung task. We do NOT `await old_task` here: a hung
                # worker is stuck inside a CDP await with no timeout, so awaiting
                # it would re-block the coordinator — the exact bug this watchdog
                # exists to fix. Cancellation fires; the task's finally block
                # closes its tab. We shield the cancel so a stuck await can't
                # swallow the CancelledError and hang us.
                old_task.cancel()
                # Spawn replacement immediately so the worker slot is reused.
                worker_tasks[i] = asyncio.create_task(
                    Worker(
                        worker_id=i,
                        browser_ws=browser_ws,
                        cache_dir=cache_dir,
                        index_path=index_path,
                        wall_halt=wall_halt,
                        busy_event=busy_events[i],
                        progress_cb=_make_progress_cb(i),
                        set_current_cb=_make_set_current_cb(i),
                        stats_cb=_make_stats_cb(),
                    ).run(queue_path)
                )
                progress_at[i] = now
                stalled_restarted.add(i)
                logger.info("STALL WATCHDOG: worker %d respawned", i)
        # -----------------------------------------------------------------

        # ---- PUBLISH LIVE STATUS (operator dashboard) --------------------
        # Compute per-worker status for the dashboard: busy (recent progress),
        # stalled (busy but silent >30s), idle (not busy). Then push a snapshot.
        workers_status = []
        last_stall = None
        for i in range(pool_size):
            lag = now - progress_at[i]
            if busy_events[i].is_set():
                status = "stalled" if lag > 30 else "busy"
            else:
                status = "idle"
            if status == "stalled" and (last_stall is None or lag > last_stall["silent_s"]):
                last_stall = {"worker_id": i, "silent_s": lag}
            workers_status.append({
                "id": i,
                "status": status,
                "current": current_blog.get(i),
                "tier": current_tier.get(i),
                "lag_s": round(lag, 1),
            })
        _publish_status(
            queue_pending=active_count(queue_path),
            queue_in_progress=sum(1 for e in busy_events if e.is_set()),
            indexed=_index_count(index_path),
            blogs_done=_live["blogs_done"],
            errors=_live["errors"],
            enqueued=_live["enqueued"],
            workers=workers_status,
            last_stall=last_stall,
            login_wall=False,
            drain_complete=False,
        )
        # -----------------------------------------------------------------

        if loop_count % 10 == 1 or qsize == 0 or not crawling:
            stalled = [
                i for i in range(pool_size)
                if busy_events[i].is_set()
                and (now := time.monotonic()) - progress_at[i] > 30
            ]
            logger.info(
                "Coordinator loop #%d: qsize=%d crawling=%s idle_since=%s "
                "progress_lag=%s",
                loop_count,
                qsize,
                crawling,
                idle_since,
                {i: round(time.monotonic() - progress_at[i]) for i in range(pool_size)},
            )
        # Require both queue empty AND no index growth for the full idle grace.
        # The old gate only checked queue state, which could be empty while
        # workers were still mid-blog and producing blog_done results afterward.
        if pending == 0 and in_progress == 0 and workers_silent:
            if idle_since is None:
                idle_since = time.monotonic()
                last_index_count = _index_count(index_path)
            elif time.monotonic() - idle_since >= DRAIN_IDLE_GRACE:
                current_index_count = _index_count(index_path)
                final_pending = pending_count(queue_path)
                if final_pending > 0:
                    logger.warning(
                        "Coordinator: final pre-halt re-check found %d pending "
                        "(transient empty during grace) — continuing drain",
                        final_pending,
                    )
                    idle_since = None
                elif current_index_count != last_index_count:
                    logger.warning(
                        "Coordinator: index still growing (%d -> %d) during drain "
                        "grace — continuing drain",
                        last_index_count,
                        current_index_count,
                    )
                    idle_since = time.monotonic()
                    last_index_count = current_index_count
                else:
                    logger.info(
                        "Coordinator: queue empty and index stable for %.0fs — "
                        "crawl complete, halting workers",
                        time.monotonic() - idle_since,
                    )
                    logger.error("DRAIN_TRACE: setting wall_halt pending=%d in_progress=%d index=%d busy=%s",
                                 pending, in_progress, current_index_count,
                                 [e.is_set() for e in busy_events])
                    wall_halt.set()
                    break
        else:
            idle_since = None

    elapsed = time.monotonic() - start
    ev("coordinator", "drain_complete", processed=processed, errors=errors, new_enqueued=total_enqueued, elapsed_seconds=round(elapsed, 1))

    # Final dashboard publish: mark drain complete.
    _publish_status(
        queue_pending=active_count(queue_path),
        queue_in_progress=0,
        indexed=_index_count(index_path),
        blogs_done=_live["blogs_done"],
        errors=_live["errors"],
        enqueued=_live["enqueued"],
        workers=[{"id": i, "status": "idle", "current": None, "lag_s": 0.0}
                 for i in range(pool_size)],
        last_stall=None,
        login_wall=False,
        drain_complete=True,
    )

    results = await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Aggregate stats
    for r in results:
        if isinstance(r, dict):
            processed += r["processed"]
            errors += r["errors"]
            total_enqueued += r["enqueued"]
        elif isinstance(r, LoginWallDetected):
            # Re-raise the caught login-wall signal to halt the pipeline.
            raise r
        elif isinstance(r, Exception):
            logger.error("Worker pool error: %s", r)
            errors += 1

    elapsed = time.monotonic() - start
    logger.info(
        "Drain complete: processed=%d errors=%d new_enqueued=%d elapsed=%.1fs",
        processed,
        errors,
        total_enqueued,
        elapsed,
    )
    queue_cleanup(queue_path)
    return {
        "processed": processed,
        "errors": errors,
        "new_enqueued": total_enqueued,
        "elapsed_seconds": elapsed,
        "queue_final": queue_size(queue_path),
        "wall_halt": wall_halt,
    }


async def queue_mode(
    target_blog: str,
    browser_ws: str,
    cache_dir: Path,
    verbose: bool = False,
    pool_size: int = WORKER_POOL_SIZE,
    wall_halt: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Run the queue-mode pipeline: fresh Chrome, worker pool, parallel from first extraction.

    1. Kill + restart Chrome
    2. Enqueue the seed blog as the FIRST queue item
    3. Start the worker pool — workers pull from the queue immediately
    4. The seed blog is just another queue item; workers crawl it, enqueue
       discoveries, and other workers pick them up — no staging gate

    Args:
        wall_halt: optional caller-owned asyncio.Event. If provided, the drain
            loop uses THIS event for shutdown (so an external signal handler
            registered by the caller can set it to halt the crawl). If None, a
            fresh event is created internally.
    """
    overall_start = time.monotonic()

    # Step 1: Fresh Chrome restart
    logger.info("=== Queue-mode pipeline: %s ===", target_blog)
    logger.info("Step 1: Restarting Chrome for fresh state")
    chrome_status = restart_chrome()
    ev("pipeline", "chrome_restart", reused=chrome_status.get("reused"), killed=chrome_status.get("killed"), port=chrome_status.get("port"), login_wall=chrome_status.get("login_wall"))
    if chrome_status.get("status") not in ("ok",):
        logger.warning("Chrome restart status: %s — continuing anyway", chrome_status)
    logger.info(
        "Chrome status: killed=%s remaining=%s debug_port=%s",
        chrome_status.get("killed"),
        chrome_status.get("remaining_after_kill"),
        chrome_status.get("debug_port", "")[:40],
    )

    actual_browser_ws = chrome_status.get("debug_port") or browser_ws
    if actual_browser_ws.startswith("ws://"):
        actual_browser_ws = "http://" + actual_browser_ws[len("ws://"):].split("/")[0]
    logger.info("Using browser endpoint: %s", actual_browser_ws)

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Step 0: Repair the queue before seeding. A prior run that died on the
    # login wall (or crashed) leaves items stuck in "in_progress" with no
    # index entry. cleanup() resets those to pending so workers can retry
    # them. Without this, seeding T0 dedupes against the orphan and the run
    # finds an empty pending queue → all workers idle forever.
    repaired = queue_cleanup(QUEUE_PATH)
    if repaired:
        logger.info("Step 0: repaired %d stale queue entries", repaired)
        ev("pipeline", "queue_repaired", entries=repaired)

    # Step 2: Seed the queue with the target blog (tier 0)
    logger.info("Step 2: Seeding queue with %s", target_blog)
    enqueue(QUEUE_PATH, target_blog, state="", tier=0)
    ev("pipeline", "seed_queue", blog=target_blog, tier=0)
    logger.info("Queue seeded: %s (tier=0)", target_blog)

    # Step 3: Drain queue — workers start immediately, parallel from first extraction
    logger.info("Step 3: Drain queue — worker pool (parallel from first extraction)")
    drain_result = await _drain_queue(
        queue_path=QUEUE_PATH,
        index_path=INDEX_PATH,
        cache_dir=cache_dir,
        browser_ws=actual_browser_ws,
        pool_size=pool_size,
        wall_halt=wall_halt,
    )

    total_elapsed = time.monotonic() - overall_start
    logger.info("=== Queue-mode pipeline complete (%.1fs) ===", total_elapsed)

    return {
        "tier": "queue",
        "target_blog": target_blog,
        "browser": browser_ws,
        "cache_dir": str(cache_dir),
        "chrome_status": chrome_status,
        "queue_path": str(QUEUE_PATH),
        "index_path": str(INDEX_PATH),
        "drain": drain_result,
        "elapsed_seconds": total_elapsed,
        "wall_halt": drain_result.get("wall_halt"),
    }

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
from typing import Any

from agent import LoginWallDetected, run as agent_run
from chrome_lifecycle import restart_chrome
from config import (
    DELAY_MIN,
    DELAY_MAX,
    INDEX_PATH,
    LIMITS_BY_TIER,
    MAX_CONCURRENT_AGENTS,
    QUEUE_EMPTY_TIMEOUT,
    QUEUE_PATH,
    QUEUE_POLL_INTERVAL,
    RECRAWL_DAYS,
)
from work_queue import dequeue, enqueue, mark_done, queue_size

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


def _index_has_fresh_entry(path: Path, username: str, recrawl_days: int) -> bool:
    """Check if username already has a fresh entry in the index. Uses LOCK_SH."""
    idx = _read_index(path)
    entry = idx.get(username)
    if not entry:
        return False
    scanned_at = entry.get("scanned_at", "")
    if not scanned_at:
        return False
    try:
        scanned_dt = datetime.fromisoformat(scanned_at)
        age_days = (datetime.now(timezone.utc) - scanned_dt).total_seconds() / 86400
        return age_days < recrawl_days
    except (ValueError, TypeError):
        return False


def _enqueue_if_not_indexed(
    queue_path: Path,
    index_path: Path,
    username: str,
    tier: int,
    recrawl_days: int,
) -> bool:
    """Enqueue username if not already in index with a fresh entry."""
    if _index_has_fresh_entry(index_path, username, recrawl_days):
        logger.debug("Skipping enqueue of %s — fresh index entry exists", username)
        return False
    enqueue(queue_path, username, state="", tier=tier)
    logger.info("Enqueued %s (tier=%s)", username, tier)
    return True


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
    recrawl_days: int,
) -> dict[str, Any]:
    """Drain the queue using a pool of workers. Parallel from first extraction.

    Each worker owns ONE tab for its entire lifetime. Workers poll the queue;
    if empty, they sleep and retry. If the queue stays empty past the timeout,
    the worker exits. The pool runs until all workers exit.

    Returns drain stats.
    """
    processed = 0
    errors = 0
    total_enqueued = 0
    start = time.monotonic()
    wall_halt = asyncio.Event()

    logger.info(
        "Starting queue drain (queue_size=%d, workers=%d)",
        queue_size(queue_path),
        MAX_CONCURRENT_AGENTS,
    )

    async def _worker(worker_id: int) -> dict[str, int]:
        """One worker: open a tab, refresh WS per blog, loop dequeue → crawl → mark_done.

        The tab is opened once and reused across every blog. It is closed
        only when the worker exits (queue empty-timeout or wall halt).
        """
        my_processed = 0
        my_errors = 0
        my_enqueued = 0
        ws_url: str | None = None
        target_id: str | None = None
        empty_since: float | None = None

        try:
            # Open ONE tab for this worker's entire lifetime
            from agent import _new_tab_url, close_tab

            ws_url, target_id = await _new_tab_url(browser_ws, "https://www.tumblr.com/")
            logger.info("Worker %d: opened tab targetId=%s", worker_id, target_id)

            while not wall_halt.is_set():
                item = dequeue(queue_path)
                if item is None:
                    # Queue empty — track how long it's been empty
                    if empty_since is None:
                        empty_since = time.monotonic()
                        logger.info("Worker %d: queue empty, polling...", worker_id)
                    elif time.monotonic() - empty_since > QUEUE_EMPTY_TIMEOUT:
                        logger.info("Worker %d: queue empty timeout — exiting", worker_id)
                        break
                    await asyncio.sleep(QUEUE_POLL_INTERVAL)
                    continue

                # Got work — reset empty timer
                empty_since = None

                username = item["username"]
                tier = item.get("tier", 1)
                logger.info("Worker %d: processing %s (tier=%s)", worker_id, username, tier)

                limits = LIMITS_BY_TIER.get(tier)
                if limits is None:
                    logger.error("Unknown tier %s for %s — skipping", tier, username)
                    mark_done(queue_path, username)
                    my_errors += 1
                    my_processed += 1
                    continue

                # Refresh WS URL before each blog (NFR-9: static WS bug fix)
                # Re-query /json/list for the current page WS URL
                try:
                    from agent import _extract_browser_ws
                    fresh_ws = await _refresh_ws_url(browser_ws, target_id)
                    if fresh_ws:
                        ws_url = fresh_ws
                except Exception:
                    pass  # Keep existing WS URL on refresh failure

                kwargs = {
                    "browser_ws": browser_ws,
                    "username": username,
                    "tier": tier,
                    "unique_limit": limits["unique"],
                    "total_limit": limits["total"],
                    "post_limit": limits["posts"],
                    "delay_min": DELAY_MIN,
                    "delay_max": DELAY_MAX,
                    "recrawl_days": recrawl_days,
                    "source_blog": None,
                    "cache_dir": cache_dir,
                    "pre_existing_ws_url": ws_url,
                }

                try:
                    result = await agent_run(**kwargs)
                except LoginWallDetected:
                    logger.warning(
                        "Worker %d: LOGIN WALL DETECTED for %s — halting. "
                        "Log in to Tumblr in the Chrome window, then re-run.",
                        worker_id,
                        username,
                    )
                    wall_halt.set()
                    raise
                except Exception as exc:
                    logger.error("Worker %d: agent crashed for %s: %s", worker_id, username, exc)
                    mark_done(queue_path, username)
                    my_errors += 1
                    my_processed += 1
                    continue

                # Write to index
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
                _write_index(index_path, username, index_entry)

                # Enqueue discoveries at next tier
                new_count = 0
                for name in result.get("usernames", []):
                    if name != username:
                        next_t = _next_tier(tier)
                        if _enqueue_if_not_indexed(queue_path, index_path, name, next_t, recrawl_days):
                            new_count += 1
                            my_enqueued += 1

                logger.info(
                    "Worker %d: done %s status=%s unique=%d new=%d",
                    worker_id,
                    username,
                    result.get("status", "unknown"),
                    result.get("unique_count", 0),
                    new_count,
                )

                mark_done(queue_path, username)
                my_processed += 1

        finally:
            # Close the worker's persistent tab only on exit
            if target_id:
                try:
                    from agent import close_tab
                    await close_tab(browser_ws, target_id)
                    logger.info("Worker %d: closed tab targetId=%s", worker_id, target_id)
                except Exception as exc:
                    logger.warning("Worker %d: failed to close tab: %s", worker_id, exc)

        return {
            "processed": my_processed,
            "errors": my_errors,
            "enqueued": my_enqueued,
        }

    # Launch the worker pool
    worker_tasks = [
        asyncio.create_task(_worker(i)) for i in range(MAX_CONCURRENT_AGENTS)
    ]
    results = await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Aggregate stats
    for r in results:
        if isinstance(r, dict):
            processed += r["processed"]
            errors += r["errors"]
            total_enqueued += r["enqueued"]
        elif isinstance(r, LoginWallDetected):
            raise
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
    return {
        "processed": processed,
        "errors": errors,
        "new_enqueued": total_enqueued,
        "elapsed_seconds": elapsed,
        "queue_final": queue_size(queue_path),
    }


async def _refresh_ws_url(browser_ws: str, target_id: str) -> str | None:
    """Re-query /json/list for the current WS URL of target_id.

    NFR-9: The WS URL can change after navigation (SPA route changes,
    execution context swaps). Must refresh before each blog.
    """
    import urllib.request

    base = browser_ws.replace("ws://", "http://").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
            targets = json.loads(resp.read())
        for t in targets:
            if t.get("type") == "page" and t.get("id") == target_id:
                return t.get("webSocketDebuggerUrl")
    except Exception:
        pass
    return None


async def queue_mode(
    target_blog: str,
    browser_ws: str,
    cache_dir: Path,
    recrawl_days: int,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the queue-mode pipeline: fresh Chrome, worker pool, parallel from first extraction.

    1. Kill + restart Chrome
    2. Enqueue the seed blog as the FIRST queue item
    3. Start the worker pool — workers pull from the queue immediately
    4. The seed blog is just another queue item; workers crawl it, enqueue
       discoveries, and other workers pick them up — no staging gate
    """
    overall_start = time.monotonic()

    # Step 1: Fresh Chrome restart
    logger.info("=== Queue-mode pipeline: %s ===", target_blog)
    logger.info("Step 1: Restarting Chrome for fresh state")
    chrome_status = restart_chrome()
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

    # Step 2: Seed the queue with the target blog (tier 0)
    logger.info("Step 2: Seeding queue with %s", target_blog)
    enqueue(QUEUE_PATH, target_blog, state="", tier=0)
    logger.info("Queue seeded: %s (tier=0)", target_blog)

    # Step 3: Drain queue — workers start immediately, parallel from first extraction
    logger.info("Step 3: Drain queue — worker pool (parallel from first extraction)")
    drain_result = await _drain_queue(
        queue_path=QUEUE_PATH,
        index_path=INDEX_PATH,
        cache_dir=cache_dir,
        browser_ws=actual_browser_ws,
        recrawl_days=recrawl_days,
    )

    total_elapsed = time.monotonic() - overall_start
    logger.info("=== Queue-mode pipeline complete (%.1fs) ===", total_elapsed)

    return {
        "tier": "queue",
        "target_blog": target_blog,
        "browser": browser_ws,
        "cache_dir": str(cache_dir),
        "recrawl_days": recrawl_days,
        "chrome_status": chrome_status,
        "queue_path": str(QUEUE_PATH),
        "index_path": str(INDEX_PATH),
        "drain": drain_result,
        "elapsed_seconds": total_elapsed,
    }

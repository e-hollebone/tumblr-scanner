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

from agent import LoginWallDetected
from cache import index_status
from chrome_lifecycle import restart_chrome
from config import (
    INDEX_PATH,
    MAX_CONCURRENT_AGENTS,
    QUEUE_PATH,
)
from work_queue import enqueue, queue_size

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


def _enqueue_by_status(
    queue_path: Path,
    index_path: Path,
    username: str,
    tier: int,
    recrawl_days: int,
) -> str:
    """Enqueue a username based on its three-way index status.

    Returns the action taken: "fresh" (dropped), "reindex" (date probe enqueued),
    or "full" (full crawl enqueued).
    """
    status = index_status(index_path, username, recrawl_days)
    if status == "fresh":
        logger.debug("Skipping enqueue of %s — fresh index entry exists", username)
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

    # Launch the worker pool — each Worker instance owns its own tab
    from worker import Worker

    worker_tasks = [
        asyncio.create_task(
            Worker(
                worker_id=i,
                browser_ws=browser_ws,
                cache_dir=cache_dir,
                index_path=index_path,
                recrawl_days=recrawl_days,
                wall_halt=wall_halt,
            ).run(queue_path)
        )
        for i in range(MAX_CONCURRENT_AGENTS)
    ]
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
    return {
        "processed": processed,
        "errors": errors,
        "new_enqueued": total_enqueued,
        "elapsed_seconds": elapsed,
        "queue_final": queue_size(queue_path),
    }


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

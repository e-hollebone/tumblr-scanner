"""
Queue-mode pipeline — fresh Chrome, T0 producer, drain T1/T2 via worker loop.

This replaces the serialized run_full_pipeline with a queue-based architecture:

  1. Kill + restart Chrome (fresh state, never depend on prior tab state).
  2. Run T0 (producer path): crawl target blog, enqueue target + discovered
     usernames into queue.jsonl, write target to index.json.
  3. Drain loop: worker-style dequeue→crawl→mark_done→enqueue discoveries,
     until the queue is empty.  T1 starts as soon as T0 enqueues; T2 starts
     as soon as T1 enqueues discoveries.  No tier-specific logic — every blog
     is an independent instance.

Usage (via run.py):
    python run.py <target> --queue
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
from work_queue import dequeue, enqueue, mark_done, startup, queue_size

logger = logging.getLogger("queue-pipeline")

# Default paths
QUEUE_PATH = Path("/Users/eric/Documents/tumblr-scanner/cache/queue.jsonl")
INDEX_PATH = Path("/Users/eric/Documents/tumblr-scanner/cache/index.json")

# T0 limits (from coordinator, but we keep them local for the producer path)
T0_LIMITS = {"unique": 250, "total": 500, "posts": 500}
T1_LIMITS = {"unique": 100, "total": 250, "posts": 250}
T2_LIMITS = {"unique": 75, "total": 125, "posts": 125}

LIMITS_BY_TIER = {0: T0_LIMITS, 1: T1_LIMITS, 2: T2_LIMITS}

# Delay between queue drain iterations
DRAIN_SLEEP = 0.5


def _next_tier(current: int) -> int:
    """Return the next tier for a discovered username (depth + 1)."""
    if current >= 2:
        return 2
    return current + 1


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
            # Read fresh from disk (never trust a pre-replace fd)
            data: dict[str, Any] = {}
            try:
                with open(path, encoding="utf-8") as rf:
                    data = json.load(rf)
            except (json.JSONDecodeError, OSError):
                data = {}
            entry.setdefault("username", username)
            data[username] = entry
            # Unique temp file per writer — avoids cross-writer clobber on the .tmp name
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
    tier: str,
    recrawl_days: int,
) -> bool:
    """Enqueue username if not already in index with a fresh entry.

    Returns True if enqueued, False if skipped.
    """
    if _index_has_fresh_entry(index_path, username, recrawl_days):
        logger.debug("Skipping enqueue of %s — fresh index entry exists", username)
        return False
    enqueue(queue_path, username, state="", tier=tier)
    logger.info("Enqueued %s (tier=%s)", username, tier)
    return True


async def _run_t0_producer(
    target_blog: str,
    browser_ws: str,
    cache_dir: Path,
    recrawl_days: int,
    queue_path: Path,
    index_path: Path,
) -> dict[str, Any]:
    """Run T0 crawl for target, enqueue discoveries, write to index.

    Returns the T0 result dict plus enqueue count.
    """
    logger.info("T0 producer: crawling %s", target_blog)
    result = await agent_run(
        browser_ws=browser_ws,
        username=target_blog,
        tier=0,
        unique_limit=T0_LIMITS["unique"],
        total_limit=T0_LIMITS["total"],
        post_limit=T0_LIMITS["posts"],
        delay_min=6.7,
        delay_max=10.0,
        recrawl_days=recrawl_days,
        source_blog=None,
        cache_dir=cache_dir,
        pre_existing_ws_url=None,
    )

    usernames = result.get("usernames", [])
    status = result.get("status", "unknown")
    unique_count = result.get("unique_count", len(usernames))
    total_count = result.get("total_count", 0)
    posts_processed = result.get("posts_processed", 0)
    dead = result.get("dead", False)

    logger.info(
        "T0 producer: %s — status=%s unique=%d total=%d posts=%d usernames_found=%d",
        target_blog,
        status,
        unique_count,
        total_count,
        posts_processed,
        len(usernames),
    )

    # Write target to index
    _write_index(
        index_path,
        target_blog,
        {
            "tier": 0,
            "status": status,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "unique": unique_count,
            "total": total_count,
            "posts": posts_processed,
            "usernames": usernames,
            "dead": dead,
        },
    )

    # Enqueue target + all discovered usernames at t1 (skip if already indexed)
    enqueued = 0
    for name in usernames:
        if name != target_blog:
            if _enqueue_if_not_indexed(queue_path, index_path, name, 1, recrawl_days):
                enqueued += 1

    # Also enqueue the target itself at tier 1 so the worker processes it
    if _enqueue_if_not_indexed(queue_path, index_path, target_blog, 1, recrawl_days):
        enqueued += 1

    logger.info("T0 producer: enqueued %d items to queue", enqueued)
    return {
        **result,
        "enqueued": enqueued,
    }


async def _drain_queue(
    queue_path: Path,
    index_path: Path,
    cache_dir: Path,
    browser_ws: str,
    recrawl_days: int,
) -> dict[str, Any]:
    """Drain the queue: dequeue → crawl → mark_done → enqueue discoveries.

    Runs until the queue is empty.  Returns drain stats.
    """
    processed = 0
    errors = 0
    total_enqueued = 0
    start = time.monotonic()

    logger.info("Starting queue drain (queue_size=%d)", queue_size(queue_path))

    while True:
        item = dequeue(queue_path)
        if item is None:
            logger.info("Queue empty — drain complete")
            break

        username = item["username"]
        tier = item.get("tier", 1)
        logger.info("Processing: %s (tier=%s)", username, tier)

        limits = LIMITS_BY_TIER.get(tier)
        if limits is None:
            logger.error("Unknown tier %s for %s — skipping", tier, username)
            mark_done(queue_path, username)
            errors += 1
            processed += 1
            time.sleep(DRAIN_SLEEP)
            continue

        kwargs = {
            "browser_ws": browser_ws,
            "username": username,
            "tier": tier,
            "unique_limit": limits["unique"],
            "total_limit": limits["total"],
            "post_limit": limits["posts"],
            "delay_min": 6.7,
            "delay_max": 10.0,
            "recrawl_days": recrawl_days,
            "source_blog": None,
            "cache_dir": cache_dir,
            "pre_existing_ws_url": None,
        }

        try:
            result = await agent_run(**kwargs)
        except LoginWallDetected:
            # Wall is up — halt the entire pipeline, don't churn through tabs
            logger.warning(
                "LOGIN WALL DETECTED for %s — halting queue drain. "
                "Log in to Tumblr in the Chrome window, then re-run.",
                username,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — agent crash, log and continue
            logger.error("Agent crashed for %s: %s", username, exc)
            mark_done(queue_path, username)
            errors += 1
            processed += 1
            time.sleep(DRAIN_SLEEP)
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
                next_tier = _next_tier(tier)
                if _enqueue_if_not_indexed(queue_path, index_path, name, next_tier, recrawl_days):
                    new_count += 1
                    total_enqueued += 1

        logger.info(
            "Done: %s status=%s unique=%d total=%d posts=%d new_enqueued=%d",
            username,
            result.get("status", "unknown"),
            result.get("unique_count", 0),
            result.get("total_count", 0),
            result.get("posts_processed", 0),
            new_count,
        )

        mark_done(queue_path, username)
        processed += 1

        # Small pause between items to avoid hammering
        time.sleep(DRAIN_SLEEP)

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
    """Run the queue-mode pipeline: fresh Chrome, T0 producer, drain T1/T2.

    1. Kill + restart Chrome
    2. Run T0 producer (crawl + enqueue + index)
    3. Drain queue (worker loop for T1/T2)
    """
    overall_start = time.monotonic()

    # Step 1: Fresh Chrome restart
    logger.info("=== Queue-mode pipeline: %s ===", target_blog)
    logger.info("Step 1: Restarting Chrome for fresh state")
    chrome_status = restart_chrome()
    if chrome_status.get("status") not in ("ok",):
        logger.warning("Chrome restart status: %s — continuing anyway", chrome_status)
    logger.info("Chrome status: killed=%s remaining=%s debug_port=%s",
                chrome_status.get("killed"),
                chrome_status.get("remaining_after_kill"),
                chrome_status.get("debug_port", "")[:40])

    # Use the actual debug port Chrome bound to (may differ from args.browser
    # if the default port was occupied and a fallback was chosen)
    actual_browser_ws = chrome_status.get("debug_port") or browser_ws
    # Convert ws://.../devtools/browser/XXX → http://host:port for _extract_browser_ws
    if actual_browser_ws.startswith("ws://"):
        actual_browser_ws = "http://" + actual_browser_ws[len("ws://"):].split("/")[0]
    logger.info("Using browser endpoint: %s", actual_browser_ws)

    # Ensure cache + queue dirs exist
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Step 2: T0 producer
    logger.info("Step 2: T0 producer — crawling %s", target_blog)
    t0_result = await _run_t0_producer(
        target_blog=target_blog,
        browser_ws=actual_browser_ws,
        cache_dir=cache_dir,
        recrawl_days=recrawl_days,
        queue_path=QUEUE_PATH,
        index_path=INDEX_PATH,
    )

    # Step 3: Drain queue (T1 + T2 consumers)
    logger.info("Step 3: Drain queue — T1/T2 worker loop")
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
        "t0": {
            "status": t0_result.get("status"),
            "unique_count": t0_result.get("unique_count", 0),
            "total_count": t0_result.get("total_count", 0),
            "posts_processed": t0_result.get("posts_processed", 0),
            "dead": t0_result.get("dead"),
            "dead_reason": t0_result.get("dead_reason"),
            "usernames": t0_result.get("usernames", []),
            "enqueued": t0_result.get("enqueued", 0),
            "scanned_at": t0_result.get("scanned_at"),
        },
        "drain": drain_result,
        "elapsed_seconds": total_elapsed,
    }

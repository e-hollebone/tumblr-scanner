"""
Producer — discovers Tumblr usernames and enqueues them for indexing.

This is the discovery half of the queue+index system. It finds usernames
(often from an existing T0 cache or a seed list) and pushes them onto the
on-disk queue so workers can pick them up.

Enqueue uses work_queue.enqueue() directly (which has its own flock-based
lock). No separate system.lock sidecar is needed — work_queue owns the
queue lock. The producer's only job is to call enqueue() with the right
tier and let the worker consume the items.

Usage:
  python producer.py --target <blog> [--browser <ws_url>]
  python producer.py seed.txt                    # feed from seed file
  python producer.py --from-index                # feed all indexed names
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from work_queue import enqueue

CACHE_DIR = Path("/Users/eric/Documents/tumblr-scanner/cache")
QUEUE_PATH = CACHE_DIR / "queue.jsonl"
INDEX_PATH = CACHE_DIR / "index.json"


def _write_index(username: str, entry: dict) -> None:
    """Write or update a single entry in index.json."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    entry.setdefault("username", username)
    data[username] = entry
    tmp = INDEX_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(INDEX_PATH)


def _read_index(path: Path) -> dict:
    """Read index.json. Returns {} on any failure."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _feed_from_seed(path: Path, index_path: Path = INDEX_PATH) -> int:
    """Read a newline-separated file of usernames (one per line) and enqueue
    each one at t1 that isn't already queued or indexed. Returns count."""
    count = 0
    if not path.exists():
        return count
    index = _read_index(index_path)
    with open(path, encoding="utf-8") as f:
        for line in f:
            name = line.strip().lower()
            if name:
                enqueue(QUEUE_PATH, name, state="", tier=1)
                count += 1
    return count


def _feed_from_index(index_path: Path = INDEX_PATH) -> int:
    """Enqueue every username currently in the index at t1."""
    if not index_path.exists():
        return 0
    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    count = 0
    for username, entry in data.items():
        enqueue(QUEUE_PATH, username, state="", tier=1)
        count += 1
    return count


async def produce_target(target: str, browser_ws: str) -> int:
    """
    Run T0 discovery for `target`, enqueue the target + all discovered
    usernames into queue.jsonl, write target to index.json.

    Returns total usernames enqueued (including target).
    """
    from agent import run as agent_run

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger_name = __name__ if hasattr(__import__(__name__), "__name__") else "producer"
    import logging

    log = logging.getLogger(logger_name)
    log.info("T0 producer: crawling %s", target)

    result = await agent_run(
        browser_ws=browser_ws,
        username=target,
        tier=0,
        unique_limit=250,
        total_limit=500,
        post_limit=500,
        delay_min=2.0,
        delay_max=3.0,
        recrawl_days=7,
        source_blog=None,
        cache_dir=CACHE_DIR,
        pre_existing_ws_url=None,
    )

    usernames = result.get("usernames", [])
    status = result.get("status", "unknown")
    unique_count = result.get("unique_count", len(usernames))
    total_count = result.get("total_count", 0)
    posts_processed = result.get("posts_processed", 0)

    log.info(
        "T0 producer: %s — status=%s unique=%d total=%d posts=%d usernames_found=%d",
        target,
        status,
        unique_count,
        total_count,
        posts_processed,
        len(usernames),
    )

    # Write target to index
    _write_index(
        target,
        {
            "tier": 0,
            "status": status,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "unique": unique_count,
            "total": total_count,
            "posts": posts_processed,
            "usernames": usernames,
        },
    )

    # Enqueue target + all discovered usernames at tier 1
    enqueued = 0
    for name in usernames:
        if name != target:
            enqueue(QUEUE_PATH, name, state="", tier=1)
            enqueued += 1

    # Also enqueue the target itself at tier 1 so the worker processes it
    enqueue(QUEUE_PATH, target, state="", tier=1)
    enqueued += 1

    log.info(
        "T0 producer: enqueued %d items (target + %d discovered)",
        enqueued,
        enqueued - 1,
    )
    return enqueued


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print(
            "Usage: python producer.py --target <blog> [--browser <ws_url>]",
            file=sys.stderr,
        )
        print("       python producer.py seed.txt", file=sys.stderr)
        print("       python producer.py --from-index", file=sys.stderr)
        return 2

    if argv[0] == "--from-index":
        n = _feed_from_index()
        print(f"Fed {n} usernames from index into queue")
        return 0

    if argv[0].startswith("--") or "=" in argv[0]:
        # Parse key=value or --key value pairs
        target = None
        browser_ws = "http://localhost:9222"
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--target" and i + 1 < len(argv):
                target = argv[i + 1]
                i += 2
            elif arg == "--browser" and i + 1 < len(argv):
                browser_ws = argv[i + 1]
                i += 2
            elif arg.startswith("--target="):
                target = arg.split("=", 1)[1]
                i += 1
            elif arg.startswith("--browser="):
                browser_ws = arg.split("=", 1)[1]
                i += 1
            else:
                print(f"Unknown argument: {arg}", file=sys.stderr)
                return 2
        if not target:
            print("Error: --target is required", file=sys.stderr)
            return 2
        return asyncio.run(produce_target(target, browser_ws))

    # Treat as seed file path
    seed_path = Path(argv[0])
    n = _feed_from_seed(seed_path)
    print(f"Fed {n} usernames from {seed_path} into queue")
    return 0


if __name__ == "__main__":
    sys.exit(main())

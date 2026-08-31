"""
Minimal durable work queue — JSONL file, POSIX flock, state attribute.

Queue file: queue.jsonl — one JSON object per line.
  ""            — pending, never popped
  "in_progress" — popped, worker is busy
  "done"        — popped, result is in index, will be removed by cleanup

Lock: fcntl.flock on the queue file fd. Open → flock → modify → close
(release). No sidecar, no stale PID tracking. Kernel releases on crash.

Lock timeout: non-blocking acquire (LOCK_NB) + exponential backoff to a
deadline. On deadline: sys.exit(2) for clean restart.

Startup repair: scan "in_progress" entries → check index.json. Not in index
→ reset to pending (worker crashed). In index → drop (worker finished, didn't
clean up). "done" entries → drop.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("queue")

# Module-level threading lock — serializes all queue operations across threads
# in this process. On macOS, fcntl.flock is per-open-file-description, so a
# flock held by one thread does NOT block another thread that opens its own fd
# to the same path. The threading lock closes that gap: it guarantees mutual
# exclusion within the process, while flock still protects against other
# processes. Held together, they make queue operations atomic.
_queue_lock = threading.Lock()

DEFAULT_QUEUE = Path("/Users/eric/Documents/tumblr-scanner/cache/queue.jsonl")
LOCK_DEADLINE = 30.0  # total wait before exit(2)
LOCK_RETRY_BASE = 0.05  # initial backoff
LOCK_RETRY_MAX = 0.5  # max backoff


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------


class LockError(Exception):
    """Raised when the lock cannot be acquired within the deadline."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def acquire_lock_timeout(fd: int, timeout: float = LOCK_DEADLINE) -> None:
    """Non-blocking acquire with exponential backoff. Raises LockError on timeout."""
    deadline = time.monotonic() + timeout
    backoff = LOCK_RETRY_BASE
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        if time.monotonic() >= deadline:
            raise LockError(f"Could not acquire lock on fd {fd} within {timeout}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, LOCK_RETRY_MAX)


# ---------------------------------------------------------------------------
# Queue read/write — all under the caller's lock
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[dict[str, Any]]:
    """Read and parse all JSON lines from a queue file path."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed queue line: %s", line[:80])
    return out


def _read_lines_from_fd(fd: int) -> list[dict[str, Any]]:
    """Read and parse all JSON lines from an already-open, locked file descriptor.

    Uses raw os.read to avoid Python file-object buffering issues that cause
    position corruption when the same fd is later written to via _write_lines.
    """
    out: list[dict[str, Any]] = []
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed queue line: %s", line[:80])
    return out


def _write_lines(path: Path, lines: list[dict[str, Any]], fd: int | None = None) -> None:
    """Overwrite the queue file with the given lines (not append).

    If fd is provided, write in-place using raw os.write (no Python buffering)
    so the inode and any held lockf persist across the write. Using raw
    os.write + os.ftruncate avoids Python file-object position/buffer issues
    that corrupt lines under concurrent load.
    """
    if fd is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = b"".join((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8") for item in lines)
        os.write(fd, raw)
        os.ftruncate(fd, len(raw))
        os.fsync(fd)
        return
    tmp = path.with_suffix(".queue.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(item, ensure_ascii=False) + "\n" for item in lines)
    os.replace(tmp, path)


def _append_line(path: Path, item: dict[str, Any]) -> None:
    """Append a single line to the queue file (no read-modify-write)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def enqueue(
    queue_path: Path,
    username: str,
    state: str = "",
    tier: int = 1,
    mode: str = "full",
) -> None:
    """
    Append a pending item to the queue if the username is not already present
    (in the queue, or in the index unless the index entry is still fresh).

    mode: "full" for a full crawl, "reindex" for a date-probe + conditional crawl.
    """
    username = username.strip().lower()
    if not username:
        return

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    with _queue_lock:
        with open(queue_path, "r+", encoding="utf-8") as f:
            try:
                lines = _read_lines_from_fd(f.fileno())
                names = {
                    ln.get("username", "").lower()
                    for ln in lines
                    if ln.get("state", "") != "done"
                }
                if username in names:
                    return
                _append_line(
                    queue_path,
                    {
                        "username": username,
                        "state": state,
                        "tier": tier,
                        "mode": mode,
                    },
                )
            finally:
                pass


def dequeue(queue_path: Path) -> dict[str, Any] | None:
    """
    Pop the first pending item, mark it "in_progress", return it.
    Returns None if nothing pending.
    """
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    with _queue_lock:
        with open(queue_path, "r+", encoding="utf-8") as f:
            try:
                lines = _read_lines_from_fd(f.fileno())
                for i, item in enumerate(lines):
                    if item.get("state", "") in ("", None):
                        item["state"] = "in_progress"
                        item["claimed_at"] = _now_iso()
                        lines[i] = item
                        _write_lines(queue_path, lines, f.fileno())
                        return item
                return None
            finally:
                pass


def mark_done(queue_path: Path, username: str) -> None:
    """
    Mark an item as "done" after the worker writes to the index.
    Cleanup will remove it later.
    """
    username = username.strip().lower()
    if not queue_path.exists():
        return
    with _queue_lock:
        with open(queue_path, "r+", encoding="utf-8") as f:
            try:
                lines = _read_lines_from_fd(f.fileno())
                for item in lines:
                    if item.get("username", "").lower() == username:
                        item["state"] = "done"
                        item["completed_at"] = _now_iso()
                        _write_lines(queue_path, lines, f.fileno())
                        return
            finally:
                pass


def cleanup(queue_path: Path) -> int:
    """
    Remove "done" entries. Reset "in_progress" entries not in index to
    pending (worker crashed). Drop "in_progress" entries already in index
    (worker finished but didn't call mark_done).
    Returns count of entries removed.
    """
    index_path = queue_path.with_name("index.json")
    index: dict[str, Any] = {}
    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    with _queue_lock:
        with open(queue_path, "r+", encoding="utf-8") as f:
            try:
                lines = _read_lines_from_fd(f.fileno())
                new_lines: list[dict[str, Any]] = []
                removed = 0
                for item in lines:
                    state = item.get("state", "")
                    name = item.get("username", "").lower()
                    if state == "done":
                        removed += 1
                        continue
                    if state == "in_progress" and name not in index:
                        # Worker crashed — reset to pending
                        item["state"] = ""
                        item.pop("claimed_at", None)
                        logger.info("Reset in_progress %s to pending (not in index)", name)
                    elif state == "in_progress" and name in index:
                        # Worker finished but didn't clean up — drop
                        removed += 1
                        logger.info("Dropped in_progress %s (already in index)", name)
                        continue
                    new_lines.append(item)
                _write_lines(queue_path, new_lines, f.fileno())
                return removed
            finally:
                pass


def queue_size(queue_path: Path) -> int:
    """Return total lines in the queue file (all states)."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    try:
        with _queue_lock:
            with open(queue_path, "r+", encoding="utf-8") as f:
                try:
                    return len(_read_lines_from_fd(f.fileno()))
                finally:
                    pass
    except (FileNotFoundError, OSError):
        return 0


def active_count(queue_path: Path) -> int:
    """Count items still requiring work: pending (state "") + in_progress."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)
    try:
        with _queue_lock:
            with open(queue_path, "r+", encoding="utf-8") as f:
                try:
                    lines = _read_lines_from_fd(f.fileno())
                finally:
                    pass
    except (FileNotFoundError, OSError):
        return 0
    return sum(1 for ln in lines if ln.get("state", "") != "done")


# ---------------------------------------------------------------------------
# Lock acquisition with deadline → exit(2)
# ---------------------------------------------------------------------------


def lock_fd(path: Path) -> int:
    """
    Open the queue file, acquire the exclusive lock (with backoff + deadline).
    On failure, sys.exit(2).

    Returns the fd. Caller must close it to release the lock.
    This function acquires the lock ONCE. The caller must NOT call flock again.
    """
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        acquire_lock_timeout(fd, LOCK_DEADLINE)
    except LockError:
        os.close(fd)
        logger.error(
            "Lock deadline exceeded for %s after %ds — exiting for clean restart",
            path,
            LOCK_DEADLINE,
        )
        sys.exit(2)
    return fd


# ---------------------------------------------------------------------------
# Startup — repair + report
# ---------------------------------------------------------------------------


def startup(queue_path: Path) -> dict[str, Any]:
    """Repair the queue on startup and return status."""
    repaired = cleanup(queue_path)
    index_path = queue_path.with_name("index.json")
    q_size = queue_size(queue_path)
    idx_size = 0
    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8") as f:
                idx = json.load(f)
            idx_size = len(idx)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "queue_path": str(queue_path),
        "queue_size": q_size,
        "index_size": idx_size,
        "repaired": repaired,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    queue_path = DEFAULT_QUEUE

    if len(argv) >= 2 and argv[0] == "--queue":
        queue_path = Path(argv[1])
        argv = argv[2:]
    elif len(argv) >= 1 and argv[0] != "--queue":
        queue_path = Path(argv[0])
        argv = argv[1:]

    command = argv[0] if argv else "startup"

    if command == "enqueue":
        name = argv[1] if len(argv) > 1 else ""
        enqueue(queue_path, name)
        print(f"Enqueued: {name}")
        return 0

    if command == "dequeue":
        item = dequeue(queue_path)
        if item:
            print(json.dumps(item))
        else:
            print("Nothing pending")
        return 0

    if command == "size":
        print(queue_size(queue_path))
        return 0

    if command == "cleanup":
        n = cleanup(queue_path)
        print(f"Cleaned up {n} entries")
        return 0

    if command == "startup":
        info = startup(queue_path)
        print(json.dumps(info, indent=2))
        return 0

    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())

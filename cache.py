"""Cache layer - JSON files, no database.

Each blog/username entry is a JSON object with a scanned_at date.
Index membership (new vs already-indexed) governs dedup - there is NO
recrawl-window/age check. Newly discovered names always crawl.

Layout:
    cache/
        t0.json          # top-level target blog result
        t1/
            <username>.json   # one file per T1 blog crawled
        t2/
            <username>.json   # one file per T2 blog crawled
        log.json         # append-only run log (tier, username, status, ts)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cache root — kept inside the scanner work dir, simple and searchable
CACHE_DIR = Path("/Users/eric/Documents/tumblr-scanner/cache")


def _ensure_dir(path: Path) -> None:
    """Create directory if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    """Current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO timestamp string."""
    # Handle both with and without timezone
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Fallback: try common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)  # noqa: DTZ007 — fallback: naive parse; caller normalizes tz
            except ValueError:
                continue
        raise ValueError(f"Cannot parse timestamp: {ts}")


def load_entry(path: Path) -> dict[str, Any] | None:
    """Load a single JSON entry, or None if file doesn't exist / is invalid."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def save_entry(path: Path, entry: dict[str, Any]) -> None:
    """Atomically save an entry to a JSON file."""
    _ensure_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def append_log(log_path: Path, record: dict[str, Any]) -> None:
    """Atomically append a single record to the append-only log (JSONL)."""
    _ensure_dir(log_path.parent)
    record["ts"] = _now_iso()
    line = json.dumps(record) + "\n"
    # True atomic append via POSIX O_APPEND — safe under concurrent writers
    # because the kernel serializes appends at the fd level.
    fd = os.open(log_path, os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def load_index(index_path: Path) -> dict[str, Any]:
    """Load the index file (username -> {scanned_at, tier, status, usernames})."""
    if not index_path.exists():
        return {}
    try:
        with open(index_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_index(index_path: Path, index: dict[str, Any]) -> None:
    """Save the index file atomically."""
    _ensure_dir(index_path.parent)
    tmp = index_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")
    os.replace(tmp, index_path)


def index_register(
    index_path: Path,
    username: str,
    tier: int,
    status: str,
    usernames: list[str],
    scanned_at: str,
) -> None:
    """Register a completed blog in the index immediately upon job completion."""
    index = load_index(index_path)
    index[username] = {
        "tier": tier,
        "status": status,
        "usernames": sorted(set(usernames)),
        "scanned_at": scanned_at,
    }
    save_index(index_path, index)


def index_status(
    index_path: Path,
    username: str,
) -> str:
    """Two-way index status for a username.

    There is no recrawl-window/age check. Index membership alone governs
    dedup (per user directive - the 7-day concept was dropped).

    Returns:
        "stale" — in index, scanned before today → reindex (date probe)
        "new"   — not in index → full crawl
    """
    index = load_index(index_path)
    entry = index.get(username)
    if not entry:
        return "new"
    return "stale"


def load_log(log_path: Path) -> list[dict[str, Any]]:
    """Load the append-only log into a list of records."""
    if not log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records

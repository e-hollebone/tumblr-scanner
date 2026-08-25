"""
Cache layer — JSON files, no database.

Each blog/username entry is a JSON object with a scanned_at date.
Re-crawl eligibility: entries older than RECRAWL_DAYS (default 7)
are eligible for re-crawl. Newly discovered names always crawl.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Default re-crawl window: 7 days
RECRAWL_DAYS = 7

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


def entry_is_stale(entry: dict[str, Any], *, recrawl_days: int = RECRAWL_DAYS) -> bool:
    """
    Return True if the entry is older than recrawl_days and should be
    eligible for re-crawl.

    A missing or unparseable scanned_at means the entry is stale
    (treat as unknown age → re-crawl).
    """
    ts = entry.get("scanned_at")
    if not ts:
        return True
    try:
        scanned = _parse_iso(ts)
    except (ValueError, TypeError):
        return True
    # Normalize naive datetimes to UTC so the comparison with the
    # (always-aware) cutoff doesn't raise TypeError.
    if scanned.tzinfo is None:
        scanned = scanned.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(scanned.tzinfo or timezone.utc) - timedelta(days=recrawl_days)
    return scanned < cutoff


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


def list_stale_t1_entries(
    tier_dir: Path, *, recrawl_days: int = RECRAWL_DAYS
) -> list[tuple[str, dict[str, Any]]]:
    """
    Return list of (username, entry) for T1 entries that are stale
    (older than recrawl_days) and thus eligible for re-crawl.

    Also returns entries that have no scanned_at (unknown age).
    """
    results: list[tuple[str, dict[str, Any]]] = []
    if not tier_dir.exists():
        return results
    for path in sorted(tier_dir.iterdir()):
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        entry = load_entry(path)
        if entry is None:
            continue
        username = entry.get("username") or path.stem
        if entry_is_stale(entry, recrawl_days=recrawl_days):
            results.append((username, entry))
    return results


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

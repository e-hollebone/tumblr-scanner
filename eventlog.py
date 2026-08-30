"""Worker event log — clean, readable lifecycle history.

This is a SEPARATE log from the Python `logging` debug spew (which is full
of CDP noise). Every significant worker/agent/pipeline event is appended
here with a millisecond timestamp and a structured format so we can trace
exactly what happened, in order, without digging through CDP debug lines.

Log location: ./cache/worker_events.log (plain text, append-only).

Format per line:
  [<ISO-8601 ms timestamp>] <LEVEL> <component> | <event> | <json-detail>

Components: pipeline | coordinator | worker<N> | agent | chrome
Levels: INFO | WARN | ERROR | DEBUG
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from config import CACHE_DIR

EVENT_LOG_PATH = CACHE_DIR / "worker_events.log"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _fmt(detail: dict[str, Any] | None) -> str:
    if not detail:
        return ""
    try:
        return json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:
        return str(detail)


def log(
    component: str,
    event: str,
    detail: dict[str, Any] | None = None,
    level: str = "INFO",
) -> None:
    """Append one structured event line to the worker event log."""
    line = f"[{_now()}] {level} {component} | {event} | {_fmt(detail)}"
    try:
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # Never let event-logging crash the pipeline.
        pass


def info(component: str, event: str, **detail: Any) -> None:
    log(component, event, detail or None, "INFO")


def warn(component: str, event: str, **detail: Any) -> None:
    log(component, event, detail or None, "WARN")


def error(component: str, event: str, **detail: Any) -> None:
    log(component, event, detail or None, "ERROR")


def get_recent(n: int = 50) -> list[str]:
    """Return the last N event lines (oldest-first)."""
    try:
        with open(EVENT_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except FileNotFoundError:
        return []


def get_since(marker: str | None = None) -> list[str]:
    """Return all event lines at or after a marker line (for live tailing)."""
    try:
        with open(EVENT_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    if not marker:
        return [ln.rstrip("\n") for ln in lines]
    out = []
    started = False
    for ln in lines:
        if started:
            out.append(ln.rstrip("\n"))
        elif ln.rstrip("\n") == marker:
            started = True
    return out


def reset() -> None:
    """Truncate the event log (call at pipeline start for a fresh trace)."""
    try:
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(EVENT_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
    except Exception:
        pass

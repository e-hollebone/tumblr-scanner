"""Plain-text live crawl status server.

The coordinator pushes a snapshot here every loop via publish(). This server
spits out a plain-text page at http://localhost:8788/ — the operator refreshes
it whenever they want the current numbers. No auto-refresh, no JS, no styling.

Keeps the exact same publish()/snapshot_copy() interface the coordinator
already calls, so this is a drop-in replacement for the HTML dashboard.
"""
from __future__ import annotations

import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import CACHE_DIR

_snapshot: dict = {
    "started_at": None,
    "now": 0.0,
    "queue_pending": 0,
    "queue_in_progress": 0,
    "queue_done": 0,
    "indexed": 0,
    "blogs_done": 0,
    "errors": 0,
    "enqueued": 0,
    "workers": [],
    "last_stall": None,
    "login_wall": False,
    "drain_complete": False,
    "throughput_per_min": 0.0,
    "eta_min": None,
}
_lock = threading.Lock()
_history: list[tuple[float, int]] = []
_hlock = threading.Lock()


def publish(**fields) -> None:
    with _lock:
        for k, v in fields.items():
            _snapshot[k] = v
        _snapshot["now"] = time.time()
        if _snapshot["started_at"] is None:
            _snapshot["started_at"] = time.time()
    if "blogs_done" in fields:
        with _hlock:
            _history.append((time.time(), fields["blogs_done"]))
            cutoff = time.time() - 600
            while _history and _history[0][0] < cutoff:
                _history.pop(0)


def _compute_throughput() -> float:
    with _hlock:
        if len(_history) < 2:
            return 0.0
        t0, c0 = _history[0]
        t1, c1 = _history[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (c1 - c0) / (dt / 60.0)


def snapshot_copy() -> dict:
    with _lock:
        s = dict(_snapshot)
    s["throughput_per_min"] = round(_compute_throughput(), 2)
    tpm = s["throughput_per_min"]
    remaining = s["queue_pending"]
    if tpm > 0.01 and remaining > 0:
        s["eta_min"] = round(remaining / tpm, 1)
    else:
        s["eta_min"] = None
    return s


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _render(s: dict, last_events: list[str]) -> str:
    lines: list[str] = []
    lines.append(f"Tumblr Crawl — status at {_fmt_ts(s['now'])}")
    lines.append("─" * 40)
    lines.append(
        f"queue:  {s['queue_pending']} pending · "
        f"{s['queue_in_progress']} in progress · "
        f"{s['blogs_done']} done"
    )
    lines.append(f"index:  {s['indexed']} blogs indexed")
    tpm = s["throughput_per_min"]
    eta = s["eta_min"]
    rate_str = f"{tpm:.1f}" if tpm else "—"
    eta_str = f"{eta:.1f}h" if eta else "—"
    lines.append(f"rate:   {rate_str} blogs/min · ETA {eta_str}")
    if s["errors"]:
        lines.append(f"errors: {s['errors']}")
    lines.append("─" * 40)

    workers = s.get("workers", [])
    if workers:
        for w in workers:
            wid = w["id"]
            st = w["status"]
            cur = w.get("current") or "(waiting)"
            lag = w.get("lag_s")
            if lag is None:
                lag_s = "—"
            elif lag >= 60:
                lag_s = f"{lag / 60:.0f}m"
            else:
                lag_s = f"{lag:.0f}s"
            flag = ""
            if st == "stalled":
                flag = "  ← STALLED"
            lines.append(f"w{wid:<2} {st.upper():6} {cur:28} lag {lag_s}{flag}")
    else:
        lines.append("(workers not yet reported)")

    if s.get("login_wall"):
        lines.append("⚠ LOGIN WALL — halting")
    if s.get("drain_complete"):
        lines.append("✓ DRAIN COMPLETE")

    if last_events:
        lines.append("─" * 40)
        lines.append("last events:")
        for ev in last_events:
            lines.append(f"  {ev}")

    lines.append("─" * 40)
    lines.append("refresh this page for fresh numbers")
    return "\n".join(lines)


def _tail_events(log_path: Path, n: int = 8) -> list[str]:
    """Read the last N event lines from the worker event log and summarize."""
    try:
        if not log_path.exists():
            return []
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []
    out: list[str] = []
    for line in lines[-(n * 4):]:
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        bracket = line.find("]")
        if bracket < 0:
            continue
        ts_raw = line[1:bracket]
        # ts like 2026-08-28T19:25:34.347-04:00
        try:
            t_part = ts_raw.split("T")[1].split(".")[0] if "T" in ts_raw else ts_raw[-8:]
        except Exception:
            t_part = ts_raw[-8:]
        # rest = " INFO source | event | {json}"
        rest = line[bracket + 1:].strip()
        parts = rest.split("|", 2)
        if len(parts) < 3:
            continue
        # parts[0] = " INFO source", parts[1] = " event", parts[2] = " {json}"
        evt = parts[1].strip()
        try:
            d = json.loads(parts[2].strip())
        except Exception:
            d = {}
        username = d.get("username", "?")
        if evt == "page_fetched":
            out.append(f"{t_part} page  {username}  off{d.get('offset','?')}  {d.get('html_len',0) // 1024}KB")
        elif evt == "blog_done":
            out.append(f"{t_part} done  {username}  unique={d.get('unique','?')} total={d.get('total','?')}")
        elif evt == "blog_start":
            out.append(f"{t_part} start {username}  tier={d.get('tier','?')}")
        elif evt == "worker_stall_restart":
            out.append(f"{t_part} STALL worker {d.get('worker_id','?')} silent={d.get('silent_s','?')}s — restarted")
        elif evt == "login_wall":
            out.append(f"{t_part} LOGIN WALL {username}")
        elif evt == "drain_start":
            out.append(f"{t_part} DRAIN START workers={d.get('workers','?')} qsize={d.get('queue_size','?')}")
        elif evt == "drain_complete":
            out.append(f"{t_part} DRAIN COMPLETE processed={d.get('processed','?')} elapsed={d.get('elapsed_seconds','?')}s")
        else:
            out.append(f"{t_part} {evt} {username}")
        if len(out) >= n:
            break
    return out[-n:]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        s = snapshot_copy()
        events = _tail_events(CACHE_DIR / "worker_events.log")
        text = _render(s, events)
        # HTML wrapper with auto-refresh every 10s. <pre> keeps the plain-text look.
        body = (
            "<!doctype html><html><head><meta charset=utf-8>\n"
            '<meta http-equiv="refresh" content="10">\n'
            "<title>Crawl Status</title></head><body>\n"
            "<pre>" + text + "</pre>\n"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        return


_server: ThreadingHTTPServer | None = None


def start_status_server(port: int = 8788) -> None:
    global _server
    if _server is not None:
        return
    _server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()


def stop_status_server() -> None:
    global _server
    if _server is not None:
        _server.shutdown()
        _server = None


def status_url(port: int = 8788) -> str:
    return f"http://localhost:{port}/"

"""
Chrome lifecycle — dedicated profile, never touches the user's Chrome.

Every pipeline run starts from clean Chrome state using a SEPARATE profile
directory (./chrome_profile). We never kill or interfere with Chrome instances
running under other profiles (e.g., the user's personal Chrome).
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.request
from typing import Any

logger = logging.getLogger("chrome-lifecycle")

# Dedicated profile — separate from the user's personal Chrome.
# Uses the absolute CACHE_DIR-based profile path from config so the launch
# directory no longer matters.
from config import CHROME_USER_DATA_DIR
CHROME_PROFILE_DIR = CHROME_USER_DATA_DIR
CHROME_DEBUG_PORT = 9222
CHROME_FALLBACK_PORTS = [9223, 9224, 9225, 9226]


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _our_chrome_pids() -> list[int]:
    """Return PIDs of Chrome processes using our dedicated profile.

    Uses `ps -ax -o pid,command` to find Chrome processes whose command
    line contains our profile path. This ensures we only ever touch our
    own Chrome instance, never the user's personal Chrome.
    """
    pids: list[int] = []
    profile_str = str(CHROME_PROFILE_DIR)
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid,command"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.decode().splitlines():
            if profile_str in line and "Google Chrome" in line:
                parts = line.strip().split(None, 1)
                if parts:
                    try:
                        pids.append(int(parts[0]))
                    except ValueError:
                        pass
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return pids


def cleanup_tabs(port: int | None = None) -> int:
    """Close all open page tabs on our Chrome.

    Does NOT destroy the Tumblr login session — that lives in the Chrome
    profile directory (--user-data-dir), not in an open tab. Closing tabs
    only frees the leaked tabs that accumulate across runs. Called at init
    (in restart_chrome reuse-mode) so every launch starts clean instead of
    stacking new tabs on top of stale ones.

    Returns the number of tabs closed.
    """
    if port is None:
        port = _our_chrome_port()
    if port is None:
        return 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
            targets = json.loads(resp.read())
    except Exception:
        return 0
    closed = 0
    for t in targets:
        if t.get("type") == "page" and t.get("id"):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=5
                )
                closed += 1
            except Exception:
                pass
    if closed:
        logger.info("cleanup_tabs: closed %d stale tab(s) on port %d", closed, port)
    return closed


def kill_chrome() -> dict[str, Any]:
    """Kill only Chrome processes using our dedicated profile.

    Returns status dict. Never touches Chrome instances running under
    other profiles.
    """
    pids = _our_chrome_pids()
    killed = 0
    for pid in pids:
        try:
            subprocess.run(["kill", "-9", str(pid)], check=False, timeout=5)
            killed += 1
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Failed to kill our Chrome PID %d", pid)

    # Wait for processes to actually die
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not _our_chrome_pids():
            break
        time.sleep(0.3)

    remaining = _our_chrome_pids()
    return {
        "killed": killed,
        "remaining": len(remaining),
        "status": "clean" if not remaining else "partial",
    }


def _find_available_port() -> int:
    """Find an available debug port, preferring the default."""
    if not _is_port_in_use(CHROME_DEBUG_PORT):
        return CHROME_DEBUG_PORT
    for port in CHROME_FALLBACK_PORTS:
        if not _is_port_in_use(port):
            logger.info(
                "Default port %d in use by another process — using fallback %d",
                CHROME_DEBUG_PORT,
                port,
            )
            return port
    # All ports taken — return default and let launch fail clearly
    logger.warning(
        "All debug ports (%s) are in use",
        [CHROME_DEBUG_PORT, *CHROME_FALLBACK_PORTS],
    )
    return CHROME_DEBUG_PORT


def _probe_login_wall(port: int) -> bool:
    """Probe a fresh Chrome tab on ``port`` for a login wall.

    Navigates to tumblr.com, waits for the redirect to settle, then
    checks the final URL. Returns True if the login wall is up.
    """
    import urllib.request as _ur
    try:
        ws_url, tab_id = None, None
        with _ur.urlopen(f"http://127.0.0.1:{port}/json/new?https://www.tumblr.com/", timeout=5) as resp:
            info = json.loads(resp.read())
            tab_id = info.get("id")
            ws_url = info.get("webSocketDebuggerUrl")
        if not tab_id:
            return False
        # Wait for the redirect to settle — the login page takes a moment
        # to load. Reading immediately gives a false negative (tumblr.com
        # before redirect → reports "no wall" when there is one).
        time.sleep(3.0)
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/json/get", timeout=5) as resp:
                targets = json.loads(resp.read())
                for t in targets:
                    if t.get("id") == tab_id:
                        final_url = t.get("url", "")
                        if "login" in final_url.lower() or "signup" in final_url.lower():
                            return True
                        return False
        except Exception:
            return False
        return False
    except Exception:
        return False
    finally:
        if tab_id:
            try:
                _ur.urlopen(f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=5)
            except Exception:
                pass


def _our_chrome_port() -> int | None:
    """Return the debug port our running Chrome actually listens on, or None.

    Detects our Chrome by PID (profile match), then finds which debug port it
    bound by probing the candidate ports for a live CDP endpoint. This is the
    correct port to reuse — NOT _find_available_port(), which returns a FREE
    port (a fallback) when 9222 is occupied and would never match a live one.
    """
    if not _our_chrome_pids():
        return None
    for port in (CHROME_DEBUG_PORT, *CHROME_FALLBACK_PORTS):
        if not _is_port_in_use(port):
            continue
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=5
            ) as resp:
                json.loads(resp.read())
            return port
        except Exception:
            pass
    return None


def _probe_cdp_health(port: int) -> bool:
    """Verify the Chrome CDP server can actually accept WebSocket connections.

    A stale Chrome instance may answer HTTP /json/list and even create tabs
    via Target.createTarget, but its per-tab WebSocket server is dead —
    CDPClient.start() hangs on the ws handshake with no timeout. This check
    creates a throwaway tab, opens a WebSocket to it, and runs a trivial
    Runtime.evaluate. Returns True if the round-trip succeeds within a
    generous deadline.
    """
    import asyncio
    import json

    def _create_tab() -> str | None:
        """Create a throwaway tab via HTTP and return its wsUrl."""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/new?about:blank", timeout=5,
            ) as resp:
                info = json.loads(resp.read())
            ws_url = info.get("webSocketDebuggerUrl", "")
            return ws_url or None
        except Exception:
            return None

    def _close_tab(target_id: str) -> None:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=5,
            )
        except Exception:
            pass

    ws_url = _create_tab()
    if not ws_url:
        # Can't even create a tab — server is definitely unhealthy
        return False

    # Extract target_id for cleanup
    target_id = ws_url.rsplit("/", 1)[-1] if "/" in ws_url else ""

    async def _ws_roundtrip() -> bool:
        try:
            import websockets
            async with websockets.connect(
                ws_url, open_timeout=8, close_timeout=5,
            ) as ws:
                msg = json.dumps({
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "1+1", "returnByValue": True},
                })
                await asyncio.wait_for(ws.send(msg), timeout=5)
                resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                result = json.loads(resp_raw)
                val = result.get("result", {}).get("result", {}).get("value")
                return val == 2
        except Exception:
            return False

    healthy = asyncio.run(_ws_roundtrip())
    if target_id:
        _close_tab(target_id)
    return healthy


def restart_chrome() -> dict[str, Any]:
    """Restart Chrome with our dedicated profile. Never touches other Chrome.

    If our Chrome is already running (and listening on a debug port), REUSE it
    — close its tabs for fresh state and keep the running process (which holds
    the authenticated login session). Only if no our-Chrome is running do we
    kill leftovers and launch a fresh instance.

    After deciding, probe the Chrome to verify login state is preserved. If the
    probe hits a login wall, the status includes ``login_wall: True`` so the
    operator knows immediately instead of discovering it mid-crawl.

    Returns status dict with the actual port used.
    """
    # Reuse our already-running Chrome if present (preserves login session).
    running_port = _our_chrome_port()
    if running_port is not None:
        logger.info(
            "Our Chrome already running on port %d — reusing (session preserved)",
            running_port,
        )
        # Close ALL leaked tabs at init. The Tumblr login session lives in the
        # Chrome profile dir, NOT in an open tab — closing tabs does NOT log us
        # out. This prevents the per-run tab accumulation that OOMs Chrome.
        closed = cleanup_tabs(running_port)
        logger.info("Reuse-mode: closed %d stale tab(s) before launch", closed)

        # Validate the reused Chrome actually accepts live CDP connections.
        # A stale Chrome instance can still answer /json/list and create tabs,
        # but its per-tab WebSocket server is dead: workers hang forever on
        # CDPClient.start() with "timed out during opening handshake".
        if _probe_cdp_health(running_port):
            login_wall = _probe_login_wall(running_port)
            return {
                "killed": 0,
                "remaining_after_kill": 0,
                "restarted": True,
                "reused": True,
                "port": running_port,
                "status": "ok",
                "login_wall": login_wall,
            }

        # CDP server is unhealthy — kill and fall through to fresh launch.
        logger.warning(
            "Reuse-mode: CDP health check failed — killing stale Chrome"
            " and launching fresh (login session persists in --user-data-dir)"
        )
        kill_result = kill_chrome()
    else:
        # Chrome not running — kill any leftover processes before launching
        kill_result = kill_chrome()

    # Find an available port for a fresh launch
    port = _find_available_port()

    # Launch Chrome directly (NOT via `open -g` — on macOS `open -g -a ...
    # --args` silently drops the --args, so the debug port never opens, AND
    # it activates the app, stealing GUI focus. NFR-4 requires no focus
    # steal, so we call the binary directly via subprocess.Popen with an
    # explicit --user-data-dir. subprocess.Popen backgrounds the process
    # (parent doesn't wait) and does not activate the GUI app.)
    from config import CHROME_PATH

    try:
        subprocess.Popen(
            [
                CHROME_PATH,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-startup-window",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give Chrome a moment to start and open the debug port
        # Poll for the port with a deadline — Chrome 152 on macOS takes
        # ~3-4s to bind the debug port; a fixed 2s sleep races it.
        port_ready = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2
                ) as resp:
                    info = json.loads(resp.read())
                    if info.get("webSocketDebuggerUrl"):
                        port_ready = True
                        break
            except (urllib.error.URLError, OSError):
                pass

        if port_ready:
            login_wall = _probe_login_wall(port)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=5
                ) as resp:
                    info: dict[str, Any] = {}
                    try:
                        info = json.loads(resp.read())
                    except (json.JSONDecodeError, OSError):
                        pass
                    return {
                        "killed": kill_result["killed"],
                        "remaining_after_kill": kill_result["remaining"],
                        "restarted": True,
                        "reused": False,
                        "debug_port": info.get("webSocketDebuggerUrl", ""),
                        "port": port,
                        "status": "ok",
                        "login_wall": login_wall,
                    }
            except (urllib.error.URLError, OSError):
                pass

        return {
            "killed": kill_result["killed"],
            "remaining_after_kill": kill_result["remaining"],
            "restarted": True,
            "reused": False,
            "debug_port": "",
            "port": port,
            "status": "no_debug_port",
        }
    except (OSError, FileNotFoundError) as exc:
        return {
            "killed": kill_result["killed"],
            "remaining_after_kill": kill_result["remaining"],
            "restarted": False,
            "reused": False,
            "error": str(exc),
            "port": port,
            "status": "launch_failed",
        }
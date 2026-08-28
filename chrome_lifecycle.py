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
from pathlib import Path
from typing import Any

logger = logging.getLogger("chrome-lifecycle")

# Dedicated profile — separate from the user's personal Chrome
CHROME_PROFILE_DIR = Path("./chrome_profile").resolve()
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


def restart_chrome() -> dict[str, Any]:
    """Restart Chrome with our dedicated profile. Never touches other Chrome.

    If our Chrome is already running with our profile, reuses it (closes
    all tabs for fresh state). If the default debug port is in use by
    another process, tries fallback ports.

    Returns status dict with the actual port used.
    """
    # Check if our Chrome is already running
    existing_pids = _our_chrome_pids()

    # Find an available port
    port = _find_available_port()

    # If our Chrome is already running on the target port, reuse it
    if existing_pids and _is_port_in_use(port):
        logger.info(
            "Our Chrome (PID %s) already running on port %d — reusing",
            existing_pids,
            port,
        )
        # Close all page tabs via CDP for fresh state
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5
            ) as resp:
                tabs = json.loads(resp.read())
            for tab in tabs:
                if tab.get("type") == "page":
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/json/close/{tab['id']}",
                            timeout=5,
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        return {
            "killed": 0,
            "remaining_after_kill": 0,
            "restarted": True,
            "reused": True,
            "port": port,
            "status": "ok",
        }

    # Kill any leftover our-Chrome processes before launching
    kill_result = kill_chrome()

    # Launch Chrome directly (NOT via `open -g` — on macOS `open -g -a ...
    # --args` silently drops the --args, so the debug port never opens).
    # subprocess.Popen already backgrounds the process (parent doesn't wait),
    # and we pass the binary path + flags explicitly so the debug port binds.
    from config import CHROME_PATH

    try:
        subprocess.Popen(
            [
                CHROME_PATH,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
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

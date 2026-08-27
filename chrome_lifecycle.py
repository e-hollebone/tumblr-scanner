"""
Chrome lifecycle — kill + restart for fresh-state pipeline runs.

Every pipeline run must start from clean Chrome state; never depend on
prior tab state. This module provides kill_chrome() and restart_chrome()
for that purpose.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

logger = logging.getLogger("chrome-lifecycle")


def _chrome_pids() -> list[int]:
    """Return PIDs of running Chrome/chromium processes (user-scoped)."""
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-x", "Google Chrome"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.decode().splitlines():
            line = line.strip()
            if line:
                try:
                    pids.append(int(line))
                except ValueError:
                    pass
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    if not pids:
        try:
            out = subprocess.check_output(
                ["pgrep", "-x", "Chromium"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            for line in out.decode().splitlines():
                line = line.strip()
                if line:
                    try:
                        pids.append(int(line))
                    except ValueError:
                        pass
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            pass
    return pids


def kill_chrome() -> dict[str, Any]:
    """Kill all running Chrome processes. Returns status dict."""
    pids = _chrome_pids()
    killed = 0
    for pid in pids:
        try:
            subprocess.run(["kill", "-9", str(pid)], check=False, timeout=5)
            killed += 1
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Failed to kill Chrome PID %d", pid)

    # Wait for processes to actually die
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not _chrome_pids():
            break
        time.sleep(0.3)

    remaining = _chrome_pids()
    return {
        "killed": killed,
        "remaining": len(remaining),
        "status": "clean" if not remaining else "partial",
    }


def restart_chrome() -> dict[str, Any]:
    """Kill Chrome, wait, then restart. Returns status dict."""
    kill_result = kill_chrome()
    if kill_result["remaining"] > 0:
        logger.warning(
            "Chrome kill left %d processes — restart may not be clean",
            kill_result["remaining"],
        )

    # Brief pause to let the OS clear the debug port
    time.sleep(1.0)

    # Restart Chrome with the debug port
    import os
    user_data_dir = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    try:
        subprocess.Popen(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "--remote-debugging-port=9222",
                f"--user-data-dir={user_data_dir}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give Chrome a moment to start and open the debug port
        time.sleep(2.0)
        # Verify the debug port is listening
        import urllib.request

        try:
            with urllib.request.urlopen("http://localhost:9222/json/version", timeout=5) as resp:
                info = {}
                try:
                    import json

                    info = json.loads(resp.read())
                except (json.JSONDecodeError, OSError):
                    pass
                return {
                    "killed": kill_result["killed"],
                    "remaining_after_kill": kill_result["remaining"],
                    "restarted": True,
                    "debug_port": info.get("webSocketDebuggerUrl", ""),
                    "status": "ok",
                }
        except (urllib.error.URLError, OSError):
            return {
                "killed": kill_result["killed"],
                "remaining_after_kill": kill_result["remaining"],
                "restarted": True,
                "debug_port": "",
                "status": "no_debug_port",
            }
    except (OSError, FileNotFoundError) as exc:
        return {
            "killed": kill_result["killed"],
            "remaining_after_kill": kill_result["remaining"],
            "restarted": False,
            "error": str(exc),
            "status": "launch_failed",
        }

"""Shared pytest fixtures for tumblr-scanner tests.

Provides mock CDPClient test doubles and isolated temp-directory scratch
spaces so unit tests never touch real Chrome or real cache files.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure the project root is on sys.path so `import config` etc. work
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


class MockCDPClient:
    """Drop-in test double for cdp_use.CDPClient.

    Interface (matches the real CDPClient):
        __init__(self, url: str)
        async start() -> None
        async stop() -> None
        async send_raw(method: str, params: dict | None = None) -> dict

    ``send_raw`` responses are driven by a script list.  Each entry is
    consumed in order.  If the script is exhausted, ``default_response``
    is returned.
    """

    def __init__(
        self,
        url: str,
        *,
        script: list[dict[str, Any]] | None = None,
        default_response: dict[str, Any] | None = None,
        start_delay: float = 0.0,
        start_fails: bool = False,
        stop_fails: bool = False,
    ):
        self.url = url
        self._script: list[dict[str, Any]] = list(script) if script else []
        self._default = default_response or {"result": {"result": {"value": "{}"}}}
        self._start_delay = start_delay
        self._start_fails = start_fails
        self._stop_fails = stop_fails
        self.started = False
        self.stopped = False
        self.call_log: list[tuple[str, dict | None]] = []

    async def start(self) -> None:
        if self._start_delay:
            await asyncio.sleep(self._start_delay)
        if self._start_fails:
            raise RuntimeError("CDP start failed (mocked)")
        self.started = True

    async def stop(self) -> None:
        if self._stop_fails:
            raise RuntimeError("CDP stop failed (mocked)")
        self.stopped = True

    async def send_raw(self, method: str, params: dict | None = None) -> dict:
        self.call_log.append((method, params))
        if self._script:
            return self._script.pop(0)
        return self._default


class MockCDPManager:
    """Factory that creates MockCDPClient instances with scripted responses.

    Patching strategy: the real code does ``from cdp_use import CDPClient``
    inside function bodies, so we patch ``cdp_use.CDPClient`` at the
    source module — this is picked up by every ``from cdp_use import``
    that happens after the patch is applied.
    """

    def __init__(self):
        self._script: list[dict[str, Any]] | None = None
        self._default: dict[str, Any] | None = None
        self._start_fails = False
        self._stop_fails = False
        self._last_url: str | None = None
        self.created: list[MockCDPClient] = []

    def set_script(self, script: list[dict[str, Any]],
                   default: dict[str, Any] | None = None):
        """Set the response script (consumed once, in order, per client)."""
        self._script = script
        self._default = default

    def set_start_fails(self, fails: bool = True):
        self._start_fails = fails

    def set_stop_fails(self, fails: bool = True):
        self._stop_fails = fails

    @property
    def last_url(self) -> str | None:
        return self._last_url

    @property
    def all_clients(self) -> list[MockCDPClient]:
        return list(self.created)

    @property
    def call_log(self) -> list[tuple[str, dict | None]]:
        """Aggregate call_log across all created clients, in creation order."""
        log = []
        for c in self.created:
            log.extend(c.call_log)
        return log

    def factory(self, url: str, **kwargs):
        """CDPClient(url) replacement — creates a MockCDPClient."""
        self._last_url = url
        client = MockCDPClient(
            url,
            script=self._script,
            default_response=self._default,
            start_fails=self._start_fails,
            stop_fails=self._stop_fails,
        )
        self.created.append(client)
        return client


def _page_ready_response(url: str = "https://www.tumblr.com/",
                         text: str = "") -> dict:
    """Build a Runtime.evaluate response for the page-state snapshot."""
    payload = json.dumps({"url": url, "text": text})
    return {"result": {"result": {"value": payload}}}


def make_cdp_response(value: str, *, is_json: bool = False) -> dict:
    """Build a CDP Runtime.evaluate response with the given value."""
    if is_json:
        return {"result": {"result": {"value": value}}}
    return {"result": {"result": {"value": value}}}


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    """Provide a clean cache directory inside tmp_path."""
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


@pytest.fixture
def tmp_queue(tmp_cache: Path) -> Path:
    """Provide an empty queue.jsonl path."""
    return tmp_cache / "queue.jsonl"


@pytest.fixture
def tmp_index(tmp_cache: Path) -> Path:
    """Provide a path for index.json."""
    return tmp_cache / "index.json"


@pytest.fixture
def wall_halt() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def mock_cdp(monkeypatch):
    """Replace ``cdp_use.CDPClient`` with a MockCDPManager factory.

    Because the production code does ``from cdp_use import CDPClient``
    inside function bodies, patching the source module's attribute is
    the only way to intercept the import.
    """
    import cdp_use
    manager = MockCDPManager()
    monkeypatch.setattr(cdp_use, "CDPClient", manager.factory, raising=False)
    return manager


@pytest.fixture
def page_ready():
    """Return the _page_ready_response helper."""
    return _page_ready_response


@pytest.fixture
def sample_index_entry():
    """A representative index.json entry for a crawled blog."""
    return {
        "username": "testblog",
        "tier": 1,
        "status": "ok",
        "scanned_at": "2026-09-10T12:00:00+00:00",
        "unique": 42,
        "total": 120,
        "posts": 10,
        "usernames": ["user1", "user2"],
        "dead": False,
    }

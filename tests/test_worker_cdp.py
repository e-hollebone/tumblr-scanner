"""Unit tests for ``worker.Worker`` — CDP client management and tab lifecycle.

Tests use ``MockCDPClient`` (via the ``mock_cdp`` fixture) and patched
``agent._new_tab_url`` / ``agent.close_tab`` to isolate the Worker logic
from real Chrome.

Test-double contract for CDPClient:
    __init__(url)  — store url
    async start()  — mark started (or raise if start_fails)
    async stop()   — mark stopped (or raise if stop_fails)
    async send_raw(method, params) -> dict  — return scripted response
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import MockCDPClient

import worker as worker_mod
from worker import Worker

# ---------------------------------------------------------------------------
# _ensure_cdp_client
# ---------------------------------------------------------------------------

class TestEnsureCDPClient:
    """Unit tests for Worker._ensure_cdp_client()."""

    async def test_creates_new_client_when_none_cached(self, mock_cdp, wall_halt):
        """_ensure_cdp_client creates a fresh CDPClient each call (no caching)."""
        w = Worker(
            worker_id=0,
            browser_ws="ws://fake",
            cache_dir=Path("/tmp/test_cache"),
            index_path=Path("/tmp/test_cache/index.json"),
            wall_halt=wall_halt,
        )
        w.ws_url = "ws://fake-devtools"
        w.target_id = "TAB1"

        client1 = await w._ensure_cdp_client()

        assert client1 is not None
        assert client1.started is True
        # Production creates a fresh client every call — no caching
        assert w._cdp_client is None
        # Production does NOT send Page.enable here — that happens in
        # navigate_to(). _ensure_cdp_client just creates + starts the client.
        assert client1.call_log == []

        # Second call creates ANOTHER fresh client (no reuse)
        client2 = await w._ensure_cdp_client()
        assert client2 is not None
        assert client2 is not client1, "Each call creates a fresh client (no caching)"
        assert client2.started is True

    async def test_reuses_cached_client_when_alive(self, mock_cdp, wall_halt):
        """NOTE: _ensure_cdp_client no longer caches — each call creates fresh.

        This test documents the current behavior: production code creates a new
        CDPClient on every _ensure_cdp_client() call. No reuse, no health-check
        loop. The previous caching behavior was removed to eliminate the
        discard/recreate cycle.
        """
        w = Worker(
            worker_id=0,
            browser_ws="ws://fake",
            cache_dir=Path("/tmp/test_cache"),
            index_path=Path("/tmp/test_cache/index.json"),
            wall_halt=wall_halt,
        )
        w.ws_url = "ws://fake-devtools"
        w.target_id = "TAB1"

        # Pre-set a cached client (simulating old behavior)
        cached = MockCDPClient(
            "ws://fake-devtools",
            default_response={"result": {"type": "string", "value": "1"}},
        )
        cached.started = True
        w._cdp_client = cached

        # Production _ensure_cdp_client ignores the cached client and creates fresh
        client = await w._ensure_cdp_client()

        assert client is not cached, "Fresh client created (cached client ignored)"
        assert client.started is True
        # Production doesn't cache — _cdp_client retains its pre-set value
        assert w._cdp_client is cached
        # Health check not sent on pre-existing client (fresh client created instead)
        assert cached.call_log == []

    async def test_recreates_dead_cached_client(self, mock_cdp, wall_halt):
        """NOTE: _ensure_cdp_client no longer caches — always creates fresh.

        Even when _cdp_client is set to a dead client, production code creates
        a fresh CDPClient rather than detecting the dead client and recreating.
        The dead client detection path no longer exists in production.
        """
        w = Worker(
            worker_id=0,
            browser_ws="ws://fake",
            cache_dir=Path("/tmp/test_cache"),
            index_path=Path("/tmp/test_cache/index.json"),
            wall_halt=wall_halt,
        )
        w.ws_url = "ws://fake-devtools"
        w.target_id = "TAB1"

        # Pre-set a dead cached client (send_raw raises)
        dead = MockCDPClient(
            "ws://fake-devtools",
            default_response={"result": {"type": "string", "value": "1"}},
        )
        dead.started = True

        async def _dead_send_raw(method, params=None):
            raise RuntimeError("connection lost (mocked)")

        dead.send_raw = _dead_send_raw
        w._cdp_client = dead

        # Production _ensure_cdp_client ignores the dead cached client and creates fresh
        client = await w._ensure_cdp_client()

        assert client is not None
        assert client is not dead, "Fresh client created (dead client ignored)"
        assert client.started is True
        # Production doesn't cache — _cdp_client retains its pre-set value
        assert w._cdp_client is dead

    async def test_raises_when_no_ws_url(self, mock_cdp, wall_halt):
        """_ensure_cdp_client raises TabDeadError when ws_url is empty."""
        from agent import TabDeadError

        w = Worker(
            worker_id=0,
            browser_ws="ws://fake",
            cache_dir=Path("/tmp/test_cache"),
            index_path=Path("/tmp/test_cache/index.json"),
            wall_halt=wall_halt,
        )
        w.ws_url = None
        w.target_id = None

        with pytest.raises(TabDeadError):
            await w._ensure_cdp_client()


# ---------------------------------------------------------------------------
# run() — preset tab reuse path
# ---------------------------------------------------------------------------

class TestRunPresetTab:
    """Unit tests for Worker.run() with preset tab (worker 0 reuse)."""

    async def test_worker_0_skips_open_tab_with_preset(self, mock_cdp, wall_halt, tmp_queue):
        """When target_id+ws_url are preset, run() skips _open_tab()."""
        # Queue is empty — worker should enter empty-wait, then we set wall_halt
        tmp_queue.touch()  # create empty file

        w = Worker(
            worker_id=0,
            browser_ws="ws://fake",
            cache_dir=tmp_queue.parent,
            index_path=tmp_queue.parent / "index.json",
            wall_halt=wall_halt,
        )
        # Preset tab state (simulating preflight reuse)
        w.ws_url = "ws://preset-ws"
        w.target_id = "PRESET_TAB"

        open_tab_calls = []

        async def fake_open_tab():
            open_tab_calls.append(True)
            return w.ws_url, w.target_id

        # Replace _open_tab with a spy
        original_open = w._open_tab
        w._open_tab = fake_open_tab  # type: ignore[method-assign]

        # Also need to patch _stop_cdp_client and close_tab to avoid errors
        async def fake_stop_cdp():
            pass
        w._stop_cdp_client = fake_stop_cdp

        async def fake_close_tab(browser_ws, target_id):
            pass

        import agent
        old_close = agent.close_tab
        agent.close_tab = fake_close_tab

        try:
            # Set wall_halt after a brief delay so the worker starts
            async def _stop():
                await asyncio.sleep(0.1)
                wall_halt.set()

            task = asyncio.create_task(w.run(tmp_queue))
            await _stop()
            result = await asyncio.wait_for(task, timeout=5.0)
            assert result["processed"] == 0  # queue was empty

            # Worker should have skipped _open_tab since tab was preset
            assert len(open_tab_calls) == 0, \
                "_open_tab should NOT be called when tab is preset"
        finally:
            agent.close_tab = old_close
            w._open_tab = original_open

    async def test_worker_creates_tab_without_preset(
        self, mock_cdp, wall_halt, tmp_queue, monkeypatch
    ):
        """Without preset, run() calls _open_tab() to create a tab."""
        tmp_queue.touch()

        # Patch _new_tab_url in agent
        async def fake_new_tab_url(browser_ws, target_url):
            return "ws://new-tab-ws", "NEW_TAB"

        import agent
        monkeypatch.setattr(agent, "_new_tab_url", fake_new_tab_url)

        async def fake_close_tab(browser_ws, target_id):
            pass
        monkeypatch.setattr(agent, "close_tab", fake_close_tab)

        # Patch _hide_chrome_window to avoid osascript calls
        monkeypatch.setattr(worker_mod, "_hide_chrome_window", lambda: None)

        # Track _open_tab calls
        open_tab_called = []

        w = Worker(
            worker_id=1,
            browser_ws="ws://fake",
            cache_dir=tmp_queue.parent,
            index_path=tmp_queue.parent / "index.json",
            wall_halt=wall_halt,
        )

        # Wrap _open_tab to track that it was called
        orig_open = w._open_tab

        async def tracking_open_tab():
            open_tab_called.append(True)
            return await orig_open()

        w._open_tab = tracking_open_tab  # type: ignore[method-assign]

        # Patch _stop_cdp_client
        async def fake_stop_cdp():
            pass
        w._stop_cdp_client = fake_stop_cdp

        try:
            async def _stop():
                await asyncio.sleep(0.1)
                wall_halt.set()

            task = asyncio.create_task(w.run(tmp_queue))
            await _stop()
            result = await asyncio.wait_for(task, timeout=5.0)

            # Worker should have called _open_tab since no tab was preset
            assert len(open_tab_called) == 1, \
                "_open_tab should be called exactly once when no tab is preset"
            assert result["processed"] == 0  # queue was empty
        except asyncio.TimeoutError:
            pytest.fail("Worker.run() did not exit after wall_halt was set")

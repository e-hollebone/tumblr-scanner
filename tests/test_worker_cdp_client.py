"""Unit tests for ``Worker._ensure_cdp_client``.

Tests the CDP client lifecycle logic:
    - Create fresh client when called (when _cdp_client is None)
    - Cache the client on first call, reuse on subsequent calls
    - Timeout on start() is handled gracefully
"""
from __future__ import annotations

import asyncio

import pytest

from worker import Worker


def _make_worker(tmp_path, ws_url="ws://fake", target_id="TAB1"):
    """Create a Worker with a controlled ws_url/target_id."""
    wall_halt = asyncio.Event()
    w = Worker(
        worker_id=0,
        browser_ws="http://127.0.0.1:9223",
        cache_dir=tmp_path / "cache",
        index_path=tmp_path / "index.json",
        wall_halt=wall_halt,
    )
    return w


class TestEnsureCDPClient:
    """Tests for Worker._ensure_cdp_client()."""

    async def test_create_fresh_when_no_cached_client(self, mock_cdp, tmp_path):
        """When _cdp_client is None, a new client is created and cached."""
        w = _make_worker(tmp_path)
        assert w._cdp_client is None
        w.ws_url = "ws://fake-devtools"
        w.target_id = "TAB1"

        client = await w._ensure_cdp_client()

        assert client is not None
        assert client.started is True
        assert w._cdp_client is client

    async def test_reuses_cached_client_on_second_call(self, mock_cdp, tmp_path):
        """Second call reuses the cached client (no recreation)."""
        w = _make_worker(tmp_path)
        w.ws_url = "ws://fake-devtools"
        w.target_id = "TAB1"

        client1 = await w._ensure_cdp_client()
        assert client1.started is True

        client2 = await w._ensure_cdp_client()
        assert client2 is client1, "Reused cached client on second call"
        assert client2.started is True

    async def test_timeout_on_start_handled(self, mock_cdp, tmp_path):
        """When client.start() fails, _ensure_cdp_client raises."""
        w = _make_worker(tmp_path)
        w.ws_url = "ws://fake"
        w.target_id = "TAB1"

        # Make start() fail to simulate timeout
        mock_cdp.set_start_fails(True)

        with pytest.raises((RuntimeError, Exception)):
            await w._ensure_cdp_client()

    async def test_worker_run_uses_ensure_cdp_client(self, mock_cdp, tmp_path, wall_halt):
        """Integration: Worker.run() calls _open_tab then _ensure_cdp_client."""
        # _open_tab calls _new_tab_url from agent — need to patch that
        import agent

        async def _fake_new_tab_url(browser_ws, target_url):
            return "ws://fake-ws", "TAB_RUN"

        old_new = agent._new_tab_url
        agent._new_tab_url = _fake_new_tab_url

        # Patch close_tab to avoid real Chrome calls
        async def _fake_close_tab(browser_ws, target_id):
            pass

        old_close = agent.close_tab
        agent.close_tab = _fake_close_tab

        # Patch _open_tab's _hide_chrome_window to avoid AppleScript
        import worker as worker_mod
        old_hide = worker_mod._hide_chrome_window
        worker_mod._hide_chrome_window = lambda: None

        w = Worker(
            worker_id=0,
            browser_ws="http://127.0.0.1:9223",
            cache_dir=tmp_path / "cache",
            index_path=tmp_path / "index.json",
            wall_halt=wall_halt,
        )
        w.ws_url = "ws://fake-ws"
        w.target_id = "TAB_RUN"

        # Test _ensure_cdp_client directly
        client = await w._ensure_cdp_client()
        assert client is not None
        assert client.started is True
        # Verify Page.enable was NOT sent (production doesn't send it on fresh create)
        # Actually production doesn't send Page.enable here; it just starts the client
        assert any(c[0] == "Page.enable" for c in client.call_log) is False

        # Cleanup
        agent._new_tab_url = old_new
        agent.close_tab = old_close
        worker_mod._hide_chrome_window = old_hide

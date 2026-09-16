"""Unit tests for ``queue_integration._preflight_t0_login_check``.

These tests use ``MockCDPClient`` (via the ``mock_cdp`` fixture) to script
CDP responses without touching real Chrome.

CDPClient test-double contract:
    __init__(url)  — store url
    async start()  — mark started (or raise if start_fails)
    async stop()   — mark stopped (or raise if stop_fails)
    async send_raw(method, params) -> dict  — return scripted response

Important: the production code imports CDPClient *inside* the function body
(``from cdp_use import CDPClient``), so we patch ``cdp_use.CDPClient``
(via the ``mock_cdp`` fixture) rather than the module-level name.
Similarly, ``_new_tab_url`` and ``close_tab`` are imported from ``agent``
inside the function, so we patch ``agent._new_tab_url`` and
``agent.close_tab``.

Script ordering: the preflight sends CDP calls in this order:
    1. Page.enable  (cdp_send)
    2. Page.navigate (cdp_send)
    3. Runtime.evaluate (cdp_send) — page-state snapshot poll
"""
from __future__ import annotations

import json

import agent
import queue_integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PAGE_ENABLE_RESPONSE = {"result": {}}
_NAV_RESPONSE = {"result": {}}


def _page_ready(url: str = "https://www.tumblr.com/", text: str = "") -> dict:
    """Build a Runtime.evaluate response returning {url, text} as JSON string."""
    payload = json.dumps({"url": url, "text": text})
    return {"result": {"type": "string", "value": payload}}


def _preflight_script(homepage_text: str = "Welcome to Tumblr posts here") -> list[dict]:
    """Build a full preflight CDP script for a successful homepage load.

    Order: Page.enable, Page.navigate, Runtime.evaluate (page-state poll).
    """
    return [
        _PAGE_ENABLE_RESPONSE,
        _NAV_RESPONSE,
        _page_ready("https://www.tumblr.com/", homepage_text),
    ]


def _wall_script() -> list[dict]:
    """Build a script that returns a login-wall page on evaluate."""
    return [
        _PAGE_ENABLE_RESPONSE,
        _NAV_RESPONSE,
        _page_ready("https://www.tumblr.com/login", "log in to continue"),
    ]


def _wire_agent_patches(monkeypatch, ws_url="ws://fake", target_id="TAB1"):
    """Patch agent._new_tab_url and agent.close_tab with fakes.

    Returns (new_tab_calls, close_calls) lists that the fakes append to.
    Uses monkeypatch so patches are auto-cleaned up per test.
    """
    new_tab_calls: list[tuple[str, str]] = []
    close_calls: list[tuple[str, str]] = []

    async def _fake_new_tab_url(browser_ws, target_url):
        new_tab_calls.append((browser_ws, target_url))
        return ws_url, target_id

    async def _fake_close_tab(browser_ws, target_id_arg):
        close_calls.append((browser_ws, target_id_arg))

    monkeypatch.setattr(agent, "_new_tab_url", _fake_new_tab_url)
    monkeypatch.setattr(agent, "close_tab", _fake_close_tab)
    return new_tab_calls, close_calls


# ---------------------------------------------------------------------------
# Preflight: tab creation
# ---------------------------------------------------------------------------
class TestPreflightTabCreation:
    """Verify the preflight opens a tab to about:blank."""

    async def test_preflight_opens_about_blank_tab(self, mock_cdp, monkeypatch):
        """Preflight must call _new_tab_url with 'about:blank'."""
        mock_cdp.set_script(_preflight_script())
        new_tab_calls, _ = _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_OK")

        # Timeout must exceed the 1s tab-registration sleep (line 118)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_WAIT_TIMEOUT", 5.0)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_POLL_INTERVAL", 0.01)

        accessible, ws_url, target_id, client = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "the-smallest-kitten-cravings",
        )

        assert new_tab_calls == [("http://127.0.0.1:9223", "about:blank")]
        assert accessible is True
        assert ws_url == "ws://fb"
        assert target_id == "TAB_OK"
        assert client is not None
        assert client.started is True

    async def test_preflight_cdp_start_fails_times_out(self, mock_cdp, monkeypatch):
        """When CDP start() always fails, preflight times out and returns False."""
        mock_cdp.set_start_fails(True)
        _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB1")

        # Short timeout so this test completes quickly
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_WAIT_TIMEOUT", 0.2)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_POLL_INTERVAL", 0.01)

        accessible, ws_url, target_id, client = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "testblog",
        )

        assert accessible is False
        assert ws_url is None
        assert target_id is None
        assert client is None


# ---------------------------------------------------------------------------
# Preflight: login wall detection
# ---------------------------------------------------------------------------
class TestPreflightLoginWall:
    """Verify login wall detection logic."""

    async def test_homepage_accessible_no_wall(self, mock_cdp, monkeypatch):
        """Homepage loads without login wall → accessible=True, tab reused."""
        mock_cdp.set_script(_preflight_script())
        _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_OK")

        # Timeout must exceed the 1s tab-registration sleep (line 118)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_WAIT_TIMEOUT", 5.0)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_POLL_INTERVAL", 0.01)

        accessible, ws_url, target_id, client = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "seedblog",
        )

        assert accessible is True
        assert ws_url == "ws://fb"
        assert target_id == "TAB_OK"
        assert client is not None
        assert client.started is True
        # Tab should NOT be closed on success (preflight returns live client)
        assert client.stopped is False

    async def test_login_wall_timeout_returns_false(self, mock_cdp, monkeypatch):
        """Preflight times out while wall persists → (False, None, None, None)."""
        mock_cdp.set_script(_wall_script(), default=_PAGE_ENABLE_RESPONSE)
        _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_TIMEOUT")

        monkeypatch.setattr(queue_integration, "LOGIN_WALL_WAIT_TIMEOUT", 0.2)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_POLL_INTERVAL", 0.01)

        accessible, ws_url, target_id, client = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "testblog",
        )

        assert accessible is False
        assert ws_url is None
        assert target_id is None
        assert client is None


# ---------------------------------------------------------------------------
# Preflight: tab cleanup
# ---------------------------------------------------------------------------
class TestPreflightTabCleanup:
    """Verify tab closing logic in the finally block."""

    async def test_tab_closed_on_timeout(self, mock_cdp, monkeypatch):
        """When preflight times out, tab_should_close=True → close_tab called."""
        mock_cdp.set_script(_wall_script(), default=_PAGE_ENABLE_RESPONSE)
        _, close_calls = _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_CLOSE")

        monkeypatch.setattr(queue_integration, "LOGIN_WALL_WAIT_TIMEOUT", 0.2)
        monkeypatch.setattr(queue_integration, "LOGIN_WALL_POLL_INTERVAL", 0.01)

        result = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "testblog",
        )

        assert result[0] is False
        assert len(close_calls) == 1
        assert close_calls[0] == ("http://127.0.0.1:9223", "TAB_CLOSE")

    async def test_tab_not_closed_on_success(self, mock_cdp, monkeypatch):
        """Tab should NOT be closed when preflight succeeds (worker 0 reuses it)."""
        mock_cdp.set_script(_preflight_script())
        _, close_calls = _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_NOCLOSE")

        accessible, ws_url, target_id, client = await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "testblog",
        )

        assert accessible is True
        assert len(close_calls) == 0, "Tab should NOT be closed on success"


# ---------------------------------------------------------------------------
# Preflight: CDP navigation commands
# ---------------------------------------------------------------------------
class TestPreflightCDPNavigation:
    """Verify the correct CDP commands are sent during preflight."""

    async def test_navigates_to_homepage_not_blog(self, mock_cdp, monkeypatch):
        """Preflight navigates CDP to https://www.tumblr.com, not to the blog."""
        mock_cdp.set_script(_preflight_script())
        _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_NAV")

        await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "target-blog-xyz",
        )

        navigate_calls = [c for c in mock_cdp.call_log if c[0] == "Page.navigate"]
        assert len(navigate_calls) >= 1
        nav_url = navigate_calls[0][1].get("url", "")
        assert nav_url == "https://www.tumblr.com", \
            f"Expected homepage URL, got {nav_url}"
        assert "target-blog-xyz" not in nav_url, \
            "Preflight must NOT navigate to the blog URL"

    async def test_page_enabled_before_navigate(self, mock_cdp, monkeypatch):
        """Page.enable must be sent before Page.navigate."""
        mock_cdp.set_script(_preflight_script())
        _wire_agent_patches(monkeypatch, ws_url="ws://fb", target_id="TAB_EN")

        await queue_integration._preflight_t0_login_check(
            "http://127.0.0.1:9223",
            "testblog",
        )

        methods = [c[0] for c in mock_cdp.call_log]
        assert "Page.enable" in methods
        assert "Page.navigate" in methods
        assert methods.index("Page.enable") < methods.index("Page.navigate"), \
            "Page.enable must precede Page.navigate"

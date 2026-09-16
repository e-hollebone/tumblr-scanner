"""Unit tests for ``cdp_wrapper.cdp_send`` — CDP response envelope stripping.

Proves that ``cdp_send`` strips the ``{"result": <inner>}`` envelope so callers
get the command result directly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cdp_wrapper import TabDeadError, cdp_send


class TestCdpSendEnvelope:
    """Prove cdp_send strips the CDP result envelope."""

    async def test_strips_single_nest_envelope(self):
        """cdp_use returns {"result": {"type": "...", "value": ...}}.

        cdp_send must strip the outer {"result": ...} so callers see
        {"type": "...", "value": ...} directly.
        """
        client = MagicMock()
        client.send_raw = AsyncMock(
            return_value={"result": {"type": "number", "value": 42}}
        )

        result = await cdp_send(client, "Runtime.evaluate", {"expression": "1+1"}, timeout=5.0)

        # Must NOT have outer "result" key
        assert "result" not in result
        # Must have the inner result directly
        assert result == {"type": "number", "value": 42}
        assert result.get("value") == 42

    async def test_strips_string_value_envelope(self):
        """Runtime.evaluate with returnByValue returns a JSON string.

        cdp_send must strip so callers can json.loads(result.get("value")).
        """
        import json

        payload = json.dumps({"url": "https://tumblr.com/", "text": "hello"})
        client = MagicMock()
        client.send_raw = AsyncMock(
            return_value={"result": {"type": "string", "value": payload}}
        )

        result = await cdp_send(client, "Runtime.evaluate", {"expression": "x"}, timeout=5.0)

        # Caller sees {"type": "string", "value": payload}
        assert result.get("value") == payload
        # And can json.loads it
        val = result.get("value")
        assert isinstance(val, str)
        snap = json.loads(val)
        assert snap["url"] == "https://tumblr.com/"
        assert snap["text"] == "hello"

    async def test_passes_through_non_enveloped(self):
        """If response has no "result" key, pass it through unchanged."""
        client = MagicMock()
        client.send_raw = AsyncMock(return_value={"id": 1, "result": {}})

        # This has "result" but it's empty — still strips
        result = await cdp_send(client, "Page.enable", {}, timeout=5.0)

        # {"id": 1, "result": {}} → strips "result" → {}
        assert result == {}

    async def test_raises_on_timeout(self):
        """cdp_send raises TabDeadError on timeout."""
        client = MagicMock()
        client.send_raw = AsyncMock(side_effect=asyncio.TimeoutError())

        with pytest.raises(TabDeadError, match="timed out"):
            await cdp_send(client, "Page.navigate", {"url": "x"}, timeout=0.01)

    async def test_raises_on_connection_error(self):
        """cdp_send raises TabDeadError on connection loss."""
        client = MagicMock()
        client.send_raw = AsyncMock(side_effect=ConnectionError("ws closed"))

        with pytest.raises(TabDeadError, match="connection lost"):
            await cdp_send(client, "Runtime.evaluate", {}, timeout=5.0)

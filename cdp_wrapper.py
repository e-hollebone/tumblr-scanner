"""Timeout-wrapped CDP client — every CDP call bounded by configurable timeout."""

from __future__ import annotations

import asyncio
import logging

from cdp_use import CDPClient

logger = logging.getLogger("cdp-wrapper")


class TabDeadError(Exception):
    """Raised when CDP connection dies or times out."""


async def cdp_send(
    client: CDPClient,
    method: str,
    params: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    """Send CDP command with timeout. Raises TabDeadError on timeout/connection loss.

    cdp_use v1.4+ returns the CDP ``result`` envelope as ``{"result": <inner>}``
    where ``<inner>`` is already the command-specific result (one level of unwrap
    applied by the library).  Normalize here so callers see the inner result
    directly — e.g. ``Runtime.evaluate`` with ``returnByValue: True`` yields
    ``{"type": "...", "value": ...}` `, not ``{"result": {"result": ...}}``.
    """
    try:
        raw = await asyncio.wait_for(
            client.send_raw(method, params or {}),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise TabDeadError(f"CDP command {method} timed out after {timeout}s") from None
    except (ConnectionError, OSError) as exc:
        raise TabDeadError(f"CDP connection lost: {exc}") from exc
    # cdp_use returns {"result": <command_result>}; strip the envelope so
    # callers get the command result directly.
    if isinstance(raw, dict) and "result" in raw:
        return raw["result"]
    return raw

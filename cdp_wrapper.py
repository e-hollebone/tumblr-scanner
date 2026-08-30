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
    """Send CDP command with timeout. Raises TabDeadError on timeout/connection loss."""
    try:
        return await asyncio.wait_for(
            client.send_raw(method, params or {}),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        raise TabDeadError(f"CDP command {method} timed out after {timeout}s")
    except (ConnectionError, OSError) as exc:
        raise TabDeadError(f"CDP connection lost: {exc}") from exc
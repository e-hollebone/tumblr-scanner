"""Unit tests for dead phrase detection in ``worker.py``.

Tests the immediate dead-phrase check logic in ``Worker.navigate_to()``
and the ``DEAD_PHRASES`` matching from config.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

from worker import Worker


def _runtime_response(text: str) -> dict:
    """Build a Runtime.evaluate response with the given body text."""
    payload = json.dumps({"text": text, "url": "https://www.tumblr.com/testblog"})
    return {"result": {"type": "string", "value": payload}}


def _make_worker(mock_cdp, tmp_path, wall_halt):
    """Create a Worker with _refresh_ws_url patched to no-op."""
    w = Worker(
        worker_id=0,
        browser_ws="ws://fake",
        cache_dir=tmp_path / "cache",
        index_path=tmp_path / "cache" / "index.json",
        wall_halt=wall_halt,
    )
    w.ws_url = "ws://fake-devtools"
    w.target_id = "TAB1"
    # Patch _refresh_ws_url to avoid HTTP calls to real Chrome
    w._refresh_ws_url = AsyncMock(return_value=None)
    return w


class TestDeadPhraseDetection:
    """Verify that dead-phrase detection works in navigate_to()."""

    async def test_dead_phrase_skips_render_poll(self, mock_cdp, wall_halt, tmp_path):
        """When page text contains a dead phrase, return ('', '') immediately."""
        from config import DEAD_PHRASES

        w = _make_worker(mock_cdp, tmp_path, wall_halt)

        dead_text = "This blog has been deactivated"
        mock_cdp.set_script([
            {"result": {}},  # Page.enable (from _ensure_cdp_client)
            {"result": {}},  # Page.navigate
            _runtime_response(dead_text),  # dead-phrase eval → triggers early return
        ])

        html, final_url = await w.navigate_to("deactivated-blog")

        assert html == ""
        assert final_url == ""
        # Verify the dead phrase was detected
        assert any(p in dead_text.lower() for p in DEAD_PHRASES)

    async def test_non_dead_page_proceeds_to_render_poll(self, mock_cdp, wall_halt, tmp_path):
        """When page text has no dead phrase, proceeds to render poll."""
        w = _make_worker(mock_cdp, tmp_path, wall_halt)

        healthy_text = (
            "This is a blog with enough text content to exceed the 100 "
            "character threshold for render convergence checking purposes "
            "and then some more text to make sure we are well over the limit"
        )
        poll_payload = json.dumps({
            "url": "https://www.tumblr.com/testblog",
            "text": healthy_text,
            "shell_articles": 0,
            "post_cells": 5,
            "author_links": 3,
        })

        mock_cdp.set_script([
            {"result": {}},  # Page.enable
            {"result": {}},  # Page.navigate
            _runtime_response(healthy_text),  # dead-phrase check — no dead phrase
            # Render poll: converges on first poll (url_stable after first update,
            # text > 100 chars, posts > 0)
            {"result": {"type": "string", "value": poll_payload}},
            # HTML fetch — single-nested value path
            {"result": {"type": "string", "value": json.dumps({
                "html": "<html><body>blog content</body></html>",
                "url": "https://www.tumblr.com/testblog"
            })}},
        ])

        html, final_url = await w.navigate_to("healthy-blog")

        assert "<html>" in html
        assert "testblog" in final_url
        # Verify 5 CDP calls happened across all clients (enable + nav + dead-check + poll + html)
        # Note: production doesn't cache _cdp_client, so we count via the mock manager
        assert len(mock_cdp.call_log) == 5

    async def test_all_dead_phrases_match(self, mock_cdp, wall_halt, tmp_path):
        """Verify every phrase in DEAD_PHRASES is matched by the detection logic."""
        from config import DEAD_PHRASES

        for phrase in DEAD_PHRASES:
            w = _make_worker(mock_cdp, tmp_path, wall_halt)
            mock_cdp._script = [
                {"result": {}},  # Page.enable
                {"result": {}},  # Page.navigate
                _runtime_response(phrase),  # dead-phrase eval
            ]
            mock_cdp._default = {"result": {"type": "string", "value": "{}"}}

            html, final_url = await w.navigate_to("testblog")
            assert html == "", f"Dead phrase '{phrase}' should trigger early return"
            assert final_url == ""


class TestRenderConvergence:
    """Verify the render convergence gate logic."""

    async def test_render_converges_on_posts_cells(self, mock_cdp, wall_halt, tmp_path):
        """Render poll converges when posts > 0, URL stable, text > 100 chars."""
        w = _make_worker(mock_cdp, tmp_path, wall_halt)

        healthy_text = (
            "This is a blog with enough text content to exceed the 100 "
            "character threshold for render convergence checking purposes "
            "and then some more text to make sure we are well over the limit"
        )

        poll_payload = json.dumps({
            "url": "https://www.tumblr.com/testblog",
            "text": healthy_text,
            "shell_articles": 0,
            "post_cells": 5,
            "author_links": 3,
        })

        mock_cdp.set_script([
            {"result": {}},  # Page.enable
            {"result": {}},  # Page.navigate
            _runtime_response(healthy_text),  # dead-phrase check — healthy
            # Render poll: converges on first poll (url stable, text > 100, posts > 0)
            {"result": {"type": "string", "value": poll_payload}},
            # HTML fetch — single-nested value path
            {"result": {"type": "string", "value": json.dumps({
                "html": "<html><body>blog</body></html>",
                "url": "https://www.tumblr.com/testblog"
            })}},
        ])

        html, final_url = await w.navigate_to("testblog")

        assert "<html>" in html
        assert w._render_complete is True

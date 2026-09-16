#!/usr/bin/env python3
"""
Isolated bug tests — each one asserts a single failure mode.

Run these to verify specific bugs are fixed:
  test_dead_blog_false_positive  — "404" in SVG coords must not trigger dead detection
  test_post_selector_returns_posts — extractor must find posts (not 0)
  test_usernames_extracted       — usernames must come from a[rel="author"]
  test_dates_extracted           — dates must come from time[datetime]

Usage: .venv/bin/python -m pytest test_extraction_bugs.py -v
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from extractor import extract_from_html

COOKIE_FILE = Path(__file__).parent / "cache" / "tumblr-cookies.json"
FIRECRAWL_URL = "http://renfrew-firecrawl.ts.hollebone.ca:3002/v1/scrape"


def load_cookie_header() -> str:
    if not COOKIE_FILE.exists():
        pytest.skip(f"Cookie file missing: {COOKIE_FILE}")
    data = json.loads(COOKIE_FILE.read_text())
    names = {c["name"] for c in data}
    for required in ("sid", "tmgioct", "pfu", "logged_in"):
        if required not in names:
            pytest.skip(f"Auth cookie '{required}' not in cache")
    parts = [f"{c['name']}={c['value']}" for c in data
             if c.get("value") and "tumblr" in c.get("domain", "")]
    return "; ".join(parts)


async def fetch_html(url: str, cookie_header: str) -> str:
    payload = {"url": url, "formats": ["html"], "headers": {"Cookie": cookie_header}, "timeout": 120000}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(FIRECRAWL_URL, json=payload)
        data = resp.json()
    assert data.get("success"), f"Firecrawl failed: {data.get('error', 'unknown')}"
    return data["data"]["html"]


@pytest.fixture(scope="module")
def alive_html():
    cookie_header = load_cookie_header()
    return asyncio.run(fetch_html("https://www.tumblr.com/the-smallest-kitten-cravings", cookie_header))


def test_dead_blog_false_positive(alive_html):
    """Bug 1: '404' in SVG path data must NOT trigger dead-blog detection."""
    result = extract_from_html(alive_html)
    assert not result["dead_blog"], (
        "False positive: dead_blog=True on a real, accessible blog. "
        "Dead phrase '404' appears in SVG coordinates, not a status message."
    )


def test_post_selector_returns_posts(alive_html):
    """Bug 2a: extractor must find posts (currently returns 0)."""
    result = extract_from_html(alive_html)
    assert result["posts_rendered"] > 0, (
        f"posts_rendered=0. Post selector 'div[data-cell-id*=\"-post-\"]' "
        f"no longer matches Tumblr's DOM. Use 'article' or 'div[data-id]'."
    )


def test_usernames_extracted(alive_html):
    """Bug 2b: usernames must be extracted from a[rel="author"]."""
    result = extract_from_html(alive_html)
    assert result["unique"] > 0, (
        f"unique=0. No usernames extracted. "
        f"Author selector 'a[href][rel=\"author\"]' may not match."
    )
    # Note: blog owner may NOT appear in usernames — the page shows reblog
    # sources (who reblogged FROM this blog), not the blog owner themselves.
    # So we just assert that SOME usernames were extracted.


def test_dates_extracted(alive_html):
    """Bug 2c: dates must be extracted from time[datetime]."""
    result = extract_from_html(alive_html)
    dates = [p["date"] for p in result["per_post"] if p["date"]]
    assert len(dates) > 0, (
        "No dates extracted. time[datetime] selector may not match."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

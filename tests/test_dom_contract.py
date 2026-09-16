#!/usr/bin/env python3
"""
DOM contract test — runs FIRST, fails fast if Tumblr changes their HTML structure.

This verifies the structural assumptions that extractor.py makes:
  1. Posts are wrapped in <article> elements (not div[data-cell-id])
  2. Author links use a[rel="author"]
  3. Dates are in <time datetime="...">
  4. Dead-blog detection does NOT trigger on real content

If this test fails, extractor.py will silently return 0 results.
Fix the selectors here, then update extractor.py to match.

Usage: .venv/bin/python -m pytest test_dom_contract.py -v
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

COOKIE_FILE = Path(__file__).parent / "cache" / "tumblr-cookies.json"
FIRECRAWL_URL = "http://renfrew-firecrawl.ts.hollebone.ca:3002/v1/scrape"

# Selectors that extractor.py depends on — keep in sync with extractor.py
POST_SELECTOR = "article"
AUTHOR_SELECTOR = 'a[href][rel="author"]'
DATE_SELECTOR = "time[datetime]"
DATA_ID_SELECTOR = "div[data-id]"

# Blogs known to be alive (for positive checks) and dead (for negative checks)
ALIVE_BLOG = "the-smallest-kitten-cravings"


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
    """Fetch a known-alive blog once for all contract tests."""
    cookie_header = load_cookie_header()
    return asyncio.run(fetch_html(f"https://www.tumblr.com/{ALIVE_BLOG}", cookie_header))


def test_post_container_selector(alive_html):
    """POST_SELECTOR must find post elements."""
    soup = BeautifulSoup(alive_html, "html.parser")
    posts = soup.select(POST_SELECTOR)
    assert len(posts) > 0, (
        f"POST_SELECTOR '{POST_SELECTOR}' found 0 elements. "
        f"Tumblr may have changed their DOM. "
        f"Check the raw HTML and update POST_SELECTOR."
    )
    assert len(posts) >= 10, (
        f"Only {len(posts)} posts found — expected ~17-20. "
        f"Incomplete page render?"
    )


def test_author_link_selector(alive_html):
    """AUTHOR_SELECTOR must find author links in posts."""
    soup = BeautifulSoup(alive_html, "html.parser")
    authors = soup.select(AUTHOR_SELECTOR)
    assert len(authors) > 0, (
        f"AUTHOR_SELECTOR '{AUTHOR_SELECTOR}' found 0 elements. "
        f"Username extraction will fail."
    )
    # Each post should have at least one author
    posts = soup.select(POST_SELECTOR)
    ratio = len(authors) / len(posts) if posts else 0
    assert ratio >= 0.5, (
        f"Only {len(authors)} author links for {len(posts)} posts "
        f"(ratio {ratio:.2f}). Some posts missing authors."
    )


def test_date_selector(alive_html):
    """DATE_SELECTOR must find time elements."""
    soup = BeautifulSoup(alive_html, "html.parser")
    times = soup.select(DATE_SELECTOR)
    assert len(times) > 0, (
        f"DATE_SELECTOR '{DATE_SELECTOR}' found 0 elements. "
        f"Date extraction will fail."
    )
    # All should have valid ISO datetime
    for t in times[:5]:
        dt = str(t.get("datetime") or "")
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", dt), (
            f"Invalid datetime format: '{dt}'"
        )


def test_data_id_selector(alive_html):
    """DATA_ID_SELECTOR must find post ID containers."""
    soup = BeautifulSoup(alive_html, "html.parser")
    data_ids = soup.select(DATA_ID_SELECTOR)
    assert len(data_ids) > 0, (
        f"DATA_ID_SELECTOR '{DATA_ID_SELECTOR}' found 0 elements."
    )
    # Verify they look like post IDs (numeric strings)
    for d in data_ids[:5]:
        did = str(d.get("data-id") or "")
        assert did.isdigit(), f"data-id is not numeric: '{did}'"


def test_no_login_wall(alive_html):
    """Login wall must NOT be present."""
    lower = alive_html.lower()
    assert "log in" not in lower[:500], "Login wall detected"
    assert "sign up" not in lower[:500], "Signup wall detected"


def test_dead_blog_not_false_positive(alive_html):
    """Dead-blog detection must NOT trigger on real content."""
    # This is the bug: "404" appears in SVG path data on real pages
    assert "404" not in alive_html[:500], (
        "Dead-blog false positive: '404' in page intro. "
        "Dead phrase list is too broad — '404' appears in SVG coordinates."
    )
    # Verify no real dead-blog phrases
    lower = alive_html.lower()
    for phrase in ["this blog has been deactivated", "there's nothing here"]:
        assert phrase not in lower, f"False positive: '{phrase}' in real content"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

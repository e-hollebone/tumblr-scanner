#!/usr/bin/env python3
"""Functional unit test: single blog extraction via Firecrawl + cookies.

Pipeline: fetch HTML with cookies -> extract_from_html -> assert structure.

This exercises the REAL extractor.py end-to-end.

Usage: .venv/bin/python -m pytest test_firecrawl_extract.py -v -s
"""
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
    """Build Cookie header string from cached tumblr cookies."""
    if not COOKIE_FILE.exists():
        pytest.skip(f"Cookie file missing: {COOKIE_FILE}")
    data = json.loads(COOKIE_FILE.read_text())
    names = {c["name"] for c in data}
    for required in ("sid", "tmgioct", "pfu", "logged_in"):
        if required not in names:
            pytest.skip(f"Auth cookie '{required}' not in cache — log in and re-extract")
    parts = [f"{c['name']}={c['value']}" for c in data
             if c.get("value") and "tumblr" in c.get("domain", "")]
    return "; ".join(parts)


@pytest.fixture
def cookie_header():
    return load_cookie_header()


@pytest.mark.asyncio
async def test_fetch_html_and_extract(cookie_header):
    """Fetch real blog HTML via Firecrawl + cookies, extract usernames."""
    blog = "the-smallest-kitten-cravings"
    url = f"https://www.tumblr.com/{blog}"

    payload = {
        "url": url,
        "formats": ["html"],   # extractor needs HTML
        "headers": {"Cookie": cookie_header},
        "timeout": 120000,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(FIRECRAWL_URL, json=payload)
        data = resp.json()

    assert data.get("success"), f"Firecrawl scrape failed: {data.get('error', 'unknown')}"
    html = data["data"]["html"]
    assert html, "Empty HTML response"
    assert len(html) > 500, f"HTML too short ({len(html)} chars) — likely a login wall page"

    # Exercise the real extractor
    result = extract_from_html(html, target_blog=blog)

    print(f"\n=== EXTRACT RESULT ===")
    print(f"Blog:           {blog}")
    print(f"HTML size:       {len(html)} chars")
    print(f"Posts rendered:  {result['posts_rendered']}")
    print(f"Unique usernames:{result['unique']}")
    print(f"Dead blog:      {result['dead_blog']}")
    print(f"Per-post sample (first 5):")
    for p in result["per_post"][:5]:
        print(f"  post {p['postIdx']}: {p['usernames']} @ {p['date']}")

    # Assertions — these are the functional contract
    assert not result["dead_blog"], "Blog detected as dead/deactivated"
    assert result["posts_rendered"] > 0, "No posts extracted"
    assert result["unique"] > 0, "No usernames extracted"

    # Note: blog owner may NOT appear in usernames — the page shows reblog
    # sources (who reblogged FROM this blog), not the blog owner themselves.
    # So we just assert that SOME usernames were extracted.


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])

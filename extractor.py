"""
Tumblr username + date extractor — importable wrapper.

Uses the validated HTML extraction approach: BeautifulSoup on page
HTML, stable selectors (data-cell-id, aria-label, author links).
Extracts both usernames AND post dates (from <time> datetime or text).

Architecture: blog => post => post details
- A blog has many posts
- Each post has: blog owner, reblog source, original poster, date
- Posts render in two formats: expanded (full markup) or collapsed (text)
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup


def _parse_post_date(cell: Any) -> date | None:
    """Extract the post date from a post's <time> element or text.

    Expanded posts have: <time datetime="2026-08-20T13:09:47.000Z">
    Collapsed posts have: "Dec 12, 2016" or "Aug 2" in text.
    Returns the date portion or None.
    """
    # Try <time> element first (expanded posts)
    time_el = cell.select_one("time[datetime]")
    if time_el:
        dt_str = time_el.get("datetime", "")
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                return dt.date()
            except (ValueError, TypeError):
                pass

    # Try text date (collapsed posts)
    text = cell.get_text(strip=True)
    # Pattern: "Dec 12, 2016", "Jul 5, 2016", "Aug 2", "Nov 1, 2024"
    date_match = re.search(
        r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s*(\d{4})", text
    )
    if date_match:
        try:
            date_str = f"{date_match.group(1)} {date_match.group(2)}, {date_match.group(3)}"
            dt = datetime.strptime(date_str, "%b %d, %Y")
            return dt.date()
        except (ValueError, TypeError):
            pass

    return None


def _clean_username(raw: str) -> str | None:
    """Clean a raw username candidate — strip emoji, validate format.

    Tumblr usernames are 1-32 chars of [A-Za-z0-9-].
    Returns cleaned username or None if invalid.
    """
    if not raw:
        return None
    # Strip emoji and other non-ASCII
    cleaned = re.sub(r"[^\x00-\x7F]+", "", raw)
    # Strip trailing non-alphanumeric (just in case)
    cleaned = re.sub(r"[^A-Za-z0-9-]+$", "", cleaned)
    cleaned = re.sub(r"^[^A-Za-z0-9-]+", "", cleaned)
    # Validate
    if re.match(r"^[A-Za-z0-9-]{1,32}$", cleaned):
        return cleaned
    return None


def _extract_usernames_from_post(post: Any) -> list[str]:
    """Extract usernames from a single post element.

    Handles two formats:
    - Expanded: <article> with aria-label and author links
    - Collapsed: text content with "ownerReblogged source18hposter" pattern

    Returns list of usernames found (may include duplicates across posts).
    """
    names: list[str] = []

    # --- Expanded post: has <article> with aria-label ---
    article = post.select_one("article")
    if article:
        # Extract from aria-label (e.g., "Posted by username", "reblogged from username")
        for el in article.select("[aria-label]"):
            label = el.get("aria-label", "")
            m = re.search(
                r"(?:(?:Posted|Reblogged) by ([A-Za-z0-9-]+))"
                r"|(?:reblogged from ([A-Za-z0-9-]+))",
                label,
            )
            if m:
                n = _clean_username(m.group(1) or m.group(2))
                if n:
                    names.append(n)

        # Extract from author links: a[href="/username"] with rel="author"
        for el in article.select('a[href][rel="author"]'):
            href = el.get("href", "")
            m = re.match(r"^/([A-Za-z0-9-]+)$", href)
            if m:
                n = _clean_username(m.group(1))
                if n:
                    names.append(n)

        if names:
            return names

    # --- Collapsed post: text-only format ---
    # Pattern: "ownerReblogged source18horiginal_poster3,91216,094"
    # Or: "ownerReblogged source18horiginal_posterDec 12, 2016Follow..."
    # Or: "ownerReblogged6d sourceJan 24, 2024 sourceAug 13..." (relative time)
    text = post.get_text(strip=True)

    # Remove mature content warnings
    text = re.sub(r"Potentially mature contentHide", "", text)
    text = re.sub(r"Mature contentHide", "", text)
    # Strip emoji and non-ASCII (Tumblr sometimes appends emoji to usernames)
    text = re.sub(r"[^\x00-\x7F]+", "", text)

    # Extract blog owner (before "Reblogged")
    owner_match = re.match(r"^([A-Za-z0-9-]+)Reblogged", text)
    if owner_match:
        n = _clean_username(owner_match.group(1))
        if n:
            names.append(n)

    # Extract reblog source (after "Reblogged", before relative time or "18h")
    # Skip relative time markers: "6d", "18h", "2d", etc.
    source_match = re.search(r"Reblogged(?:\d+[dhms])?\s*([A-Za-z0-9-]+?)(?:18h|\d+[dhms]|[A-Z][a-z]{2}\s)", text)
    if source_match:
        n = _clean_username(source_match.group(1))
        if n:
            names.append(n)

    # Extract original poster (after "18h", before note count or date)
    # Pattern: "18husername3,91216,094" or "18husernameDec 12, 2016"
    poster_match = re.search(r"18h([A-Za-z0-9-]+?)(?:\d{1,3}(?:,\d{3})*|[A-Z][a-z]{2}\s)", text)
    if poster_match:
        n = _clean_username(poster_match.group(1))
        if n:
            names.append(n)

    return names


def extract_from_html(
    html: str,
    target_blog: str | None = None,
) -> dict[str, Any]:
    """
    Extract usernames from Tumblr page HTML.

    Architecture: blog => post => post details
    - Finds all posts by [data-cell-id] selector
    - Each post yields: blog owner, reblog source, original poster, date
    - Handles both expanded (full markup) and collapsed (text) post formats

    Returns dict with:
        posts_rendered     int
        per_post           list[dict(postIdx=int, usernames=list[str], date=str|None)]
        total_occurrences  int
        unique             int   (distinct usernames, including blog owner)
        usernames          list[str]  (sorted distinct names)
        occurrences        dict[str, int]  (name -> count)
        page_date_min      str | None
        page_date_max      str | None
    """
    if not html or not isinstance(html, str):
        return {
            "posts_rendered": 0,
            "per_post": [],
            "total_occurrences": 0,
            "unique": 0,
            "usernames": [],
            "occurrences": {},
            "page_date_min": None,
            "page_date_max": None,
        }

    soup = BeautifulSoup(html, "html.parser")

    # Find all post containers: div with "post" in data-cell-id
    # Criteria: tag is div AND data-cell-id contains "post"
    all_elements = soup.select("[data-cell-id]")
    posts = [
        el for el in all_elements
        if el.name == "div" and "post" in el.get("data-cell-id", "")
    ]

    results: dict[str, Any] = {
        "posts_rendered": len(posts),
        "per_post": [],
        "total_occurrences": 0,
        "unique": 0,
        "usernames": [],
        "occurrences": {},
        "page_date_min": None,
        "page_date_max": None,
    }

    all_names: list[str] = []
    post_dates: list[date | None] = []

    for idx, post in enumerate(posts):
        # Extract usernames from this post
        names = _extract_usernames_from_post(post)

        # Extract date from this post
        post_date = _parse_post_date(post)

        post_result = {
            "postIdx": idx,
            "usernames": sorted(set(names)),
            "date": post_date.isoformat() if post_date else None,
        }
        results["per_post"].append(post_result)
        all_names.extend(names)
        post_dates.append(post_date)

    # Aggregate
    cnt = Counter(all_names)
    results["total_occurrences"] = len(all_names)
    results["unique"] = len(cnt)
    results["usernames"] = sorted(cnt.keys())
    results["occurrences"] = dict(cnt)

    # Post date range for this page (for refresh cutoff)
    valid_dates = [d for d in post_dates if d is not None]
    if valid_dates:
        results["page_date_min"] = min(valid_dates).isoformat()
        results["page_date_max"] = max(valid_dates).isoformat()

    return results


def check_limit(
    unique_count: int,
    total_count: int,
    posts_count: int,
    unique_limit: int,
    total_limit: int,
    post_limit: int,
) -> bool:
    """Return True if any limit has been reached (stop condition)."""
    return (
        unique_count >= unique_limit
        or total_count >= total_limit
        or posts_count >= post_limit
    )

"""
Tumblr username extractor — importable wrapper.

Uses the validated HTML extraction approach: BeautifulSoup on page
HTML, stable selectors (data-cell-id, aria-label, author links).
This is the only accepted extraction method.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup


def extract_from_html(html: str, target_blog: str | None = None) -> dict[str, Any]:
    """
    Extract usernames from Tumblr page HTML.

    Uses stable HTML structure: data-cell-id, aria-label, author links.
    Blog owner is included in counts unless target_blog is given and
    caller explicitly wants it excluded.

    Returns dict with:
        cells_rendered     int
        per_cell           list[dict(cellIdx=int, usernames=list[str])]
        total_occurrences  int
        unique             int   (distinct usernames, including blog owner)
        usernames          list[str]  (sorted distinct names)
        occurrences        dict[str, int]  (name -> count)
    """
    if not html or not isinstance(html, str):
        return {
            "cells_rendered": 0,
            "per_cell": [],
            "total_occurrences": 0,
            "unique": 0,
            "usernames": [],
            "occurrences": {},
        }

    soup = BeautifulSoup(html, "html.parser")

    # Find all post cells by stable selector.
    # [data-cell-id] is the stable attribute Tumblr attaches to post
    # containers. Filter to cells that actually contain author information
    # (aria-label with Posted/Reblogged, or rel="author" links) to exclude
    # non-post cells (nav widgets, sidebar, etc.).
    all_cells = soup.select("[data-cell-id]")
    # Only keep cells that contain actual post-author markers — this filters
    # out nav widgets, sidebar cells, and other non-post elements that also
    # carry data-cell-id but have no author information.
    cells = [
        c
        for c in all_cells
        if c.select_one(
            'article [aria-label], [data-post] [aria-label], a[href][rel="author"]'
        )
    ]

    results: dict[str, Any] = {
        "cells_rendered": len(cells),
        "per_cell": [],
        "total_occurrences": 0,
        "unique": 0,
        "usernames": [],
        "occurrences": {},
    }

    all_names: list[str] = []

    for idx, cell in enumerate(cells):
        names: set[str] = set()

        # 1) aria-label on elements within the cell — narrow to post-author
        # elements to avoid counting non-post cells (nav widgets, sidebar, etc.)
        for el in cell.select("article [aria-label], [data-post] [aria-label]"):
            label = el.get("aria-label", "")
            m = re.search(
                r"(?:(?:Posted|Reblogged) by ([A-Za-z0-9-]+))"
                r"|(?:reblogged from ([A-Za-z0-9-]+))",
                label,
            )
            if m:
                n = m.group(1) or m.group(2)
                if n:
                    names.add(n)

        # 2) author links: a[href="/username"] with rel="author" for disambiguation
        for el in cell.select('a[href][rel="author"]'):
            href = el.get("href", "")
            m = re.match(r"^/([A-Za-z0-9-]+)$", href)
            if m:
                names.add(m.group(1))

        cell_result = {"cellIdx": idx, "usernames": sorted(names)}
        results["per_cell"].append(cell_result)
        all_names.extend(list(names))

    # Aggregate
    cnt = Counter(all_names)
    results["total_occurrences"] = len(all_names)
    results["unique"] = len(cnt)
    results["usernames"] = sorted(cnt.keys())
    results["occurrences"] = dict(cnt)

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

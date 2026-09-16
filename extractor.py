"""
Tumblr username + date extractor — importable wrapper.

Uses the validated HTML extraction approach: BeautifulSoup on page HTML,
stable selectors (data-cell-id, aria-label, author links). Extracts both
usernames AND post dates from blog pages.

Architecture: blog => post => post details
- A blog has many posts
- Each post has: blog owner, reblog source, original poster, date
- Posts render in two formats: expanded (full markup) or collapsed (text)
"""

from __future__ import annotations

import re
import signal
import threading
import json
import fcntl
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# Dead blog detection phrases — Tumblr shows these when a blog has been
# deactivated or does not exist
DEAD_PHRASES = [
    "this blog has been deactivated",
    "blog has been deactivated",
    "there's nothing here",
    "this blog doesn't exist",
    "page not found",
    # NOTE: "404" removed — it appears in SVG path coordinates on real pages.
    # If needed for actual dead blogs, use a structural check (HTTP status,
    # or ">404<" pattern) instead of substring matching.
    "sorry, this blog does not exist",
    "the blog you are looking for does not exist",
]


def _detect_dead_blog(html: str) -> bool:
    """Detect if a Tumblr blog page indicates a deactivated/blog-does-not-exist state.

    Checks for specific phrases and patterns that Tumblr shows when a blog
    has been deactivated or does not exist.

    Returns True if the blog is dead/inaccessible, False otherwise.
    """
    if not html or not isinstance(html, str):
        return False

    html_lower = html.lower()

    # Check for dead blog phrases
    for phrase in DEAD_PHRASES:
        if phrase.lower() in html_lower:
            return True

    # Check for common Tumblr deactivation patterns
    # Pattern: "This blog does not exist"
    if re.search(r"this\s+blog\s+does\s+not\s+exist", html_lower):
        return True

    # Pattern: looking for blog content but finding none
    if re.search(r"no\s+posts\s+found", html_lower):
        # Only if also has deactivation-like text
        if any(kw in html_lower for kw in ["deactivat", "exist"]):
            return True

    return False


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
    """Extract usernames from a single <article> element."""
    names: list[str] = []

    for el in post.select('a[href][rel="author"]'):
        href = el.get("href", "")
        m = re.match(r"^/([A-Za-z0-9-]+)$", href)
        if m:
            n = _clean_username(m.group(1))
            if n:
                names.append(n)

    if not names:
        for el in post.select('a[rel="author"]'):
            text = el.get_text(strip=True)
            n = _clean_username(text)
            if n:
                names.append(n)

    return names


def _extract_usernames_collapsed(post: Any) -> list[str]:
    """Extract usernames from collapsed/text-only post format."""
    names: list[str] = []
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
    source_match = re.search(
        r"Reblogged(?:\\d+[dhms])?\\s*([A-Za-z0-9-]+?)(?:18h|\\d+[dhms]|[A-Z][a-z]{2}\\s)",
        text,
    )
    if source_match:
        n = _clean_username(source_match.group(1))
        if n:
            names.append(n)

    # Extract original poster (after "18h", before note count or date)
    # Pattern: "18husername3,91216,094" or "18husernameDec 12, 2016"
    poster_match = re.search(r"18h([A-Za-z0-9-]+?)(?:\d{1,3}(?:,\d{3})*|[A-Z][a-z]{2} )", text)
    if poster_match:
        n = _clean_username(poster_match.group(1))
        if n:
            names.append(n)

    return names


def extract_from_html(
    html: str,
    target_blog: str | None = None,
) -> dict[str, Any]:
    """Extract usernames from Tumblr page HTML.

    Architecture: blog => post => post details
    - Finds all posts by detecting post containers in the HTML
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
        dead_blog          bool    (True if blog is deactivated/does not exist)
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
            "dead_blog": False,
        }

    # Check for dead blog before processing
    if _detect_dead_blog(html):
        return {
            "posts_rendered": 0,
            "per_post": [],
            "total_occurrences": 0,
            "unique": 0,
            "usernames": [],
            "occurrences": {},
            "page_date_min": None,
            "page_date_max": None,
            "dead_blog": True,
        }

    soup = BeautifulSoup(html, "html.parser")

    # Find post containers: <article> elements are the post wrappers
    posts = soup.select("article")

    results: dict[str, Any] = {
        "posts_rendered": len(posts),
        "per_post": [],
        "total_occurrences": 0,
        "unique": 0,
        "usernames": [],
        "occurrences": {},
        "page_date_min": None,
        "page_date_max": None,
        "dead_blog": False,
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


# --- Queue management ---

QUEUE_FILE = Path("/Users/eric/Documents/tumblr-scanner/cache/queue.jsonl")
QUEUE_LOCK = threading.Lock()


def _read_queue() -> list[dict]:
    """Read all entries from the queue file."""
    entries = []
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            pass
    return entries


def _rewrite_queue(entries: list[dict]) -> None:
    """Rewrite the entire queue file with the given entries."""
    with open(QUEUE_FILE, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for entry in entries:
                json.dump(entry, f)
                f.write("\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def enqueue_usernames(usernames: list[str], tier: int = 2, mode: str = "reindex") -> None:
    """Enqueue usernames for processing.

    Adds each username to the queue with the specified tier and mode.
    Uses file locking for atomicity.
    """
    for username in usernames:
        entry = {
            "username": username,
            "state": "pending",
            "tier": tier,
            "mode": mode,
            "enqueued_at": datetime.now(ZoneInfo("America/Toronto")).isoformat(),
        }
        entries = _read_queue()
        entries.append(entry)
        _rewrite_queue(entries)


def dequeue_username() -> dict | None:
    """Dequeue a single username from the queue.

    Returns the entry dict, or None if queue is empty.
    Removes the entry from the queue entirely.
    Uses flock for atomic pop operation.
    """
    with QUEUE_LOCK:
        entries = _read_queue()
        if not entries:
            return None

        # Find first pending entry
        for i, entry in enumerate(entries):
            if entry.get("state") == "pending":
                # Mark as in_progress and remove from queue
                entry["state"] = "in_progress"
                entry["dequeued_at"] = datetime.now(ZoneInfo("America/Toronto")).isoformat()

                # Remove this entry from the list
                remaining = entries[:i] + entries[i+1:]

                # Write back without the dequeued entry
                _rewrite_queue(remaining)

                return entry

        # No pending entries found
        return None


def get_queue_size() -> int:
    """Return the number of entries in the queue."""
    with QUEUE_LOCK:
        entries = _read_queue()
        return len(entries)


def get_pending_count() -> int:
    """Return the number of pending entries in the queue."""
    with QUEUE_LOCK:
        entries = _read_queue()
        return sum(1 for e in entries if e.get("state") == "pending")


# --- SIGINT handling ---

_shutdown_requested = False
_shutdown_event = threading.Event()


def _signal_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) for clean shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_event.set()


def setup_signal_handling():
    """Set up SIGINT handler for clean shutdown."""
    signal.signal(signal.SIGINT, _signal_handler)


def is_shutdown_requested() -> bool:
    """Check if shutdown has been requested via SIGINT."""
    return _shutdown_requested


def wait_for_shutdown(timeout: float = 5.0) -> bool:
    """Wait for shutdown signal or timeout.

    Returns True if shutdown was requested, False if timeout expired.
    """
    return _shutdown_event.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Module init: install SIGINT handler on import
# ---------------------------------------------------------------------------
setup_signal_handling()
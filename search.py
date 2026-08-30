#!/usr/bin/env python3
"""Companion search over the Tumblr index (cache/index.json).

Lets you find whether a given username (the "target") is in the crawl index,
and — if it appears as a discovered companion — which blog(s) surfaced it.

Two match modes:
  exact    case-insensitive equality after normalization
  partial  substring containment after normalization (+ stemming of
           -deactivatedYYYY noise suffixes)

Normalization collapses the common OCR/leet confusions between digits and
letters so a mangled query still hits the real name, e.g.:
    0 -> o/O   1 -> l/L/i   3 -> e   5 -> s   7 -> t   8 -> b   4 -> a
Both the query and every index key are normalized the same way, so a digit in
either side matches its letter form in the other.

Usage:
    python3 search.py <target> [--mode exact|partial] [--index path]
    python3 search.py kitten --mode partial
    python3 search.py the-smallest-kitten-cravings --mode exact
    python3 search.py "submisiv3-tendencies" --mode exact   # 3 -> e
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Standard OCR / leetspeak digit->letter confusions. Extend here as needed.
# A digit normalizes to its letter form; a letter stays as-is. Because BOTH
# sides are normalized, a literal digit in the query matches a letter in the
# index and vice versa.
DIGIT_TO_LETTER = {
    "0": "o",
    "1": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
}

# Tumblr "deactivated" marker noise, e.g. "-deactivated20240803",
# "-deactivated2015", "-deactivated". Stripped for stemming in partial mode.
_DEACT_RE = re.compile(r"-deactivated\d*$")


def normalize(name: str) -> str:
    """Lowercase + collapse digit->letter confusions."""
    s = name.strip().lower()
    return "".join(DIGIT_TO_LETTER.get(ch, ch) for ch in s)


def stem(name: str) -> str:
    """Strip trailing -deactivatedYYYY noise for partial matching."""
    return _DEACT_RE.sub("", name)


def load_index(index_path: Path) -> dict:
    if not index_path.exists():
        return {}
    try:
        with open(index_path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def search(target: str, mode: str, index: dict) -> list[dict]:
    """Return matches. Each match:
        target_blog  - the index key (blog that was crawled)
        tier         - crawl tier of that blog
        status       - crawl status
        unique       - unique usernames found on that blog
        posts        - posts processed
        match        - 'self' (target is the blog) or 'companion' (found in
                      the blog's discovered usernames list)
    """
    q = normalize(target)
    q_stem = normalize(stem(target))
    results: list[dict] = []

    for blog, entry in index.items():
        blog_norm = normalize(blog)
        blog_stem = normalize(stem(blog))

        # --- self match (target is the crawled blog itself) ---
        if mode == "exact":
            if q == blog_norm:
                results.append(_hit(blog, entry, "self"))
                continue
        else:  # partial
            if q_stem and (q_stem in blog_stem or blog_stem in q_stem):
                results.append(_hit(blog, entry, "self"))
                continue

        # --- companion match (target appears in this blog's discovered list) ---
        companions = entry.get("usernames") or []
        for c in companions:
            c_norm = normalize(c)
            c_stem = normalize(stem(c))
            if mode == "exact":
                if q == c_norm:
                    results.append(_hit(blog, entry, "companion", via=c))
                    break
            else:
                if q_stem and (q_stem in c_stem or c_stem in q_stem):
                    results.append(_hit(blog, entry, "companion", via=c))
                    break

    return results


def _hit(blog: str, entry: dict, match: str, via: str | None = None) -> dict:
    return {
        "target_blog": blog,
        "tier": entry.get("tier"),
        "status": entry.get("status"),
        "unique": entry.get("unique", 0),
        "posts": entry.get("posts", 0),
        "match": match,
        "via": via,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search the Tumblr index for a target username.")
    parser.add_argument("target", help="Username to find (case-insensitive; digits normalized to letters)")
    parser.add_argument(
        "--mode",
        choices=["exact", "partial"],
        default="exact",
        help="exact = normalized equality; partial = substring + stemming (default: exact)",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="Path to index.json (default: ./cache/index.json)",
    )
    args = parser.parse_args(argv)

    index_path = args.index or (Path(__file__).parent / "cache" / "index.json")
    index = load_index(index_path)
    if not index:
        print(f"index empty or missing: {index_path}", file=sys.stderr)
        return 1

    results = search(args.target, args.mode, index)

    print(f"target:  {args.target!r}  mode={args.mode}  index_size={len(index)}")
    print(f"matches: {len(results)}")
    print("-" * 60)
    for r in sorted(results, key=lambda x: (x["match"] != "self", x["target_blog"])):
        via = f" (via {r['via']})" if r["via"] else ""
        print(
            f"{r['match']:8} {r['target_blog']:42} t{r['tier']} "
            f"{str(r['status']):12} u={r['unique']:>4} p={r['posts']:>4}{via}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

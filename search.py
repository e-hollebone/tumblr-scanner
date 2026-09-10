"""Companion search over the Tumblr index (cache/index.json).

Lets you find whether a given username (the "target") is in the crawl index,
and — if it appears as a discovered companion — which blog(s) surfaced it.

Two match modes:
  exact    case-insensitive equality after normalization
  partial  substring containment after normalization (+ stemming of
           -deactivatedYYYY noise suffixes)

Normalization collapses common OCR/leet confusions between digits and
letters so a mangled query still hits the real name, e.g.:
    0 <-> o/O   1 <-> l/L/i   3 <-> e   5 <-> s   7 <-> t   8 <-> b   4 <-> a
Both the query and every index key are normalized the same way, so a digit in
either side matches its letter form in the other, and vice versa.

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

# Tumblr usernames often use leet/OCR variants. Normalize both query and
# index entries through the same bidirectional mapping so a literal digit
# matches its letter form and vice versa:
#   0 <-> o/O   1 <-> l/L/i   3 <-> e   4 <-> a   5 <-> s   7 <-> t   8 <-> b
BIDI_MAP = {
    "0": "o",
    "o": "o",
    "O": "o",
    "1": "l",
    "l": "l",
    "L": "l",
    "i": "l",
    "I": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
}


def normalize(name: str) -> str:
    """Lowercase + collapse digit/letter leet confusions to one canonical form."""
    return "".join(BIDI_MAP.get(ch, ch.lower()) for ch in name)


def build_indexes(index: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Pre-normalize index for O(1) search.

    Returns:
        blog_norm_map: {original_blog_name: normalized_blog_name}
        companion_index: {normalized_username: [original_blog_name, ...]}
    """
    blog_norm_map: dict[str, str] = {}
    companion_index: dict[str, list[str]] = {}
    for blog, entry in index.items():
        blog_n = normalize(blog)
        blog_norm_map[blog] = blog_n
        companions = entry.get("usernames") or []
        for c in companions:
            c_n = normalize(c)
            if c_n not in companion_index:
                companion_index[c_n] = []
            companion_index[c_n].append(blog)
    return blog_norm_map, companion_index


# Tumblr "deactivated" marker noise, e.g. "-deactivated20240803",
# "-deactivated2015", "-deactivated". Stripped for stemming in partial mode.
_DEACT_RE = re.compile(r"-deactivated\d*$")


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


def search(target: str, mode: str, index: dict, _indexes=None) -> list[dict]:
    """Return matches. Each match:

        target_blog  - the index key (blog that was crawled)
        tier         - crawl tier of that blog
        status       - crawl status
        unique       - unique usernames found on that blog
        posts        - posts processed
        match        - 'self' (target is the blog) or 'companion' (found in
                      the blog's discovered usernames list)

    If _indexes (blog_norm_map, companion_index) is provided from a prior
    build_indexes() call, uses O(1) lookup. Otherwise falls back to on-the-fly
    normalization (slow on large indexes; use build_indexes for speed).
    """
    results: list[dict] = []

    if _indexes is not None:
        blog_norm_map, companion_index = _indexes
        q = normalize(target)
        q_stem = normalize(stem(target))

        # --- companion matches (fast reverse index lookup) ---
        if mode == "partial" and len(q_stem) < 3:
            # Avoid trivial substring hits against single/two-char normalized
            # names created by digit<->letter mapping (e.g. 1->l, 3->e).
            pass
        else:
            for c_n, blogs in companion_index.items():
                if mode == "exact":
                    matched = (c_n == q)
                else:
                    matched = bool(q_stem) and (q_stem in c_n)
                if matched:
                    for blog in blogs:
                        entry = index.get(blog, {})
                        results.append(_hit(blog, entry, "companion"))
                    break  # only process this companion name once

        # --- self matches (normalized blog name lookup) ---
        if mode == "exact":
            for blog, blog_n in blog_norm_map.items():
                if blog_n == q:
                    results.append(_hit(blog, index[blog], "self"))
        else:
            if len(q_stem) >= 3:
                for blog, blog_n in blog_norm_map.items():
                    blog_s = normalize(stem(blog))
                    if q_stem and q_stem in blog_s:
                        results.append(_hit(blog, index[blog], "self"))
        return results

    # --- slow path: normalize on every comparison (kept for compatibility) ---
    q = normalize(target)
    q_stem = normalize(stem(target))

    for blog, entry in index.items():
        blog_norm = normalize(blog)
        blog_stem = normalize(stem(blog))

        if mode == "exact":
            if q == blog_norm:
                results.append(_hit(blog, entry, "self"))
                continue
        else:
            if q_stem and q_stem in blog_stem:
                results.append(_hit(blog, entry, "self"))
                continue

        companions = entry.get("usernames") or []
        for c in companions:
            c_norm = normalize(c)
            c_stem = normalize(stem(c))
            if mode == "exact":
                if q == c_norm:
                    results.append(_hit(blog, entry, "companion", via=c))
                    break
            else:
                if q_stem and q_stem in c_stem:
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

    # Pre-build indexes for O(1) search
    blog_norm_map, companion_index = build_indexes(index)

    import time
    t0 = time.monotonic()
    results = search(args.target, args.mode, index, _indexes=(blog_norm_map, companion_index))
    dt = time.monotonic() - t0

    print(f"target:  {args.target!r}  mode={args.mode}  index_size={len(index)}")
    print(f"matches: {len(results)}  (searched in {dt:.3f}s)")
    print("-" * 60)
    for r in sorted(results, key=lambda x: (x["match"] != "self", x["target_blog"])):
        via = f" (via {r['via']})" if r["via"] else ""
        print(
            f"{r['match']:8} {r['target_blog']:42} t{r['tier']} "
            f"{r['status']!s:12} u={r['unique']:>4} p={r['posts']:>4}{via}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

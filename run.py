#!/usr/bin/env python3
"""
Tumblr multi-tier username crawler — CLI entry point.

Delegates to coordinator.run_full_pipeline() or sub-commands.

Usage:
    python run.py <target_blog>              # full T0 → T1 → T2 pipeline
    python run.py <target_blog> --t0-only   # T0 only
    python run.py <target_blog> --t1-only   # T0 + T1 only
    python run.py <target_blog> --verbose   # DEBUG logging
"""

from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

# Local imports — submodules we just wrote
from coordinator import (
    CACHE_DIR,
    DEFAULT_CDP_BROWSER,
    DEFAULT_RECRAWL_DAYS,
    LIMITS,
    MAX_CONCURRENT_AGENTS,
    get_t1_list_from_t0,
    run_full_pipeline,
    run_t0,
    run_t1_batch,
    run_t2_batch,
    setup_logging,
)
from extractor import extract_from_html

# Public API surface
__all__ = [
    "CACHE_DIR",
    "DEFAULT_CDP_BROWSER",
    "DEFAULT_RECRAWL_DAYS",
    "LIMITS",
    "MAX_CONCURRENT_AGENTS",
    "extract_from_html",
    "run_full_pipeline",
    "run_t0",
    "run_t1_batch",
    "run_t2_batch",
    "setup_logging",
]


def main(argv: list[str] | None = None) -> int:
    """CLI main — returns exit code."""
    parser = ArgumentParser(
        description="Tumblr multi-tier username crawler — CLI entry point.",
        prog="run.py",
    )
    parser.add_argument(
        "target_blog",
        nargs="?",
        help="Top-level target blog (tumblr.com/<username>)",
    )
    parser.add_argument(
        "--browser",
        default=DEFAULT_CDP_BROWSER,
        help=f"CDP browser HTTP endpoint (default: {DEFAULT_CDP_BROWSER})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=CACHE_DIR,
        help=f"Cache root directory (default: {CACHE_DIR})",
    )
    parser.add_argument(
        "--recrawl-days",
        type=int,
        default=DEFAULT_RECRAWL_DAYS,
        help=f"Re-crawl window in days (default: {DEFAULT_RECRAWL_DAYS})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    parser.add_argument(
        "--t0-only",
        action="store_true",
        help="Run T0 only, then stop",
    )
    parser.add_argument(
        "--t1-only",
        action="store_true",
        help="Run T0 + T1 only, then stop",
    )
    parser.add_argument(
        "--t2-only",
        action="store_true",
        help="Run T2 only (requires prior T0+T1 run producing a T2 list)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without executing any CDP calls",
    )

    args = parser.parse_args(argv)

    if not args.target_blog:
        parser.print_help()
        print("\nError: target_blog is required", file=sys.stderr)
        return 1

    setup_logging(args.verbose)
    print(f"Verbose:  {args.verbose}")  # DEBUG

    if args.dry_run:
        print("=== DRY RUN ===")
        print(f"Target:       {args.target_blog}")
        print(f"Browser:      {args.browser}")
        print(f"Cache:        {args.cache_dir}")
        print(f"Recrawl:      {args.recrawl_days}d")
        print(f"Concurrent:   {MAX_CONCURRENT_AGENTS}")
        print()
        print("Limits:")
        for tier, lim in LIMITS.items():
            print(
                f"  {tier}: unique={lim['unique']} total={lim['total']} posts={lim['posts']}"
            )
        return 0

    async def _run() -> dict[str, Any]:
        if args.t0_only:
            result = await run_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            return {"tier": "t0", "result": result, "target": args.target_blog}
        elif args.t1_only:
            t0_result = await run_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            t1_list = get_t1_list_from_t0(t0_result)
            t1_results = await run_t1_batch(
                browser_ws=args.browser,
                t1_usernames=t1_list,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            return {
                "tier": "t1",
                "t0": t0_result,
                "t1_results": t1_results,
                "t1_list_size": len(t1_list),
                "target": args.target_blog,
            }
        elif args.t2_only:
            # T2-only: build T2 list from existing T1 cache entries
            from cache import load_entry

            t1_dir = args.cache_dir / "t1"
            t2_dir = args.cache_dir / "t2"

            # Gather T1 usernames from fresh cache entries
            t1_candidates: set[str] = set()
            if t1_dir.exists():
                for path in t1_dir.iterdir():
                    if path.suffix != ".json" or path.name.startswith("."):
                        continue
                    entry = load_entry(path)
                    if entry and not entry.get("dead", False):
                        t1_candidates.add(entry.get("username", path.stem))

            # Build T2 list (same logic as build_t2_list_from_t1 but in CLI)
            fresh_t2: set[str] = set()
            if t2_dir.exists():
                for path in t2_dir.iterdir():
                    if path.suffix != ".json" or path.name.startswith("."):
                        continue
                    entry = load_entry(path)
                    if entry and not entry.get("dead", False):
                        fresh_t2.add(entry.get("username", path.stem))

            t2_list = sorted(t1_candidates - fresh_t2)

            if not t2_list:
                print("No T2 candidates found in cache.", file=sys.stderr)
                return {"tier": "t2", "t2_list": [], "status": "empty"}

            t2_results = await run_t2_batch(
                browser_ws=args.browser,
                t2_usernames=t2_list,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            return {
                "tier": "t2",
                "t2_list": t2_list,
                "t2_results": t2_results,
                "target": args.target_blog,
            }
        else:
            return await run_full_pipeline(
                target_blog=args.target_blog,
                browser_ws=args.browser,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
                verbose=args.verbose,
            )

    result = asyncio.run(_run())
    print_result(result)
    return 0


def print_result(result: dict[str, Any]) -> None:
    """Pretty-print the pipeline result."""
    tier = result.get("tier", "full")

    print()
    print("=" * 60)

    if tier == "t0":
        r = result["result"]
        print("Tier:  T0")
        print(f"Target: {result['target']}")
        print(f"Status: {r.get('status')}")
        print(f"Unique: {r.get('unique_count', 0)}")
        print(f"Total:  {r.get('total_count', 0)}")
        print(f"Posts:  {r.get('posts_processed', 0)}")
        print(f"Dead:   {r.get('dead')}")
        if r.get("dead_reason"):
            print(f"Dead reason: {r['dead_reason']}")
        print(f"Usernames: {len(r.get('usernames', []))}")
        if r.get("usernames"):
            print(f"First 20: {r['usernames'][:20]}")
        print(f"Cache: {r.get('scanned_at', 'N/A')}")

    elif tier == "t1":
        t0 = result["t0"]
        t1_results = result["t1_results"]
        print("Tier:  T0 + T1")
        print(f"Target: {result['target']}")
        print()
        print("--- T0 ---")
        print(f"Status:    {t0.get('status')}")
        print(f"Unique:    {t0.get('unique_count', 0)}")
        print(f"Total:     {t0.get('total_count', 0)}")
        print(f"Posts:     {t0.get('posts_processed', 0)}")
        print(f"Usernames: {len(t0.get('usernames', []))}")
        print()
        print("--- T1 ---")
        print(f"T1 list size: {result['t1_list_size']}")
        print(f"Dispatched:   {len(t1_results)}")
        success = [r for r in t1_results if r.get("status") not in ("error",)]
        dead = [r for r in t1_results if r.get("dead")]
        error = [r for r in t1_results if r.get("status") == "error"]
        cached = [r for r in t1_results if r.get("status") == "cached"]
        print(f"Success:  {len(success)}")
        print(f"Cached:   {len(cached)}")
        print(f"Dead:     {len(dead)}")
        print(f"Error:    {len(error)}")
        if success:
            total_unique = sum(r.get("unique_count", 0) for r in success)
            total_occurrences = sum(r.get("total_count", 0) for r in success)
            print(f"Total unique across T1:     {total_unique}")
            print(f"Total occurrences across T1: {total_occurrences}")

    elif tier == "t2":
        t2_list = result.get("t2_list", [])
        t2_results = result.get("t2_results", [])
        print("Tier:  T2")
        print(f"Target: {result['target']}")
        print(f"T2 candidates: {len(t2_list)}")
        print(f"Dispatched:    {len(t2_results)}")
        success = [r for r in t2_results if r.get("status") not in ("error",)]
        dead = [r for r in t2_results if r.get("dead")]
        error = [r for r in t2_results if r.get("status") == "error"]
        print(f"Success:  {len(success)}")
        print(f"Dead:     {len(dead)}")
        print(f"Error:    {len(error)}")
        if success:
            total_unique = sum(r.get("unique_count", 0) for r in success)
            print(f"Total unique across T2: {total_unique}")

    else:  # full pipeline
        print(f"Target:      {result['target_blog']}")
        print(f"Browser:     {result['browser']}")
        print(f"Cache:       {result['cache_dir']}")
        print(f"Recrawl:     {result['recrawl_days']}d")
        print(f"Concurrent:  {result['concurrent_cap']}")
        print()

        t0 = result["t0"]
        print("--- T0 ---")
        print(f"Status:     {t0.get('status')}")
        print(f"Unique:     {t0.get('unique_count', 0)}")
        print(f"Total:      {t0.get('total_count', 0)}")
        print(f"Posts:      {t0.get('posts_processed', 0)}")
        print(f"Dead:       {t0.get('dead')}")
        if t0.get("dead_reason"):
            print(f"Dead reason: {t0['dead_reason']}")
        print(f"Usernames:  {len(t0.get('usernames', []))}")
        if t0.get("usernames"):
            print(f"First 20:   {t0['usernames'][:20]}")
        print()

        t1 = result["t1"]
        s = t1["summary"]
        print("--- T1 ---")
        print(f"T1 list:    {len(t1['usernames'])}")
        print(f"Dispatched: {s['total']}")
        print(f"Success:    {s['success']}")
        print(f"Cached:     {s.get('cached', 0)}")
        print(f"Dead:       {s['dead']}")
        print(f"Error:      {s['error']}")
        print(f"Time:       {s['elapsed_seconds']:.1f}s")
        print()

        t2 = result["t2"]
        s = t2["summary"]
        print("--- T2 ---")
        print(f"Candidates: {len(t2['usernames'])}")
        print(f"Dispatched: {s['total']}")
        print(f"Success:    {s['success']}")
        print(f"Dead:       {s['dead']}")
        print(f"Error:      {s['error']}")
        print(f"Time:       {s['elapsed_seconds']:.1f}s")
        print()
        print(f"Total time: {result['elapsed_seconds']:.1f}s")

    print("=" * 60)
    print()


def run_target_blog(target_blog: str, **kwargs: Any) -> dict[str, Any]:
    """Programmatic entry point — run full pipeline for a target blog."""
    return asyncio.run(
        run_full_pipeline(
            target_blog=target_blog,
            **kwargs,
        )
    )


if __name__ == "__main__":
    sys.exit(main())

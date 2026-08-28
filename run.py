#!/usr/bin/env python3
"""
Tumblr multi-tier username crawler — CLI entry point.

Delegates to queue_integration.queue_mode() (the only production path).

Usage:
    python run.py <target_blog>                  # queue-mode: fresh Chrome, T0 producer, drain T1/T2
    python run.py <target_blog> --cache-dir DIR  # custom cache root (default: ./cache)
    python run.py <target_blog> --t0-only        # T0 only
    python run.py <target_blog> --t1-only        # T0 + T1 only
    python run.py <target_blog> --t2-only        # T2 only (requires prior T0+T1 run)
    python run.py <target_blog> --verbose        # DEBUG logging
    python run.py <target_blog> --dry-run         # print plan, no CDP calls
    python run.py <target_blog> --parallel       # parallel pipeline
"""

from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

# Local imports — submodules we just wrote
from agent import LoginWallDetected
from cache import CACHE_DIR
from config import DEFAULT_CDP_BROWSER, MAX_CONCURRENT_AGENTS
from queue_integration import queue_mode


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
        "--queue",
        action="store_true",
        help="Queue-mode pipeline: fresh Chrome, T0 producer, drain T1/T2 via worker loop",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Parallel pipeline: fresh Chrome, date-aware indexing, T1/T2 dispatch on-the-fly",
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

    import logging
    from logging import DEBUG, INFO, basicConfig
    from logging.handlers import RotatingFileHandler

    log_path = Path.home() / ".hermes" / "logs" / "tumblr-scanner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    basicConfig(
        level=DEBUG if args.verbose else INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3),
            logging.StreamHandler(),
        ],
    )

    # Signal handling — graceful shutdown on Ctrl-C / SIGTERM.
    import signal as _signal

    _cancel_requested = {"flag": False}

    def _request_shutdown(signum, frame):
        _cancel_requested["flag"] = True
        print(
            "\n\n⚠️  Shutdown requested (SIGINT/SIGTERM). "
            "Finishing current blog, then exiting cleanly...",
            file=sys.stderr,
        )

    try:
        _signal.signal(_signal.SIGINT, _request_shutdown)
        _signal.signal(_signal.SIGTERM, _request_shutdown)
    except ValueError:
        pass

    if args.dry_run:
        print("=== DRY RUN ===")
        print(f"Target:       {args.target_blog}")
        print(f"Browser:      {args.browser}")
        print(f"Cache:        {args.cache_dir}")
        print(f"Concurrent:   {MAX_CONCURRENT_AGENTS}")
        return 0

    async def _run() -> dict[str, Any]:
        # Reset the clean event log for a fresh trace of this run.
        from eventlog import reset as ev_reset
        ev_reset()
        # The only production path is queue-mode: fresh Chrome, seed-on-queue,
        # worker pool drains T1/T2 in parallel from first extraction.
        return await queue_mode(
            target_blog=args.target_blog,
            browser_ws=args.browser,
            cache_dir=args.cache_dir,
            verbose=args.verbose,
        )

    try:
        result = asyncio.run(_run())
    except LoginWallDetected:
        print("\n🛑  LOGIN WALL DETECTED — agent halted.")
        print("   Open the Chrome window and log in to Tumblr.")
        print(f"   Then re-run: python3 run.py {args.target_blog} --queue")
        print("   (Chrome is still open — your login state is preserved.)")
        return 2
    print_result(result)
    return 0


def print_result(result: dict[str, Any]) -> None:
    """Pretty-print the pipeline result."""
    tier = result.get("tier", "full")

    print()
    print("=" * 60)

    if tier == 0:
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

    elif tier == 1:
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

    elif tier == 2:
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

    elif tier == "queue":
        print("Tier:  Queue-mode (T0 producer + T1/T2 worker drain)")
        print(f"Target: {result.get('target_blog')}")
        print(f"Chrome: {result.get('chrome_status', 'N/A')}")
        print(f"Queue:  {result.get('queue_path', 'N/A')}")
        print(f"Index:  {result.get('index_path', 'N/A')}")
        print()
        t0 = result.get("t0")
        if t0:
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
            print(f"Enqueued:   {t0.get('enqueued', 0)}")
        drain = result.get("drain", {})
        if drain:
            print()
            print("--- Drain ---")
            print(f"Processed:  {drain.get('processed', 0)}")
            print(f"Errors:     {drain.get('errors', 0)}")
            print(f"Elapsed:    {drain.get('elapsed_seconds', 0):.1f}s")
            print(f"Queue final:{drain.get('queue_final', -1)}")
        print(f"Total time: {result.get('elapsed_seconds', 0):.1f}s")

    else:  # full pipeline
        print(f"Target:      {result['target_blog']}")
        print(f"Browser:     {result.get('browser', 'N/A')}")
        print(f"Cache:       {result.get('cache_dir', 'N/A')}")
        print(f"Concurrent:  {result.get('concurrent_cap', 'N/A')}")
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


if __name__ == "__main__":
    sys.exit(main())

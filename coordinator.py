"""
Coordinator — T0 + T1 + T2 orchestration.

Manages:
  1. T0: crawl the target blog, build the T1 username list
  2. T1: dispatch up to 3 concurrent agents against the T1 list
  3. Aggregate T1 results → dedup → build T2 list
  4. T2: dispatch up to 3 concurrent agents against the T2 list

All HTML extraction goes through extractor.extract_from_html (the canonical
method). All caching goes through cache.py. All crawling goes through agent.run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from agent import run as agent_run
from cache import (
    CACHE_DIR,
    list_stale_t1_entries,
    load_entry,
    load_log,
    save_entry,
)
from extractor import extract_from_html

logger = logging.getLogger("tumblr-coordinator")

# Default CDP browser endpoint (same as throughout this project)
DEFAULT_CDP_BROWSER = "http://localhost:9222"

# Limit presets
LIMITS = {
    "t0": {"unique": 250, "total": 500, "posts": 500},
    "t1": {"unique": 100, "total": 250, "posts": 250},
    "t2": {"unique": 75, "total": 125, "posts": 125},
}

# Maximum concurrent agents (hard cap)
MAX_CONCURRENT_AGENTS = 3

# Re-crawl window (days)
DEFAULT_RECRAWL_DAYS = 7


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the coordinator and its children."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    # Silence noisy third-party loggers
    logging.getLogger("cdp_use").setLevel(logging.WARNING)
    logging.getLogger("bs4").setLevel(logging.WARNING)


def get_t1_list_from_t0(t0_result: dict[str, Any]) -> list[str]:
    """
    Extract the T1 username list from a completed T0 crawl.

    The T0 result contains all usernames found on the target blog.
    These become the T1 crawl list.
    """
    usernames = t0_result.get("usernames", [])
    if not usernames:
        logger.warning("T0 result has no usernames — empty T1 list")
    return usernames


def build_t2_list_from_t1(
    t1_results: list[dict[str, Any]],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """
    Aggregate all T1 agent results, deddup, and produce the T2 crawl list.

    The T2 list is every unique username found across all T1 crawls,
    minus usernames already cached as fresh T2 entries.
    """
    cache_root = cache_dir or CACHE_DIR
    t2_dir = cache_root / "t2"

    # Collect all usernames from T1 results
    all_t2_candidates: set[str] = set()
    for t1_result in t1_results:
        if t1_result.get("status") in ("cached", "error", "dead"):
            continue
        usernames = t1_result.get("usernames", [])
        all_t2_candidates.update(usernames)

    # Remove ones already crawled as fresh T2 entries
    fresh_t2: set[str] = set()
    if t2_dir.exists():
        for path in t2_dir.iterdir():
            if path.suffix != ".json" or path.name.startswith("."):
                continue
            entry = load_entry(path)
            if entry and not entry.get("dead", False):
                fresh_t2.add(entry.get("username", path.stem))

    t2_list = sorted(all_t2_candidates - fresh_t2)
    logger.info(
        "T2 list: %d candidates (T1 discovered %d unique, %d already fresh T2)",
        len(t2_list),
        len(all_t2_candidates),
        len(fresh_t2),
    )
    return t2_list


async def run_t0(
    browser_ws: str,
    target_blog: str,
    *,
    cache_dir: Path | None = None,
    recrawl_days: int = DEFAULT_RECRAWL_DAYS,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run T0: crawl the target blog and build the T1 username list.

    Uses agent.run with T0 limits. The result is cached as t0.json.
    Returns the full T0 result dict.
    """
    cache_root = cache_dir or CACHE_DIR
    t0_path = cache_root / "t0.json"

    # Check if T0 is already cached and fresh
    existing = load_entry(t0_path)
    if existing and not existing.get("dead", False):
        t0_scanned = existing.get("scanned_at", "")
        logger.info("T0 already cached (scanned %s) — re-using", t0_scanned)
        # Still return the result so caller can build T1 list from it
        return existing

    logger.info("Starting T0 crawl for %s", target_blog)
    logger.info(
        "T0 limits: unique=%d total=%d posts=%d",
        LIMITS["t0"]["unique"],
        LIMITS["t0"]["total"],
        LIMITS["t0"]["posts"],
    )

    result = await agent_run(
        browser_ws=browser_ws,
        username=target_blog,
        tier="t0",
        unique_limit=LIMITS["t0"]["unique"],
        total_limit=LIMITS["t0"]["total"],
        post_limit=LIMITS["t0"]["posts"],
        recrawl_days=recrawl_days,
        source_blog=None,
        cache_dir=cache_root,
    )

    # Save T0 result explicitly (agent.run already saved it to cache/t0.json
    # via its own save_entry, but we re-save here to ensure the wrapper
    # format is consistent)
    save_entry(t0_path, result)

    logger.info(
        "T0 complete: status=%s unique=%d total=%d posts=%d",
        result.get("status"),
        result.get("unique_count", 0),
        result.get("total_count", 0),
        result.get("posts_processed", 0),
    )

    return result


async def run_t1_batch(
    browser_ws: str,
    t1_usernames: list[str],
    *,
    cache_dir: Path | None = None,
    recrawl_days: int = DEFAULT_RECRAWL_DAYS,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict[str, Any]]:
    """
    Run T1 agents against the T1 username list, bounded to MAX_CONCURRENT_AGENTS.

    Each agent crawls its assigned blog independently. Results are cached
    individually per username in cache/t1/<username>.json.

    Returns list of result dicts in the same order as t1_usernames.
    """
    cache_root = cache_dir or CACHE_DIR
    sem = semaphore or asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    async def bounded(username: str) -> dict[str, Any]:
        async with sem:
            logger.info("T1 agent starting: %s", username)
            start = time.monotonic()
            ws_url = None
            target_id = None
            try:
                # T0 creates the tab — sub-agent receives the ws_url
                target_url = f"https://www.tumblr.com/{username}"
                logger.info("Creating T1 tab for %s", username)
                ws_url, target_id = await agent_run._new_tab_url(
                    browser_ws, target_url
                )
                logger.info(
                    "T1 tab created for %s: ws=%s target_id=%s",
                    username, ws_url, target_id,
                )
                result = await agent_run(
                    browser_ws=browser_ws,
                    username=username,
                    tier="t1",
                    unique_limit=LIMITS["t1"]["unique"],
                    total_limit=LIMITS["t1"]["total"],
                    post_limit=LIMITS["t1"]["posts"],
                    recrawl_days=recrawl_days,
                    source_blog=None,
                    cache_dir=cache_root,
                    pre_existing_ws_url=ws_url,
                )
            except Exception as exc:
                logger.error("T1 agent failed for %s: %s", username, exc)
                result = {
                    "username": username,
                    "tier": "t1",
                    "status": "error",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": False,
                    "dead_reason": str(exc),
                    "source_blog": None,
                }
            finally:
                # T0 owns cleanup — close the tab regardless of agent outcome
                if target_id:
                    await agent_run.close_tab(browser_ws, target_id)
                    logger.info("T1 tab closed for %s (target_id=%s)", username, target_id)
                elif ws_url:
                    logger.warning(
                        "T1 agent for %s had ws_url but no target_id — "
                        "tab may not be closable", username
                    )
            elapsed = time.monotonic() - start
            logger.info(
                "T1 agent done: %s status=%s (%.1fs)",
                username,
                result.get("status"),
                elapsed,
            )
            return result

    tasks = [bounded(u) for u in t1_usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert any unexpected exceptions into error results
    processed: list[dict[str, Any]] = []
    for username, result in zip(t1_usernames, results):
        if isinstance(result, Exception):
            logger.error("T1 agent unexpected exception for %s: %s", username, result)
            processed.append(
                {
                    "username": username,
                    "tier": "t1",
                    "status": "error",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": False,
                    "dead_reason": str(result),
                    "source_blog": None,
                }
            )
        else:
            processed.append(result)

    return processed


async def run_t2_batch(
    browser_ws: str,
    t2_usernames: list[str],
    *,
    cache_dir: Path | None = None,
    recrawl_days: int = DEFAULT_RECRAWL_DAYS,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict[str, Any]]:
    """
    Run T2 agents against the T2 username list, bounded to MAX_CONCURRENT_AGENTS.

    Same structure as T1 but with T2 limits and tier='t2'.
    """
    cache_root = cache_dir or CACHE_DIR
    sem = semaphore or asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    async def bounded(username: str) -> dict[str, Any]:
        async with sem:
            logger.info("T2 agent starting: %s", username)
            start = time.monotonic()
            ws_url = None
            target_id = None
            try:
                # T0 creates the tab — sub-agent receives the ws_url
                target_url = f"https://www.tumblr.com/{username}"
                logger.info("Creating T2 tab for %s", username)
                ws_url, target_id = await agent_run._new_tab_url(
                    browser_ws, target_url
                )
                logger.info(
                    "T2 tab created for %s: ws=%s target_id=%s",
                    username, ws_url, target_id,
                )
                result = await agent_run(
                    browser_ws=browser_ws,
                    username=username,
                    tier="t2",
                    unique_limit=LIMITS["t2"]["unique"],
                    total_limit=LIMITS["t2"]["total"],
                    post_limit=LIMITS["t2"]["posts"],
                    recrawl_days=recrawl_days,
                    source_blog=None,
                    cache_dir=cache_root,
                    pre_existing_ws_url=ws_url,
                )
            except Exception as exc:
                logger.error("T2 agent failed for %s: %s", username, exc)
                result = {
                    "username": username,
                    "tier": "t2",
                    "status": "error",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": False,
                    "dead_reason": str(exc),
                    "source_blog": None,
                }
            finally:
                # T0 owns cleanup — close the tab regardless of agent outcome
                if target_id:
                    await agent_run.close_tab(browser_ws, target_id)
                    logger.info("T2 tab closed for %s (target_id=%s)", username, target_id)
                elif ws_url:
                    logger.warning(
                        "T2 agent for %s had ws_url but no target_id — "
                        "tab may not be closable", username
                    )
            elapsed = time.monotonic() - start
            logger.info(
                "T2 agent done: %s status=%s (%.1fs)",
                username,
                result.get("status"),
                elapsed,
            )
            return result

    tasks = [bounded(u) for u in t2_usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed: list[dict[str, Any]] = []
    for username, result in zip(t2_usernames, results):
        if isinstance(result, Exception):
            logger.error("T2 agent unexpected exception for %s: %s", username, result)
            processed.append(
                {
                    "username": username,
                    "tier": "t2",
                    "status": "error",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": False,
                    "dead_reason": str(result),
                    "source_blog": None,
                }
            )
        else:
            processed.append(result)

    return processed


async def run_full_pipeline(
    target_blog: str,
    *,
    browser_ws: str | None = None,
    cache_dir: Path | None = None,
    recrawl_days: int = DEFAULT_RECRAWL_DAYS,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run the complete T0 → T1 → T2 pipeline.

    Args:
        target_blog: Top-level target blog (tumblr.com/<username>).
        browser_ws: CDP browser HTTP endpoint (default: http://localhost:9222).
        cache_dir: Cache root directory (default: /Users/eric/...)
        recrawl_days: Re-crawl window in days (default: 7).
        verbose: Enable DEBUG logging.

    Returns:
        Pipeline result with T0, T1 summary, T2 summary, and timing.
    """
    browser = browser_ws or os.environ.get("TUMBLR_CDP_BROWSER", DEFAULT_CDP_BROWSER)
    cache_root = cache_dir or CACHE_DIR

    logger.info("=== Pipeline start ===")
    logger.info("Target: %s", target_blog)
    logger.info("CDP browser: %s", browser)
    logger.info("Cache: %s", cache_root)
    logger.info("Recrawl window: %d days", recrawl_days)
    logger.info("Concurrent agent cap: %d", MAX_CONCURRENT_AGENTS)

    overall_start = time.monotonic()

    # --- T0 ---
    t0_result = await run_t0(
        browser_ws=browser,
        target_blog=target_blog,
        cache_dir=cache_root,
        recrawl_days=recrawl_days,
    )

    t0_usernames = get_t1_list_from_t0(t0_result)
    logger.info("T1 list size: %d usernames", len(t0_usernames))

    if not t0_usernames:
        logger.warning("No T1 usernames from T0 — pipeline ending early")
        return {
            "target_blog": target_blog,
            "t0": t0_result,
            "t1": {"usernames": [], "results": []},
            "t2": {"usernames": [], "results": []},
            "elapsed_seconds": time.monotonic() - overall_start,
        }

    # --- T1 ---
    logger.info("Starting T1: %d usernames, max %d concurrent", len(t0_usernames), MAX_CONCURRENT_AGENTS)
    t1_start = time.monotonic()
    t1_results = await run_t1_batch(
        browser_ws=browser,
        t1_usernames=t0_usernames,
        cache_dir=cache_root,
        recrawl_days=recrawl_days,
    )
    t1_elapsed = time.monotonic() - t1_start

    # Summary stats
    t1_success = [r for r in t1_results if r.get("status") not in ("error",)]
    t1_dead = [r for r in t1_results if r.get("dead")]
    t1_error = [r for r in t1_results if r.get("status") == "error"]
    t1_cached = [r for r in t1_results if r.get("status") == "cached"]

    logger.info(
        "T1 complete: %d total, %d success, %d cached, %d dead, %d error (%.1fs)",
        len(t1_results),
        len(t1_success),
        len(t1_cached),
        len(t1_dead),
        len(t1_error),
        t1_elapsed,
    )

    # --- Build T2 list ---
    t2_usernames = build_t2_list_from_t1(t1_results, cache_dir=cache_root)

    if not t2_usernames:
        logger.info("No T2 candidates — pipeline ending")
        return {
            "target_blog": target_blog,
            "t0": t0_result,
            "t1": {
                "usernames": t0_usernames,
                "results": t1_results,
                "summary": {
                    "total": len(t1_results),
                    "success": len(t1_success),
                    "cached": len(t1_cached),
                    "dead": len(t1_dead),
                    "error": len(t1_error),
                    "elapsed_seconds": t1_elapsed,
                },
            },
            "t2": {"usernames": [], "results": []},
            "elapsed_seconds": time.monotonic() - overall_start,
        }

    # --- T2 ---
    logger.info(
        "Starting T2: %d usernames, max %d concurrent",
        len(t2_usernames),
        MAX_CONCURRENT_AGENTS,
    )
    t2_start = time.monotonic()
    t2_results = await run_t2_batch(
        browser_ws=browser,
        t2_usernames=t2_usernames,
        cache_dir=cache_root,
        recrawl_days=recrawl_days,
    )
    t2_elapsed = time.monotonic() - t2_start

    t2_success = [r for r in t2_results if r.get("status") not in ("error",)]
    t2_dead = [r for r in t2_results if r.get("dead")]
    t2_error = [r for r in t2_results if r.get("status") == "error"]

    logger.info(
        "T2 complete: %d total, %d success, %d dead, %d error (%.1fs)",
        len(t2_results),
        len(t2_success),
        len(t2_dead),
        len(t2_error),
        t2_elapsed,
    )

    total_elapsed = time.monotonic() - overall_start

    logger.info("=== Pipeline complete (%.1fs total) ===", total_elapsed)

    return {
        "target_blog": target_blog,
        "browser": browser,
        "cache_dir": str(cache_root),
        "recrawl_days": recrawl_days,
        "concurrent_cap": MAX_CONCURRENT_AGENTS,
        "t0": t0_result,
        "t1": {
            "usernames": t0_usernames,
            "results": t1_results,
            "summary": {
                "total": len(t1_results),
                "success": len(t1_success),
                "cached": len(t1_cached),
                "dead": len(t1_dead),
                "error": len(t1_error),
                "elapsed_seconds": t1_elapsed,
            },
        },
        "t2": {
            "usernames": t2_usernames,
            "results": t2_results,
            "summary": {
                "total": len(t2_results),
                "success": len(t2_success),
                "dead": len(t2_dead),
                "error": len(t2_error),
                "elapsed_seconds": t2_elapsed,
            },
        },
        "elapsed_seconds": total_elapsed,
    }


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Tumblr multi-tier username crawler coordinator"
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
        action="store_true",
        help="Enable DEBUG logging",
    )
    parser.add_argument(
        "--t0-only",
        action="store_true",
        help="Run T0 only, then stop (for testing T0 in isolation)",
    )
    parser.add_argument(
        "--t1-only",
        action="store_true",
        help="Run T0 + T1 only, then stop (for testing through T1)",
    )

    args = parser.parse_args()

    if not args.target_blog:
        parser.error("target_blog is required (the top-level Tumblr blog username)")

    setup_logging(args.verbose)

    async def _run() -> None:
        if args.t0_only:
            result = await run_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            print("\n=== T0 RESULT ===")
            print(f"Status: {result.get('status')}")
            print(f"Unique: {result.get('unique_count', 0)}")
            print(f"Total:  {result.get('total_count', 0)}")
            print(f"Posts:  {result.get('posts_processed', 0)}")
            print(f"Dead:   {result.get('dead')}")
            if result.get("dead_reason"):
                print(f"Dead reason: {result['dead_reason']}")
            print(f"Usernames found: {len(result.get('usernames', []))}")
            if result.get("usernames"):
                print("First 20:", result["usernames"][:20])
        elif args.t1_only:
            result = await run_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            t1_list = get_t1_list_from_t0(result)
            if t1_list:
                t1_results = await run_t1_batch(
                    browser_ws=args.browser,
                    t1_usernames=t1_list,
                    cache_dir=args.cache_dir,
                    recrawl_days=args.recrawl_days,
                )
                print("\n=== T1 RESULT ===")
                print(f"Dispatched: {len(t1_list)}")
                print(f"Completed:  {len(t1_results)}")
                success = [r for r in t1_results if r.get("status") not in ("error",)]
                dead = [r for r in t1_results if r.get("dead")]
                error = [r for r in t1_results if r.get("status") == "error"]
                print(f"Success:    {len(success)}")
                print(f"Cached:     {len([r for r in t1_results if r.get('status') == 'cached'])}")
                print(f"Dead:       {len(dead)}")
                print(f"Error:      {len(error)}")
        else:
            result = await run_full_pipeline(
                target_blog=args.target_blog,
                browser_ws=args.browser,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
                verbose=args.verbose,
            )
            print("\n=== PIPELINE RESULT ===")
            print(f"Target: {result['target_blog']}")
            print(f"Browser: {result['browser']}")
            print(f"Cache: {result['cache_dir']}")
            print(f"Recrawl: {result['recrawl_days']}d")
            print(f"Concurrent cap: {result['concurrent_cap']}")
            print()
            print("--- T0 ---")
            t0 = result["t0"]
            print(f"Status: {t0.get('status')}")
            print(f"Unique: {t0.get('unique_count', 0)}")
            print(f"Total:  {t0.get('total_count', 0)}")
            print(f"Posts:  {t0.get('posts_processed', 0)}")
            print(f"Dead:   {t0.get('dead')}")
            if t0.get("dead_reason"):
                print(f"Dead reason: {t0['dead_reason']}")
            print(f"Usernames: {len(t0.get('usernames', []))}")
            if t0.get("usernames"):
                print("First 20:", t0["usernames"][:20])
            print()
            print("--- T1 ---")
            t1 = result["t1"]
            s = t1["summary"]
            print(f"Dispatched: {s['total']}")
            print(f"Success:    {s['success']}")
            print(f"Cached:     {s.get('cached', 0)}")
            print(f"Dead:       {s['dead']}")
            print(f"Error:      {s['error']}")
            print(f"Time:       {s['elapsed_seconds']:.1f}s")
            print()
            print("--- T2 ---")
            t2 = result["t2"]
            s = t2["summary"]
            print(f"Candidates: {len(t2['usernames'])}")
            print(f"Dispatched: {s['total']}")
            print(f"Success:    {s['success']}")
            print(f"Dead:       {s['dead']}")
            print(f"Error:      {s['error']}")
            print(f"Time:       {s['elapsed_seconds']:.1f}s")
            print()
            print(f"Total time: {result['elapsed_seconds']:.1f}s")

    asyncio.run(_run())


if __name__ == "__main__":
    main()

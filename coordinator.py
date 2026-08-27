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
import sys
import time
from pathlib import Path
from typing import Any

from agent import run as agent_run
from agent import probe_blog
from agent import _new_tab_url, close_tab
from cache import (
    CACHE_DIR,
    load_entry,
    save_entry,
    load_index,
    save_index,
    index_register,
    index_should_skip,
)

from chrome_lifecycle import restart_chrome, kill_chrome

logger = logging.getLogger("tumblr-coordinator")

# Default CDP browser endpoint (same as throughout this project)
DEFAULT_CDP_BROWSER = "http://localhost:9222"

# Limit presets
LIMITS = {
    0: {"unique": 250, "total": 500, "posts": 500},
    1: {"unique": 100, "total": 250, "posts": 250},
    2: {"unique": 75, "total": 125, "posts": 125},
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

    Deactivated usernames are NOT filtered out — they stay in the index
    so they can be queried later. They are skipped at dispatch time
    (no CDP tab, no page fetch) via the dead-check in run_t1's bounded().
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


def get_refresh_t1_list(
    t0_result: dict[str, Any],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """
    Build the T1 refresh list from a T0 result.

    Returns all T1 usernames from T0, minus those with a fresh cache entry.
    Unlike the initial crawl, deactivated usernames are NOT filtered here —
    they flow through to agent.run() which will short-circuit them via
    the dead-check.  This keeps them in the index.
    """
    cache_root = cache_dir or CACHE_DIR
    t1_dir = cache_root / "t1"

    usernames = t0_result.get("usernames", [])
    if not usernames:
        logger.warning("T0 result has no usernames — empty refresh T1 list")
        return []

    # Remove ones with fresh T1 cache entries
    fresh_t1: set[str] = set()
    if t1_dir.exists():
        for path in t1_dir.iterdir():
            if path.suffix != ".json" or path.name.startswith("."):
                continue
            entry = load_entry(path)
            if entry and not entry.get("dead", False):
                fresh_t1.add(entry.get("username", path.stem))

    refresh_list = sorted(set(usernames) - fresh_t1)
    logger.info(
        "T1 refresh list: %d usernames (%d already fresh, %d total from T0)",
        len(refresh_list),
        len(fresh_t1),
        len(usernames),
    )
    return refresh_list


def get_refresh_t2_list(
    t1_results: list[dict[str, Any]],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """
    Build the T2 refresh list from T1 results.

    Unlike build_t2_list_from_t1, this does NOT remove already-fresh T2
    entries — those will be skipped by agent.run()'s cache check.  We
    include everything so that stale T2 entries get re-scanned.
    """
    cache_root = cache_dir or CACHE_DIR

    all_t2_candidates: set[str] = set()
    for t1_result in t1_results:
        if t1_result.get("status") in ("cached", "error", "dead"):
            continue
        usernames = t1_result.get("usernames", [])
        all_t2_candidates.update(usernames)

    refresh_list = sorted(all_t2_candidates)
    logger.info(
        "T2 refresh list: %d candidates (from %d T1 results)",
        len(refresh_list),
        len(t1_results),
    )
    return refresh_list


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
        LIMITS[0]["unique"],
        LIMITS[0]["total"],
        LIMITS[0]["posts"],
    )

    result = await agent_run(
        browser_ws=browser_ws,
        username=target_blog,
        tier=0,
        unique_limit=LIMITS[0]["unique"],
        total_limit=LIMITS[0]["total"],
        post_limit=LIMITS[0]["posts"],
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
            # Short-circuit deactivated blogs — keep in index, skip the crawl.
            # No CDP tab, no page fetch; the username is already cached by T0
            # and stays queryable.
            if "deactivat" in username.lower():
                logger.info("Skipping deactivated blog (index-only): %s", username)
                return {
                    "username": username,
                    "tier": "t1",
                    "status": "skipped",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": True,
                    "dead_reason": "deactivated",
                    "source_blog": None,
                }

    running_blogs: dict[str, tuple[str, str]] = {}

    async def _get_or_create_tab(username: str, target_url: str) -> tuple[str, str]:
        """Get existing tab for a blog if one is open, otherwise create a new one."""
        if username in running_blogs:
            ws_url, target_id = running_blogs[username]
            logger.info("Reusing existing tab for %s via %s", username, ws_url)
            return ws_url, target_id
        ws_url, target_id = await _new_tab_url(browser_ws, target_url)
        running_blogs[username] = (ws_url, target_id)
        logger.info(
            "Created new tab for %s: ws=%s target_id=%s",
            username, ws_url, target_id,
        )
        return ws_url, target_id

    async def _release_tab(username: str) -> None:
        """Close the tab for a blog and remove it from the running set."""
        if username in running_blogs:
            ws_url, target_id = running_blogs.pop(username)
            if target_id:
                try:
                    await close_tab(browser_ws, target_id)
                    logger.info(
                        "Closed tab for %s (target_id=%s)", username, target_id
                    )
                except Exception as exc:
                    logger.warning("Failed to close tab for %s: %s", username, exc)

    async def bounded(username: str) -> dict[str, Any]:
        async with sem:
            # Short-circuit deactivated blogs
            if "deactivat" in username.lower():
                logger.info("Skipping deactivated blog (index-only): %s", username)
                return {
                    "username": username,
                    "tier": "t1",
                    "status": "skipped",
                    "unique_count": 0,
                    "total_count": 0,
                    "posts_processed": 0,
                    "usernames": [],
                    "all_occurrences": [],
                    "per_page": [],
                    "dead": True,
                    "dead_reason": "deactivated",
                    "source_blog": None,
                }

            logger.info("T1 agent starting: %s", username)
            start = time.monotonic()
            target_url = f"https://www.tumblr.com/{username}"
            ws_url, target_id = await _get_or_create_tab(username, target_url)
            try:
                result = await agent_run(
                    browser_ws=browser_ws,
                    username=username,
                    tier=1,
                    unique_limit=LIMITS[1]["unique"],
                    total_limit=LIMITS[1]["total"],
                    post_limit=LIMITS[1]["posts"],
                    recrawl_days=recrawl_days,
                    source_blog=None,
                    cache_dir=cache_root,
                    pre_existing_ws_url=ws_url,
                )
            except Exception as exc:  # noqa: BLE001
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
                await _release_tab(username)
            elapsed = time.monotonic() - start
            logger.info(
                "T1 agent done: %s status=%s (%.1fs)",
                username, result.get("status"), elapsed,
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

    running_blogs: dict[str, tuple[str, str]] = {}

    async def _get_or_create_tab(username: str, target_url: str) -> tuple[str, str]:
        """Get existing tab for a blog if one is open, otherwise create a new one."""
        if username in running_blogs:
            ws_url, target_id = running_blogs[username]
            logger.info("Reusing existing tab for %s via %s", username, ws_url)
            return ws_url, target_id
        ws_url, target_id = await _new_tab_url(browser_ws, target_url)
        running_blogs[username] = (ws_url, target_id)
        logger.info(
            "Created new tab for %s: ws=%s target_id=%s",
            username, ws_url, target_id,
        )
        return ws_url, target_id

    async def _release_tab(username: str) -> None:
        """Close the tab for a blog and remove it from the running set."""
        if username in running_blogs:
            ws_url, target_id = running_blogs.pop(username)
            if target_id:
                try:
                    await close_tab(browser_ws, target_id)
                    logger.info(
                        "Closed tab for %s (target_id=%s)", username, target_id
                    )
                except Exception as exc:
                    logger.warning("Failed to close tab for %s: %s", username, exc)

            logger.info("T2 agent starting: %s", username)
            start = time.monotonic()
            target_url = f"https://www.tumblr.com/{username}"
            ws_url, target_id = await _get_or_create_tab(username, target_url)
            try:
                result = await agent_run(
                    browser_ws=browser_ws,
                    username=username,
                    tier=2,
                    unique_limit=LIMITS[2]["unique"],
                    total_limit=LIMITS[2]["total"],
                    post_limit=LIMITS[2]["posts"],
                    recrawl_days=recrawl_days,
                    source_blog=None,
                    cache_dir=cache_root,
                    pre_existing_ws_url=ws_url,
                )
            except Exception as exc:  # noqa: BLE001
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
                await _release_tab(username)
            elapsed = time.monotonic() - start
            logger.info(
                "T2 agent done: %s status=%s (%.1fs)",
                username, result.get("status"), elapsed,
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
    browser = browser_ws or DEFAULT_CDP_BROWSER
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

    # Build T1 list — filter out usernames that already have fresh T1
    # cache entries to avoid re-indexing blogs unnecessarily.
    t0_usernames = get_refresh_t1_list(t0_result, cache_dir=cache_root)
    logger.info("T1 list size: %d usernames", len(t0_usernames))

    if not t0_usernames:
        logger.warning("No T1 usernames from T0 — pipeline ending early")
        return {
            "target_blog": target_blog,
            "t0": t0_result,
            "t1": {
                "usernames": [],
                "results": [],
                "summary": {
                    "total": 0,
                    "success": 0,
                    "cached": 0,
                    "dead": 0,
                    "error": 0,
                    "elapsed_seconds": 0,
                },
            },
            "t2": {
                "usernames": [],
                "results": [],
                "summary": {
                    "total": 0,
                    "success": 0,
                    "cached": 0,
                    "dead": 0,
                    "error": 0,
                    "elapsed_seconds": 0,
                },
            },
            "elapsed_seconds": time.monotonic() - overall_start,
        }

    # --- T1 ---
    logger.info(
        "Starting T1: %d usernames, max %d concurrent",
        len(t0_usernames),
        MAX_CONCURRENT_AGENTS,
    )
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
            "t2": {
                "usernames": [],
                "results": [],
                "summary": {
                    "total": 0,
                    "success": 0,
                    "cached": 0,
                    "dead": 0,
                    "error": 0,
                    "elapsed_seconds": 0,
                },
            },
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


async def run_parallel_pipeline(
    target_blog: str,
    *,
    browser_ws: str | None = None,
    cache_dir: Path | None = None,
    recrawl_days: int = DEFAULT_RECRAWL_DAYS,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run the parallel pipeline: fresh Chrome, date-aware indexing, T1/T2 dispatch on-the-fly.

    Three mandated changes:
    1. Fresh Chrome restart at pipeline start
    2. Date-aware per-blog indexing (ALL tiers) — probe page 0, compare dates, skip if no new content
    3. Parallel T1/T2 dispatch — T2 starts as soon as T1 results arrive, not after all T1 completes
    """
    browser = browser_ws or DEFAULT_CDP_BROWSER
    cache_root = cache_dir or CACHE_DIR
    index_path = cache_root / "index.json"

    logger.info("=== Parallel pipeline start ===")
    logger.info("Target: %s", target_blog)
    logger.info("Index: %s", index_path)

    overall_start = time.monotonic()

    # 1. Fresh Chrome restart
    chrome_status = restart_chrome()
    logger.info("Chrome restart: %s", chrome_status.get("status"))
    if chrome_status.get("status") != "ok":
        logger.warning("Chrome restart issue: %s — continuing anyway", chrome_status)

    # 2. T0 probe — check if target blog has new content
    t0_probe = await probe_blog(
        browser_ws=browser,
        username=target_blog,
        cache_dir=cache_root,
        index_path=index_path,
    )
    if t0_probe["skip"]:
        logger.info("T0 skip: no new content since last scan")
        return {
            "target_blog": target_blog,
            "status": "skipped",
            "reason": "no_new_content",
            "t0_probe": t0_probe,
            "elapsed_seconds": time.monotonic() - overall_start,
        }

    # 3. T0 full crawl
    t0_result = await run_t0(
        browser_ws=browser,
        target_blog=target_blog,
        cache_dir=cache_root,
        recrawl_days=recrawl_days,
    )

    # Register T0 in index
    index_register(
        index_path,
        username=target_blog,
        tier=0,
        status=t0_result.get("status", "unknown"),
        usernames=t0_result.get("usernames", []),
        scanned_at=t0_result.get("scanned_at", ""),
    )

    # 4. Build T1 list from T0 results
    t0_usernames = get_refresh_t1_list(t0_result, cache_dir=cache_root)
    logger.info("T1 list size: %d usernames", len(t0_usernames))

    if not t0_usernames:
        logger.warning("No T1 usernames from T0 — pipeline ending early")
        return {
            "target_blog": target_blog,
            "t0": t0_result,
            "t1": {"usernames": [], "results": [], "summary": {"total": 0}},
            "t2": {"usernames": [], "results": [], "summary": {"total": 0}},
            "elapsed_seconds": time.monotonic() - overall_start,
        }

    # 5. Parallel T1/T2 dispatch
    # T1 agents run with bounded concurrency. As each T1 completes, its usernames
    # are immediately dispatched to T2 agents (also bounded).
    t1_results: list[dict[str, Any]] = []
    t2_results: list[dict[str, Any]] = []
    t1_sem = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)
    t2_sem = asyncio.Semaphore(MAX_CONCURRENT_AGENTS)

    # Queue of T1 usernames not yet dispatched
    t1_queue: asyncio.Queue[str] = asyncio.Queue()
    for u in t0_usernames:
        await t1_queue.put(u)

    # Track T2 dispatch tasks
    t2_tasks: set[asyncio.Task] = set()

    async def run_t1_with_t2_dispatch(username: str) -> dict[str, Any]:
        """Run T1 agent, then immediately dispatch its usernames to T2."""
        # Probe first
        probe = await probe_blog(
            browser_ws=browser,
            username=username,
            cache_dir=cache_root,
            index_path=index_path,
        )
        if probe["skip"]:
            logger.info("T1 skip: %s (no new content)", username)
            return {
                "username": username,
                "tier": "t1",
                "status": "skipped",
                "unique_count": 0,
                "total_count": 0,
                "posts_processed": 0,
                "usernames": [],
                "all_occurrences": [],
                "per_page": [],
                "dead": False,
                "dead_reason": "no_new_content",
                "source_blog": target_blog,
            }

        # Full T1 crawl
        result = await _run_single_agent(
            browser_ws=browser,
            username=username,
            tier=1,
            cache_root=cache_root,
            recrawl_days=recrawl_days,
            semaphore=t1_sem,
        )

        # Register in index immediately
        index_register(
            index_path,
            username=username,
            tier=1,
            status=result.get("status", "unknown"),
            usernames=result.get("usernames", []),
            scanned_at=result.get("scanned_at", ""),
        )

        # Immediately dispatch T2 for each new username
        for name in result.get("usernames", []):
            if name == username:
                continue
            # Check if already in T2 queue or done
            name_lower = name.lower()
            if "deactivat" in name_lower:
                continue
            # Fire-and-forget T2 dispatch
            task = asyncio.create_task(_run_single_agent(
                browser_ws=browser,
                username=name,
                tier=2,
                cache_root=cache_root,
                recrawl_days=recrawl_days,
                semaphore=t2_sem,
            ))
            t2_tasks.add(task)
            task.add_done_callback(t2_tasks.discard)

        return result

    # Run all T1 agents (bounded by semaphore via _run_single_agent)
    t1_tasks = [run_t1_with_t2_dispatch(u) for u in t0_usernames]
    t1_results = await asyncio.gather(*t1_tasks, return_exceptions=True)

    # Convert exceptions to error results
    t1_processed: list[dict[str, Any]] = []
    for username, result in zip(t0_usernames, t1_results):
        if isinstance(result, Exception):
            logger.error("T1 agent unexpected exception for %s: %s", username, result)
            t1_processed.append({
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
                "source_blog": target_blog,
            })
        else:
            t1_processed.append(result)

    # Wait for all T2 tasks to complete
    if t2_tasks:
        t2_raw = await asyncio.gather(*t2_tasks, return_exceptions=True)
        for result in t2_raw:
            if isinstance(result, Exception):
                logger.error("T2 agent unexpected exception: %s", result)
            elif isinstance(result, dict):
                t2_results.append(result)
                # Register T2 in index
                index_register(
                    index_path,
                    username=result.get("username", ""),
                    tier=2,
                    status=result.get("status", "unknown"),
                    usernames=result.get("usernames", []),
                    scanned_at=result.get("scanned_at", ""),
                )

    total_elapsed = time.monotonic() - overall_start
    logger.info(
        "=== Parallel pipeline complete: T0=%d T1=%d T2=%d (%.1fs) ===",
        1,
        len(t1_processed),
        len(t2_results),
        total_elapsed,
    )

    return {
        "target_blog": target_blog,
        "status": "complete",
        "t0": t0_result,
        "t1": {
            "usernames": t0_usernames,
            "results": t1_processed,
            "summary": {
                "total": len(t1_processed),
                "success": len([r for r in t1_processed if r.get("status") not in ("error",)]),
                "skipped": len([r for r in t1_processed if r.get("status") == "skipped"]),
                "dead": len([r for r in t1_processed if r.get("dead")]),
                "error": len([r for r in t1_processed if r.get("status") == "error"]),
            },
        },
        "t2": {
            "results": t2_results,
            "summary": {
                "total": len(t2_results),
                "success": len([r for r in t2_results if r.get("status") not in ("error",)]),
                "dead": len([r for r in t2_results if r.get("dead")]),
                "error": len([r for r in t2_results if r.get("status") == "error"]),
            },
        },
        "elapsed_seconds": total_elapsed,
    }


async def _run_single_agent(
    browser_ws: str,
    username: str,
    tier: int,
    cache_root: Path,
    recrawl_days: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Run a single agent with bounded concurrency and tab reuse."""
    async with semaphore:
        if "deactivat" in username.lower():
            return {
                "username": username,
                "tier": f"t{tier}",
                "status": "skipped",
                "unique_count": 0,
                "total_count": 0,
                "posts_processed": 0,
                "usernames": [],
                "all_occurrences": [],
                "per_page": [],
                "dead": True,
                "dead_reason": "deactivated",
                "source_blog": None,
            }

        target_url = f"https://www.tumblr.com/{username}"
        ws_url, target_id = await _new_tab_url(browser_ws, target_url)
        try:
            result = await agent_run(
                browser_ws=browser_ws,
                username=username,
                tier=tier,
                unique_limit=LIMITS[tier]["unique"],
                total_limit=LIMITS[tier]["total"],
                post_limit=LIMITS[tier]["posts"],
                recrawl_days=recrawl_days,
                source_blog=None,
                cache_dir=cache_root,
                pre_existing_ws_url=ws_url,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("Agent failed for %s tier %d: %s", username, tier, exc)
            return {
                "username": username,
                "tier": f"t{tier}",
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
            try:
                await close_tab(browser_ws, target_id)
            except Exception:
                pass


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
    parser.add_argument(
        "--refresh-t0",
        action="store_true",
        help="Refresh T0 only: re-scan target blog from offset 0, stop at date cutoff",
    )
    parser.add_argument(
        "--refresh-t1",
        action="store_true",
        help="Refresh T0 + T1: re-scan target blog, then refresh T1 blogs with date cutoff",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Full refresh: T0 → T1 → T2, all with date cutoff. Alias for --refresh-t2.",
    )
    parser.add_argument(
        "--refresh-t2",
        action="store_true",
        help="Refresh T0 + T1 + T2: full pipeline refresh with date cutoff",
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
                print(
                    f"Cached:     {len([r for r in t1_results if r.get('status') == 'cached'])}"
                )
                print(f"Dead:       {len(dead)}")
                print(f"Error:      {len(error)}")
        elif args.refresh_t0:
            result = await run_refresh_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            print("\n=== T0 REFRESH RESULT ===")
            print(f"Status: {result.get('status')}")
            print(f"Unique: {result.get('unique_count', 0)}")
            print(f"Total:  {result.get('total_count', 0)}")
            print(f"Posts:  {result.get('posts_processed', 0)}")
            print(f"Dead:   {result.get('dead')}")
            if result.get("dead_reason"):
                print(f"Dead reason: {result['dead_reason']}")
            print(f"Usernames found: {len(result.get('usernames', []))}")
            print(f"Recovery attempts: {result.get('recovery_attempts', 0)}")
            if result.get("usernames"):
                print("First 20:", result["usernames"][:20])
        elif args.refresh_t1:
            t0_result = await run_refresh_t0(
                browser_ws=args.browser,
                target_blog=args.target_blog,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
            )
            t1_list = get_t1_list_from_t0(t0_result)
            if t1_list:
                t1_results = await run_refresh_t1(
                    browser_ws=args.browser,
                    t0_result=t0_result,
                    cache_dir=args.cache_dir,
                    recrawl_days=args.recrawl_days,
                )
                print("\n=== T1 REFRESH RESULT ===")
                print(f"Dispatched: {len(t1_list)}")
                print(f"Completed:  {len(t1_results)}")
                success = [r for r in t1_results if r.get("status") not in ("error",)]
                dead = [r for r in t1_results if r.get("dead")]
                error = [r for r in t1_results if r.get("status") == "error"]
                print(f"Success:    {len(success)}")
                print(
                    f"Cached:     {len([r for r in t1_results if r.get('status') == 'cached'])}"
                )
                print(f"Dead:       {len(dead)}")
                print(f"Error:      {len(error)}")
            else:
                print("\n=== T1 REFRESH: no usernames from T0 ===")
        elif args.refresh or args.refresh_t2:
            result = await run_refresh(
                target_blog=args.target_blog,
                browser_ws=args.browser,
                cache_dir=args.cache_dir,
                recrawl_days=args.recrawl_days,
                verbose=args.verbose,
            )
            print("\n=== REFRESH RESULT ===")
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

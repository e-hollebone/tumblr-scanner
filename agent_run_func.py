    cache_root = cache_dir or CACHE_DIR
    tier_dir = cache_root / tier

    # Check if this username already has a fresh cache entry
    existing = load_entry(tier_dir / f"{username}.json")
    if existing and not entry_is_stale(existing, recrawl_days=recrawl_days):
        logger.info("Skipping %s — cache is fresh (< %d days)", username, recrawl_days)
        return {
            "username": username,
            "tier": tier,
            "status": "cached",
            "cached_entry": existing,
        }

    # Determine starting offset.
    # For a refresh (existing cache exists but is stale), start from 0
    # and use date cutoff to skip already-indexed posts.
    # For a net-new blog (no cache), start from 0 with no cutoff.
    if existing:
        # Refresh mode: start from offset 0, stop when we reach posts
        # older than the last scan date.
        last_scanned_str = existing.get("scanned_at", "")
        cutoff_date: date | None = None
        if last_scanned_str:
            try:
                cutoff_date = datetime.strptime(
                    last_scanned_str[:10], "%Y-%m-%d"
                ).date()
            except (ValueError, TypeError):
                cutoff_date = None
        logger.info(
            "Refresh mode for %s: last scanned %s, cutoff_date=%s",
            username,
            last_scanned_str[:10] if last_scanned_str else "unknown",
            cutoff_date.isoformat() if cutoff_date else "none",
        )
    else:
        # Net-new blog: full scan, no date cutoff
        cutoff_date = None
        logger.info("Net-new blog %s — full scan, no date cutoff", username)

    page_size = 20  # Tumblr renders ~20 posts per page

    # Cumulative state — carried across tab replacements
    all_usernames: list[str] = []  # all occurrences, in order
    unique_set: set[str] = set()
    per_page_results: list[dict[str, Any]] = []
    posts_processed = 0
    total_posts = 0  # absolute post counter — cumulative posts seen across all pages
    status = "running"
    dead = False
    dead_reason: str | None = None

    offset = 0
    recovery_attempts = 0
    MAX_RECOVERY_ATTEMPTS = 3

    # The CDP tab lifecycle is managed per-page so we can recover from
    # tab crashes (Chrome error code 5 / page.documentCleared).
    # Each page fetch gets a fresh client; if the tab dies mid-fetch,
    # we close it, create a new one from the same offset, and retry.
    while True:
        # Check limits before fetching
        if check_limit(
            unique_count=len(unique_set),
            total_count=len(all_usernames),
            posts_count=total_posts,
            unique_limit=unique_limit,
            total_limit=total_limit,
            post_limit=post_limit,
        ):
            logger.info(
                "Limit reached for %s: unique=%d total=%d posts=%d",
                username,
                len(unique_set),
                len(all_usernames),
                posts_processed,
            )
            status = "limit_reached"
            break

        # --- Page fetch with tab recovery ---
        page_html = None
        page_text = ""
        tab_target_id: str | None = None
        tab_dead = False

        for recovery_round in range(MAX_RECOVERY_ATTEMPTS):
            recovery_attempts = recovery_round + 1

            if tab_target_id:
                # Close the dead tab before creating a new one
                try:
                    await close_tab(browser_ws, tab_target_id)
                except Exception:
                    pass
                await asyncio.sleep(2.0)

            # Create or reuse a CDP tab
            if pre_existing_ws_url and recovery_round == 0:
                ws_url = pre_existing_ws_url
                logger.info("Using pre-existing tab for %s via %s", username, ws_url)
                tab_target_id = None
            else:
                target_url = f"https://www.tumblr.com/{username}?offset={offset}"
                logger.info(
                    "Creating tab for %s offset=%d (recovery %d/%d)",
                    username,
                    offset,
                    recovery_round + 1,
                    MAX_RECOVERY_ATTEMPTS,
                )
                try:
                    ws_url, tab_target_id = await _new_tab_url(browser_ws, target_url)
                except Exception as exc:
                    logger.error(
                        "Tab creation failed for %s offset=%d (recovery %d/%d): %s",
                        username,
                        offset,
                        recovery_round + 1,
                        MAX_RECOVERY_ATTEMPTS,
                        exc,
                    )
                    if recovery_round + 1 < MAX_RECOVERY_ATTEMPTS:
                        await asyncio.sleep(5.0)
                        continue
                    dead = True
                    dead_reason = f"tab_creation_failed: {exc}"
                    status = "dead"
                    break

            client = CDPClient(ws_url)
            await client.start()
            logger.info("Agent connected for %s via %s", username, ws_url)

            try:
                # Fetch page HTML
                logger.info("Fetching %s offset=%d", username, offset)
                page_html = await fetch_page_html(client, username, offset)

                if not page_html:
                    # Empty HTML — could be a dead blog or a tab crash
                    # Try to get page text to distinguish
                    try:
                        text_result = await client.send.Runtime.evaluate(
                            params={
                                "expression": "document.body ? document.body.innerText : ''",
                                "returnByValue": True,
                            }
                        )
                        page_text = text_result.get("result", {}).get("value", "")
                    except Exception:
                        page_text = ""

                    if detect_dead(page_text):
                        matched = None
                        text_lower = page_text.lower()
                        for phrase in DEAD_PHRASES:
                            if phrase in text_lower:
                                matched = phrase
                                break
                        dead = True
                        dead_reason = (
                            f"phrase:{matched}" if matched else "dead_phrase_match"
                        )
                        status = "dead"
                        logger.info(
                            "Blog %s is dead: %s (matched=%s)",
                            username,
                            dead_reason,
                            matched,
                        )
                    else:
                        # No HTML and no dead signal — likely tab crash
                        logger.warning(
                            "No HTML for %s at offset %d — possible tab crash, "
                            "retry %d/%d",
                            username,
                            offset,
                            recovery_round + 1,
                            MAX_RECOVERY_ATTEMPTS,
                        )
                        tab_dead = True
                    break  # exit recovery loop — handle outcome below

                # Get page text for dead/end detection
                try:
                    text_result = await client.send.Runtime.evaluate(
                        params={
                            "expression": "document.body ? document.body.innerText : ''",
                            "returnByValue": True,
                        }
                    )
                    page_text = text_result.get("result", {}).get("value", "")
                except Exception:  # noqa: BLE001 — text extraction is best-effort
                    page_text = ""

                # Dead blog detection
                if detect_dead(page_text):
                    matched = None
                    text_lower = page_text.lower()
                    for phrase in DEAD_PHRASES:
                        if phrase in text_lower:
                            matched = phrase
                            break
                    dead = True
                    dead_reason = (
                        f"phrase:{matched}" if matched else "dead_phrase_match"
                    )
                    status = "dead"
                    logger.info(
                        "Blog %s is dead: %s (matched=%s)",
                        username,
                        dead_reason,
                        matched,
                    )
                    break  # exit recovery loop — blog is dead, no retry

                # Extract usernames + dates from page HTML
                page_result = compute_page_metrics(page_html, source_blog)
                page_usernames = page_result["usernames"]
                page_date_max = page_result.get("page_date_max")
                page_date_min = page_result.get("page_date_min")

                # --- Date-based cutoff for refresh mode ---
                if cutoff_date is not None and page_date_max is not None:
                    try:
                        page_max_date = datetime.strptime(
                            page_date_max[:10], "%Y-%m-%d"
                        ).date()
                        # Posts are newest-first.  If the newest post on this
                        # page is older than the cutoff day, all posts on this
                        # page (and all subsequent pages) are already indexed.
                        if page_max_date < cutoff_date:
                            logger.info(
                                "Date cutoff reached for %s: page newest date %s "
                                "< cutoff %s — stopping scan",
                                username,
                                page_date_max,
                                cutoff_date.isoformat(),
                            )
                            status = "finished"
                            break  # exit recovery loop — done
                    except (ValueError, TypeError):
                        pass  # unparseable date, skip cutoff check

                # First page with no usernames AND end-of-posts signal — blog is empty
                if (
                    not page_usernames
                    and posts_processed == 0
                    and detect_end_of_posts(page_text, page_html)
                ):
                    logger.info("Blog %s has no posts (end signal on first page)", username)
                    status = "empty"
                    break  # exit recovery loop

                # Accumulated successfully — break recovery loop, continue main loop
                break

            except Exception as exc:  # noqa: BLE001 — tab crash during fetch
                logger.warning(
                    "Page fetch exception for %s offset=%d (recovery %d/%d): %s",
                    username,
                    offset,
                    recovery_round + 1,
                    MAX_RECOVERY_ATTEMPTS,
                    exc,
                )
                tab_dead = True
                break  # exit recovery loop — handle retry below
            finally:
                try:
                    await client.stop()
                except Exception:
                    pass

        # --- Post-recovery handling ---
        if dead or status in ("finished", "empty", "limit_reached"):
            # Terminal state — close tab if we have one, then exit main loop
            if tab_target_id:
                try:
                    await close_tab(browser_ws, tab_target_id)
                except Exception:
                    pass
            break

        if tab_dead and recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
            # Exhausted tab recovery — treat as dead
            dead = True
            dead_reason = "tab_recovery_exhausted"
            status = "dead"
            logger.error(
                "Tab recovery exhausted for %s at offset %d after %d attempts",
                username,
                offset,
                recovery_attempts,
            )
            if tab_target_id:
                try:
                    await close_tab(browser_ws, tab_target_id)
                except Exception:
                    pass
            break

        if page_html is None:
            # Should not reach here — handled above
            break

        # --- Accumulate results (only reached on successful page fetch) ---
        for name in page_usernames:
            all_usernames.append(name)
            unique_set.add(name)

        per_page_results.append(
            {
                "offset": offset,
                "cell_count": page_result.get("posts_rendered", 0),
                "usernames_this_page": sorted(page_usernames),
                "total_this_page": len(page_usernames),
                "date_min": page_date_min,
                "date_max": page_date_max,
            }
        )
        posts_processed += 1
        total_posts += page_size  # absolute post count: each page = 20 posts

        # Commit to cache after every page
        entry = {
            "username": username,
            "tier": tier,
            "source_blog": source_blog,
            "status": status,
            "unique_count": len(unique_set),
            "total_count": len(all_usernames),
            "posts_processed": posts_processed,
            "usernames": sorted(unique_set),
            "all_occurrences": all_usernames,
            "per_page": per_page_results,
            "dead": dead,
            "dead_reason": dead_reason,
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "recovery_attempts": recovery_attempts,
        }
        save_entry(tier_dir / f"{username}.json", entry)
        append_log(
            cache_root / "log.json",
            {
                "tier": tier,
                "username": username,
                "status": status,
                "unique_count": len(unique_set),
                "total_count": len(all_usernames),
                "posts_processed": posts_processed,
                "dead": dead,
                "dead_reason": dead_reason,
                "recovery_attempts": recovery_attempts,
            },
        )

        # Check if we've hit the end of posts (natural end, not date cutoff)
        if detect_end_of_posts(page_text, page_html):
            logger.info("End of posts for %s at offset %d", username, offset)
            status = "finished"
            if tab_target_id:
                try:
                    await close_tab(browser_ws, tab_target_id)
                except Exception:
                    pass
            break

        # Move to next page
        offset += page_size

        # Random delay between fetches
        delay = random.uniform(delay_min, delay_max)
        logger.debug("Sleeping %.2fs before next fetch", delay)
        await asyncio.sleep(delay)

    finally:
        # Final save
        entry = {
            "username": username,
            "tier": tier,
            "source_blog": source_blog,
            "status": status,
            "unique_count": len(unique_set),
            "total_count": len(all_usernames),
            "posts_processed": posts_processed,
            "usernames": sorted(unique_set),
            "all_occurrences": all_usernames,
            "per_page": per_page_results,
            "dead": dead,
            "dead_reason": dead_reason,
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }
        save_entry(tier_dir / f"{username}.json", entry)
        append_log(
            cache_root / "log.json",
            {
                "tier": tier,
                "username": username,
                "status": status,
                "unique_count": len(unique_set),
                "total_count": len(all_usernames),
                "posts_processed": posts_processed,
                "dead": dead,
                "dead_reason": dead_reason,
            },
        )

        try:
            await client.stop()
        except Exception:  # noqa: BLE001, S110 — final cleanup, non-fatal if it fails
            pass

    return {
        "username": username,
        "tier": tier,
        "status": status,
        "unique_count": len(unique_set),
        "total_count": len(all_usernames),
        "posts_processed": posts_processed,
        "usernames": sorted(unique_set),
        "all_occurrences": all_usernames,
        "per_page": per_page_results,
        "dead": dead,
        "dead_reason": dead_reason,
        "source_blog": source_blog,
    }

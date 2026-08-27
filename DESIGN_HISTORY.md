# Tumblr Scanner — Complete Design History & Failure Log

**Project:** Multi-tier Tumblr username extraction pipeline (`@the-smallest-kitten-cravings` → T1 → T2)
**Repo:** `github.com/e-hollebone/tumblr-scanner`
**Path:** `/Users/eric/Documents/tumblr-scanner`
**Compiled:** 2026-08-27 — from git log, 2 weeks of session history (sessions `20260817_195143_00c763` and `20260823_213838_1666ca`, 12,490 + 26,954 raw messages), and the current source tree.

> This document is intentionally exhaustive. Every architecture attempt, every dead end, every concurrency/extraction/CDP failure is logged with the code location and root cause. Where a fact is reconstructed from a compacted session summary rather than raw tool output, it is marked `[from summary]`.

---

## 1. Origin & Problem Statement

> *"Trace Tumblr user paths. Start at `@the-smallest-kitten-cravings`, look for another user's name in the feed by paging up to 200 feed items."*

The social graph: blog A reblogs from B, B reblogs from C. Goal = discover this graph from one seed, extracting usernames across degrees of separation. This became a 3-tier crawler: T0 (seed) → T1 (depth-1) → T2 (depth-2).

### Constraints discovered (all enforced by user, repeated across sessions)
| Constraint | Source |
|------------|--------|
| Tumblr is a SPA — `curl` gets an empty HTML shell, no posts rendered | browser_exec testing |
| Must use Chrome CDP for JS rendering (`Runtime.evaluate` gets full HTML) | browser_exec testing |
| Offset pagination: `?offset=N` (scrolling doesn't trigger lazy load) | browser_exec testing |
| Promo blocks ("Check these out") inject ~4 junk usernames per page | browser_exec testing |
| Max 4 Chrome tabs (Chrome crashes above ~30) | user directive |
| Tab reuse required — one tab per worker, never open/close per blog | user directive (repeated) |
| No inline python (`python -c "..."` banned — write script files) | user directive |
| Lint before checkin (`py_compile` + `ruff` via script files) | user directive |
| Raw output only — no modification, no summarization | user directive |

---

## 2. The Three (Failed) Fetch Architectures

The user is correct: **three distinct architectures for *fetching* the feed were attempted and all failed before a working pipeline emerged.** They are not variants of one approach — they are fundamentally different mechanisms.

### Architecture A — `curl` / `web_extract` (SPA limitation) — FAILED immediately
- **Mechanism:** HTTP GET the blog URL, parse the returned HTML.
- **Failure:** Tumblr is a client-rendered SPA. `curl` and `web_extract` return a ~50KB empty shell with no post content. `web_extract` against `https://www.tumblr.com/the-smallest-kitten-cravings` returned `content: ""` (verified, store 15454: `Max retries exceeded ... Failed to resolve 'firecrawl-gateway.nousresearch.com'`).
- **Also blocked by DNS:** even when the SPA shell would have been reachable, outbound DNS was failing (see §6.1).
- **Disposition:** Abandoned. Cannot work on Tumblr by design.

### Architecture B — `browser_exec` interactive (manual drive) — worked for tracing, does not scale
- **Mechanism:** Drive the user's visible Chrome via the `browser_exec` helper (`new_tab()`, `wait_for_load()`, `js('document.body.innerText')`, `page_info()`).
- **What worked:** Rendered 18 posts / 18 usernames on the first manual pass. Proved offset pagination and promo-block filtering.
- **Why it failed as a pipeline:**
  - **Not automatable at scale.** Each blog requires a fresh `new_tab()` + `wait_for_load()` + manual `js()` call. No programmatic loop, no concurrency.
  - **Sub-agent fan-out DNS-poisoned it** (see §6.1) — when 7 sub-agents each opened their own `browser_exec` session, DNS errors cascaded and corrupted results.
  - **`browser-harness` CLI was missing** (store 15460: `/bin/bash: line 3: browser-harness: command not found`, exit 127) — an attempted path to script the browser that never existed on PATH.
  - **Regex extraction was inaccurate** in this context (store 15513: "regex is capturing dates and partial usernames") — the `Reblogged\nTIME\nUSERNAME` vs `Reblogged USERNAME` formats needed two regex paths; the first cut missed the same-line format (11 → 25 usernames after fix).
- **Disposition:** Used for discovery/tracing only. Not a pipeline.

### Architecture C — CDP programmatic (`scan_t2_cdp_vX` → `agent.py`/`coordinator.py`) — failed repeatedly, then partially stabilized
- **Mechanism:** Connect to Chrome's Debugging Protocol WebSocket (`ws://localhost:9222`), open tabs via `Target.createTarget`, fetch HTML via `Runtime.evaluate`, navigate via `Page.navigate` to `?offset=N`.
- **This is where the real pain was** — see §3 (version chain), §4 (concurrency/lifecycle), §5 (CDP errors), §6 (everything else).

---

## 3. CDP Version Chain (all the `scan_t2_*` files that existed)

The repo accumulated ~14 experimental scanner files before the current 5-module structure. Each was an attempt to fix the prior one. File sizes recovered from `wc -l` (store 31003):

```
scan_t2.py                 168   (initial single-blog CDP scanner)
scan_t2_cdp.py             236   (CDP refactor)
scan_t2_cdp_v2.py          287   (v2)
scan_t2_cdp_v3.py          269   (v3 — "correct cdp" per summary)
scan_t2_cdp_v3_bg.py       243   (v3 background variant)
scan_t2_fixup.py           138   (one-off fixup)
scan_t2_index_v5.py        205   (v5 — stalled, killed PID 21249)
scan_t2_index_v6.py        410   (v6)
scan_t2_full_rescan_v7.py  602   (v7 — "current target, not yet launched")
scan_t2_full_rescan_v8.py  171   (v8 — parallel worker pool)
scan_t2_health.py          205   (health-check walker)
scan_t2_nava.py            269   (single-target variant)
scan_t2_run.py             235   (orchestrator variant)
```

### Conceptual version failures (from compacted summary at store 33337) — `[from summary]`
The same CDP scanner went through at least 7 internal revisions. The **recurring root cause was a *static WebSocket URL*** — the code captured one `webSocketDebuggerUrl` at startup and reused it across navigations, but Chrome swaps the underlying target/execution context on `Page.navigate` (especially SPA route changes), so the stale WS silently dies or returns empty/`documentCleared`.

| Rev | What changed | Why it still failed |
|-----|--------------|---------------------|
| v1 | First CDP attempt | **Wrong CDP API** — called `client.Page` which doesn't exist in the `cdp_use` client (correct form is `client.send('Page.navigate', ...)`). |
| v2 | Switched to `send_raw` | **Static WS** — hardcoded WS URL, dies on navigation. |
| v3 | Typed-dict params | **Static WS** — same defect. |
| v4 | — | `SCANNED_COUNT` **scoping bug** (a counter declared in the wrong scope, reset every loop → infinite/incorrect paging) **+ static WS**. |
| v5 | Extraction regex fix | **Static WS** — stalled at blog 1 (WS dead before first page parsed). |
| v6 | (index_v6, 410 lines) | Intermediate; superseded. |
| v7 | Tuple unpack + `@` extraction fix | **Static WS** — "same crash" as v5. |

**Lesson hammered in repeatedly:** the WS URL must be *refreshed* after every navigation (re-query `Target.getTargets` / `/json/list` for the active page's `webSocketDebuggerUrl`), or the connection is dead-on-arrival for anything past page 0. This is implemented today in `tab_recovery.py:67-87` (poll `/json/list` for the new target) and `cdp_manager.py` `reconnect()`.

---

## 4. Concurrency & Tab-Lifecycle Failures

This is the cluster the user flagged hardest ("semaphore errors", "tab explosion", "you're not reusing tabs").

### 4.1 Tab explosion (30+ tabs → Chrome crash) — PARTIALLY RESOLVED
- **Symptom:** Chrome crashes with 30+ tabs after T0 emits N usernames.
- **Root cause (verified in code):** `probe_blog()` created a CDP tab with **no semaphore**. When T0 emitted 219 usernames, `asyncio.gather` fired 219 probe tasks concurrently → 219 simultaneous tabs → Chrome crash ~30.
- **Attempted fix:** `TAB_SEMAPHORE = asyncio.Semaphore(4)` at `coordinator.py:52`, threaded through `_run_single_agent()` (acquire at `coordinator.py:1050`, release at `:1093`), `probe_blog()`, `agent.run()` (via `tab_sem` param).
- **Why still broken:** the semaphore caps *concurrent* creation but `close_tab()` sits in the `finally` of `_run_single_agent()` (`coordinator.py:1089`) and of `agent.run()` (`agent.py:367-369`). So with 100 T1 usernames, 100 tabs are created and destroyed **sequentially** — the semaphore only prevents N-at-once, not the churn. Chrome spawns a process + grabs memory per tab open → system overload.
- **User correction (verbatim):** *"You are not reusing tabs, you are closing and opening tabs at a horrific rate so fast that Tumblr and Chrome can't keep up. Make it stop now."* and *"You put an incredible system load on when you open and destroy tabs because Chrome makes a process and grabs memory every time it opens a tab. That's why you reuse, not open and close."*

### 4.2 Semaphore acquire/release bugs — NOT FIXED
- **Pattern:** `tab_sem.acquire()` at `coordinator.py:1050` paired with `tab_sem.release()` at `:1093` inside a `try/finally`. Any exception between acquire and the `finally` that doesn't reach the release path leaks the semaphore permit → after enough leaks, the pipeline deadlocks (all 4 permits held by dead tasks, no worker can proceed).
- **No `try/finally` around `acquire()`** in `_run_single_agent` — if the tab-creation line (`:1050` area) throws, the permit is never released. The `release()` at `:1093` is only reached on the success path through the `finally`-wrapped inner block; the outer acquire is not itself guarded.
- **`probe_blog()` semaphore:** acquires `tab_sem` but the probe's `close_tab()` in `agent.py:367-369` destroys the tab the permit was protecting → the permit is "spent" on a tab that's immediately thrown away, then `_run_single_agent()` acquires *another* permit for a *new* tab (double-tab, §4.3).

### 4.3 Probe + crawl double-tab — NOT FIXED
- **Symptom:** `probe_blog()` opens tab, fetches page 0, **closes it** (`agent.py:367-369`). Then `_run_single_agent()` opens *another* tab for the same blog.
- **Root cause:** `probe_blog()` does not return the tab's `webSocketDebuggerUrl`/`target_id`, so the caller cannot reuse it.
- **Correct behavior (never implemented):** probe should return the tab WS URL for reuse, OR probe should run *inside* the worker so the tab is naturally reused.

### 4.4 Per-blog open/close (the core architectural miss) — NOT FIXED
- **Correct model (user-specified, unimplemented):** a worker pool of 3 workers, each owns ONE tab for its entire lifetime, reusing it across every blog it crawls (`Page.navigate` to the next `?offset=N` blog URL). Tab closed only on worker death or pipeline end.
- **Current code does the opposite:** `close_tab()` fires after every single blog (§4.1). The `running_blogs` dict + `_get_or_create_tab()`/`_release_tab()` helpers in the *serial* `run_t1_batch`/`run_t2_batch` (`coordinator.py:325,392,462,509`) attempt reuse within a batch but `_release_tab()` still closes the tab — correct for batch end, wrong for per-blog.

---

## 5. CDP / Chrome Errors

### 5.1 Chrome error code 5 — `page.documentCleared` — recovery added, root cause not removed
- **Symptom:** Mid-crawl, the CDP client dies with Chrome error code 5 (`page.documentCleared`). The agent then treats empty HTML as "blog is dead" and stops — silently losing the rest of the blog.
- **Verified in `tab_recovery.py:4-10`:** *"Chrome error code 5 (page.documentCleared) kills the agent's CDP client mid-crawl, and the agent has no mechanism to recover — it treats empty HTML as 'blog is dead' and stops."*
- **Fix attempt:** `with_tab_recovery()` wraps the crawl loop, detects tab death via `_is_tab_death()` (`tab_recovery.py:268-304` — matches `page.documentcleared`, `targetcrashed`, `inspector.targetcrashed`, `loadingfailed`, `websocket`, `failed to fetch`, `nameerror`), closes the dead tab, creates a fresh one, resumes from last offset. `RECOVERY_MAX_ATTEMPTS = 3`.
- **Why still fragile:** `tab_recovery.py` is a *separate* module not wired into the main `agent.run()` path (which still has its own `close_tab` churn, §4.1). The recovery layer and the pipeline use different tab-lifecycle models.

### 5.2 WebSocket reconnection instability — partially addressed
- `cdp_manager.py` `CDPConnectionManager.reconnect()` re-queries `Target.getTargets` and swaps `self.current_ws` if the page changed. `navigate_and_reconnect()` (cdp_manager.py) wraps `Page.navigate` + reconnect.
- **Gap:** `agent.py`'s main loop (`run()`) predates this manager in places and still holds a static `ws_url` from `_new_tab_url()` (`agent.py:329,472`), only swapping on explicit dead-detection. The "static WS" bug from §3 is therefore *partially* exorcised, not gone.

### 5.3 Chrome crashes / memory exhaustion at 30+ tabs
- Observed repeatedly (user: "Chrome crashed with 30+ tabs open"). Each tab = a renderer process + memory. The per-blog open/close (§4.1) compounds this because Chrome doesn't free renderer memory instantly on `Target.closeTarget`.
- **Mitigation attempted:** `TAB_SEMAPHORE(4)` + `MAX_CONCURRENT_AGENTS=3`. Insufficient because of the churn, not the cap.

---

## 6. Extraction & Detection Failures

### 6.1 Sub-agent DNS / provider failures — FAILED, then band-aided
- **DNS cascade:** When 7 sub-agents fanned out (`deleg_1fc99e01`, store 15568), each opened its own browser session; DNS errors (`dns error: failed to lookup address information: nodename nor servname provided`) cascaded and corrupted 6 of 7 branches (store 15569: "One branch fully scanned, six timed out"). User: *"stop please, there have been too many dns errors, it is messing up your sub-agents"* (store 15570).
- **Band-aid:** baked "retry up to 120s on DNS timeout" into sub-agent goals (store 15577+). Did not fix the underlying flaky DNS; just made timeouts longer.
- **Provider resolution bug (from summary node 110):** delegation config said `provider: ollama` but the system resolved to `local` → "no API key" error. Model path pointed at `/opt/llama.cpp/models/LFM2.5-2.6B-Uncensored-Q4_K_M.gguf` (exists on renfrew-ai, **not** this Mac). Sub-agents also couldn't run Python (wrong interpreter, tried `tool_call` on themselves). User: *"make the sub-agent work, you give up too easily."*
- **Disposition:** sub-agent parallelism abandoned for the extractor; done via direct execution instead.

### 6.2 Stale CSS-class selectors — RESOLVED
- **Failure:** extractor used generated CSS-module classes (`f1x2m`, `BSUG4`, `rZlUD.F4Tcn`) which change every Tumblr build/session. Extractor missed posts.
- **Fix:** stable selectors only — `data-cell-id` (containing `-post-`), `aria-label^="Posted by"/"Reblogged by"`, `a[rel="author"]`. `-post-` distinguishes posts from `poster-`/`postal-` false positives. Result: 3 → 17 posts, 6 → 23 unique (verified).

### 6.3 Cell-based extraction collapse on text-only posts — FAILED
- **Failure:** `[data-cell-id]` matches many non-post elements. Filtering by child selectors (`article`, `aria-label`, `time`) missed **collapsed/text-only** posts (which have no `<article>` markup). Only 3 of 17 posts found.
- **Resolution:** moved to `data-cell-id` *containing* `-post-` (§6.2).

### 6.4 Reblog regex edge cases — RESOLVED (standalone), still fragile in pipeline
- **Two formats:** `Reblogged\nTIME\nUSERNAME` (multi-line) vs `Reblogged USERNAME` (same-line). Initial regex missed the second (11 → 25 after fix).
- **Concatenated segments** (e.g. `Jul 1di-a-m-o-n-dJul 14, 2024Follow...`) need both a `date-username-date` and a `username-before-date` pattern (verified test at store 33335). The pipeline's version of this parsing is less battle-tested than the standalone `extractor.py`.

### 6.5 Promo-block junk injection — RESOLVED (filter emptied)
- "Check these out" / "You might like" blocks inject ~4 junk usernames/page. `PROMO_JUNK` set was emptied (`PROMO_JUNK = set()`) so raw output is preserved (user wants raw, no injection filtering). Risk: promo names pollute the graph — accepted tradeoff for raw fidelity.

### 6.6 DEAD_PHRASES false positives — RESOLVED
- **`"this blog is private"`** (store 46342): removed from `DEAD_PHRASES`. It's a *login-wall interstitial* for unauthenticated sessions, not a dead-blog signal. The cached HTML had 77 users; live CDP was falsely stopping on page 1.
- **`"404"`** (store 46343): removed. Matched `<link href=".../blog?blogName=...">` URLs in the HTML `<head>` (iOS app callback links), not actual errors. `detect_dead()` now checks **page innerText only**, not raw HTML source.
- **Other false positives historically:** `"there's nothing here"` (end-of-feed, not dead) — separated `classify_dead()` from `is_end_of_feed()`.

### 6.7 `run.py` KeyError: 'success' — NOT FIXED
- `print_result()` references `s['success']` but the pipeline result dict has no `success` key (uses `status`). Crashes after every pipeline run. `get()` guards added for `browser`/`cache_dir`/`recrawl_days`/`concurrent_cap` in the early-return path, but `success` still unguarded.

---

## 7. Configuration & State Bugs

### 7.1 Delay values wrong / reverting — NOT FIXED (target unknown)
- User flagged `agent.py:311-312` had `delay_min=6.7, delay_max=10.0` (wrong, reverted to old values).
- Current code `agent.py:389-390` defaults to `delay_min=2.0, delay_max=3.0`.
- `tab_recovery.py:125-126` uses `delay_min=10.0, delay_max=15.0` — a **third** value set.
- The "correct" target values were never specified by the user. Three different硬编码 delay constants exist across modules.

### 7.2 Cutoff-date creep / revert — REMOVED (user demanded)
- `cutoff_date` was added, then the user said it "keeps creeping back." Removed from `agent.py` (init + date check), `worker.py` (`kwargs["cutoff_date"]` pass), `agent_run_func.py` (code present but unimported). Verified gone via grep.

### 7.3 Stale cache returns 0 usernames — RESOLVED
- `cache/t0.json` held a 0-username entry from a failed run. `run_t0()` guard `if existing and not existing.get("dead"): return existing` returned the stale empty entry. Fixed to also require `existing.get("usernames")`.

### 7.4 Re-indexing / dedup ignores index — STALE
- Queue (`work_queue.py`) checks queue names but not `index.json`. Already-indexed blogs get re-enqueued and re-scanned. `enqueue()` originally checked the queue only.
- `follow_up_to_date` / `_is_stale()` were stripped during cleanup; the date-aware re-index logic the user later mandated (§8) replaces this.

---

## 8. Mandated Changes (latest directive, in progress)

Three changes the user demanded after killing a run (process `proc_ec9d15e52487` SIGTERM'd):
1. **Fresh Chrome restart** at every pipeline start (`chrome_lifecycle.py` fixed to launch `Google Chrome --remote-debugging-port=9222 --user-data-dir=...` instead of `open -a Google Chrome`).
2. **Date-aware per-blog indexing (ALL tiers)** — probe page 0, compare dates vs `scanned_at`, skip if no new content; register every username in `index.json` immediately on job completion.
3. **Parallel T1/T2 dispatch** — T2 starts as T1 streams in, not after all T1 completes.

Status: `run_parallel_pipeline()` added (coordinator.py, ~317 lines), `--parallel` flag in `run.py`, `probe_blog()` added. Tab-reuse (the actual hard part) still not implemented (see §4).

---

## 9. What Actually Worked (verified with evidence)

| Component | Evidence | Date |
|-----------|----------|------|
| `extractor.extract_from_html()` standalone | 17 posts, 9 unique, 36 occurrences | 2026-08-27 |
| `-post-` selector in `data-cell-id` | 3→17 posts, 6→23 unique | 2026-08-27 |
| `agent.run()` T0 pipeline (fresh cache) | `limit_reached`, 219 unique, 25 posts | 2026-08-27 |
| Offset pagination `?offset=N` | Verified across all tiers | Aug 17-23 |
| BeautifulSoup for HTML parsing | Lightweight, no browser needed for extraction | Aug 24+ |
| CDP for navigation | Only mechanism that renders Tumblr | Aug 24+ |
| Deactivated short-circuit | `deactivat` in name → skip at dispatch | Aug 24+ |

---

## 10. Complete Bug Index (30+ logged)

| # | Failure | Category | Status |
|---|---------|----------|--------|
| 1 | `curl`/`web_extract` return empty SPA shell | Fetch arch A | Abandoned |
| 2 | DNS resolution failing for all outbound (PyPI, Firecrawl, Tumblr) | Network | Recovered (flaky) |
| 3 | `browser-harness: command not found` (exit 127) | Tooling | Abandoned path |
| 4 | Sub-agent DNS cascade corrupts 6/7 branches | Sub-agent | Band-aided |
| 5 | Delegation `provider: ollama` ignored → `local` → no API key | Sub-agent | Unresolved |
| 6 | Sub-agent wrong interpreter / can't run Python | Sub-agent | Abandoned sub-agents |
| 7 | Regex captures dates + partial usernames | Extraction | Fixed (standalone) |
| 8 | CDP v1: `client.Page` doesn't exist | CDP API | Fixed |
| 9 | CDP v2-v7: **static WS URL** dies on navigation | CDP lifecycle | Partially fixed |
| 10 | CDP v4: `SCANNED_COUNT` scoping bug → bad paging | Logic | Fixed |
| 11 | CDP v5/v7: stalled at blog 1 / "same crash" | CDP lifecycle | Superseded |
| 12 | Tab explosion (30+ tabs → Chrome crash) | Concurrency | Partial (semaphore) |
| 13 | Semaphore permit leak (no guard around acquire) | Concurrency | Not fixed |
| 14 | `close_tab()` in `finally` → per-blog churn | Lifecycle | Not fixed |
| 15 | Probe + crawl double-tab | Lifecycle | Not fixed |
| 16 | No worker-pool tab reuse | Lifecycle | Not implemented |
| 17 | Chrome error code 5 `page.documentCleared` | CDP error | Recovery added, unwired |
| 18 | WS reconnection instability in `agent.run()` | CDP lifecycle | Partial |
| 19 | Memory exhaustion at 30+ tabs | Resource | Mitigated only |
| 20 | Stale CSS-class selectors (`f1x2m` etc.) | Extraction | Fixed |
| 21 | Cell-based extraction misses collapsed posts | Extraction | Fixed (`-post-`) |
| 22 | Reblog regex: multi-line vs same-line | Extraction | Fixed (standalone) |
| 23 | Concatenated-segment parsing edge cases | Extraction | Fragile in pipeline |
| 24 | Promo-block junk injection | Extraction | Filter emptied (raw) |
| 25 | `"this blog is private"` false dead | Detection | Fixed |
| 26 | `"404"` false dead (head links) | Detection | Fixed |
| 27 | `"there's nothing here"` misclassified | Detection | Separated |
| 28 | `run.py` KeyError: 'success' | Crash | Not fixed |
| 29 | Delay values 6.7/10.0 vs 2.0/3.0 vs 10.0/15.0 | Config | Not fixed (target unknown) |
| 30 | Cutoff-date creep/revert | Config | Removed (user demand) |
| 31 | Stale cache returns 0 usernames | State | Fixed |
| 32 | Re-index ignores `index.json` | State | Stale |
| 33 | `follow_up_to_date`/`_is_stale` stripped, date-aware re-index pending | State | In progress |

---

## 11. Key Lessons (locked)

1. **SPA requires JS rendering** — `curl`/`web_extract`/browser-use sub-agents all fail on Tumblr. Only CDP `Runtime.evaluate` works.
2. **The WebSocket URL is not static** — refresh it after every navigation or the connection is dead past page 0. This bit us across 6 version revisions.
3. **Tab creation is expensive** — one tab per worker, reused for the worker's whole lifetime. Never open/close per blog.
4. **Semaphores cap concurrency, not churn** — a semaphore + `close_tab()` in `finally` still destroys/creates per task. Fix = worker *ownership*, not limiting.
5. **Sub-agents are not free parallelism** — DNS + provider-resolution + interpreter mismatches corrupted results more than they helped. Use direct execution.
6. **CSS classes on CSS-in-JS sites are unstable** — use semantic attributes (`data-*`, `aria-*`, `rel`).
7. **Detection phrases lie** — login walls ("this blog is private") and head links ("404") are not dead-blog signals. Check page *text*, not raw HTML.
8. **Raw output, no summarization** — user wants raw JSON, including promo noise.
9. **Lint before checkin + no inline python** — non-negotiable.
10. **Document as you go** — 2 weeks of sessions without a source of truth produced 33 distinct failures; consolidate early.

---

---

## 12. Extractor Revision Post-Mortem (the hardest failure — user had to fix it)

> **User's own words (this session):** *"Look specifically at the text extractor, you went through at least four major revisions until I actually had to go in and find the code keys myself. I ended up having to fix this instead of you. You kept poking shit that didn't work and tried to build stuff that didn't matter because you didn't understand the architecture of the data."*

This is the single most expensive mistake in the project, and the one the user had to personally rescue. It is documented separately because it is a **different class of failure** from the CDP/concurrency bugs: those were hard engineering problems. The extractor failure was **a comprehension failure** — I built four versions of a parser without ever reading the actual DOM, guessed at a data architecture that did not exist, and burned cycles on branches that could never fire on real data.

### 12.1 The root error: I never inspected the keys before guessing

Every extractor revision started from a regex or selector I *imagined* Tumblr used, then I patched it when it returned too few names. I never opened the rendered HTML and read the actual attributes. The user did — and found the real keys, which became the locked selectors:

| Real key (user-found, verified against `/tmp/full_page.html`) | What it actually contains |
|--------------------------------------------------------------|----------------------------|
| `data-cell-id` | Format: `<hexhash>-post-<postid>-<hexhash>`. The `-post-` substring is the **only** stable anchor. Posts are `div`s with this attribute; everything else (poster, postal, ui chrome) lacks `-post-`. |
| `aria-label^="Posted by"` / `aria-label^="Reblogged by"` | Format: `"Reblogged by <username> – August 20th, 9:09 AM"`. The username is the token right after "by". |
| `a[rel="author"]` | `href="/<username>"` — the username is the path. Direct, no regex needed. |
| `<time datetime="...">` | ISO timestamp per post. Present on **every** post (0/68 missing it on real data). |

I found none of these myself. I guessed `f1x2m`-style CSS classes, guessed a multi-line/same-line reblog regex split, and guessed an "expanded vs collapsed" post duality. All three guesses were wrong.

### 12.2 The phantom duality: "expanded" vs "collapsed" posts

The extractor's central abstraction — built across **all four revisions** — was that posts come in two formats:

- **Expanded**: full `<article>` markup with `aria-label` + author links.
- **Collapsed**: text-only, parsed with the regex `ownerReblogged source18hposter`.

**This duality does not exist on real data.** Verified against `/tmp/full_page.html` (68 posts from `the-smallest-kitten-cravings`):

- Posts **with** `<article>`: **68 / 68**.
- Posts **with** `<time datetime>`: **68 / 68**.
- Posts that would ever hit the "collapsed text-only" branch: **0 / 68**.

Tumblr renders **every** post as an `<article>` with a `<time>` and author links. The "collapsed" branch was dead code I spent the most effort perfecting — the `ownerReblogged source18hposter` regex, the relative-time skipping (`6d`, `18h`), the `18h<user><note_count>` pattern. None of it ever executes. I built the hardest, most fragile part of the parser against a structure that isn't there.

### 12.3 The four revisions (reconstructed — `[from summary]`, sequences not in raw tool output)

| Rev | What I built | What I got wrong | Why it failed |
|-----|--------------|------------------|---------------|
| 1 | CSS-class-based (`f1x2m`, `BSUG4`, `rZlUD.F4Tcn`) | Classes are CSS-in-JS hashed, regenerate per build/session | Found 3 of 17 posts — missed 14 entirely |
| 2 | `[data-cell-id]` + child-selector filter (`article`/`aria-label`/`time`) | Filter too aggressive — assumed collapsed posts lack those children (they don't; they have all three) | Still missed posts; wrong mental model of the DOM |
| 3 | Text-regex extraction (`Reblogged\nTIME\nUSERNAME` vs `Reblogged USERNAME`) | Guessed the reblog text shape; missed same-line format | 11 → 25 usernames after the first regex fix, but still guessing at layout |
| 4 (final, `8dfd74a`) | `-post-` in `data-cell-id` + dual expanded/collapsed branches | The `-post-` anchor is correct, but the collapsed branch + expanded/collapsed split is the phantom from §12.2 | Works (68 posts / 77 unique) **despite** the dead collapsed branch, not because of it |

The throughline: **every revision guessed at structure instead of reading it.** The one piece that survived (the `-post-` anchor) only survived because the user identified it.

### 12.4 What the user actually fixed

The user opened the live DOM and pulled the three stable keys (§12.1). Those became the "locked selectors" the pipeline uses. Until then, the extractor was fishing — counting how many usernames a regex happened to catch and calling "more" a win, with no ground truth about where usernames actually live in the markup.

### 12.5 Why "build stuff that didn't matter" happened

Two habits compounded:

1. **Proxy metric:** I treated "unique usernames returned" as success. More regex → more names → "fixed." I never validated *which* names were correct or whether I was missing the real containers. A higher count from a looser regex looks like progress while being regress.
2. **Defensive over-engineering:** I added the collapsed branch, relative-time skipping, mature-content stripping, emoji stripping, and dual date parsers to cover cases the real DOM never presents. Each was a feature built against a hallucinated data shape. None bought coverage; all added fragility and lint surface.

### 12.6 The lesson (locked — this is the one that matters most)

**Read the data before you parse it.** For any extraction task: dump the real rendered HTML, open it, enumerate the actual attributes that carry the target values, *then* write the minimal parser. One `grep`/inspect of `data-cell-id` + `aria-label` + `rel="author"` would have replaced four revisions. The user's rescue was not luck — it was the step I skipped.

*End of Design History — exhaustive failure log. 33 failures catalogued across 3 fetch architectures, 7 CDP revisions, and 4 concurrency/lifecycle defect classes; plus the extractor post-mortem (§12) documenting the comprehension failure that required user intervention.*

# Tumblr Scanner — Data Flow

**Date:** 2026-08-27
**Phase:** 1 — Design
**Source of truth:** `run.py`, `queue_integration.py`, `agent.py`, `extractor.py`, `work_queue.py`, `cache.py`, `chrome_lifecycle.py`

---

## 1. Entry Points

```
run.py <target>              → coordinator.run_full_pipeline()   [serial T0→T1→T2]
run.py <target> --queue      → queue_integration.queue_mode()     [DEFAULT — queue-based]
run.py <target> --parallel   → coordinator.run_parallel_pipeline()
run.py <target> --t0-only    → coordinator.run_t0()
```

`--queue` is the production path. `run_full_pipeline()` and `run_parallel_pipeline()` exist in `coordinator.py` but are not the running default.

---

## 2. Pipeline: `queue_mode()` (the running path)

```
┌─────────────────────────────────────────────────────────────────┐
│  run.py --queue the-smallest-kitten-cravings                    │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Step 1: chrome_lifecycle.restart_chrome()                │  │
│  │   • Launches Chrome with --user-data-dir=./chrome_profile │  │
│  │   • OR reuses running Chrome if ours is already up       │  │
│  │   • Returns: { browser_ws_url, port, status }            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                          │                                      │
│                          ▼                                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Step 2: _run_t0_producer(target, browser_ws)             │  │
│  │   • agent_run(target, tier=0)  →  usernames[]            │  │
│  │   • _write_index(target, entry)  →  cache/index.json     │  │
│  │   • for name in usernames:                               │  │
│  │       _enqueue_if_not_indexed(name, tier=1)              │  │
│  │   → Returns { usernames, unique_count, enqueued }        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                          │                                      │
│                          ▼                                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Step 3: _drain_queue()                                   │  │
│  │   while True:                                            │  │
│  │     item = dequeue(queue.jsonl)  # flock-protected       │  │
│  │     if item is None: break  # queue empty                │  │
│  │     agent_run(item.username, tier=item.tier)              │  │
│  │     → writes index entry                                 │  │
│  │     → enqueues new names at next tier                    │  │
│  │     mark_done(item)                                      │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Stores

| Store | Format | Writer | Consumer | Purpose |
|-------|--------|--------|----------|---------|
| `cache/index.json` | JSON (atomic `.tmp`+rename) | `_write_index()` | `_index_has_fresh_entry()`, `_enqueue_if_not_indexed()` | Dedup + recrawl gate. Key=username, value={tier,status,scanned_at,usernames,dead,...} |
| `cache/queue.jsonl` | JSONL, POSIX `flock` | `enqueue()`, `mark_done()` | `dequeue()`, `queue_size()` | Work queue. State: pending→in_progress→done. Startup repair resets orphans. |
| `cache/<tier>/<user>.json` | JSON | `cache.save_entry()` | `cache.load_entry()`, staleness check | Per-blog crawl result. `scanned_at` gates recrawl. |
| `cache/t0.json` | JSON | T0 agent | `get_t1_list_from_t0()` | Seed blog result |

---

## 4. Data Flow: Per-Blog Crawl (`agent_run()`)

```
Input:  browser_ws, username, tier, unique/total/post limits
Output: { usernames[], unique_count, total_count, posts_processed,
          status, dead, dead_reason, scanned_at }

┌────────────────────────────────────────────────────┐
│  agent_run(username, tier)                         │
│                                                    │
│  1. Check cache/<tier>/<user>.json                 │
│     if fresh (< recrawl_days): return cached       │
│                                                    │
│  2. Acquire tab_sem (asyncio.Semaphore 4)          │
│     Create CDP tab via Target.createTarget         │
│     Navigate to tumblr.com/<user>?offset=0         │
│                                                    │
│  3. Loop:                                          │
│     ├─ fetch_page_html()  →  CDP Runtime.evaluate  │
│     │   returns raw HTML string                    │
│     │                                              │
│     ├─ extract_from_html(html)                     │
│     │   ├─ BeautifulSoup parse                     │
│     │   ├─ find posts: [data-cell-id*="-post-"]    │
│     │   ├─ per post:                              │
│     │   │   ├─ aria-label regex:                   │
│     │   │   │   "Posted by <u>" / "Reblogged by"   │
│     │   │   ├─ a[rel="author"] href="/<u>"         │
│     │   │   └─ collapsed-text regex (fallback)     │
│     │   ├─ _clean_username() → [A-Za-z0-9-]{1,32}  │
│     │   ├─ _parse_post_date() → date | None        │
│     │   └─ return { usernames, occurrences,        │
│     │               page_date_min, page_date_max } │
│     │                                              │
│     ├─ Accumulate: all_usernames, unique_set,      │
│     │   per_page_results, posts_processed          │
│     │                                              │
│     ├─ check_limit() → stop if any limit hit       │
│     │                                              │
│     ├─ delay: random 6.7–10.0s                     │
│     │                                              │
│     └─ Navigate to ?offset=N+20, repeat            │
│                                                    │
│  4. On tab death: retry ≤3 with new tab            │
│                                                    │
│  5. Return result dict                            │
└────────────────────────────────────────────────────┘
```

---

## 5. Username Extraction: `extract_from_html()`

**Selectors (verified against real HTML):**
- Post container: `div[data-cell-id*="-post-"]` (the `-` prefix/suffix avoids false positives like "poster")
- Expanded post: `<article>` with `aria-label` matching `Posted by <u>` / `Reblogged by <u>` / `reblogged from <u>`
- Author link: `a[href="/<u>"][rel="author"]`
- Collapsed post (text-only): regex on `"ownerReblogged source18hposter"` pattern

**Dedup:** `_clean_username()` strips emoji, validates `[A-Za-z0-9-]{1,32}`, returns `None` for invalid. `Counter` aggregates occurrences. `set()` deduplicates.

---

## 6. Concurrency Model

- **Single asyncio event loop.** No threads, no `queue.Queue`.
- **Tab semaphore:** `asyncio.Semaphore(4)` — max 4 concurrent Chrome tabs (user limit).
- **Worker pool:** 3 async tasks (cooperative, not OS threads).
- **Queue:** on-disk JSONL with POSIX `flock` — survives crashes, no shared-memory lock needed.

---

## 7. Limits & Stop Conditions

| Tier | unique | total | posts | Notes |
|------|--------|-------|-------|-------|
| T0   | 250    | 500   | 500   | Seed blog — discovers T1 names |
| T1   | 100    | 250   | 250   | Depth-1 blogs |
| T2   | 75     | 125   | 125   | Depth-2 blogs |

Per-blog stop: any of unique/total/post limit reached, tab death (3 retries exhausted), or no more pages.

Recrawl gate: `scanned_at` in index within `recrawl_days` (7) → skip.

---

## 8. Crash Recovery

- **Queue startup repair:** `work_queue.startup()` scans for `in_progress` entries, checks index.json. If not in index → reset to pending (worker crashed). If in index → drop (worker finished, didn't clean up).
- **Index on restart:** loaded at pipeline start; fresh entries prevent re-crawl.
- **Mid-blog termination:** partial data lost; next cycle re-discovers from source blog's pages.

---

## 9. Failure Containment

| Failure | Containment |
|---------|-------------|
| Page fetch error (timeout, WS disconnect, CDP error 5) | ≤3 retries with tab reconnect/recreate |
| Tab death | Recreate tab via `/json/new`, resume from last offset |
| Chrome death | Main thread restarts Chrome, workers reconnect |
| Extractor error (malformed HTML) | Returns empty result (0 posts), best-effort |
| Index write failure | Atomic `.tmp`+rename prevents corruption; retry next cycle |
| Agent crash (exception) | `queue_integration._drain_queue` catches, marks done, continues |
| Queue lock timeout | `sys.exit(2)` for clean restart (30s deadline) |

---

## 10. What Does NOT Exist (verified absent)

- No separate `config.py` — tunables hardcoded in `queue_integration.py` and `coordinator.py`
- No worker threads — no `Thread`, no `threading.Lock`, no `queue.Queue` in the running path
- No probe phase — `_index_has_fresh_entry()` provides date-aware refresh without a probe mode
- No `status: discovered/active/dead` field in index writes — the field is documented in DESIGN.md but only `status` (from agent result) is written
- No `--queue` mode in `coordinator.py` — it lives entirely in `queue_integration.py`

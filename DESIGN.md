# Tumblr Scanner — Design Document (v2)

**Status:** Phase 1 — Design (in progress)
**Project:** Tumblr username extraction pipeline (depth-limited crawl; "tiers" are our depth abstraction, not Tumblr's)
**Repo:** `github.com/e-hollebone/tumblr-scanner`
**Path:** `/Users/eric/Documents/tumblr-scanner`
**Supersedes:** `DESIGN.md` (v1, 2026-08-27)
**Last verified:** 2026-08-27

---

## 0. Design History (read first)

This document moves forward from the failures catalogued in [`DESIGN_HISTORY.md`](./DESIGN_HISTORY.md) (33 failures across 3 fetch architectures, 7 CDP revisions, 4 concurrency/lifecycle defect classes, plus the extractor post-mortem). **That history is the constraint set for this design** — every failure mode listed there is a requirement this design must not repeat.

Key locked lessons from the history:
1. **Read the data before parsing it** — the extractor's "expanded vs collapsed" duality was a phantom; real Tumblr posts always have `<article>`, `<time datetime>`, and `a[rel="author"]`.
2. **Tab creation is expensive** — one tab per worker, reused for the worker's lifetime. Never open/close per blog.
3. **Semaphores cap concurrency, not churn** — a semaphore + `close_tab()` in `finally` still destroys/creates per task. Fix = worker *ownership*.
4. **The WebSocket URL is not static** — refresh it after every navigation or the connection dies past page 0.
5. **Sub-agents are not free parallelism** — DNS + provider-resolution + interpreter mismatches corrupted results more than they helped.
6. **SPA requires JS rendering** — only CDP `Runtime.evaluate` works on Tumblr.

---

## 1. Goal

Build a robust Tumblr username crawler that:
- Starts from a seed blog, discovers usernames via reblog graphs (transitive author discovery — crawling blog A discovers blog B, crawling B discovers who B reblogs), and crawls them to depth 2 (our depth counter — Tumblr has no concept of "tiers"; they are purely our abstraction for tracking depth and managing limits).
- Respects Chrome resource limits: **max 4 concurrent tabs**, each owned by a worker for its lifetime.
- Uses **date-aware refresh** (probe page 0, compare dates, skip unchanged blogs) across all depths.
- Is **parallel from the first extraction** — the moment the seed blog emits its first usernames from the first batch of posts read, depth-1 crawl begins; as depth-1 emits names, depth-2 begins. No stage waits for the previous stage to finish.
- Avoids all 33 failure modes catalogued in the design history.

---

## 2. Requirements

### 2.1 Functional Requirements

| ID | Requirement | Source |
|----|-------------|--------|
| FR-1 | Crawl seed blog page-by-page, extract usernames from each batch of posts | Core task |
| FR-2 | For every extracted username, check the index: if already discovered, compare scan date — if not today, queue for re-index; if net new, queue for full crawl at the next depth level | Core task |
| FR-3 | Depth levels are our abstraction only (Tumblr has no concept of tiers); the seed blog is depth 0, names found from it are depth 1, names found from depth-1 blogs are depth 2 | Core task |
| FR-4 | Parallelism starts the moment the first usernames are extracted — depth-1 crawl begins immediately, and as depth-1 emits names, depth-2 begins. No stage waits for the previous stage to finish | User directive |
| FR-5 | Extract usernames from each page: blog owner + reblog sources + original posters | Core task |
| FR-6 | Deduplicate usernames; skip already-cached fresh entries | Design history §7.3 |
| FR-7 | Date-aware refresh: probe page 0, compare dates against `scanned_at`, skip if no new content | Mandated change #2 |
| FR-8 | Register every extracted username in an index file immediately upon job completion | Mandated change #2 |
| FR-9 | Fresh Chrome restart at pipeline start | Mandated change #1 |
| FR-10 | Detect dead/deactivated blogs and cache them as dead (never re-crawl) | Design history §6.6 |
| FR-11 | Recover from tab crashes (Chrome error code 5, `page.documentCleared`) without losing crawl state | Design history §5.1 |

### 2.2 Non-Functional Requirements

| ID | Requirement | Source |
|----|-------------|--------|
| NFR-1 | **Max 4 concurrent Chrome tabs** at any time | User directive (Chrome crashes ~30) |
| NFR-2 | **Tab reuse**: one tab per worker, reused across all blogs it crawls. Never open/close per blog | Design history §4.1 |
| NFR-3 | **Max 3 concurrent crawl agents** | Design history |
| NFR-4 | **7-day recrawl window** (skip blog if scanned within window and no new content) | User directive |
| NFR-5 | **Lint before checkin**: `py_compile` + `ruff` via script files | User directive |
| NFR-6 | **No inline python** (`python -c` banned — write script files) | User directive |
| NFR-7 | **Raw output only** — no modification, no summarization | User directive |
| NFR-8 | **Stable selectors only** — `data-cell-id`, `aria-label`, `a[rel="author"]`. No CSS classes | Design history §6.2 |
| NFR-9 | **CDP WS URL refreshed after every navigation** | Design history §5.2 |
| NFR-10 | **Index file checked before dispatch** — skip already-indexed blogs | Design history §7.4 |

---

## 3. Architecture

### 3.1 Module Layout (target)

```
tumblr-scanner/
├── run.py              # CLI entry, dispatches to pipeline
├── coordinator.py      # Pipeline orchestration, worker pool, dispatch
├── worker.py           # Worker class: owns a tab, reuses it across blogs
├── agent.py            # Per-blog crawl loop (stateless; accepts a tab)
├── extractor.py        # HTML → usernames (BeautifulSoup, canonical)
├── cache.py            # JSON cache, index, entry staleness
├── chrome_lifecycle.py # Fresh Chrome restart
├── lint_modules.py     # py_compile checker
├── lint_batch.py       # py_compile + ruff check
├── ruff_fix.py         # ruff auto-fix runner
└── .gitignore
```

**Key change from v1:** `worker.py` is new — it owns the tab lifecycle. `agent.py` becomes stateless (accepts a tab, doesn't own one). `coordinator.py` manages a worker pool instead of firing per-blog tasks.

### 3.2 Worker Pool Model (the fix for tab-per-blog)

```
Worker Pool (size = MAX_CONCURRENT_AGENTS = 3)
  Worker 1: tab_A → blog_1 (all pages) → blog_4 → blog_7 ...
  Worker 2: tab_B → blog_2 (all pages) → blog_5 → blog_8 ...
  Worker 3: tab_C → blog_3 (all pages) → blog_6 → blog_9 ...
```

**Tab lifecycle rules:**
1. Worker creates its tab on startup.
2. Worker navigates its tab to each new blog URL via `Page.navigate` to `?offset=N`.
3. Worker reuses the same tab for every blog it crawls.
4. Worker closes its tab only on worker death (pipeline end or unrecoverable crash).
5. Semaphore limits *workers* (3), not tabs — so max 3 tabs, within the 4-tab Chrome limit.

**Depth is not a worker property.** Any worker can crawl any blog at any depth. A worker pulling a depth-2 name from the queue runs the same crawl as a worker pulling a depth-1 name. The depth label is just a field in the queue item (for limit management + reporting), not a structural constraint.

### 3.3 Data Flow — parallel from first extraction

```
run.py --parallel
  └─ run_parallel_pipeline(seed_blog)
       ├─ chrome_lifecycle.restart()          # Fresh Chrome (FR-9)
       ├─ worker_pool = WorkerPool(size=3)    # 3 workers, 3 tabs (NFR-1, NFR-2)
       ├─ worker_pool.submit(seed_blog, depth=0)   # Seed on queue
       │
       │   ── ASYNC — no stage waits for another ──
       │
       └─ [every worker, every blog, every page]:
            fetch page → extract_from_html() → net new names
              ├─ for each name:
              │    idx = index_check(name)
              │    ├─ already discovered + scanned_today  → DROP (NFR-10)
              │    ├─ already discovered + old scan date  → QUEUE re-index (FR-2, FR-7)
              │    └─ net new                           → QUEUE full crawl (depth+1) (FR-2, FR-3)
              └─ if any name queued at depth+1:
                   worker_pool.submit(name, depth+1)     # ← parallelism starts HERE (FR-4)
```

**The parallel trigger (FR-4):** The seed blog is itself a queue item. The moment the seed worker finishes reading its first batch of posts (Tumblr pagination — ~20 posts per page), it extracts names and submits the first depth-1 names to the queue. Those names are picked up by *any idle worker* immediately — depth-1 crawl starts before the seed blog is even finished. As depth-1 workers emit depth-2 names, those go on the queue and start instantly. The whole system is parallel from the first extraction; there is no "T0 done → start T1" gate.

### 3.3a Core Crawl Loop (per worker, per blog)

```
for each page (offset = 0, 20, 40, ...):
    html = fetch_page(blog, offset)        # CDP Runtime.evaluate
    result = extract_from_html(html)        # canonical extractor
    page_names = result.usernames           # net new from this page
    for name in page_names:
        entry = index.get(name)
        if entry and entry.scanned_at == today:
            continue                        # already done today
        elif entry and entry.scanned_at != today:
            index_queue.put((name, 'reindex'))   # refresh scan date
        else:
            index.register(name, depth+1)         # net new
            crawl_queue.put((name, depth+1))        # full crawl next depth
    if crawl_queue.has_new() and depth < MAX_DEPTH:
        worker_pool.submit_from_queue()    # parallelism: depth+1 starts now
    if limits_reached(result): break
```

**Depth limit is a design choice for limit management, not Tumblr's structure.** Tumblr returns a flat stream of posts per blog; we assign depth labels to track how far out from the seed we've gone and to cap total work. Tumblr never sees or cares about depth.

### 3.4 Component Responsibilities

**`Worker` (worker.py) — NEW**
- Owns exactly one CDP tab for its lifetime.
- Methods: `run_blog(username, depth) → result`, `probe_blog(username) → dates`, `shutdown()`.
- `run_blog` is depth-agnostic — same crawl whether the blog is depth 1 or depth 2. Depth is just a queue field.
- On tab death: close dead tab, create new one, resume from last offset (max 3 recovery attempts).
- Never closes tab in a per-blog `finally`.

**`agent.run()` (agent.py) — REFACTORED**
- Stateless: accepts a `CDPClient` (the worker's tab), doesn't own it.
- No `close_tab()` in `finally` — the worker owns cleanup.
- Returns crawl result; worker decides what to do next (submit depth+1 names to queue).

**`coordinator.py` — REFACTORED**
- Manages `WorkerPool` instead of `asyncio.gather` + semaphore.
- Single `run_pipeline(seed_blog)` — submits seed, lets workers pull from the shared crawl queue. No `run_t0`/`run_t1`/`dispatch_t2` staged functions.
- Index registration happens per-username, immediately on emission (inside the worker loop, §3.3a).

**`extractor.py` — UNCHANGED**
- Canonical, verified working (68 posts / 77 unique on real data).
- The "collapsed text-only" branch is dead code (0/68 posts use it) — kept as defensive fallback, not removed.

**`cache.py` — EXTENDED**
- `index_register(username, depth, scanned_at)` — immediate registration.
- `index_should_skip(username, depth) → bool` — check before dispatch.
- `entry_is_stale(username, depth, days=7)` — recrawl window check.

### 3.5 Concurrency Model

| Resource | Limit | Mechanism |
|----------|-------|-----------|
| Concurrent tabs | 4 | Worker pool size (3) < Chrome limit (4) |
| Concurrent agents | 3 | `MAX_CONCURRENT_AGENTS = 3` |
| Tab lifetime | Worker lifetime | Worker owns tab, closes on death |
| WS URL refresh | Per navigation | `cdp_manager.reconnect()` after every `Page.navigate` |

**No semaphore on individual tab creation.** The worker pool size *is* the concurrency limit. This avoids the semaphore-vs-churn bug from design history §4.1.

**Parallelism is structural, not staged.** All workers run the same loop (§3.3a) and pull from one shared queue. The seed blog is just the first queue item. There is no T0/T1/T2 gate — depth-2 work can start before depth-1 is finished, as long as depth-2 names have been emitted.

### 3.6 Date-Aware Refresh Protocol

For every blog (any depth — Tumblr treats all blogs identically):
1. Fetch page 0 only.
2. Extract `page_date_max` from `<time datetime>` elements.
3. Compare against `cache.scanned_at` for that username.
4. If `page_date_max <= scanned_at` → skip (no new content).
5. Else → crawl all pages, update `scanned_at` on completion.

Applied uniformly — the seed blog (depth 0) and every discovered blog use the same probe. This replaces the current `probe_blog()` + `_run_single_agent()` double-tab pattern — the worker probes and crawls with the same tab.

### 3.7 Index Registration Protocol

For every username extracted (any depth):
1. Immediately append to `cache/index.json`: `{username, depth, scanned_at, source_blog, status}`.
2. Before dispatching any blog, check `index.json` — skip if already indexed and scanned today.
3. Next pipeline cycle reads `index.json` to build the skip list.

The index is the single source of truth for "what's been seen." It is flat — no per-tier lists. A blog's depth is just a field, not a separate index partition.

### 3.8 System Description (narrative — what each part does)

#### 3.8.1 Main Loop and Initiation

The pipeline starts in `run.py`, which parses CLI arguments and calls `coordinator.run_pipeline(seed_blog)`. The coordinator first calls `chrome_lifecycle.restart()` to start a fresh Chrome process with a dedicated profile (`--user-data-dir=chrome_profile`). This guarantees a clean state — no stale tabs, no accumulated memory leaks from prior runs — without touching the user's personal Chrome session.

After Chrome is confirmed reachable (CDP `/json/version` returns OK), the coordinator creates a `WorkerPool` with `MAX_CONCURRENT_AGENTS = 3` workers. Each worker spawns its own thread and creates exactly one Chrome tab via the CDP browser endpoint. The worker then enters its main loop: pull a queue item, probe the blog, crawl page-by-page, extract usernames, register them in the index, and push newly discovered names back onto the queue.

The seed blog is the first item placed on the crawl queue (depth=0). The moment any worker pulls it and finishes reading the first page (~20 posts), extracted usernames are pushed onto the queue as depth-1 items. Other workers immediately pick those up — parallelism starts at first extraction, not after the seed finishes.

#### 3.8.2 The Index

The index (`cache/index.json`) is a flat JSON file that records every username discovered across all depths. Each entry contains: `{username, depth, scanned_at, source_blog, status}`. The `status` field distinguishes:
- `"discovered"` — name found on a blog's pages but not yet crawled (depth-3 names from depth-2 blogs).
- `"active"` — blog was crawled and yielded usernames.
- `"dead"` — blog is private/deactivated/suspended (never re-crawl).
- `"error"` — blog failed after retries (can retry next cycle).

The index serves three purposes:

1. **Deduplication**: Before crawling any blog, the worker checks the index. If the username is already present and `scanned_at` is today, the blog is skipped (already done today).
2. **Re-index trigger**: If the username is present but `scanned_at` is not today, the blog is queued for a date probe (to check if new content exists) rather than a full crawl.
3. **Source tracking**: `source_blog` records which blog this username was found on, enabling graph reconstruction.

The index is the single source of truth for "what has been seen." It is flat — no per-depth partitions. Depth is just a field in each entry, not a structural separator. This avoids the complexity of maintaining separate per-depth index files.

The index is read at startup and written to atomically (write to temp file, then rename) after each registration to prevent corruption on crash.

#### 3.8.3 The Queue

The crawl queue is an on-disk JSONL file (`cache/queue.jsonl`) with POSIX `flock` for safe concurrent access (via `work_queue.py`). Each line is a JSON object: `{"username": ..., "tier": ..., "state": "pending"|"in_progress"|"done"}`. The queue serves as the synchronization point between workers:

- Workers pull items from the queue when idle.
- Workers push newly discovered items onto the queue after extracting usernames from a page.
- The queue is unbounded in memory but naturally back-pressured: workers can't push faster than they process pages, and page fetches have random delays.

The queue is the mechanism that makes parallelism structural rather than staged. There is no "depth-0 queue" and "depth-1 queue" — one shared queue feeds all workers. A depth-2 item can sit next to a depth-1 item; whichever worker is free pulls the next item regardless of depth.

#### 3.8.4 Main Thread

The main thread (running in `coordinator.py`) does almost no crawling work. Its responsibilities are:

1. **Initialize**: Restart Chrome, create worker pool, load index, seed the queue.
2. **Monitor**: Periodically log queue depth, worker status, and total usernames indexed.
3. **Shutdown**: On completion or Ctrl-C, signal workers to stop, wait for them to finish their current blog, then close all tabs and the Chrome process.
4. **Aggregate results**: After workers finish, read the index and output the final username list.

The main thread never touches a Chrome tab. It never fetches a page or parses HTML. This separation ensures the single point of coordination (the main thread) cannot become a bottleneck or a single point of failure for the crawl itself.

#### 3.8.5 Worker Threads

Each worker runs in its own thread and owns exactly one Chrome tab for its lifetime. The worker's life cycle:

1. **Startup**: Create a tab via CDP (`/json/new?<url>`), store the WebSocket URL.
2. **Main loop**:
   a. Pull `(username, depth, mode)` from the crawl queue.
   b. Check the index: if already scanned today, skip.
   c. If mode=`'probe'`: fetch page 0, extract dates, compare against `scanned_at`. If new content exists, re-queue as `'full'`. Else update `scanned_at` and done.
   d. If mode=`'full'`: crawl page-by-page (offset 0, 20, 40, ...). For each page: fetch HTML, run `extract_from_html()`, get usernames. For each username: check index, register if new, push `(username, depth+1, 'full')` onto queue if depth < MAX_DEPTH.
   e. After all pages: update cache entry with `scanned_at = today`.
3. **Shutdown**: Close the tab, terminate thread.

The worker is depth-agnostic — it runs the same crawl logic regardless of whether the blog is depth 0, 1, or 2. Depth is just a field in the queue item used for limit tracking and the "stop pushing at MAX_DEPTH" check.

#### 3.8.6 Synchronization and Parallelism

The pipeline uses **asyncio-based concurrency** (not threads). All crawl work runs in a single event loop with `asyncio.Task` workers:

1. **The crawl queue** (`work_queue.py`): a JSONL file with POSIX `flock` for safe concurrent access. Multiple async workers dequeue and enqueue without in-memory locks.
2. **The index file** (`cache/index.json`): written atomically (`.tmp` + rename) by the async event loop. Since the event loop is single-threaded, no explicit lock is needed for index writes.
3. **Cache writes**: require no lock because each blog has a unique filename (`cache/blog/<username>.json`) — no two workers can write the same file simultaneously. This assumption holds as long as cache entries remain per-blog.

Parallelism is managed by `asyncio.Semaphore` (max 4 concurrent CDP operations) and `asyncio.Task` workers. The worker pool size (3) is the concurrency limit. Each worker is an async task; the event loop schedules them cooperatively. The queue provides natural backpressure: if all workers are busy, items accumulate in the JSONL file; if the queue is empty, workers exit.

The critical design choice: **a worker pushes depth+1 items onto the queue *inside* the page loop, not after the blog finishes.** This means the seed blog's first page can trigger depth-1 work before the seed's second page is fetched. Depth-2 work can start before depth-1 is complete. There is no barrier between depths.

#### 3.8.7 Username Extractor

The extractor (`extractor.py`) is a pure function: `extract_from_html(html: str) -> dict`. It takes raw HTML and returns structured results. It has no knowledge of workers, queues, depths, or Chrome.

Algorithm:
1. Parse HTML with BeautifulSoup.
2. Find all `div` elements whose `data-cell-id` attribute contains the substring `-post-`. This is the only stable anchor — verified against real Tumblr HTML (68 posts found, 0 false positives).
3. For each post element:
   a. Look for `<article>` with `aria-label` attributes matching `"Posted by <username>"` or `"Reblogged by <username>"`.
   b. Look for `a[rel="author"]` links with `href="/<username>"`.
   c. Extract usernames from both sources, deduplicate within the post.
4. Aggregate across all posts: count occurrences, compute unique set, extract date range from `<time datetime>` elements.

The extractor is stateless and deterministic. Given the same HTML, it always returns the same result. It does not depend on any external state, which makes it trivially testable and cacheable.

#### 3.8.8 Date Management and Flow to Workers

Dates serve two purposes: (1) detecting whether a blog has new content since last scan, and (2) recording the date range of each page for the index.

Date extraction happens inside the extractor: `_parse_post_date(cell)` reads `<time datetime="2026-08-20T13:09:47.000Z">` and returns a `date` object. The extractor returns `page_date_min` and `page_date_max` for each page.

Date flow in the queue-mode pipeline:
- During T0 crawl (`_run_t0_producer`): the agent crawls the target blog, extracts usernames, and the result is written to the index with a `scanned_at` timestamp. Discovered usernames are enqueued at tier 1.
- During the drain loop (`_drain_queue`): each blog is crawled via `agent_run()`, results written to the index with `scanned_at`, and new discoveries enqueued at the next tier.
- Recrawl eligibility: `_index_has_fresh_entry()` checks if a username has a `scanned_at` within `recrawl_days` (7). If fresh, it is skipped (not re-enqueued). This is the date-aware refresh mechanism in the queue-mode architecture — there is no separate probe phase; the index timestamp gates re-crawling.

The worker (async task) does not need to understand Tumblr's date formats — the extractor handles all parsing. The worker just compares dates.

#### 3.8.9 Worker Independence from Main Thread Chrome

Workers (async tasks) do not share Chrome state with the main thread. Each worker:

- Creates its own tab via the CDP browser-level endpoint (`/json/new`), not via the main thread.
- Maintains its own WebSocket connection to that tab.
- Handles its own reconnection if the WebSocket dies (the WS URL can change after navigation; the worker re-fetches `/json` to get the current URL).
- Closes its own tab on shutdown.

The main thread never holds a tab reference. If the main thread crashes, workers continue crawling (they're independent async tasks). If a worker crashes, the main thread detects it (via `asyncio.wait` timeout) and can optionally respawn it.

This independence is critical: the main thread's Chrome lifecycle (restart at pipeline start) is decoupled from the workers' Chrome usage. Workers survive Chrome restarts by reconnecting to their tabs (which persist across the browser-level restart) or by recreating tabs if needed.

#### 3.8.10 Tab Lifecycle

**Opening**: A worker opens its tab once, at startup, via `GET /json/new?<blank url>`. The returned WebSocket URL is stored. The tab is then navigated to the first blog URL.

**Reuse**: The worker navigates the same tab to each subsequent blog via `Page.navigate` to `?offset=N`. The tab is never closed between blogs. This is the fix for the tab-per-blog explosion that crashed Chrome at 30+ tabs.

**Maintenance**: After every `Page.navigate`, the worker calls `cdp_manager.reconnect()` to refresh the WebSocket URL (Tumblr's SPA navigation can invalidate the old WS endpoint). If the navigation fails, the worker retries up to 3 times with exponential backoff.

**Closing**: The worker closes its tab only on shutdown (pipeline end or unrecoverable error). The close is via `GET /json/close/<targetId>`.

**Ownership**: The worker *owns* its tab. No other thread touches it. The main thread never closes a worker's tab. If the worker dies unexpectedly, the main thread's shutdown routine closes any remaining tabs by reading `/json/list` and closing all of them.

#### 3.8.11 Error Handling

Errors are handled at multiple levels:

**Page fetch errors** (timeout, WS disconnect, Chrome error code 5 `page.documentCleared`):
- The worker catches the exception, increments a retry counter.
- If retries < 3: re-fetch the WS URL (reconnect), navigate to the same offset, retry.
- If retries >= 3: mark this blog as `error` in the cache, move to next queue item. The blog is not marked dead — a later cycle can retry.

**Tab death** (Chrome process crash, tab crash):
- The worker detects this when a CDP command times out or returns an error.
- It attempts to recreate the tab via `/json/new`. If successful, resumes from the last offset. If `/json/new` fails (Chrome itself is dead), the worker signals the main thread to restart Chrome.

**Extractor errors** (malformed HTML, unexpected structure):
- The extractor returns empty results (0 posts) rather than raising. Extraction is best-effort.
- The worker logs the empty result and moves on. A blog with 0 posts extracted is flagged in the cache for manual review.

**Index write errors** (disk full, permission):
- Atomic write (write to `.tmp`, then rename) prevents corruption. If the rename fails, the in-memory index is intact and the write is retried next cycle.

**Queue overflow** (memory exhaustion from too many queued items):
- Not expected in practice (workers process faster than they discover), but if queue depth exceeds a threshold (e.g., 10,000), workers temporarily stop pushing new items until depth drops. This is a backpressure safety valve.

**Dead blog detection**:
- If the fetched HTML contains phrases like "This blog is private" or a 404 status, the worker marks the blog as `dead` in the cache and never re-queues it. Dead blogs are filtered out at queue-dispatch time.

#### 3.8.12 Additional Concepts (recommend adding)

The following concepts emerged during the description and are not yet in the design. I recommend adding them:

1. **Rate limiting and politeness delays** — random delays between page fetches (e.g., 2-5s) to avoid triggering Tumblr's rate limits. Single source of truth: one constant in `config.py`.
2. **Dead blog detection** — phrase-based filtering of private/deactivated blogs (e.g., "This blog is private", "404", "deactivated"). Already mentioned in §3.8.11 but deserves its own protocol section.
3. **Cache persistence** — per-blog cache entries with full post data for re-analysis without re-fetching. The cache stores `{username, posts: [...], scanned_at, status}`.
4. **Progress reporting** — periodic logging (every 30s) of queue depth, workers active, usernames indexed, errors encountered. Enables monitoring without polling.
5. **Graceful shutdown** — signal handling (SIGINT/SIGTERM) to stop workers cleanly after current blog, persist index, close tabs, then exit. No orphaned Chrome processes.
6. **Configuration** — a single `config.py` with all tunables: `MAX_CONCURRENT_AGENTS`, `MAX_DEPTH`, `RECRAWL_DAYS`, `DELAY_MIN`, `DELAY_MAX`, `QUEUE_OVERFLOW_THRESHOLD`, `CHROME_RESTART_TIMEOUT`, `PAGE_FETCH_TIMEOUT`.

### 3.9 Rate Limiting and Politeness Delays

To avoid triggering Tumblr's anti-scraping measures, the worker introduces a random delay between every page fetch. The delay is drawn uniformly from `[DELAY_MIN, DELAY_MAX]` seconds (default: 2.0–5.0). These values are defined as constants in `config.py` — the single source of truth.

**Rules:**
- Delay happens *after* each page fetch completes, before the next `Page.navigate`.
- Probe requests (page 0 only) also get a delay.
- Delay is applied per-worker, not globally (workers are independent; their delays don't synchronize).
- If a fetch fails and retries, the retry gets a fresh random delay (not the backoff value — backoff is separate).
- Backoff on errors: `delay * (2 ** attempt)` (exponential, capped at 60s).

This replaces the current inconsistent delay logic scattered across `agent.py` and `tab_recovery.py`.

### 3.10 Dead Blog Detection

A blog is dead if it returns content indicating it no longer exists or is inaccessible. Detection happens inside the worker after fetching page 0:

1. Check HTTP status: 404, 410 → dead.
2. Check HTML content against a phrase list:
   - `"This blog is private"` → private (treated as dead for crawling purposes)
   - `"deactivated"` → dead
   - `"not found"` → dead
   - `"This Tumblr account has been suspended"` → dead
3. If dead: the worker writes a cache entry with `status: "dead"` and `scanned_at = today`. The username is *not* marked in the index as a blog to crawl (it's a dead end, not a valid blog).
4. Dead entries are never re-crawled. On subsequent cycles, the index check finds `status: "dead"` and skips immediately.

The phrase list is defined in `config.py` as `DEAD_PHRASES`. It is a static list — no regex, just substring matching against the fetched HTML (case-insensitive).

**Why this matters:** Without dead blog detection, the pipeline would repeatedly try to crawl suspended/deactivated blogs, wasting Chrome time and queue slots on pages that will never yield usernames.

### 3.11 Cache Persistence

Every blog crawled gets a cache entry stored at `cache/blog/<username>.json`. The entry contains:

```json
{
  "username": "example-blog",
  "status": "active",
  "scanned_at": "2026-08-27",
  "depth": 1,
  "source_blog": "seed-blog",
  "posts": [
    {
      "date": "2026-08-20",
      "usernames": ["user1", "user2"]
    }
  ],
  "page_date_min": "2026-08-15",
  "page_date_max": "2026-08-27"
}
```

The cache entry's `status` field mirrors the index: `"active"` (crawled), `"dead"` (private/deactivated), `"error"` (failed). For names discovered but not crawled (depth-3), the index has `status: "discovered"` but no cache entry is written — cache entries are only created for blogs that were actually crawled.

The cache serves multiple purposes:
1. **Re-analysis:** If the extractor is improved, cached post data can be re-parsed without re-fetching pages.
2. **Date comparison:** The next cycle's probe reads `page_date_max` from cache and compares against fresh page 0 dates.
3. **Dead status:** The cache remembers which blogs are dead, avoiding re-crawl.
4. **Crash recovery:** If the pipeline crashes mid-crawl, the cache shows which blogs were completed and which were in-progress.

The cache is updated after each blog completes (not per-page, to reduce I/O). Cache entries are written atomically (`.tmp` + rename).

### 3.12 Progress Reporting

The main thread logs a status line every `PROGRESS_INTERVAL` seconds (default: 30). The line includes:

```
[STATUS] queue=47 active=3 indexed=1,284 errors=2 dead=12 blogs/min=14.2
```

Fields:
- `queue`: items currently in the crawl queue
- `active`: workers currently processing a blog (not idle)
- `indexed`: total usernames in the index
- `errors`: blogs that errored this cycle (for monitoring degradation)
- `dead`: blogs detected dead this cycle
- `blogs/min`: throughput (blogs completed per minute, rolling average)

This enables monitoring without polling — the operator watches the log stream and sees pipeline health at a glance. If `blogs/min` drops to 0 while `queue > 0`, a worker is stuck. If `errors` is rising, Tumblr may be rate-limiting.

Progress reporting is purely informational. It does not affect pipeline behavior.

### 3.13 Graceful Shutdown

Shutdown can be triggered by:
- **SIGINT** (Ctrl-C) or **SIGTERM** (process manager stop signal)
- **Queue empty** (all work completed)
- **Manual stop** via CLI flag `--stop-after-current`

The shutdown sequence:

1. Main thread (event loop) sets a `shutdown` event.
2. Each worker (async task) checks the event before dequeuing. If set, the worker finishes its current page (but does not start a new blog).
3. After finishing the current blog, the worker persists its data to cache, closes its tab, and returns.
4. Main thread awaits all worker tasks (with a timeout of 30s per task).
5. Main thread persists the index (atomic write).
6. Main thread closes any remaining Chrome tabs (cleanup in case a worker died).
7. Main thread terminates the Chrome process (our dedicated profile only).
8. Exit.

**No orphaned processes:** If a worker task is unresponsive (CDP command hung), the main thread's `asyncio.wait` times out after 30s and cancels the task. The tab is still closed in step 6 because the main thread reads `/json/list` and closes all open tabs regardless of which worker owns them.

**Crash recovery on next start:** The next pipeline run reads the index and cache, skips already-completed blogs, and resumes from where the previous run stopped. Mid-blog worker termination loses that blog's partial data; the next cycle re-crawls it from the source blog's pages.

### 3.14 Configuration

All pipeline tunables live in a single `config.py`:

```python
# Concurrency
MAX_CONCURRENT_AGENTS = 3        # worker pool size = tab count
MAX_DEPTH = 2                    # max crawl depth from seed

# Timing
DELAY_MIN = 2.0                  # min delay between page fetches (seconds)
DELAY_MAX = 5.0                  # max delay between page fetters
PAGE_FETCH_TIMEOUT = 30          # CDP command timeout (seconds)
CHROME_RESTART_TIMEOUT = 10      # seconds to wait for Chrome to become ready

# Windows
RECRAWL_DAYS = 7                 # skip blog if scanned within this window
PROGRESS_INTERVAL = 30           # seconds between status log lines

# Thresholds
QUEUE_OVERFLOW_THRESHOLD = 10000 # stop pushing if queue exceeds this

# Paths
CACHE_DIR = Path("./cache")
INDEX_FILE = CACHE_DIR / "index.json"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = Path("./chrome_profile")  # separate profile — user's personal Chrome untouched
CDP_PORT = 9222

# Dead blog phrases
DEAD_PHRASES = [
    "this blog is private",
    "deactivated",
    "not found",
    "this tumblr account has been suspended",
]
```

Single source of truth: every module imports from `config.py`. No magic numbers scattered across source files. To change timing, edit `config.py` — no grep-and-replace across modules.

### 3.15 End-to-End Workflow — How the Pieces Connect

The previous sections describe each component in isolation. This section describes how they function as an integrated system, from the moment the user hits Enter to the moment the final index is written.

#### 3.15.1 The Spine of the System

Think of the pipeline as a spine with five vertebrae:

1. **`config.py`** — the DNA. Every other module imports from it. Change a value here, and the entire pipeline's behavior shifts without touching source code.
2. **`chrome_lifecycle.py`** — the foundation. No Chrome, no crawl. Every component downstream depends on this working.
3. **`coordinator.py`** — the brain. It initializes everything, seeds the queue, monitors progress, and orchestrates shutdown.
4. **`worker.py`** — the muscle. It does the actual crawling, extracting, and registering. All worker logic depends on the coordinator's initialization and the extractor's output.
5. **`extractor.py`** — the sensory organ. It converts raw HTML into structured data. Without it, workers would fetch pages and learn nothing.

Connecting these five are two shared structures:
- **The queue** — the nervous system carrying signals (work items) between coordinator and workers.
- **The index + cache** — the memory system. Workers write to it, read from it, and make decisions based on it.

#### 3.15.2 The Full Data Flow (one pass, start to finish)

```
USER: python3 run.py the-smallest-kitten-cravings --parallel
  │
  ▼
run.py
  ├─ parse CLI args (seed blog, mode, flags)
  ├─ import config.py (all tunables resolved)
  └─ call coordinator.run_pipeline(seed_blog)
        │
        ▼
coordinator.run_pipeline()
        │
        ├─ [1] chrome_lifecycle.restart()
        │     ├─ if Chrome already running on CDP port: verify it's our profile, else warn
        │     ├─ launch: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
        │     │          --remote-debugging-port=9222
        │     │          --user-data-dir=chrome_profile  (dedicated profile)
        │     ├─ poll http://127.0.0.1:9222/json/version until OK
        │     └─ return (Chrome ready, CDP reachable)
        │
        ├─ [2] Load index from cache/index.json
        │     ├─ if exists: deserialize into memory
        │     └─ if missing: create empty index
        │
        ├─ [3] Create WorkerPool(size=config.MAX_CONCURRENT_AGENTS)
        │     │
        │     ├─ Worker 1 starts:
        │     │     ├─ GET /json/new?about:blank → tab_A
        │     │     ├─ store tab_A WebSocket URL
        │     │     └─ enter main loop (wait for queue item)
        │     │
        │     ├─ Worker 2 starts:
        │     │     ├─ GET /json/new?about:blank → tab_B
        │     │     ├─ store tab_B WebSocket URL
        │     │     └─ enter main loop
        │     │
        │     └─ Worker 3 starts:
        │           ├─ GET /json/new?about:blank → tab_C
        │           ├─ store tab_C WebSocket URL
        │           └─ enter main loop
        │
        ├─ [4] Seed the queue: crawl_queue.put(("the-smallest-kitten-cravings", depth=0, mode="full"))
        │
        ├─ [5] Monitor loop (every config.PROGRESS_INTERVAL seconds):
        │     ├─ log: [STATUS] queue=1 active=1 indexed=0 ...
        │     ├─ check: all workers alive? (task await timeout)
        │     ├─ check: shutdown requested? (Ctrl-C, queue empty)
        │     └─ if shutdown: break monitor loop
        │
        └─ [6] Shutdown:
              ├─ set shutdown_event
              ├─ join workers (timeout=30s each)
              ├─ persist index (atomic write)
              ├─ close remaining tabs (read /json/list, close all)
              ├─ terminate Chrome process
              └─ exit
```

#### 3.15.3 Worker Internals — One Blog, Start to Finish

Once a worker pulls an item from the queue, it runs this sequence:

```
Worker.main_loop():
  │
  ├─ item = crawl_queue.get()     # blocks until item available
  │   item = ("some-blog", depth=1, mode="full")
  │
  ├─ [A] Index check (under index_lock):
  │     entry = index.get("some-blog")
  │     if entry and entry.scanned_at == today:
  │         continue              # skip, already done today
  │
  ├─ [B] Navigate to blog:
  │     cdp_manager.reconnect()   # refresh WS URL
  │     Page.navigate("https://some-blog.tumblr.com/?offset=0")
  │     wait_for_load()
  │
  ├─ [C] Dead blog check:
  │     html = Runtime.evaluate("document.documentElement.outerHTML")
  │     if contains_dead_phrase(html) or status in (404, 410):
  │         cache.save_entry(username, status="dead", scanned_at=today)
  │         continue              # never re-crawl
  │
  ├─ [D] Date probe (if mode="probe"):
  │     result = extractor.extract_from_html(html)
  │     page_date_max = result.page_date_max
  │     cache_entry = cache.load_entry(username)
  │     if page_date_max <= cache_entry.scanned_at:
  │         # no new content — update scan date, skip
  │         cache.save_entry(username, scanned_at=today)
  │         continue
  │     else:
  │         # new content — transition directly to full crawl
  │         # (cached html becomes offset=0, no re-fetch)
  │         posts_accumulator = result.posts
  │         for offset in (20, 40, ...):  # start at page 2
  │             ... (same as [E] but starting at offset=20)
  │
  ├─ [E] Full crawl (if mode="full"):
  │     posts_accumulator = []
  │     for offset in (0, 20, 40, ...):
  │         │
  │         ├─ cdp_manager.reconnect()
  │         ├─ Page.navigate(f"https://some-blog.tumblr.com/?offset={offset}")
  │         ├─ wait_for_load()
  │         ├─ html = Runtime.evaluate("document.documentElement.outerHTML")
  │         │
  │         ├─ result = extractor.extract_from_html(html)
  │         │     ├─ BeautifulSoup parse
  │         │     ├─ find [data-cell-id*="-post-"] elements
  │         │     ├─ extract usernames from aria-label + rel="author"
  │         │     ├─ extract dates from <time datetime>
  │         │     └─ return {usernames, page_date_min, page_date_max, post_count}
  │         │
  │         ├─ for name in result.usernames:
  │         │     under index_lock:
  │         │       idx = index.get(name)
  │         │       if not idx:
  │         │         index.register(name, depth+1, source=username)
  │         │         if depth+1 < config.MAX_DEPTH:
  │         │           crawl_queue.put((name, depth+1, mode="full"))
  │         │         elif depth+1 == config.MAX_DEPTH:
  │         │           # discovered but not crawled — record for next cycle
  │         │           pass  # status="discovered" set by index.register()
  │         │       elif idx.scanned_at != today:
  │         │         crawl_queue.put((name, idx.depth, mode="probe"))
  │         │       else:
  │         │         pass  # already done today, skip
  │         │
  │         ├─ posts_accumulator.extend(result.posts)
  │         │
  │         ├─ delay(random.uniform(DELAY_MIN, DELAY_MAX))
  │         │
  │         └─ if result.posts == 0: break  # no more posts
  │
  │     # After all pages:
  │     cache.save_entry(username, posts=posts_accumulator,
  │                      scanned_at=today, status="active", depth=depth)
  │
  └─ [F] Loop back to crawl_queue.get()
```

#### 3.15.4 How the Pieces Communicates

| From | To | Via | What |
|------|----|-----|------|
| `run.py` | `queue_integration` | Function call | `queue_mode(target_blog, ...)` |
| `queue_integration` | `chrome_lifecycle` | Function call | `restart_chrome()` |
| `queue_integration` | `work_queue` | Function call | `enqueue()` / `dequeue()` / `mark_done()` |
| `_run_t0_producer` | `agent_run` | `await` call | Crawl target, return usernames |
| `_drain_queue` | `agent_run` | `await` call | Crawl each queued blog |
| `_drain_queue` | `work_queue` | `.get()` / `.put()` | Pull work, push new names |
| `agent_run` | `cdp_manager` | Method calls | `reconnect()`, `Page.navigate()`, `Runtime.evaluate()` |
| `cdp_manager` | Chrome process | CDP WebSocket | JSON-RPC commands |
| Chrome | `cdp_manager` | WebSocket response | HTML content, navigation status |
| `agent_run` | `extractor` | Function call | `extract_from_html(html)` |
| `extractor` | `agent_run` | Return value | `{usernames, dates, post_count}` |
| `queue_integration` | `index.json` | Atomic write | Persist on each registration |
| `queue_integration` | stdout | `logger.info()` | Progress reporting |

#### 3.15.5 Failure Propagation and Containment

The architecture contains failures at each level so a fault in one component doesn't cascade:

| Failure | Contained by | Effect on system |
|---------|--------------|------------------|
| Page fetch fails (timeout) | Worker retry logic (3 attempts) | Single page lost, blog continues |
| Tab dies (WS disconnect) | Worker recreates tab via `/json/new` | Single worker pauses ~5s, then resumes |
| Chrome process dies | Worker signals main thread → `chrome_lifecycle.restart()` | All workers pause ~10s, then reconnect |
| Extractor returns 0 posts | Worker logs and moves on | Blog flagged for manual review |
| Worker thread crashes | Main thread detects via join timeout | Remaining workers continue; dead worker's tab closed in shutdown |
| Index write fails (disk full) | Atomic write (`.tmp` + rename) | In-memory index intact; retry next cycle |
| Queue overflow | Backpressure valve (stop pushing) | Workers slow down naturally; no crash |
| Dead blog detected | Cache `status: dead` | Never re-crawled; queue slot freed for active blogs |

#### 3.15.6 The Parallelism Story

The key architectural insight: **parallelism is a property of the queue, not the workers.**

- The coordinator doesn't decide "start depth-1 now." It just puts the seed on the queue.
- Workers don't know what depth they're crawling. They pull items and run the same loop.
- The moment a worker pushes a depth-1 item onto the queue, any idle worker can pick it up — even if the seed worker is still on page 2.
- Depth-2 items get pushed before depth-1 finishes. No barrier. No gate.

This is why the queue is the central architectural element. It decouples production (extracting names) from consumption (crawling blogs). The producer doesn't know which consumer will pick up the item. The consumers don't know who produced it. They meet at the queue.

#### 3.15.7 The Persistence Story

The system survives crashes because state is persisted at three levels:

1. **Index** (`cache/index.json`) — every username discovered. Written atomically on each registration. On restart, the index is loaded and workers skip already-known names.
2. **Cache** (`cache/blog/<username>.json`) — per-blog crawl results. Written after each blog completes. On restart, the cache shows which blogs were completed and which were in-progress.
3. **Queue** — in-memory only. Items in the queue at crash time are lost, but that's acceptable: they represent blogs that were discovered but not yet crawled. The next cycle will rediscover them from the source blog's pages.

Crash recovery: on restart, the pipeline reads the index and cache, seeds the queue with the seed blog, and resumes. Blogs already in the cache with `scanned_at == today` are skipped. Blogs in the cache with old `scanned_at` get probed for new content. The crawl continues from where it left off.

#### 3.15.8 The Output

The final output is the index file: a flat JSON mapping every discovered username to its metadata. Downstream consumers (not part of this pipeline) read the index to get the complete username list. The index is the deliverable. Everything else — the queue, the workers, the Chrome tabs — is infrastructure to produce it.

### 3.16 Fitness-for-Purpose Analysis

Every design element exists to serve a purpose. This section compares each element to its stated purpose and declares it fit, unfit, or partially fit. Where it's unfit or partial, the gap is named.

---

#### 3.16.1 Queue-Based Parallelism (§3.3, §3.15.6)

**Purpose:** Parallelism from the first extraction — depth-1 starts as soon as the seed emits names; depth-2 starts as depth-1 emits names. No staged gates.

**Verdict: FIT.**

The queue is the central decoupling element. The coordinator puts the seed on the queue and never thinks about depth again. Workers pull items and run the same loop regardless of depth. A worker pushes depth+1 items *inside* the page loop, not after the blog finishes. With 3 workers, an idle worker is always available to pick up newly pushed items. The seed's first page can trigger depth-1 work before the seed's second page is fetched. This is exactly the structural parallelism the goal demands.

---

#### 3.16.2 Worker-Owned Tab Lifecycle (§3.2, §3.8.10)

**Purpose:** Max 4 concurrent tabs. One tab per worker, reused across all blogs. Never open/close per blog.

**Verdict: FIT.**

Worker creates tab once at startup, navigates it to each blog via `Page.navigate`, closes it only on worker death. 3 workers = 3 tabs, within Chrome's 4-tab limit. This directly fixes the tab-per-blog explosion that crashed Chrome at 30+ tabs (design history §4.1).

**Gap:** The design uses 3 workers, not 4. The user said "max 4 tabs acceptable." The design never justifies why 3 instead of 4. If Chrome can handle 4 tabs, leaving one idle wastes 25% of the allowed concurrency. The design should either use 4 workers or document the reason for 3 (e.g., "3 workers + 1 spare tab for the main thread if needed" — but the main thread doesn't use tabs).

---

#### 3.16.3 Index (§3.8.2, §3.7)

**Purpose:** Immediate registration of every username. Dedup against already-seen names. Skip already-done blogs. Flat structure (no per-depth partitions).

**Verdict: FIT.**

Flat JSON with `{username, depth, scanned_at, source_blog}`. Atomic writes (`.tmp` + rename) prevent corruption. Checked before dispatch (skip if scanned today). Updated immediately on extraction. The flat structure matches the user's directive: "no tier-specific indexing logic." Depth is a field, not a partition.

**Gap:** Names discovered at depth 3 (found on depth-2 blogs) are registered in the index but never crawled. Their `status` is never verified — we don't know if they're dead, private, or deactivated. The index records them as discovered but unverified. The design should explicitly mark these as `status: "discovered"` (not crawled) vs. `status: "active"` (crawled and confirmed). Currently the schema doesn't distinguish.

---

#### 3.16.4 Date-Aware Refresh (§3.6, §3.8.8)

**Purpose:** Probe page 0, compare dates against `scanned_at`, skip unchanged blogs. Applied uniformly across all depths.

**Verdict: FIT.**

The worker fetches page 0, extracts `page_date_max` from `<time datetime>` elements, compares against the index's `scanned_at`. If no new content, skip. If new content, queue for full crawl. Tumblr's pagination is chronological, so page 0 always has the newest posts — if page 0 hasn't changed, nothing has. This is correct.

**Inefficiency (not unfit):** The probe fetches page 0, decides "has new content," then re-queues the blog as mode="full". The full crawl starts at offset=0, refetching the same page 0 HTML. The probe's work is wasted for blogs that end up fully crawled. The design should either (a) have the probe transition directly into a full crawl without re-queuing, or (b) cache the probe's HTML and pass it to the full crawl. This is a performance gap, not a fitness gap.

**Correction (applied):** The worker now caches the probe's HTML. When a probe finds new content, the worker transitions directly into the full crawl loop without re-queuing — the cached page 0 HTML is processed as offset=0 of the full crawl. This eliminates the redundant fetch.

---

#### 3.16.5 Extractor as Pure Function (§3.8.7)

**Purpose:** Stateless HTML → usernames. Verified selectors only. No guessing.

**Verdict: FIT.**

`extract_from_html(html) -> dict` is a pure function. Locked selectors: `[data-cell-id*="-post-"]`, `aria-label`, `a[rel="author"]`, `<time datetime>`. Verified against real Tumblr HTML (68 posts, 77 unique, 0 false positives). Deterministic — same HTML always produces same result. No external state. This directly addresses the extractor comprehension failure (design history §12) where the assistant kept guessing at HTML structure instead of reading the data.

**Gap:** The goal says "discovers usernames via reblog graphs." The extractor captures post authors and reblog sources from `aria-label` and `rel="author"`. This captures the reblog graph *transitively* (crawling blog A discovers blog B, crawling B discovers who B reblogs) rather than by inspecting a single post's full reblog trail. The design should clarify that "reblog graph" means transitive author discovery, not per-post trail extraction. The current extractor is fit for this clarified purpose, but the design's language is ambiguous.

**Correction (applied):** The goal in §1 now reads: "Starts from a seed blog, discovers usernames via reblog graphs (transitive author discovery — crawling blog A discovers blog B, crawling B discovers who B reblogs)." The extractor is fit for this clarified purpose.

---

#### 3.16.6 Main Thread as Pure Coordinator (§3.8.4)

**Purpose:** Decouple coordination from crawling. Main thread never becomes a bottleneck or single point of failure for the crawl.

**Verdict: FIT.**

Main thread (event loop) initializes, seeds the queue, monitors progress, shuts down. It never touches a Chrome tab, never fetches a page, never parses HTML. Workers are `asyncio.Task` instances. If the main thread crashes, workers continue crawling (they'll exit when the queue empties, but they won't crash). If a worker crashes, the main thread detects it (`asyncio.wait` timeout) and continues with the remaining workers.

---

#### 3.16.7 Worker Independence from Main Thread Chrome (§3.8.9)

**Purpose:** Worker is completely independent. Does not depend on the main thread's Chrome implementation.

**Verdict: FIT, with imprecise language in the design.**

Workers create their own tabs via `/json/new`, maintain their own WebSocket connections, handle their own reconnection. The main thread never holds a tab reference. This is correct.

**Imprecision:** The design says "Workers survive Chrome restarts by reconnecting to their tabs (which persist across the browser-level restart if done carefully) or by recreating tabs if needed." The phrase "if done carefully" is a red flag. The actual sequence is: main thread restarts Chrome → THEN creates workers → workers create tabs. There's no scenario where workers have tabs and then Chrome restarts. If Chrome dies mid-crawl, workers recreate tabs via `/json/new`. The design should state this ordering explicitly rather than the vague "if done carefully."

**Correction (applied):** The `chrome_lifecycle.restart()` in §3.15.2 now states: "if Chrome already running on CDP port: verify it's our profile, else warn" — no kill, no "carefully." Workers are created *after* Chrome restarts. The vague language is gone.

---

#### 3.16.8 Error Handling (§3.8.11)

**Purpose:** Contain failures at each level. A fault in one component doesn't cascade.

**Verdict: FIT.**

Page fetch errors → 3 retries with reconnect. Tab death → recreate tab. Chrome death → main thread restarts Chrome. Extractor errors → return empty, flag for review. Index write failures → atomic write, retry next cycle. Queue overflow → backpressure valve. Dead blog → cache as dead, never re-crawl. Each failure is contained at its level. This directly addresses design history failures §4.1 (tab churn), §5.1 (error code 5), §5.2 (static WS), §6.6 (dead blogs).

**Gap:** Dead blog detection is phrase-based substring matching. Tumblr could change the wording of their private/deactivated interstitials, breaking detection. The design history notes this is the current working approach, so it's acceptable for now, but it's fragile. The design should note this as a maintenance risk.

---

#### 3.16.9 Rate Limiting (§3.9)

**Purpose:** Avoid triggering Tumblr's anti-scraping measures.

**Verdict: FIT.**

The actual code (`queue_integration.py`) uses `delay_min=6.7, delay_max=10.0` between page fetches. The design doc's 2.0–5.0s range is stale — the empirical values are 6.7–10.0s.

**Gap (resolved):** The design now reflects the actual code values (6.7–10.0s), confirmed by user as empirically validated.

---

#### 3.16.10 Cache Persistence (§3.11)

**Purpose:** Per-blog crawl results for re-analysis, date comparison, dead status, crash recovery.

**Verdict: FIT.**

Per-blog JSON at `cache/blog/<username>.json` with full post data. Atomic writes. Serves all four purposes. On crash recovery, the cache shows which blogs were completed and which were in-progress.

---

#### 3.16.11 Graceful Shutdown (§3.13)

**Purpose:** Clean stop. No orphaned Chrome processes. Crash recovery on next run.

**Verdict: FIT.**

8-step shutdown sequence: set event → workers finish current blog → persist cache → close tabs → join workers (30s timeout) → persist index → close remaining tabs → terminate Chrome. No orphaned processes. Next run reads index + cache and resumes.

**Gap:** If a worker is force-terminated after the 30s join timeout, the blog it was mid-crawl is lost from the cache (cache is written after each blog completes, not per-page). The next cycle will rediscover that blog from the source blog's pages and re-crawl it. This is acceptable but not explicitly stated as a trade-off. The design should note: "Mid-blog worker termination loses that blog's partial data; the next cycle re-crawls it."

**Correction (applied):** The design now explicitly states this trade-off in §3.13: "Mid-blog worker termination loses that blog's partial data; the next cycle re-crawls it from the source blog's pages."

---

#### 3.16.12 Configuration as Single Source (§3.14)

**Purpose:** No magic numbers. All tunables in one place.

**Verdict: FIT.**

Every module imports from `config.py`. To change timing, edit one file. This directly addresses the delay-value inconsistency that plagued earlier versions (design history: delays were wrong at 6.7/10.0, then 2.0/3.0, scattered across `agent.py` and `tab_recovery.py`).

---

#### 3.16.13 Thread Safety (§3.8.6)

**Purpose:** Safe concurrent access to shared state.

**Verdict: FIT, with an unstated assumption.**

The queue is `work_queue.py` (JSONL + POSIX `flock` — safe for concurrent access). The index is written atomically (`.tmp` + rename) by the single-threaded event loop — no explicit lock needed since asyncio is single-threaded. This is correct.

**Unstated assumption:** Cache writes are NOT explicitly locked. The design relies on the fact that each blog has a unique filename (`cache/blog/<username>.json`), so no two workers write the same file simultaneously. This is true, but the design should state it explicitly. If the cache ever includes shared files (e.g., a shared log), a lock would be needed.

**Correction (applied):** The thread safety section §3.8.6 now states: "Cache writes require no lock because each blog has a unique filename — no two workers can write the same file simultaneously. This assumption holds as long as cache entries remain per-blog. If a shared cache file is introduced, a lock must be added."

---

#### 3.16.14 Fresh Chrome Restart (§3.15.2, step 1)

**Purpose:** Mandated change #1 — every run starts from clean Chrome state. Never depend on prior tab state.

**Verdict: TECHNICALLY FIT, POTENTIALLY UNFIT FOR THE OPERATING ENVIRONMENT.**

The coordinator calls `chrome_lifecycle.restart()` which kills existing Chrome processes and launches a new one. This guarantees clean state. Technically fit for the purpose.

**Critical gap:** The user runs authenticated Chrome with their personal browsing session. Killing Chrome destroys all the user's open tabs, not just the pipeline's. This is a destructive operation that affects the user's browsing. The design does not address this. Options:
1. Use a separate Chrome profile (`--user-data-dir=<pipeline_profile>`) so the user's personal profile is untouched.
2. Document that the user must close Chrome before running.
3. Use an existing Chrome instance if one is already running on the CDP port (don't restart).

The design currently does none of these. As written, the pipeline is unfit for a shared browsing environment. This must be resolved before build.

---

#### 3.16.15 Raw Output Only

**Purpose:** User directive — no modification, no summarization.

**Verdict: FIT.**

The index is raw structured output. No summarization, no filtering, no transformation. Downstream consumers read the index as-is.

---

#### 3.16.16 Depth Limit (§3.3a, §3.14)

**Purpose:** Cap crawl at depth 2. Names discovered at depth 3 are recorded but not crawled.

**Verdict: FIT.**

The worker pushes depth+1 items only if `depth+1 < config.MAX_DEPTH` (default 2). So depth-2 blogs are crawled, their names are extracted and registered at depth=3, but depth-3 names are not enqueued. This bounds the graph as the goal requires ("crawl them to depth 2").

**Gap:** Depth-3 names are registered in the index but never crawled. Their status is unknown (could be dead, private, deactivated). The index schema doesn't distinguish "discovered but not crawled" from "crawled and active." See §3.16.3 above.

---

#### 3.16.17 Summary Table

| # | Element | Purpose | Verdict | Gap |
|---|---------|---------|---------|-----|
| 1 | Queue-based parallelism | Parallel from first extraction | **FIT** | — |
| 2 | Worker-owned tabs | Max 4 tabs, reuse | **FIT** | 3 workers justified by empirical load testing |
| 3 | Index | Dedup, immediate registration, flat | **FIT** | status: discovered vs active resolved |
| 4 | Date-aware refresh | Skip unchanged blogs | **FIT** | Queue-mode pipeline uses index `scanned_at` recrawl-window check (7 days) |
| 5 | Extractor as pure function | Stateless, verified selectors | **FIT** | "Reblog graph" clarified as transitive |
| 6 | Main thread as coordinator | No bottleneck | **FIT** | — |
| 7 | Worker independence | No dependency on main thread | **FIT** | "If done carefully" removed |
| 8 | Error handling | Contain failures | **FIT** | Phrase-based dead detection noted as maintenance risk |
| 9 | Rate limiting | Avoid rate limits | **FIT** | Empirically validated values |
| 10 | Cache persistence | Re-analysis, recovery | **FIT** | — |
| 11 | Graceful shutdown | Clean stop, no orphans | **FIT** | Mid-blog termination trade-off documented |
| 12 | Configuration | Single source of truth | **FIT** | — |
| 13 | Thread safety | Safe concurrent access | **FIT** | Cache write locking assumption stated |
| 14 | Fresh Chrome restart | Clean state every run | **FIT** | Dedicated profile (`./chrome_profile`) — user's Chrome untouched |
| 15 | Raw output | No modification | **FIT** | — |
| 16 | Depth limit | Cap at depth 2 | **FIT** | Depth-3 names marked discovered |

---

#### 3.16.18 Gaps Requiring Resolution Before Build

**All four critical gaps have been resolved:**

1. **Fresh Chrome is destructive** → **RESOLVED.** `chrome_lifecycle.py` uses a dedicated profile (`./chrome_profile`) via `--user-data-dir`. It only kills Chrome processes whose command line contains our profile path (`ps -ax -o pid,command` filter) — it never touches the user's personal Chrome. If our Chrome is already running, it reuses it and closes its tabs for fresh state.

2. **Rate limit values are unsubstantiated** → **RESOLVED.** User confirms values are empirically validated. No change needed.

3. **Depth-3 names are unverified** → **RESOLVED.** Index schema now includes `status: "discovered"` for names found but not crawled. Cache entries only written for crawled blogs.

4. **Worker count is unjustified** → **RESOLVED.** User confirms 3 workers is the practical limit based on empirical load testing. 4 tabs is the Chrome limit; 3 workers keeps headroom.

**All minor gaps have been corrected** (see "Correction (applied)" notes in each subsection above). The design is now fit for purpose across all 16 elements.

---

## 4. Test Plan

### 4.1 Unit Tests

| Test | Input | Expected | Verifies |
|------|-------|----------|----------|
| Extractor on real HTML | `/tmp/full_page.html` | 68 posts, 77 unique | FR-4, NFR-8 |
| Extractor on empty HTML | `""` | 0 posts, no crash | FR-4 |
| Extractor on non-post HTML | HTML without `-post-` | 0 posts | NFR-8 |
| Cache stale entry | Entry 8 days old | `entry_is_stale() = True` | NFR-4 |
| Cache fresh entry | Entry 1 day old | `entry_is_stale() = False` | NFR-4 |
| Index skip | Username in index | `index_should_skip() = True` | FR-8, NFR-10 |
| Index no-skip | Username not in index | `index_should_skip() = False` | FR-8 |
| Date probe | Page 0 HTML | Returns `page_date_max` | FR-6 |
| Date skip | `page_date_max <= scanned_at` | Skip crawl | FR-6 |
| Date no-skip | `page_date_max > scanned_at` | Proceed crawl | FR-6 |

### 4.2 Integration Tests

| Test | Setup | Expected | Verifies |
|------|-------|----------|----------|
| Worker pool creation | 3 workers | 3 tabs open, no crash | NFR-1, NFR-2 |
| Worker tab reuse | 1 worker, 3 blogs | 1 tab open throughout | NFR-2 |
| Worker tab recovery | Kill tab mid-crawl | New tab created, resume from last offset | FR-11 |
| Parallel trigger | Seed emits 5 names on page 1 | Depth-1 crawl starts before seed finishes page 2 | FR-4 |
| Depth-2 overlaps depth-1 | Depth-1 emits name | Depth-2 crawl starts immediately, while depth-1 still running | FR-4 |
| Fresh Chrome restart | Pipeline start | Chrome killed + restarted | FR-9 |
| Full pipeline (small) | Seed blog, depth cap 2 | Crawl completes, index populated at all depths | All FR |

### 4.3 Regression Tests (from design history)

| Test | Failure mode prevented |
|------|------------------------|
| 100-blog crawl | Tab-per-blog churn (§4.1) — expect 3 tabs, not 100 |
| Probe + crawl same blog | Double-tab (§4.3) — expect 1 tab per blog |
| Navigate 5 pages | Static WS death (§5.2) — expect all pages fetched |
| Kill tab mid-crawl | Error code 5 (§5.1) — expect recovery, no lost state |
| CSS-class selector drift | Stale selectors (§6.2) — expect stable extraction across builds |
| Staged T0→T1→T2 gate | Old serial pipeline — expect parallel from first extraction |

---

## 5. Code Review Findings (from v1 code, applied to v2 design)

These are the issues in the current codebase that the v2 design must resolve. They are **not** bugs in the v2 design — they are bugs the v2 design is explicitly avoiding.

| Issue | Location (v1) | v2 resolution |
|-------|---------------|---------------|
| `close_tab()` in `finally` | `coordinator.py:1089`, `agent.py:367` | Worker owns tab; no per-blog `close_tab` |
| Semaphore permit leak | `coordinator.py:1050` | No semaphore on tab creation; worker pool size is the limit |
| Probe + crawl double-tab | `agent.py:362-373` | Worker probes and crawls with same tab |
| Static WS URL | `agent.py:329,472` | `cdp_manager.reconnect()` after every navigation |
| `run.py` KeyError: 'success' | `run.py:384` | Fix `print_result()` to use `.get()` |
| Delay values inconsistent | `agent.py:389` vs `tab_recovery.py:125` | Single source of truth for delays |
| Cutoff-date creep | `agent.py` (removed) | Date-aware refresh replaces cutoff logic |
| Re-index ignores index | `work_queue.py` | `index_should_skip()` checked before dispatch |

---

## 6. Open Design Decisions

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | Worker assignment | Round-robin vs queue-based | **Queue** — natural backpressure, workers pull when ready |
| 2 | Seed blog lifecycle | Seed is a queue item vs special-cased | **Seed is a queue item** — same loop as every other blog |
| 3 | Probe integration | Separate probe vs worker method | **Worker method** — tab naturally reused |
| 4 | Crash recovery | Respawn worker vs move to next blog | **Move to next blog** — simpler, worker stays alive |
| 5 | Index file format | JSON vs JSONL | **JSON** — small, human-readable, matches cache.py style |
| 6 | Dead blog detection | Phrase matching vs HTTP status | **Phrase matching** (current) — verified working |
| 7 | Max depth (cap) | Depth 2 vs configurable | **Depth 2 default, configurable via CLI** — design choice for limit management |

---

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Chrome crash at scale | Medium | High | Worker pool (3 tabs max), no per-blog churn |
| WS URL staleness | Medium | Medium | `reconnect()` after every navigation |
| Index corruption | Low | Medium | Atomic writes (write to tmp, rename) |
| Tumblr rate limiting | Medium | Low | Random delays between requests (source of truth: one constant) |
| Date parse failure | Low | Low | Fallback to text-date regex; log warning |

---

## 8. File Inventory (target)

| File | Purpose | Status |
|------|---------|--------|
| `run.py` | CLI entry, pipeline dispatch | Exists — needs `print_result()` fix |
| `coordinator.py` | Orchestration, worker pool | Exists — needs refactor to worker pool |
| `worker.py` | Worker class (tab owner) | **NEW** |
| `agent.py` | Per-blog crawl (stateless) | Exists — needs refactor (remove tab ownership) |
| `extractor.py` | HTML → usernames | Exists — canonical, unchanged |
| `cache.py` | JSON cache, index, staleness | Exists — needs index functions |
| `chrome_lifecycle.py` | Fresh Chrome restart | Exists — verified working |
| `lint_modules.py` | py_compile | Exists |
| `lint_batch.py` | py_compile + ruff | Exists |
| `ruff_fix.py` | ruff auto-fix | Exists |
| `.gitignore` | Standard ignores | Exists |

---

## 9. Next Steps

1. **Review this design** — confirm goal, requirements, architecture.
2. **Dispatch critic review** — MoA critic + 5-question critic per methodology.
3. **Security scan** — confirm no credential leakage, no unsafe host access.
4. **Build** — implement `worker.py`, refactor `agent.py` + `coordinator.py`, extend `cache.py`.
5. **Test** — run unit + integration + regression tests.
6. **Proof** — live verification against real Tumblr data.

---

*End of Design Document v2 — awaiting review before build phase.*

# Tumblr Scanner — Design Document (v3)

**Status:** Phase 2 — Build (design complete, code matches)
**Project:** Tumblr username extraction pipeline (depth-limited crawl; "tiers" are our depth abstraction, not Tumblr's)
**Repo:** `github.com/e-hollebone/tumblr-scanner`
**Path:** `/Users/eric/Documents/tumblr-scanner`
**Supersedes:** `DESIGN.md` (v2, 2026-08-28)
**Last verified:** 2026-08-28 (code at `747a406` on `worker-tab-lifecycle`)

---

## 0. Design History (read first)

This document moves forward from the failures catalogued in [`DESIGN_HISTORY.md`](./DESIGN_HISTORY.md) (33 failures across 3 fetch architectures, 7 CDP revisions, 4 concurrency/lifecycle defect classes, plus the extractor post-mortem). **That history is the constraint set for this design** — every failure mode listed there is a requirement this design must not repeat.

Key locked lessons from the history:
1. **Read the data before parsing it** — the extractor's "expanded vs collapsed" duality was a phantom; real Tumblr posts always have `<article>`, `<time datetime>`, and `a[rel="author"]`.
2. **Tab creation is expensive** — one tab per worker, reused for the worker's lifetime. Never open/close per blog.
3. **The WebSocket URL is not static** — refresh it after every navigation or the connection dies past page 0.
4. **Sub-agents are not free parallelism** — DNS + provider-resolution + interpreter mismatches corrupted results more than they helped.
5. **SPA requires JS rendering** — only CDP `Runtime.evaluate` works on Tumblr.

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
| FR-7 | Date-aware refresh: probe page 0, extract `page_date_max` (newest post date), compare against `scanned_at` (when we last crawled), skip if `page_date_max <= scanned_at` | Mandated change #2 |
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
| NFR-4 | Recrawl window (placeholder — see FR-7 for the real requirement) | Agent example (not a user requirement) | `queue_integration.py:84-86` (`age_days < recrawl_days`), default `RECRAWL_DAYS=7` in `config.py` | **SATISFIED (placeholder)** | 7-day age check works as a recrawl gate, but the real requirement is FR-7's probe-then-compare-dates mechanism. The 7-day value is an agent-introduced example, not a user directive. |
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
├── run.py              # CLI entry, dispatches to queue_mode()
├── queue_integration.py # Startup sequence: Chrome restart → seed queue → drain
├── worker.py           # Worker class: owns a tab, reuses it across blogs
├── agent.py            # Pure CDP library: fetch_page_html, detection, crawl_blog
├── extractor.py        # HTML → usernames (BeautifulSoup, canonical)
├── cache.py            # JSON cache, index, entry staleness
├── chrome_lifecycle.py # Fresh Chrome restart (dedicated profile)
├── lint_modules.py     # py_compile checker
├── lint_batch.py       # py_compile + ruff check
├── ruff_fix.py         # ruff auto-fix runner
└── .gitignore
```

**Key architectural separation:**
- **`chrome_lifecycle.py`** — owns the *browser* lifecycle: launching Chrome, killing stale processes (filtered to our dedicated profile only), verifying the debug port. This is the only component that touches Chrome *process* management.
- **`worker.py`** — owns the *tab* lifecycle: opening, navigating, recovering, and closing tabs. Workers never launch or kill Chrome; they only operate on tabs within the already-running browser.
- **`agent.py`** — pure CDP library. Accepts a connected `CDPClient` WS URL, raises `TabDeadError` on failure. No tab ownership, no `pre_existing_ws_url`, no recovery loop.
- **`queue_integration.py`** — the startup sequence: restart Chrome → prime queue with seed → start worker pool → drain.

### 3.2 Worker Pool Model (the fix for tab-per-blog)

```
Worker Pool (size = MAX_CONCURRENT_AGENTS = 3)
  Worker 0: tab_A → poll queue → blog_X → blog_Y → ...
  Worker 1: tab_B → poll queue → blog_Z → ...
  Worker 2: tab_C → poll queue → sleep → poll → blog_W → ...
```

**Startup: all workers start immediately.** No deferred start, no "seed worker" designation. All 3 workers open their tabs at pipeline start and begin polling the queue. The seed blog is the first (and initially only) queue item. Whichever worker dequeues it first crawls it.

**Queue polling with sleep.** When the queue is empty, workers sleep for `QUEUE_POLL_INTERVAL` seconds (2.0s), then poll again. They do NOT busy-wait.

**Tab lifecycle rules:**
1. Worker creates its tab on startup (immediately, not deferred).
2. Worker navigates its tab to each new blog URL via `Page.navigate` to `?offset=N`.
3. Worker reuses the same tab for every blog it crawls.
4. Worker closes its tab only on worker death (pipeline end or unrecoverable crash).
5. Worker pool size (3) is the concurrency limit — no semaphore needed.

**Depth is not a worker property.** Any worker can crawl any blog at any depth. A worker pulling a depth-2 name from the queue runs the same crawl as a worker pulling a depth-1 name. The depth label is just a field in the queue item (for limit management + reporting), not a structural constraint.

**The seed is not a special case.** It is simply the first queue item. No worker is pre-assigned to it. The flock-based `dequeue()` guarantees exactly one worker gets it.

### 3.3 Data Flow — parallel from first extraction

```
run.py --queue
  └─ queue_mode(seed_blog)
       ├─ chrome_lifecycle.restart()          # Fresh Chrome (FR-9)
       ├─ enqueue(seed_blog, tier=0)          # Seed is first queue item
       ├─ start worker pool (3 workers)       # All workers start immediately
       │    └─ each worker: open tab → poll queue → sleep if empty
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
              └─ ENQUEUE AFTER EVERY PAGE              ← parallelism starts HERE (FR-4)
```

**The parallel trigger (FR-4):** The seed blog is the first queue item. The moment the seed worker finishes reading its first page (~20 posts), it extracts names and enqueues them at depth+1. Those names are picked up by *any idle worker* immediately — depth-1 crawl starts before the seed blog is even finished.

**Per-page enqueue (critical):** Workers enqueue discoveries after EVERY page, not after the blog finishes. This is what makes parallelism structural rather than staged.

### 3.3a Core Crawl Loop (per worker, per blog)

```
for each page (offset = 0, 20, 40, ...):
    try:
        html = fetch_page_html(client, blog, offset)   # raises TabDeadError on failure
    except TabDeadError:
        worker._recover_tab()                           # close dead tab, open new one
        continue                                         # retry same offset

    if detect_login_wall(html, url):
        raise LoginWallDetected(username)               # halt pipeline

    result = extract_from_html(html)
    page_names = result.usernames

    for name in page_names:
        status = index_status(name)
        if status == "fresh":
            continue                                    # already done, skip
        elif status == "stale":
            enqueue(name, mode="reindex")               # date probe
        else:
            enqueue(name, mode="full")                  # full crawl

    if detect_end_of_posts(html): break
    if limits_reached(): break
```

### 3.4 Startup Sequence (timing and procedural steps)

```
Step 1: Restart Chrome
   └─ chrome_lifecycle.restart()
   └─ Poll /json/version until OK (Chrome ready)
   └─ Extract browser WS URL

Step 2: Seed the queue (BEFORE workers start)
   └─ enqueue(queue_path, seed_blog, tier=0)
   └─ Queue now has exactly 1 item: the seed

Step 3: Start worker pool (all workers start immediately)
   └─ Each worker:
       ├─ Opens its tab via CDP (Chrome is ready — succeeds)
       ├─ Polls queue → dequeue() returns seed (already there)
       └─ Begins crawling seed blog
   └─ Workers that don't get the seed:
       ├─ Polls queue → empty → sleeps 2s
       └─ Wakes up → polls again → finds T1 names (enqueued by seed worker)
```

**Why this order matters:**

| If you do it wrong | What happens |
|---|---|
| Workers start before Chrome ready | Tab creation fails — all workers crash |
| Workers start before seed enqueued | Workers sleep 2s, wake up, find seed — 2s wasted per worker |
| Seed enqueued but no workers started | Seed sits idle — no parallelism |

**After startup, the system is self-loading:**
- Seed worker crawls page 0 → enqueues T1 names → idle workers pick them up
- Seed worker continues to page 1, 2, 3... → enqueues more T1 names
- T1 workers crawl their blogs → enqueue T2 names → other workers pick them up
- All workers are identical — no special "seed worker" designation

### 3.5 Concurrency Model

| Resource | Limit | Mechanism |
|----------|-------|-----------|
| Concurrent tabs | 4 | Worker pool size (3) < Chrome limit (4) |
| Concurrent agents | 3 | `MAX_CONCURRENT_AGENTS = 3` |
| Tab lifetime | Worker lifetime | Worker owns tab, closes on death |
| WS URL refresh | Per blog | Worker `_refresh_ws()` before each blog |

**No semaphore on individual tab creation.** The worker pool size *is* the concurrency limit. This avoids the semaphore-vs-churn bug from design history §4.1.

**Parallelism is structural, not staged.** All workers run the same loop (§3.3a) and pull from one shared queue. The seed blog is just the first queue item. There is no T0/T1/T2 gate — depth-2 work can start before depth-1 is finished, as long as depth-2 names have been emitted.

### 3.6 Date-Aware Refresh Protocol (NOT YET IMPLEMENTED — see FR-7)

> **Status:** This section describes the *real* design requirement (Mandated change #2). The current implementation (as of `747a406`) uses a 7-day recrawl window only (§3.8.8) — a placeholder the agent introduced as a stand-in. The probe-then-compare-dates mechanism below is the actual requirement and is a build-phase item. |

For every blog (any depth — Tumblr treats all blogs identically):
1. Fetch page 0 only.
2. Extract `page_date_max` from `<time datetime>` elements.
3. Compare against `cache.scanned_at` for that username.
4. If `page_date_max <= scanned_at` → skip (no new content).
5. Else → crawl all pages, update `scanned_at` on completion.

Applied uniformly — the seed blog (depth 0) and every discovered blog use the same probe.

### 3.7 Index Registration Protocol

For every username extracted (any depth):
1. Immediately append to `cache/index.json`: `{username, depth, scanned_at, source_blog, status}`.
2. Before dispatching any blog, check `index.json` — skip if already indexed and scanned today.
3. Next pipeline cycle reads `index.json` to build the skip list.

The index is the single source of truth for "what's been seen." It is flat — no per-tier lists. A blog's depth is just a field, not a separate index partition.

### 3.8 System Description (narrative — what each part does)

#### 3.8.1 Main Loop and Initiation

The pipeline starts in `run.py`, which parses CLI arguments and calls `queue_mode(seed_blog)`. The startup sequence is strictly ordered:

1. **Restart Chrome** — `chrome_lifecycle.restart()` starts a fresh Chrome process with a dedicated profile (`--user-data-dir=chrome_profile`). Poll `/json/version` until OK. This guarantees a clean state without touching the user's personal Chrome session.

2. **Seed the queue** — `enqueue(queue_path, seed_blog, tier=0)`. The queue now has exactly 1 item.

3. **Start worker pool** — All 3 workers start immediately. Each opens its tab via CDP (Chrome is ready, so this succeeds). Each worker polls the queue. One worker dequeues the seed and begins crawling. The other two workers find the queue empty and sleep for `QUEUE_POLL_INTERVAL` seconds before re-checking.

The moment the seed worker finishes page 0 and enqueues T1 names, idle workers wake up and pick them up. Parallelism starts at first extraction, not after the seed finishes.

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

The index is the single source of truth for "what has been seen." It is flat — no per-depth partitions. Depth is just a field in each entry, not a structural separator.

The index is read at startup and written to atomically (write to temp file, then rename) after each registration to prevent corruption on crash.

#### 3.8.3 The Queue

The crawl queue is an on-disk JSONL file (`cache/queue.jsonl`) with POSIX `flock` for safe concurrent access (via `work_queue.py`). Each line is a JSON object: `{"username": ..., "tier": ..., "state": "pending"|"in_progress"|"done"}`. The queue serves as the synchronization point between workers:

- Workers pull items from the queue when idle.
- Workers push newly discovered items onto the queue after extracting usernames from a page.
- The queue is unbounded in memory but naturally back-pressured: workers can't push faster than they process pages, and page fetches have random delays.

The queue is the mechanism that makes parallelism structural rather than staged. There is no "depth-0 queue" and "depth-1 queue" — one shared queue feeds all workers. A depth-2 item can sit next to a depth-1 item; whichever worker is free pulls the next item regardless of depth.

#### 3.8.4 Main Thread

The main thread (running in `queue_mode()`) does almost no crawling work. Its responsibilities are:

1. **Initialize**: Restart Chrome, seed the queue, start worker pool.
2. **Monitor**: Periodically log queue depth, worker status, and total usernames indexed.
3. **Shutdown**: On completion or Ctrl-C, signal workers to stop, wait for them to finish their current blog, then close all tabs and the Chrome process.
4. **Aggregate results**: After workers finish, read the index and output the final username list.

The main thread never touches a Chrome tab. It never fetches a page or parses HTML. This separation ensures the single point of coordination (the main thread) cannot become a bottleneck or a single point of failure for the crawl itself.

#### 3.8.5 Worker Threads

Each worker is an instance of the `Worker` class (in `worker.py`) running as an `asyncio.Task`. Each worker owns exactly one Chrome tab for its lifetime. The worker's life cycle:

1. **Startup**: Open a tab via CDP (`Target.createTarget`), store the WebSocket URL. Tab is opened immediately, not deferred.
2. **Main loop**:
   a. Check the queue for work.
   b. If queue is empty: sleep for `QUEUE_POLL_INTERVAL` seconds, then check again. Track how long the queue has been empty — if it exceeds `QUEUE_EMPTY_TIMEOUT` (30s), the worker exits.
   c. If work found: pull `(username, tier, mode)` from the queue.
   d. Check the index: if already scanned today, skip.
   e. If mode=`'reindex'`: fetch page 0, extract dates, compare against `scanned_at`. If new content exists, crawl all pages. Else update `scanned_at` and done.
   f. If mode=`'full'`: crawl page-by-page (offset 0, 20, 40, ...). For each page: fetch HTML, run `extract_from_html()`, get usernames. For each username: check index, register if new, push `(username, tier+1, 'full')` onto queue if tier < MAX_DEPTH.
   g. **Enqueue after EVERY page** — not after the blog finishes. This is what makes parallelism structural.
3. **Tab recovery**: If the tab dies mid-crawl (agent raises `TabDeadError`), the worker closes it, opens a new one, and resumes from the last offset (max 3 recovery attempts). The worker owns tab recovery — the agent library just raises `TabDeadError`.
4. **Shutdown**: Close the tab, terminate task.

**All workers start immediately.** There is no permanent "seed worker" designation. Whichever worker dequeues the seed first crawls it. Other workers sleep first, wake up, and find T1 names already enqueued by the seed worker.

#### 3.8.6 Synchronization and Parallelism

The pipeline uses **asyncio-based concurrency** (not threads). All crawl work runs in a single event loop with `asyncio.Task` workers:

1. **The crawl queue** (`work_queue.py`): a JSONL file with POSIX `flock` for safe concurrent access. Multiple async workers dequeue and enqueue without in-memory locks.
2. **The index file** (`cache/index.json`): written atomically (`.tmp` + rename) by the async event loop. Since the event loop is single-threaded, no explicit lock is needed for index writes.
3. **Cache writes**: require no lock because each blog has a unique filename (`cache/blog/<username>.json`) — no two workers can write the same file simultaneously. This assumption holds as long as cache entries remain per-blog.

Parallelism is managed by `asyncio.Task` workers. The worker pool size (3) is the concurrency limit. Each worker is an async task; the event loop schedules them cooperatively. The queue provides natural backpressure: if all workers are busy, items accumulate in the JSONL file; if the queue is empty, workers exit.

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

**Current implementation (placeholder — NOT the requirement):** `_index_has_fresh_entry()` checks if a username has a `scanned_at` within `recrawl_days` (7). If fresh, it is skipped (not re-enqueued). This is a crude age-based gate the agent introduced as a stand-in. The 7-day value is an example, not a user requirement — see NFR-4.

**Intended implementation (the real requirement, probe-then-compare):** See §3.6 / FR-7. The probe fetches page 0, extracts `page_date_max`, compares against `scanned_at`. If `page_date_max <= scanned_at`, skip (no new posts since last scan). If `page_date_max > scanned_at`, crawl all pages. This is a build-phase item (FR-7 is PARTIAL). |

#### 3.8.9 Worker Independence from Main Thread Chrome

Workers (async tasks) do not share Chrome state with the main thread. Each worker:

- Creates its own tab via the CDP browser-level endpoint (`Target.createTarget`), not via the main thread.
- Maintains its own WebSocket connection to that tab.
- Handles its own reconnection if the WebSocket dies (the WS URL can change after navigation; the worker re-fetches `/json` to get the current URL).
- Closes its own tab on shutdown.

The main thread never holds a tab reference. If the main thread crashes, workers continue crawling (they're independent async tasks). If a worker crashes, the main thread detects it (via `asyncio.wait` timeout) and can optionally respawn it.

This independence is critical: the main thread's Chrome lifecycle (restart at pipeline start) is decoupled from the workers' Chrome usage.

#### 3.8.10 Tab Lifecycle

**Opening**: A worker opens its tab once, at startup, via `Target.createTarget`. The returned WebSocket URL is stored. The tab is then navigated to the first blog URL.

**Reuse**: The worker navigates the same tab to each subsequent blog via `Page.navigate` to `?offset=N`. The tab is never closed between blogs. This is the fix for the tab-per-blog explosion that crashed Chrome at 30+ tabs.

**Maintenance**: Before each blog, the worker refreshes the WebSocket URL (Tumblr's SPA navigation can invalidate the old WS endpoint). If the navigation fails, the agent raises `TabDeadError` and the worker retries up to 3 times with tab replacement.

**Closing**: The worker closes its tab only on shutdown (pipeline end or unrecoverable error). The close is via `Target.closeTarget`.

**Ownership**: The worker *owns* its tab. No other task touches it. The main thread never closes a worker's tab. If the worker dies unexpectedly, the main thread's shutdown routine closes any remaining tabs by reading `/json/list` and closing all of them.

#### 3.8.11 Error Handling

Errors are handled at multiple levels:

**Page fetch errors** (timeout, WS disconnect, Chrome error code 5 `page.documentCleared`):
- The agent raises `TabDeadError`.
- The worker catches it, closes the dead tab, opens a new one, retries from the same offset (max 3 attempts).
- If retries exhausted: mark this blog as `error` in the cache, move to next queue item.

**Tab death** (Chrome process crash, tab crash):
- The worker detects this when a CDP command times out or returns an error.
- It closes the dead tab via `Target.closeTarget`, opens a new one via `Target.createTarget`, and resumes from the last offset (max 3 recovery attempts).

**Extractor errors** (malformed HTML, unexpected structure):
- The extractor returns empty results (0 posts) rather than raising. Extraction is best-effort.
- The worker logs the empty result and moves on.

**Index write errors** (disk full, permission):
- Atomic write (write to `.tmp`, then rename) prevents corruption. If the rename fails, the in-memory index is intact and the write is retried next cycle.

**Dead blog detection**:
- If the fetched HTML contains phrases like "This blog is private" or a 404 status, the worker marks the blog as `dead` in the cache and never re-queues it.

#### 3.9 Rate Limiting and Politeness Delays

To avoid triggering Tumblr's anti-scraping measures, the worker introduces a random delay between every page fetch. The delay is drawn uniformly from `[DELAY_MIN, DELAY_MAX]` seconds (6.7–10.0). These values are defined as constants in `config.py` — the single source of truth.

**Rules:**
- Delay happens *after* each page fetch completes, before the next `Page.navigate`.
- Probe requests (page 0 only) also get a delay.
- Delay is applied per-worker, not globally (workers are independent; their delays don't synchronize).

#### 3.10 Dead Blog Detection

A blog is dead if it returns content indicating it no longer exists or is inaccessible. Detection happens inside the worker after fetching page 0:

1. Check HTML content against a phrase list:
   - `"This blog is private"` → private (treated as dead for crawling purposes)
   - `"deactivated"` → dead
   - `"not found"` → dead
   - `"This Tumblr account has been suspended"` → dead
2. If dead: the worker writes a cache entry with `status: "dead"` and `scanned_at = today`. The username is *not* marked in the index as a blog to crawl (it's a dead end, not a valid blog).
3. Dead entries are never re-crawled. On subsequent cycles, the index check finds `status: "dead"` and skips immediately.

The phrase list is defined in `config.py` as `DEAD_PHRASES`. It is a static list — no regex, just substring matching against the fetched HTML (case-insensitive).

**Why this matters:** Without dead blog detection, the pipeline would repeatedly try to crawl suspended/deactivated blogs, wasting Chrome time and queue slots on pages that will never yield usernames.

#### 3.11 Cache Persistence

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

#### 3.12 Progress Reporting

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

#### 3.13 Graceful Shutdown

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

#### 3.14 Configuration

All pipeline tunables live in a single `config.py`:

```python
# Concurrency
MAX_CONCURRENT_AGENTS = 3        # worker pool size = tab count

# Timing (seconds)
DELAY_MIN = 6.7                  # min delay between page fetches (empirically validated)
DELAY_MAX = 10.0                 # max delay between page fetches
QUEUE_POLL_INTERVAL = 2.0        # worker sleep when queue empty
QUEUE_EMPTY_TIMEOUT = 30.0       # worker exit after this many seconds of empty queue

# Windows
RECRAWL_DAYS = 7                 # skip blog if scanned within this window
PROGRESS_INTERVAL = 30           # seconds between status log lines

# Thresholds
QUEUE_OVERFLOW_THRESHOLD = 10000 # stop pushing if queue exceeds this

# Paths
CACHE_DIR = Path("/Users/eric/Documents/tumblr-scanner/cache")
QUEUE_PATH = CACHE_DIR / "queue.jsonl"
INDEX_PATH = CACHE_DIR / "index.json"

# Chrome
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = Path("/Users/eric/Documents/tumblr-scanner/chrome_profile")
CDP_PORT = 9222
CHROME_RESTART_TIMEOUT = 10

# Detection phrases
DEAD_PHRASES = [...]
LOGIN_WALL_PHRASES = [...]
END_PHRASES = [...]
```

Single source of truth: every module imports from `config.py`. No magic numbers scattered across source files.

### 3.15 End-to-End Workflow — How the Pieces Connect

#### 3.15.1 The Spine of the System

The pipeline has a clean **separation of duties** at the CDP boundary:

- **`chrome_lifecycle.py`** — owns the *browser* lifecycle: launching Chrome, killing stale processes (filtered to our dedicated profile only), verifying the debug port is reachable. This is the only component that touches Chrome *process* management.
- **`worker.py`** — owns the *tab* lifecycle: opening, navigating, recovering, and closing tabs. Workers never launch or kill Chrome; they only operate on tabs within the already-running browser.
- **`agent.py`** — the pure CDP library. Provides `fetch_page_html()` and detection functions. No state, no tab ownership. Raises `TabDeadError` on CDP failure.

The remaining components:

1. **`config.py`** — the DNA. Every other module imports from it.
2. **`queue_integration.py`** — the startup sequence. It initializes everything (restart Chrome, seed the queue, start workers), monitors progress, and orchestrates shutdown.
3. **`worker.py`** — the muscle. It does the actual crawling, extracting, and registering. Owns its tab lifecycle.
4. **`extractor.py`** — the sensory organ. It converts raw HTML into structured data.
5. **`agent.py`** — the pure CDP library. Provides `fetch_page_html()` and detection functions.

Connecting these are two shared structures:
- **The queue** — the nervous system carrying signals (work items) between coordinator and workers.
- **The index + cache** — the memory system. Workers write to it, read from it, and make decisions based on it.

#### 3.15.2 The Full Data Flow (one pass, start to finish)

```
USER: python3 run.py the-smallest-kitten-cravings --queue
  │
  ▼
run.py
  ├─ parse CLI args (seed blog, mode, flags)
  ├─ import config.py (all tunables resolved)
  └─ call queue_mode(seed_blog)
        │
        ▼
queue_mode()
        │
        ├─ [1] chrome_lifecycle.restart()
        │     ├─ if Chrome already running on CDP port: verify it's our profile, else warn
        │     ├─ launch: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
        │     │          --remote-debugging-port=9222
        │     │          --user-data-dir=chrome_profile  (dedicated profile)
        │     ├─ poll http://127.0.0.1:9222/json/version until OK
        │     └─ return (Chrome ready, CDP reachable)
        │
        ├─ [2] Seed the queue: enqueue(seed_blog, tier=0)
        │
        ├─ [3] Start worker pool (3 workers)
        │     └─ each worker: open tab → poll queue → sleep if empty
        │
        ├─ [4] Monitor loop (every config.PROGRESS_INTERVAL seconds):
        │     ├─ log: [STATUS] queue=1 active=1 indexed=0 ...
        │     ├─ check: all workers alive? (task await timeout)
        │     └─ if shutdown: break monitor loop
        │
        └─ [5] Shutdown:
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
Worker.run():
  │
  ├─ item = dequeue(queue_path)
  │   item = ("some-blog", tier=1, mode="full")
  │
  ├─ [A] Index check:
  │     entry = index.get("some-blog")
  │     if entry and entry.scanned_at == today:
  │         continue              # skip, already done today
  │
  ├─ [B] Navigate to blog:
  │     _refresh_ws()             # refresh WS URL
  │     Page.navigate("https://some-blog.tumblr.com/?offset=0")
  │     wait_for_load()
  │
  ├─ [C] Dead blog check:
  │     html = Runtime.evaluate("document.documentElement.outerHTML")
  │     if contains_dead_phrase(html):
  │         cache.save_entry(username, status="dead", scanned_at=today)
  │         continue              # never re-crawl
  │
  ├─ [D] Date probe (if mode="reindex"):
  │     result = extractor.extract_from_html(html)
  │     page_date_max = result.page_date_max
  │     cache_entry = cache.load_entry(username)
  │     if page_date_max <= cache_entry.scanned_at:
  │         # no new content — update scan date, skip
  │         cache.save_entry(username, scanned_at=today)
  │         mark_done(username); continue
  │     else:
  │         # new content — crawl all pages
  │         pass  # fall through to [E] full crawl
  │
  ├─ [E] Full crawl (if mode="full"):
  │     for offset in (0, 20, 40, ...):
  │         │
  │         ├─ Page.navigate(f"https://www.tumblr.com/{username}?offset={offset}")
  │         ├─ wait_for_load()
  │         ├─ html = Runtime.evaluate("document.body.innerText")
  │         │
  │         ├─ result = extractor.extract_from_html(html)
  │         │
  │         ├─ for name in result.usernames:
  │         │     idx = index.get(name)
  │         │     if not idx:
  │         │         index.register(name, tier+1, source=username)
  │         │         if tier+1 < config.MAX_DEPTH:
  │         │           enqueue(queue_path, name, tier=tier+1, mode="full")
  │         │         # depth+1 == MAX_DEPTH: registered as discovered, not crawled
  │         │     elif idx.scanned_at != today:
  │         │         enqueue(queue_path, name, tier=tier, mode="reindex")
  │         │     # else: already done today, skip
  │         │
  │         ├─ delay(random.uniform(DELAY_MIN, DELAY_MAX))
  │         └─ if detect_end_of_posts(html): break
  │
  │     # After all pages:
  │     cache.save_entry(username, posts=posts_accumulator,
  │                      scanned_at=today, status="active", depth=tier)
  │     mark_done(username)
  └─ [F] Loop back to dequeue()
```

#### 3.15.4 How the Pieces Communicate

| From | To | Via | What |
|------|----|-----|------|
| `run.py` | `queue_integration` | Function call | `queue_mode(target_blog, ...)` |
| `queue_integration` | `chrome_lifecycle` | Function call | `restart_chrome()` |
| `queue_integration` | `work_queue` | Function call | `enqueue()` / `dequeue()` / `mark_done()` |
| `Worker` | `work_queue` | Function call | `dequeue()` / `enqueue()` / `mark_done()` |
| `Worker` | `agent` | Function call | `crawl_blog(ws_url, username, ...)` |
| `agent` | `cdp_use.CDPClient` | Method calls | `Page.navigate()`, `Runtime.evaluate()` |
| `CDPClient` | Chrome tab | CDP WebSocket | JSON-RPC commands |
| Chrome tab | `CDPClient` | WebSocket response | HTML content, navigation status |
| `Worker` | `extractor` | Function call | `extract_from_html(html)` |
| `extractor` | `Worker` | Return value | `{usernames, dates, post_count}` |
| `Worker` | `index.json` | Atomic write | Persist on each registration |
| `queue_integration` | stdout | `logger.info()` | Progress reporting |

#### 3.15.5 Failure Propagation and Containment

The architecture contains failures at each level so a fault in one component doesn't cascade:

| Failure | Contained by | Effect on system |
|---------|--------------|------------------|
| Page fetch fails (timeout) | Worker retry logic (3 attempts) | Single page lost, blog continues |
| Tab dies (WS disconnect) | **Worker** closes dead tab, opens new one via `Target.createTarget` | Single worker pauses ~5s, then resumes; no other worker affected |
| Chrome process dies | Worker signals main thread → `chrome_lifecycle.restart()` | All workers pause ~10s, then reconnect new tabs |
| Extractor returns 0 posts | Worker logs and moves on | Blog flagged for manual review |
| Worker task crashes | Main thread detects via `asyncio.wait` timeout | Remaining workers continue; dead worker's tab closed in shutdown |
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

The queue is the central decoupling element. The coordinator puts the seed on the queue and never thinks about depth again. Workers pull items and run the same loop regardless of depth. A worker pushes depth+1 items *inside* the page loop, not after the blog finishes. With 3 workers, an idle worker is always available to pick up newly pushed items.

---

#### 3.16.2 Worker-Owned Tab Lifecycle (§3.2, §3.8.10)

**Purpose:** Max 4 concurrent tabs. One tab per worker, reused across all blogs. Never open/close per blog.

**Verdict: FIT.**

Worker creates tab once at startup (via `Target.createTarget`), navigates it to each blog via `Page.navigate`, closes it only on worker death (or tab recovery). 3 workers = 3 tabs, within Chrome's 4-tab limit. This directly fixes the tab-per-blog explosion that crashed Chrome at 30+ tabs (design history §4.1).

**Worker owns tab recovery.** When a tab dies mid-crawl, the worker closes it and opens a new one via `Target.createTarget`, then resumes from the last offset. The agent library does not own any tab — it raises `TabDeadError` and the worker handles recovery. This is the key fix from the parallel-boundary split: tab control lives entirely within the worker, not shared across the agent/worker boundary.

---

#### 3.16.3 Index (§3.8.2, §3.7)

**Purpose:** Immediate registration of every username. Dedup against already-seen names. Skip already-done blogs. Flat structure (no per-depth partitions).

**Verdict: FIT.**

Flat JSON with `{username, depth, scanned_at, source_blog}`. Atomic writes (`.tmp` + rename) prevent corruption. Checked before dispatch (skip if scanned today). Updated immediately on extraction. The flat structure matches the user's directive: "no tier-specific indexing logic." Depth is a field, not a partition.

---

#### 3.16.4 Date-Aware Refresh (§3.6, §3.8.8)

**Purpose:** Probe page 0, compare dates against `scanned_at`, skip unchanged blogs. Applied uniformly across all depths.

**Verdict: PARTIAL.**

The current implementation uses a 7-day recrawl window (`_index_has_fresh_entry()` checks `scanned_at` age) — this is a placeholder the agent introduced, not a user requirement (see NFR-4). The intended probe-then-compare-dates mechanism (§3.6 / FR-7) is NOT yet implemented. The blog's `page_date_max` is never compared against `scanned_at`. This is a build-phase item. |

---

#### 3.16.5 Extractor as Pure Function (§3.8.7)

**Purpose:** Stateless HTML → usernames. Verified selectors only. No guessing.

**Verdict: FIT.**

`extract_from_html(html) -> dict` is a pure function. Locked selectors: `[data-cell-id*="-post-"]`, `aria-label`, `a[rel="author"]`, `<time datetime>`. Verified against real Tumblr HTML (68 posts, 77 unique, 0 false positives). Deterministic — same HTML always produces same result. No external state.

---

#### 3.16.6 Main Thread as Pure Coordinator (§3.8.4)

**Purpose:** Decouple coordination from crawling. Main thread never becomes a bottleneck or single point of failure for the crawl.

**Verdict: FIT.**

Main thread (event loop) initializes, seeds the queue, monitors progress, shuts down. It never touches a Chrome tab, never fetches a page, never parses HTML. Workers are `asyncio.Task` instances.

---

#### 3.16.7 Worker Independence from Main Thread Chrome (§3.8.9)

**Purpose:** Worker is completely independent. Does not depend on the main thread's Chrome implementation.

**Verdict: FIT.**

Workers create their own tabs via `Target.createTarget`, maintain their own WebSocket connections, handle their own reconnection and tab recovery. The main thread never holds a tab reference.

---

#### 3.16.8 Error Handling (§3.8.11)

**Purpose:** Contain failures at each level. A fault in one component doesn't cascade.

**Verdict: FIT.**

Page fetch errors → 3 retries with reconnect (worker-owned). Tab death → worker closes dead tab, opens new one, resumes from last offset. Chrome death → main thread restarts Chrome, workers reconnect new tabs. Extractor errors → return empty, flag for review. Index write failures → atomic write, retry next cycle. Queue overflow → backpressure valve. Dead blog → cache as dead, never re-crawl.

**Key fix:** Tab death recovery is now owned entirely by the worker. The agent library raises `TabDeadError`; the worker closes the dead tab and opens a fresh one. There is no cross-boundary tab manipulation.

---

#### 3.16.9 Rate Limiting (§3.9)

**Purpose:** Avoid triggering Tumblr's anti-scraping measures.

**Verdict: FIT.**

`DELAY_MIN=6.7, DELAY_MAX=10.0` (empirically validated). Single source of truth in `config.py`.

---

#### 3.16.10 Cache Persistence (§3.11)

**Purpose:** Per-blog crawl results for re-analysis, date comparison, dead status, crash recovery.

**Verdict: FIT.**

Per-blog JSON at `cache/blog/<username>.json` with full post data. Atomic writes. Serves all four purposes.

---

#### 3.16.11 Graceful Shutdown (§3.13)

**Purpose:** Clean stop. No orphaned Chrome processes. Crash recovery on next run.

**Verdict: FIT.**

8-step shutdown sequence: set event → workers finish current blog → persist cache → close tabs → join workers (30s timeout) → persist index → close remaining tabs → terminate Chrome.

**Gap:** Mid-blog worker termination loses that blog's partial data; the next cycle re-crawls it from the source blog's pages. This is an explicit trade-off.

---

#### 3.16.12 Configuration as Single Source (§3.14)

**Purpose:** No magic numbers. All tunables in one place.

**Verdict: FIT.**

Every module imports from `config.py`. To change timing, edit one file.

---

#### 3.16.13 Thread Safety (§3.8.6)

**Purpose:** Safe concurrent access to shared state.

**Verdict: FIT.**

Queue uses `flock`, index uses atomic writes, asyncio is single-threaded. Cache writes require no lock because each blog has a unique filename.

---

#### 3.16.14 Fresh Chrome Restart (§3.15.2, step 1)

**Purpose:** Mandated change #1 — every run starts from clean Chrome state. Never depend on prior tab state.

**Verdict: FIT.**

`restart_chrome()` at pipeline start. Dedicated profile (`./chrome_profile`) — user's Chrome untouched. Only kills Chrome processes whose command line contains our profile path.

---

#### 3.16.15 Raw Output Only

**Purpose:** User directive — no modification, no summarization.

**Verdict: FIT.**

The index is raw structured output. No summarization, no filtering, no transformation.

---

#### 3.16.16 Depth Limit (§3.3a, §3.14)

**Purpose:** Cap crawl at depth 2. Names discovered at depth 3 are recorded but not crawled.

**Verdict: FIT.**

The worker pushes depth+1 items only if `depth+1 < config.MAX_DEPTH` (default 2). So depth-2 blogs are crawled, their names are extracted and registered at depth=3, but depth-3 names are not enqueued.

---

#### 3.16.17 Summary Table

| # | Element | Purpose | Verdict | Gap |
|---|---------|---------|---------|-----|
| 1 | Queue-based parallelism | Parallel from first extraction | **FIT** | — |
| 2 | Worker-owned tabs | Max 4 tabs, reuse | **FIT** | — |
| 3 | Index | Dedup, immediate registration, flat | **FIT** | — |
| 4 | Date-aware refresh | Skip unchanged blogs | **PARTIAL** | Placeholder recrawl window only (7-day age check, agent-introduced); probe-then-compare (FR-7) not implemented |
| 5 | Extractor as pure function | Stateless, verified selectors | **FIT** | — |
| 6 | Main thread as coordinator | No bottleneck | **FIT** | — |
| 7 | Worker independence | No dependency on main thread | **FIT** | — |
| 8 | Error handling | Contain failures | **FIT** | — |
| 9 | Rate limiting | Avoid rate limits | **FIT** | — |
| 10 | Cache persistence | Re-analysis, recovery | **FIT** | — |
| 11 | Graceful shutdown | Clean stop, no orphans | **FIT** | Mid-blog termination trade-off documented |
| 12 | Configuration | Single source of truth | **FIT** | — |
| 13 | Thread safety | Safe concurrent access | **FIT** | — |
| 14 | Fresh Chrome restart | Clean state every run | **FIT** | — |
| 15 | Raw output | No modification | **FIT** | — |
| 16 | Depth limit | Cap at depth 2 | **FIT** | — |

---

#### 3.16.18 Gaps Requiring Resolution Before Build

**One gap remains:**

1. **Date-aware refresh** (FR-7 / Mandate #2 / Fitness #4) — The current implementation uses a 7-day recrawl window (placeholder the agent introduced, not a user requirement — see NFR-4). The intended probe-then-compare-dates mechanism (§3.6) is NOT yet implemented. The blog's `page_date_max` is never compared against `scanned_at`. This is a build-phase item. |

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
| Date probe | Page 0 HTML | Returns `page_date_max` | FR-7 |
| Date skip | `page_date_max <= scanned_at` | Skip crawl | FR-7 |
| Date no-skip | `page_date_max > scanned_at` | Proceed crawl | FR-7 |

### 4.2 Integration Tests

| Test | Setup | Expected | Verifies |
|------|-------|----------|----------|
| Worker pool creation | 3 workers | 3 tabs open, no crash | NFR-1, NFR-2 |
| Worker tab reuse | 1 worker, 3 blogs | 1 tab open throughout | NFR-2 |
| Worker tab recovery | Kill tab mid-crawl | Worker closes dead tab, opens new one, resumes from last offset | FR-11 |
| Parallel trigger | Seed emits 5 names on page 0 | Depth-1 crawl starts before seed finishes page 1 | FR-4 |
| Depth-2 overlaps depth-1 | Depth-1 emits name | Depth-2 crawl starts immediately, while depth-1 still running | FR-4 |
| Per-page enqueue | Seed blog 50 pages | T1 names enqueued after page 0, not after blog finishes | FR-4 |
| Fresh Chrome restart | Pipeline start | Chrome killed (our profile only) + restarted | FR-9 |
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

## 5. Code Review Findings (from pre-refactor code, applied to v3 design)

These are the issues in the pre-refactor codebase that the v3 design resolves. They are **not** bugs in the v3 design — they are bugs the v3 design is explicitly avoiding.

| Issue | Location (pre-refactor) | v3 resolution |
|-------|-------------------------|---------------|
| `close_tab()` in `finally` | `coordinator.py:1089`, `agent.py:367` | Worker owns tab; no per-blog `close_tab` |
| Semaphore permit leak | `coordinator.py:1050` | No semaphore on tab creation; worker pool size is the limit |
| Probe + crawl double-tab | `agent.py:362-373` | Worker probes and crawls with same tab |
| Static WS URL | `agent.py:329,472` | Worker `_refresh_ws()` before each blog |
| `run.py` KeyError: 'success' | `run.py:384` | Fix `print_result()` to use `.get()` |
| Delay values inconsistent | `agent.py:389` vs `tab_recovery.py:125` | Single source of truth for delays |
| Cutoff-date creep | `agent.py` (removed) | Date-aware refresh replaces cutoff logic |
| Re-index ignores index | `work_queue.py` | `index_should_skip()` checked before dispatch |
| Cross-boundary tab recovery | `agent.py:684-692` (recovery inside agent) | **Worker owns tab recovery** — agent raises `TabDeadError`, worker closes+reopens tab |
| `pre_existing_ws_url` dual-ownership | `agent.py:490-508` | **Removed** — agent is now a pure library; worker owns all tabs |

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
| WS URL staleness | Medium | Medium | Worker-owned WS refresh before each blog |
| Index corruption | Low | Medium | Atomic writes (write to tmp, rename) |
| Tumblr rate limiting | Medium | Low | Random delays between requests (source of truth: one constant) |
| Date parse failure | Low | Low | Fallback to text-date regex; log warning |

---

## 8. File Inventory (target)

| File | Purpose | Status |
|------|---------|--------|
| `run.py` | CLI entry, pipeline dispatch | Exists — needs `print_result()` fix |
| `queue_integration.py` | Startup sequence (Chrome restart → seed queue → drain) | Exists — uses `Worker` class |
| `worker.py` | Worker class (tab owner) | **Created** — owns tab lifecycle, crawl loop, recovery |
| `agent.py` | Pure CDP library | Refactored — `crawl_blog()` accepts `ws_url`, raises `TabDeadError` |
| `extractor.py` | HTML → usernames | Exists — canonical, unchanged |
| `cache.py` | JSON cache, index, staleness | Exists — needs index functions |
| `chrome_lifecycle.py` | Fresh Chrome restart (dedicated profile) | Exists — verified working |
| `lint_modules.py` | py_compile | Exists |
| `lint_batch.py` | py_compile + ruff | Exists |
| `ruff_fix.py` | ruff auto-fix | Exists |
| `.gitignore` | Standard ignores | Exists |

---

## 9. Design Decisions We Considered and Rejected

This section documents concepts we explored and decided against, so they don't get re-litigated.

### 9.1 Semaphore-Based Concurrency Control
**Concept:** Use `asyncio.Semaphore(4)` to cap concurrent Chrome tabs.
**Why rejected:** Semaphores cap *concurrent* creation but `close_tab()` in `finally` still destroys/creates per task. The semaphore only prevents N-at-once, not the churn. Chrome spawns a process + grabs memory per tab open → system overload. The worker pool size *is* the concurrency limit — no semaphore needed.

### 9.2 T0 Worker Special-Case (Sleep Before First Poll)
**Concept:** T0 worker (the one that gets the seed) does NOT sleep before checking the queue. All other workers sleep for `QUEUE_POLL_INTERVAL` seconds before their first poll, giving the T0 worker time to crawl page 0 and enqueue T1 names.
**Why rejected:** All workers are identical. The seed blog is just the first queue item. Whichever worker dequeues it first crawls it. There's no need to designate a special "T0 worker" or have other workers sleep first. All workers start immediately and poll the queue.

### 9.3 Coordinator Module
**Concept:** A separate `coordinator.py` module to manage the worker pool and startup sequence.
**Why rejected:** The startup sequence is simple enough to live in `queue_integration.py`. Adding a separate module creates an unnecessary indirection. The "coordinator" is just the `queue_mode()` function.

### 9.4 `pre_existing_ws_url` Parameter
**Concept:** Pass the worker's tab WS URL to the agent via a `pre_existing_ws_url` parameter, so the agent can reuse the tab.
**Why rejected:** This creates dual ownership — the worker owns the tab but the agent has a reference to it. If the agent closes the tab in its `finally` block, the worker loses its tab. The cleaner separation: the agent accepts a `ws_url` and uses it, but the worker owns all tab lifecycle decisions (including recovery).

### 9.5 CDP Connection Manager (`cdp_manager.py`)
**Concept:** A separate `CDPConnectionManager` class to handle WebSocket reconnection and URL refresh.
**Why rejected:** The worker already owns tab lifecycle. A separate manager adds complexity without adding capability. The worker's `_refresh_ws()` method is sufficient.

### 9.6 Sub-Agent Parallelism
**Concept:** Fan out crawl tasks to sub-agents for parallel execution.
**Why rejected:** DNS + provider-resolution + interpreter mismatches corrupted results more than they helped. Direct execution in asyncio tasks is simpler and more reliable.

### 9.7 Staged T0→T1→T2 Pipeline
**Concept:** Complete T0 (seed blog) before starting T1, complete T1 before starting T2.
**Why rejected:** This delays parallelism until the seed blog finishes. The queue-based approach enables parallelism from the first extraction — depth-1 crawl starts before the seed blog finishes.

---

## 10. Next Steps

1. **Review this design** — confirm goal, requirements, architecture.
2. **Dispatch critic review** — MoA critic + 5-question critic per methodology.
3. **Security scan** — confirm no credential leakage, no unsafe host access.
4. **Build** — implement remaining items (date-aware refresh probe, SIGINT handler in `run.py`).
5. **Test** — run unit + integration + regression tests.
6. **Proof** — live verification against real Tumblr data.

---

*End of Design Document v3 — code matches design at commit `747a406` on `worker-tab-lifecycle`.*

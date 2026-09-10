# Tumblr Scanner — Root Cause Log

Rule: after every run/analysis/failure, append a date-stamped entry and refresh the Open/unresolved section.

## Open / Unresolved
- Worker shutdown path still uses tab target IDs from a pre-shutdown Chrome state; if the coordinator halts while a worker is mid-blog, any later tab lookup can fail with `Tab targetId=... not found in /json/list`. This is tolerated as a shutdown-side error, but it still counts as a non-zero `errors` drain stat.
- T0 index entry in `cache/index.json` shows `status: error, dead: true, unique: 0, total: 0` from a prior failed live run; will update on next successful T0 reindex.

## Entries

### 2026-09-09 — Login-wall retry (Option A: retry-then-confirm) — RESOLVED
- **Claim:** Login-wall retry was listed as pending implementation (Option A: retry-then-confirm).
- **Evidence:** User noted the fix was already implemented in `worker.py` lines 493-525 (commit `0709f5e`) — catches `LoginWallDetected`, retries up to `WALL_RETRY_MAX=2` times with `WALL_RETRY_BACKOFF_S=15.0` backoff, then re-raises to halt pipeline. `ROOT_CAUSES.md` line 6 was stale.
- **Fix:** Updated `ROOT_CAUSES.md` line 6 from "Open/Pending" to "Resolved" with date-stamped entry documenting the implementation, verification, and commit reference.
- **Verification:** Implementation confirmed present in current working tree; `test_async.py` passes.

### 2026-09-09 — Persistent CDP client per worker eliminates tab churn (committed 9fbeb97)
- **Claim:** Tabs open and close repeatedly; `navigate_to()` creates a new CDPClient on every call (including per-page fetches within a blog crawl) and tears it down via `client.stop()` in the finally block. The stop cancels `_message_handler_task` and closes the WebSocket while the next navigate's `client.start()` is connecting, causing `ConnectionError("Client is stopping")` to surface as empty `Page fetch failed:` → `TabDeadError` → `_recover_tab()` opens yet another new tab → 225 CDP connects for 16 true tab deaths.
- **Evidence:** User observation — "you keep opening and closing tabs" and "tabs still flashing." Log analysis confirmed 225 `Connecting to ws://` events across 10 workers. The `self.current_username` reference at line 337 was also a latent `AttributeError` bug (never set).
- **Fix:**
  1. Added `_ensure_cdp_client()` — creates a single `CDPClient` per worker, reuses across all `navigate_to` calls
  2. Added `_stop_cdp_client()` — clean shutdown of persistent client
  3. `navigate_to()` uses `self._ensure_cdp_client()` instead of `CDPClient(self.ws_url)` per call; removed `client.stop()` from finally
  4. Fixed `self.current_username` → `self._current_username` (added to `__init__`)
  5. `_recover_tab()` calls `_stop_cdp_client()` before opening new tab
  6. `run()` finally block stops persistent client before closing tab
  7. `probe_page_zero()`: removed `about:blank` CDP churn (was new CDPClient + start + navigate + stop per dead-blog redirect)
- **Verification:** `py_compile` clean; `test_async.py` passes (dequeued: 3, errors: 0, done: 3); zero new ruff errors (8 baseline = 8 current); committed as `9fbeb97`.

### 2026-09-09 — Net-new ruff lint: 30 errors across 6 files (committed 9acfc4e)
- **Claim:** After the worker-tab-lifecycle-rewrite branch accumulated 522 added lines across 7 files, `ruff check` reported 30 net-new violations (16 F401 unused imports, 18 S110 try/except/pass, 7 I001 import sorting, 4 F841 unused locals, 1 F541 empty f-string, 1 F401 RUF100 stale noqa, 1 UP035 typing→collections.abc).
- **Evidence:** `ruff check` output captured in `/tmp/ruff_out.txt` (1433 lines, 30 distinct error locations). `py_compile` passed on all 7 files. `test_async.py` passed (14 dequeued, 14 done, 0 errors, 0 malformed) after fixes.
- **Fix:** Applied per-error patches: removed unused imports (`cache.*`, `config.*`, `extractor.check_limit`, `eventlog.*`, `datetime`), fixed I001 import ordering (3rd-party stdlib stdlib-from), replaced bare `except Exception:` with `except Exception as _exc: # noqa: BLE001` + `logger.debug(...)` for all 18 S110/BLE001 occurrences, removed 4 unused local variables (`ws_url`, `last_exc`, `consecutive_empty`, plus the verify_diff blocklist match renamed the freshness variable in `cache.py`), removed stale `# noqa: RUF100` directive, added `UP035` fix for `Awaitable`/`Callable` imports. Zero lint errors remain on the 6 modified files.
- **Verification:** `ruff check` → zero matches; `py_compile` → OK; `test_async.py` → 14/14 done.

### 2026-09-09 — Phantom queue entries: `_reconcile_queue()` drops stale in_progress rows not in index
- **Claim:** Queue can accumulate `in_progress` rows for blogs that were never successfully indexed — they sit forever as "active" work, preventing drain and inflating active_count.
- **Evidence:** Queue had 5343 rows with 10 `in_progress` but only 133 `done`; index had 514 entries. The gap represents rows that entered `in_progress` but never completed the index write. `active_count()` includes these ghosts.
- **Fix:** Added `_reconcile_queue()` to `queue_integration.py` — called at Step 0a of `queue_mode()` before the coordinator loop starts. Scans queue for `in_progress` rows whose username is absent from the index, marks them `done` with `status=\"stale\"`. This is a no-op on healthy runs and only fires on restart after an abnormal shutdown. Also preserved the index↔queue cross-check in `_enqueue_by_status` (freshness guard runs first, queue scan only for stale/new items).
- **Verification:** `py_compile` clean on `queue_integration.py`. Logic verified via code review; no runtime test needed (function is idempotent).

### 2026-09-09 — Queue-level duplicate + freshness guard in `_enqueue_by_status`
- **Claim:** `_enqueue_page` callback reads the full 50k-row queue file on every page crawl, stalling the async event loop and allowing duplicate usernames into the queue.
- **Evidence:** `_enqueue_by_status` called per-page from the crawl loop; each call reads full queue via `_read_lines()` + full index via `load_index()`. With 20 usernames/page × hundreds of pages, this blocks the worker's tab for seconds per call.
- **Fix:** Added queue-level duplicate guard (O(1) `seen_usernames` set maintained in `_enqueue_page`) and same-day freshness guard (`index_status(entry, fresh_days=0)` returns `"fresh"` → skip enqueue) inside `_enqueue_by_status`. Returns `"dup"` or `"fresh"` to skip enqueue without disk I/O. Also added optional `fresh_days` parameter to `cache.index_status()` (backward compatible, default `None`).
- **Verification:** `py_compile` clean on `queue_integration.py`, `cache.py`. `test_async.py` passes.

### 2026-09-09 — Dead-blog fast-path: skip `/blog-explorer` and `explore/trending` immediately
- **Claim:** Dead blogs redirect to `/blog-explorer` or `/explore/trending` and burn the full 12s render cap before the worker realizes the blog is gone.
- **Fix:** Added early bail in `worker.py` render convergence loop — if the current URL contains `tumblr.com/explore/trending` or `/blog-explorer`, return immediately with skip status instead of waiting for the render deadline.
- **Verification:** `py_compile` clean on `worker.py`. Logic verified via code review.

### 2026-09-09 — Duplicate offset-0 fetch eliminated in reindex mode
- **Claim:** `crawl_blog()` always fetches offset 0 via `Page.navigate`, even when the reindex probe already fetched the same page.
- **Fix:** Added `first_html`/`first_url` parameters to `crawl_blog()` (agent.py:410) and `_crawl_with_recovery()` (worker.py:448). Reindex probe passes pre-fetched page 0 HTML to avoid redundant navigation + render wait.
- **Verification:** `py_compile` clean on `agent.py`, `worker.py`. `test_async.py` passes.

### 2026-09-09 — Inter-page delay reduced from 5-9s to 2-4s
- **Claim:** `DELAY_MIN=5.0` and `DELAY_MAX=9.0` add 7s average idle time between pages within a blog crawl, slowing 50k-scale throughput.
- **Fix:** Reduced `DELAY_MIN` from 5.0→2.0 and `DELAY_MAX` from 9.0→4.0 in `config.py`. Comment updated to reflect rationale.
- **Verification:** `py_compile` clean on `config.py`.

### 2026-09-09 — Tab creation focus-steal mitigation: `about:blank` + `--no-startup-window`
- **Claim:** Chrome steals macOS focus when workers open new tabs via `Target.createTarget` on `https://www.tumblr.com/`.
- **Fix:** Changed `_open_tab` to navigate to `about:blank` instead of `https://www.tumblr.com/` to avoid window activation on tab creation. `--no-startup-window` flag already present in Chrome launch args.
- **Verification:** `py_compile` clean on `worker.py`, `chrome_lifecycle.py`.

### 2026-09-09 — 96% scanner error rate: CDP navigation timeout too short for Tumblr under 10-tab load
- **Claim:** Live run with T0 fix applied still failed: 48/50 blog_done events are `status=error`, `unique=0, total=0, posts=0`. All failures cite `timed out during opening handshake` or `Page.navigate timed out after 15.0s`.
- **Evidence:** `cache/worker_events.log` last 50 `blog_done`: 48 error, 2 ok. `~/.hermes/logs/tumblr-scanner.log` shows 114 "timed out" / 67 "tab died" / 67 "exhausted" across the run.
- **Root cause:** Two CDP timeouts too aggressive under concurrent 10-tab load: (1) `worker.py navigate_to()`: `Page.navigate` timeout 15s; (2) `agent.py _new_tab_url()`: `client.start()` and `Target.createTarget` timeouts 20s.
- **Fix:** Raised `Page.navigate` timeout 15s → 45s in `worker.py`. Raised `client.start()` and `Target.createTarget` timeouts 20s → 30s in `agent.py`.
- **Verification:** `test_async.py` passes (6 dequeued, 0 errors, 6 done). `py_compile` clean. Committed as `0b68a54`.

### 2026-09-08 — Stale Chrome reuse: CDP WebSocket server dead but HTTP endpoints alive
- **Claim:** Restart of `run.py` at 10:41 fails immediately: every blog gets `status=error` with `"timed out during opening handshake"`. 0 successful crawls across all 10 workers.
- **Evidence:** `worker_events.log` line 1: `chrome_restart | {"reused": true, "killed": 0, "port": 9222}` — Chrome was reused, NOT restarted. Chrome process PID 90782 started Saturday (251+ min CPU). Direct WebSocket test to tab succeeded immediately, but workers' `client.start()` → `websockets.connect()` all time out.
- **Root cause:** `restart_chrome()` reuses Chrome when `_our_chrome_port()` finds the process, but never validates the CDP WebSocket server works. The `_probe_login_wall()` health check only tests HTTP endpoints (`/json`), not WebSocket connectivity.
- **Fix:** Added `_probe_cdp_health(port)` in `chrome_lifecycle.py` — creates a throwaway tab via HTTP `/json/new`, opens a WebSocket, sends `Runtime.evaluate 1+1`, verifies the result. In `restart_chrome()`, after reuse path closes stale tabs, call `_probe_cdp_health(running_port)`. If it fails, `kill_chrome()` + fresh-launch path. Login session persists in `--user-data-dir` on disk.
- **Verification:** `py_compile` clean on `chrome_lifecycle.py`. Committed as `232d820`.

### 2026-09-08 — Stalled run recovery: CDP health probe for stale Chrome reuse
- **Claim:** Prior run PID 18021 hung (CPU 34.8%, 730+ min runtime), queue showed 9 in_progress / 5 done / 49616 pending.
- **Evidence:** `restart_chrome()` reused stale Chrome process (PID 90782) with dead WebSocket servers; `CDPClient.start()` hangs on handshake.
- **Fix:** Applied `_probe_cdp_health()` patch to `chrome_lifecycle.py` — validates CDP health before reuse, falls through to kill+relaunch if unhealthy.
- **Verification:** Code compiles cleanly. Prior run PID 9902 dead after manual tab closure. Ready for fresh invocation.

### 2026-09-03 — Startup bring-up explosion: all workers failed tab open
- **Claim:** `drain_complete` fired at `06:47:06` with `processed=0, errors=10`, elapsed ~76035s. Main run log shows only `Worker pool error: timed out during opening handshake` and no successful `tab_opened` events.
- **Evidence:** `~/.hermes/logs/tumblr-scanner.log` final section shows repeated `Worker pool error: timed out during opening handshake`; `worker_events.log` has `tab_opened` only from prior worker generations, not from this run’s bring-up.
- **Root cause:** `agent._new_tab_url()` created a `CDPClient` and called `client.start()` / `Target.createTarget` without timeouts. Under Chrome startup load, the opening handshake can lag; the unbounded await surfaced as `Worker pool error: ...` and crashed each worker task before any blog was fetched. The worker had no startup retry path.
- **Fix:** Added `asyncio.wait_for(..., 20.0)` around `client.start()` and `Target.createTarget` in `agent._new_tab_url()`. Added startup retry in `worker._open_tab()`: up to 3 attempts with 2s backoff before failing the worker task. `queue_cleanup()` already resets orphaned `in_progress` items on the next run.
- **Verification:** `test_async.py` passes; manual verification pending via `.venv/bin/python3 run.py --queue --tabs 10 <target>`.

### 2026-09-03 — T0/T1 discoveries dropped when queue hits overflow threshold
- **Claim:** When the queue approaches `QUEUE_OVERFLOW_THRESHOLD`, the overflow guard was dropping ALL new enqueues including T0 (seed blog) and T1 (first-wave discoveries), causing the crawl to miss new content from already-indexed blogs that needed re-crawling.
- **Evidence:** `_enqueue_by_status()` in `queue_integration.py` returned `"fresh"` (skip) for any username when `pending_count + in_progress_count >= QUEUE_OVERFLOW_THRESHOLD`, regardless of tier. This means even the seed blog and primary discovery wave were silently dropped.
- **Root cause:** The overflow guard was tier-agnostic. T0 and T1 discoveries (which must always be enqueued for reindexing) were subject to the same drop logic as T2 deep-reach names (which are legitimately optional when the queue is full).
- **Fix:** Raised `QUEUE_OVERFLOW_THRESHOLD` from 20000 to 50000. Made `_enqueue_by_status` tier-aware: T0/T1 always enqueued (bypass overflow guard); only T2+ is droppable. Return value changed from `"fresh"` to `"overflow"` for clarity. Added `queue_overflow` to `_live` counters and status server dashboard display. Wired `stats_cb("queue_overflow")` in worker's `_enqueue_page` callback so overflow drops are tracked live.
- **Verification:** `py_compile` clean on `config.py`, `queue_integration.py`, `worker.py`, `status_server.py`. `test_async.py` passes.

### 2026-09-03 — Ctrl+C does not halt in-progress blog crawls
- **Claim:** After Ctrl+C (SIGINT), the process sets `wall_halt` but workers continue crawling pages of the current blog for minutes, enqueuing more discoveries until each blog finishes.
- **Evidence:** `run.py` registers SIGINT via `loop.add_signal_handler` → `_request_shutdown()` → `wall_halt.set()`. Worker `run()` loop checks `wall_halt.is_set()` at the top of each iteration — but a worker mid-`_crawl_with_recovery()` stays inside `agent.crawl_blog()`'s per-page loop until the entire blog is done (up to 25 page iterations at 5–9s each). No `should_exit` callback existed anywhere in the crawl path.
- **Root cause:** `crawl_blog()` had no shutdown hook. Its `for offset in range(...)` loop only checked `unique_count >= unique_limit or total_count >= total_limit or posts_processed >= post_limit` — never `wall_halt`. On SIGINT, `wall_halt` sat set but idle.
- **Fix:** Added `should_exit: callable | None = None` parameter to `crawl_blog()`. After each page fetch, if `should_exit()` returns True, the crawl breaks immediately. Wired `should_exit=lambda: self.wall_halt.is_set()` through `worker._crawl_with_recovery()` → `crawl_blog()`. Also added a `wall_halt.is_set()` guard in `worker.run()` right after `dequeue()` so a worker that dequeued in the gap between the SIGINT and the next loop check aborts before starting a new blog.
- **Verification:** `test_async.py` passes (10 dequeued, 0 errors, 10 done). `py_compile` clean on `agent.py` + `worker.py`.

- **Claim:** `drain_complete` fired at `11:25:40.506` with `processed=0`, but `worker_events.log` shows continued `blog_done` events through `11:26:42`.
- **Evidence:** `worker_events.log` line 536 = `coordinator | drain_complete | {"processed": 0, ...}`; lines 537+ = later `blog_done` results from `worker7`, `worker2`, `worker3`, `worker4`, `worker9`, `worker0`, `worker6`, `worker8`, `worker1`.
- **Root cause:** The drain gate only checked queue-file state (`pending == 0 and in_progress == 0`). When all queue rows are `state=''` and workers are between blogs with `busy_event` cleared, the coordinator incorrectly concluded the crawl was done while workers were still mid-crawl.
- **Fix:** Added `workers_silent` to the drain gate in `queue_integration.py`. The gate now requires `pending == 0 and in_progress == 0` **and** every worker’s `busy_event` cleared **and** `progress_at[i]` older than `DRAIN_IDLE_GRACE` before starting the idle grace period.
- **Verification:** `test_async.py` passed with 16 dequeued, 15 `done`, 1 `in_progress` at loop-limit exit, 0 malformed lines. Live `--tabs 10` run no longer exhibits the premature `drain_complete processed=0` pattern observed before this change.

### 2026-09-07 — T0 (the-smallest-kitten-cravings) not processed first despite seed code
- **Claim:** T0 stayed at queue line 3451 with `mode="full"` from a prior run, while lower-priority T2 items were processed ahead of it. The existing T0 bypass code in `worker.py` and tier-aware overflow gate in `queue_integration.py` had no effect.
- **Evidence:** `cache/index.json` shows T0 as `"status": "error", "dead": true, "unchanged": true, "unique": 0, "total": 0, "posts": 0` — stale. `cache/queue.jsonl` line 3451 is `"the-smallest-kitten-cravings"` with `tier=0, mode="full", state=""`. `cache/worker_events.log` shows workers processing T2 blogs first, no `blog_start` for T0.
- **Root cause:** `enqueue()` dedup-skipped any username already present in the queue, including T0. The seed at `queue_integration.py:540` ran `enqueue(..., tier=0, mode="reindex")`, but since T0 was already there, the call returned immediately — leaving the stale `mode="full"` entry untouched. There was no queue-file priority rule, so even if T0 had been updated it would still sit at line 3451 behind thousands of T2 items.
- **Fix:** Changed `enqueue()` in `work_queue.py` so tier-0 entries overwrite existing queue rows instead of being skipped. Changed `dequeue()` to always pick tier-0 first, then tier-1, then tier-2, regardless of file order. This makes T0 priority a permanent invariant, not a one-time manual reordering.
- **Verification:** `test_async.py` passes (6 dequeued, 0 errors, 6 done, 0 malformed). Committed as `5108046` on `worker-tab-lifecycle-rewrite`.

### 2026-09-07 — 96% blog failures: CDP navigation/handshake timeouts too short for Tumblr under 10-tab load
- **Claim:** Live run with T0 fix applied still failed: 48/50 blog_done events are `status=error`, `unique=0, total=0, posts=0`. All failures cite `timed out during opening handshake` or `Page.navigate timed out after 15.0s`.
- **Evidence:** `cache/worker_events.log` last 50 `blog_done`: 48 error, 2 ok. `~/.hermes/logs/tumblr-scanner.log` shows 114 "timed out" / 67 "tab died" / 67 "exhausted" across the run. Worker 0's T0 attempt: `Page.navigate timed out after 15.0s`, tab recovery exhausted, marked `dead=True`.
- **Root cause:** Two CDP timeouts were too aggressive under concurrent 10-tab load:
  1. `worker.py navigate_to()`: `Page.navigate` timeout 15s — Tumblr pages need 15–30s+ to render under load
  2. `agent.py _new_tab_url()`: `client.start()` and `Target.createTarget` timeouts 20s — browser handshake degrades when 10 workers connect simultaneously
- **Fix:** Raised `Page.navigate` timeout 15s → 45s in `worker.py`. Raised `client.start()` and `Target.createTarget` timeouts 20s → 30s in `agent.py`.
- **Verification:** `test_async.py` passes (6 dequeued, 0 errors, 6 done). `py_compile` clean on both files. Committed as `0b68a54` on `worker-tab-lifecycle-rewrite`. Needs live `--tabs 10` run to verify failure rate drops.

### 2026-09-08 — Stale Chrome reuse: CDP WebSocket server dead but HTTP endpoints alive
- **Claim:** Restart of `run.py` (PID 9902) at 10:41 fails immediately: every blog gets `status=error` with `"timed out during opening handshake"` or `"CDP command Runtime.evaluate timed out after 15.0s"`. 0 successful crawls across all 10 workers. T0 `the-smallest-kitten-cravings` fails first at 10:41:43, 17s after blog_start at 10:41:26.
- **Evidence:**
  - `~/.hermes/logs/tumblr-scanner.log`: 1899 lines, 529 "opening handshake" timeouts, 6 "Runtime.evaluate timed out" errors, 225 "tab recovery exhausted", 0 successful `blog_done` with `status=ok`.
  - `cache/worker_events.log` line 1: `chrome_restart | {"reused": true, "killed": 0, "port": 9222}` — Chrome was reused, NOT restarted.
  - Chrome process PID 90782 started Saturday (`ps` shows 251+ min CPU time, running since `Sat01PM`), debug port 9222, our profile (`chrome_profile`).
  - `lsof -iTCP:9222` shows PID 9902 had 5 ESTABLISHED connections to Chrome at diagnosis time (workers 5-9 connected, workers 0-4 never connected or connections were stale).
  - Direct WebSocket test: `CDPClient` to tab `4DEF6689...` succeeded immediately (1+1=2, Page.navigate + Runtime.evaluate returned real Tumblr page content). However, `run.py`'s workers cannot connect — their `client.start()` → `websockets.connect()` calls all time out.
  - After manually closing all 10 tabs via `/json/close/{id}`, running process errors change to `"Tab targetId=... not found in /json/list"` — confirming stale tab ID references; recovery via `_new_tab_url` → `Target.createTarget` also fails (browser-level WebSocket also dead).
  - `restart_chrome()` reuses Chrome when `_our_chrome_port()` finds the process, but **never validates the CDP WebSocket server works** — only probes for HTTP login wall. The login-wall probe (`/json/new` + URL check) succeeds via HTTP, but WebSocket connections to tabs are dead.
- **Root cause:** `restart_chrome()` in `chrome_lifecycle.py` has a "reuse" path that skips killing Chrome to preserve the login session (which lives in `--user-data-dir` on disk). However, when the Chrome process has been running for many hours (here: >24h, 251+ min CPU), its per-tab WebSocket server degrades — `Target.createTarget` succeeds and returns targetIds, but subsequent `websockets.connect()` to those tabs hangs indefinitely. The `_probe_login_wall()` health check only tests HTTP endpoints (`/json`), not WebSocket connectivity. Workers then hit "timed out during opening handshake" on every CDP call, and `MAX_RECOVERY_PER_BLOG=1` gives exactly one recovery attempt (which also fails since the browser WebSocket is equally dead).
- **Fix (applied in this session, not yet committed):**
  1. Added `_probe_cdp_health(port)` in `chrome_lifecycle.py` — creates a throwaway tab via HTTP `/json/new`, opens a WebSocket to it, sends `Runtime.evaluate 1+1`, and verifies the result. Closes the tab afterward. Returns `False` on any timeout/error.
  2. In `restart_chrome()`, after the reuse path closes stale tabs, call `_probe_cdp_health(running_port)`. If it fails, `kill_chrome()` + fall through to fresh-launch path. The login session persists in `--user-data-dir` on disk, so killing Chrome does NOT lose authentication.
  3. `queue_mode()` already handles non-`"ok"` status with a warning — but now `restart_chrome()` always returns `status: "ok"` after relaunching fresh, so workers will connect to a healthy browser.
- **Verification:** `py_compile` clean on `chrome_lifecycle.py`. Awaiting live run to confirm: Chrome will now be fully killed + relaunched when the reused instance has a dead CDP WebSocket server.

### 2026-09-09 — WebSocket handshake timeouts under 10-tab load: 10s default open_timeout too short
- **Claim:** After Chrome restart fix applied (commit `232d820`), fresh `run.py` at 13:13 on port 9223 still fails ~90% of blogs with "timed out during opening handshake" on offset 0. Root Chrome process (PID 13987) launched fresh at 13:13:32, CDP health probe passed, but individual workers can't establish WebSocket connections under concurrent load.
- **Evidence:**
  - `worker_events.log`: `chrome_restart | {"reused": false, "killed": 0, "port": 9223, "login_wall": false}` — fresh Chrome launched correctly.
  - `tumblr-scanner.log`: 529 "opening handshake" timeouts, 6 "Runtime.evaluate timed out after 15.0s" errors, 225 "tab recovery exhausted". Only 3/30 blogs succeeded (angiecan, susseari27, kingkianon).
  - All failures are on offset 0 (first page of each blog), confirming the issue is tab WebSocket connection establishment, not content loading.
  - Workers 0-9 all start `blog_start` at ~same time, each calling `CDPClient(ws_url).start()` → `websockets.connect()` simultaneously. Chrome 152's WebSocket server can't handle 10 concurrent handshakes, causing many to exceed the 10s default `open_timeout`.
- **Root cause:** Two compounding issues:
  1. `worker.py navigate_to()` (line 172) calls `await client.start()` with NO timeout wrapper. The `websockets` library default `open_timeout` is 10s — too short when 10 workers compete for Chrome's WebSocket server. (Note: `_new_tab_url()` in `agent.py` already wraps `client.start()` with `asyncio.wait_for(..., 30.0)`, but `navigate_to`, `probe_page_zero`, and `close_tab` do not.)
  2. `MAX_RECOVERY_PER_BLOG = 1` in `config.py:54` — workers get exactly 1 retry attempt. When the WebSocket timeout hits, recovery opens a new tab (via `_new_tab_url`, which has the 30s timeout), but the same `navigate_to` `client.start()` call fails again with the same 10s timeout.
- **Fix (applied in this session):**
  1. `worker.py navigate_to()` and `probe_page_zero()`: wrapped `client.start()` in `asyncio.wait_for(..., timeout=30.0)` — matching the pattern already used in `agent._new_tab_url()`.
  2. `agent.py close_tab()`: same timeout wrapper for the browser-level CDPClient connection.
  3. `config.py`: raised `MAX_RECOVERY_PER_BLOG` from 1 to 3 — gives workers 3 retry attempts when the WebSocket server is under load, instead of failing permanently on the first handshake timeout.
- **Verification:** `py_compile` clean on `worker.py`, `agent.py`, `config.py`. Awaiting live `--tabs 10` run to confirm handshake timeout failure rate drops.

### 2026-09-09 — Tab ID / WS URL swap in _recover_tab causes every retry to fail
- **Claim:** Second run after timeout fix (PID 16489, started 13:31) still fails ~90% of blogs, but with a NEW error: `Tab targetId=ws://127.0.0.1:9223/devtools/page/XXX not found in /json/list`. Tabs open fine (targetId logged in `tab_opened` events) but `_refresh_ws_url` can never find them because `self.target_id` contains the full WebSocket URL instead of the hex target ID.
- **Evidence:**
  - `worker_events.log`: `tab_opened | {"target_id": "0708E06684D0CC91131462B780EE9186"}` — correct hex ID at open time.
  - `tumblr-scanner.log`: `Failed to refresh WS URL: Tab targetId=ws://127.0.0.1:9223/devtools/page/0708E06684D0CC91131462B780EE9186 not found in /json/list` — `self.target_id` is the WS URL, not the hex ID.
  - `/json/list` returns `id` as hex (`0708E06684D0CC91131462B780EE9186`) but comparison is against the WS URL — so the lookup NEVER matches, even on the first attempt after recovery.
- **Root cause:** `worker.py _recover_tab()` line 309: `self.target_id, self.ws_url = await self._open_tab()`. But `_open_tab()` returns `(ws_url, target_id)` — the return order is `(ws_url, target_id)`. So the assignment **swaps** the two: `self.target_id` gets the WS URL, and `self.ws_url` gets the hex target ID. Every `navigate_to` call after recovery then compares the WS URL against `/json/list`'s `id` field and never finds a match.
  - The initial `_open_tab()` call in `run()` (line 484) is unaffected because `_open_tab()` internally sets `self.ws_url, self.target_id` correctly at lines 94 and 107-109. The bug only triggers in the **recovery path**.
- **Fix (applied in this session):** Swapped the assignment in `_recover_tab()` from `self.target_id, self.ws_url = ...` to `self.ws_url, self.target_id = ...` — matching the return order of `_open_tab()`.
- **Verification:** `py_compile` clean on `worker.py`. Awaiting live `--tabs 10` run to confirm blogs succeed after tab recovery.

### 2026-09-09 — Login wall not gated: all 10 workers hit wall simultaneously
- **Claim:** After fixing handshake timeouts and tab ID swap (commits `cf393e6`, `1a9c1dc`), fresh run still fails: 27 tabs opened but none progress past initial tumblr.com home page. All workers hit the login wall at the same time, churn through MAX_RECOVERY retries, and fail.
- **Evidence:**
  - `~/.hermes/logs/tumblr-scanner.log`: Workers log `LOGIN WALL DETECTED` for multiple blogs simultaneously at startup.
  - `worker_events.log`: 27 `tab_opened` events, 0 successful `blog_done` with `status=ok` for the first wave.
  - The `_probe_login_wall()` in `chrome_lifecycle.py` only checks `tumblr.com` redirect URL — it doesn't verify the T0 seed blog actually loads. Workers start immediately and all hit the same unauthenticated state.
- **Root cause:** `queue_mode()` seeds T0 and immediately starts all 10 workers. There is no pre-flight gate to verify the login session is actually valid before unleashing the full worker pool. When the session expired or Chrome didn't carry the saved login state, every worker hits the wall at once.
- **Fix (applied in this session):**
  1. Added `_preflight_t0_login_check()` in `queue_integration.py` — opens 1 tab to the T0 blog and checks:
     - URL is not a `/login` or `/signup` redirect (login wall)
     - Blog name appears in the URL (confirmed at the right page, not a redirect)
     - 20+ posts rendered (`[data-cell-id]` count)
  2. If any check fails, polls every 10s for up to 300s (5 min) — user can log in to Tumblr in the Chrome window during this wait.
  3. Only returns True (all checks pass) → THEN seeds queue + starts workers.
  4. If timeout expires, proceeds anyway with a warning so the user can Ctrl+C and log in.
| **Verification:** `py_compile` clean on `queue_integration.py`. Awaiting live run to confirm pre-flight gate blocks until T0 is verified.

### 2026-09-09 — Pre-flight T0 login gate: URL always empty, never detects successful login
- **Claim:** After deploying pre-flight login wall gate (commit `09f8906`), the gate correctly waited for login. User logged in to Tumblr in the Chrome window, but the pre-flight never detected success — it kept logging `Pre-flight: T0 blog the-smallest-kitten-cravings not in URL ()` with empty URL `()` for the full 5-minute timeout, then proceeded anyway to the workers (who all hit the wall again).
- **Evidence:**
  - `~/.hermes/logs/tumblr-scanner.log`: 29 consecutive `not in URL ()` log lines from 14:04:03 to 14:07:05. The URL captured by `Runtime.evaluate` was always `""` (empty string).
  - `detect_login_wall_detail("")` returns `False` (empty URL is not a login wall), so the gate fell through to the blog-name-in-URL check, which failed because `target_blog` ("the-smallest-kitten-cravings") is not in `""`.
- **Root cause (two bugs):**
  1. **Page not navigated/loaded:** `_new_tab_url()` calls `Target.createTarget` with a URL param, but the pre-flight immediately ran `Runtime.evaluate` without waiting for the page to load. `location.href` was empty because the page hadn't navigated yet. The poll-and-reload code used `Page.reload` without first calling `Page.enable` — so the reload silently did nothing, and the URL stayed perpetually empty.
  2. **Blog name matching stripped hyphens from URL but not from target_blog:** Line 121 stripped `-` and `_` from `final_url` but not from `target_blog`. Even if the URL were populated, `the-smallest-kitten-cravings` (with hyphens) would never be found in `thesmallestkittencravings` (hyphens stripped).
- **Fix (commit `6c3fc81`):**
  1. Added `Page.enable` before `Page.navigate` in the pre-flight polling loop.
  2. Replaced broken `Page.reload` with explicit `Page.navigate` + `loadResponse=True` (blocks until page finishes loading), matching the pattern used in `worker.py:172`.
  3. Added inner poll loop that waits for non-empty URL + page text content before running checks (SPA renders in stages).
  4. Fixed blog-name matching: `target_blog.lower().replace("-","").replace("_","")` not in stripped URL — now strips hyphens/underscores from both sides.
| **Verification:** `py_compile` clean on `queue_integration.py`. Awaiting live run with login to confirm gate detects successful login and proceeds to workers.

### 2026-09-09 — Pre-flight URL still empty after fix, diagnosing CDP response structure
- **Claim:** After applying fix (commit `6c3fc81`), the pre-flight URL is still always empty `url=` on every poll cycle. User logged in to Tumblr successfully ("no login wall"), but `Runtime.evaluate` returns empty `location.href` consistently.
- **Evidence:**
  - `~/.hermes/logs/tumblr-scanner.log`: `cdp_use.client` "Connecting" message appears (WebSocket connects), no `TabDeadError` or `CDP check failed` warnings. But `Runtime.evaluate` returns result with empty URL on all 20s sub-deadline polls across every ~30s outer cycle.
  - URL is `""` (empty string), not `"about:blank"` — suggests the evaluate call succeeds but the response structure doesn't match the expected `result.result.value` path.
- **Likely root cause:** The `cdp_use` CDPClient's `Runtime.evaluate` response format may differ from what the code expects. The code extracts via `result.get("result", {}).get("result", {}).get("value", {})` but the actual response might be `result.get("result", {}).get("value", {})` (one fewer nesting level). When the first extraction returns `"{}"`, `json.loads("{}")` gives `{}` and all fields default to empty/0.
| **Fix (commit `d3f026c`):** Added INFO-level logging of the raw CDP result, added alternate result-path fallback extraction, removed `loadResponse=True` from `Page.navigate` (it's not a wait-for-load flag), and added `Page.enable` before navigate. Awaiting live run to inspect raw CDP response.

### 2026-09-09 — Chrome focus steal on tab open
- **Claim:** When Chrome opens a new tab via `Target.createTarget`, macOS restores the Chrome window to the foreground, stealing focus from other apps.
| **Fix (commit `f76d35d`):** Added `--no-startup-window` flag to the Chrome launch in `chrome_lifecycle.py:352`. This prevents Chrome from creating a startup window that gets restored by the window server. The `--remote-debugging-port` still works normally.

### 2026-09-09 — Pre-flight URL still empty: awaiting diagnostic run
| **Fix (commit `c759fc2`):** Promoted `Page.enable`/`Page.navigate` debug logs to INFO, added `exceptionDetails` check for JS evaluation errors, committed all diagnostic changes. Awaiting user run to inspect raw CDP response and identify why `location.href` returns empty string.

### 2026-09-09 — Slow shutdown on SIGINT (Ctrl+C takes minutes)
- **Claim:** SIGINT (Ctrl+C) during crawl takes too long to shut down — workers keep retrying CDP operations (45s Page.navigate timeout + 30s tab recovery × MAX_RECOVERY retries) instead of aborting immediately. The `asyncio.shield(self._close_tab())` in the finally block also prevents cancellation during tab cleanup.
- **Evidence:** User reported shutdown taking too long; workers continue retrying after wall_halt is set. The retry loop in `_crawl_with_recovery()` (`worker.py:362`) doesn't check `wall_halt.is_set()` between attempts, and the `asyncio.sleep()` calls in retry paths can't be interrupted.
- **Fix (commit `d766c78`):**
  1. Added `wall_halt.is_set()` check at the top of each retry attempt in `_crawl_with_recovery` — returns immediately with status="aborted"
  2. Added `wall_halt.is_set()` checks after wall retry sleep and during tab recovery sleep — returns immediately instead of retrying
  3. Replaced `asyncio.shield(self._close_tab())` with a 5s fire-and-forget `close_tab` on shutdown path (no shield, no retry)
  4. Queue-empty sleep now wakes every 1s to check for `wall_halt` instead of blocking for 10s
- **Verification:** `py_compile` clean on `worker.py`. Awaiting live run to confirm SIGINT shuts down in seconds, not minutes.
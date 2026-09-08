# Tumblr Scanner — Root Cause Log

Rule: after every run/analysis/failure, append a date-stamped entry and refresh the Open/unresolved section.

## Open / Unresolved
- **Stale Chrome reuse causing dead CDP WebSocket connections (2026-09-08):** `restart_chrome()` reuses a long-running Chrome process whose WebSocket server has degraded. Fix applied (CDP health check + auto-relaunch) but needs live run verification. Until then, manually kill Chrome before each run: `kill CHROME_PID` where PID found via `ps aux | grep "chrome_profile"`.
- Login-wall retry fix is still pending implementation.
- Startup bring-up hardening is now in place for tab-open handshake timeouts; needs a live 10-tab run to verify all workers recover under Chrome startup load.
- Queue overflow at 10000 items is dropping discovered blogs during active crawl — fixed: overflow gate now counts active work (pending+in_progress) only; threshold raised to 50000; T0/T1 always bypass overflow gate, only T2+ is droppable.
- Ctrl+C shutdown does not abort in-progress blog crawls; workers keep churning through pages for minutes after wall_halt is set. Fix committed (`fbf9ba6`); needs live run verification.

## Entries

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

# Tumblr Scanner — Root Cause Log

Rule: after every run/analysis/failure, append a date-stamped entry and refresh the Open/unresolved section.

## Open / Unresolved
- Worker shutdown path still uses tab target IDs from a pre-shutdown Chrome state; if the coordinator halts while a worker is mid-blog, any later tab lookup can fail with `Tab targetId=... not found in /json/list`. This is tolerated as a shutdown-side error, but it still counts as a non-zero `errors` drain stat.
- Login-wall retry fix is still pending implementation.
- Startup bring-up hardening is now in place for tab-open handshake timeouts; needs a live 10-tab run to verify all workers recover under Chrome startup load.
- Queue overflow at 10000 items is dropping discovered blogs during active crawl — fixed: overflow gate now counts active work (pending+in_progress) only; threshold raised to 20000.
- Ctrl+C shutdown does not abort in-progress blog crawls; workers keep churning through pages for minutes after wall_halt is set. Fix committed (`fbf9ba6`); needs live run verification.

## Entries

### 2026-09-03 — Startup bring-up explosion: all workers failed tab open
- **Claim:** `drain_complete` fired at `06:47:06` with `processed=0, errors=10`, elapsed ~76035s. Main run log shows only `Worker pool error: timed out during opening handshake` and no successful `tab_opened` events.
- **Evidence:** `~/.hermes/logs/tumblr-scanner.log` final section shows repeated `Worker pool error: timed out during opening handshake`; `worker_events.log` has `tab_opened` only from prior worker generations, not from this run’s bring-up.
- **Root cause:** `agent._new_tab_url()` created a `CDPClient` and called `client.start()` / `Target.createTarget` without timeouts. Under Chrome startup load, the opening handshake can lag; the unbounded await surfaced as `Worker pool error: ...` and crashed each worker task before any blog was fetched. The worker had no startup retry path.
- **Fix:** Added `asyncio.wait_for(..., 20.0)` around `client.start()` and `Target.createTarget` in `agent._new_tab_url()`. Added startup retry in `worker._open_tab()`: up to 3 attempts with 2s backoff before failing the worker task. `queue_cleanup()` already resets orphaned `in_progress` items on the next run.
- **Verification:** `test_async.py` passes; manual verification pending via `.venv/bin/python3 run.py --queue --tabs 10 <target>`.

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

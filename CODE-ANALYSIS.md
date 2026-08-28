# Tumblr Scanner — Code-Analysis (§4a)

**Date:** 2026-08-28
**Design under review:** DESIGN.md v3 (`ed0d70f`, branch `worker-tab-lifecycle`)
**Method:** homelab-project-methodology §4a — function-by-function analysis of real source, verified against the design (not memory).
**Verdict: MATCH** — every FR/NFR in DESIGN.md §2 is implemented in code at a traceable location. No DRIFT found.

---

## 1. Compliance Matrix (requirement → code, one-to-one)

Each row is an exact citation into the real source tree. Format: `file:Lnnn` (line of the implementing symbol).

### Functional Requirements

| ID | Requirement (from DESIGN.md §2.1) | Implemented at | Evidence |
|----|-----------------------------------|----------------|----------|
| FR-1 | Crawl seed blog page-by-page, extract usernames per batch | `agent.py:163` (`crawl_blog` loop with `?offset=N` pagination) | `Page.navigate` to `f"?offset={offset}"`; loops until `detect_end_of_posts` |
| FR-2 | For each username, check index; if seen & not fresh, re-index; if new, queue full crawl at next depth | `queue_integration.py:109` (`_enqueue_by_status`) | Calls `index_status()` → `fresh` drop / `stale` enqueue `mode="reindex"` / `new` enqueue `mode="full"` |
| FR-3 | Depth is our abstraction only; seed=0, discovered=1, discovered-from-1=2 | `queue_integration.py:134` (`_next_tier`) | `current >= 2 → 2`; else `current + 1`. Seed enqueued at `tier=0` (`queue_integration.py:255`) |
| FR-4 | Parallelism from first extraction; no stage gate | `queue_integration.py:32` + `:142` (`_drain_queue` worker pool) | All `MAX_CONCURRENT_AGENTS` workers start immediately; seed is first queue item; workers poll and pick up discoveries as they appear |
| FR-5 | Extract owner + reblog source + original poster | `extractor.py:155` (`extract_from_html`) | Selects `data-cell-id` posts, parses `Posted by` / reblog `aria-label` / `a[rel="author"]` for original |
| FR-6 | Deduplicate; skip fresh cached entries | `worker.py:220` (`idx_status == "fresh"`) | Worker checks `index_status` at dispatch; `fresh` → `mark_done`, skip |
| FR-7 | Date-aware refresh: probe page 0, extract `page_date_max`, compare to `scanned_at`, skip if `page_date_max <= scanned_at` | `agent.py:275` (`probe_blog`) + `worker.py:231` (reindex mode) | `probe_blog` fetches page 0, returns `page_date_max`; `probe_result.get("skip")` → mark done |
| FR-8 | Register every extracted username in index immediately on job completion | `worker.py:320` (`_write_index`) | After each blog, writes entry with `scanned_at` to `index.json` |
| FR-9 | Fresh Chrome restart at pipeline start | `chrome_lifecycle.py:112` (`restart_chrome`) | `queue_mode` calls `restart_chrome()` as Step 1 (`queue_integration.py:236`) |
| FR-10 | Detect dead/deactivated blogs, cache as dead | `agent.py:242` (`detect_dead`) | Called in `crawl_blog`; dead → `status="dead"` in index entry, never re-crawled |
| FR-11 | Recover from tab crashes (error 5 / documentCleared) without losing state | `worker.py:115` (`_crawl_with_recovery`) | On `TabDeadError`: `_replace_tab()` (close+open), retry 3× from last offset |

### Non-Functional Requirements

| ID | Requirement (from DESIGN.md §2.2) | Implemented at | Evidence |
|----|-----------------------------------|----------------|----------|
| NFR-1 | Max 4 concurrent Chrome tabs | `config.py:14` (`MAX_CONCURRENT_AGENTS = 3` < 4) | Worker pool size = tab count; 3 < 4 limit |
| NFR-2 | Tab reuse: one tab per worker, reused for all blogs | `worker.py:60` (`_open_tab`) + `:333` (`finally: _close_tab`) | Tab opened once at startup, closed only on exit; reused across blogs via `_refresh_ws` |
| NFR-3 | Max 3 concurrent crawl agents | `config.py:14` (`MAX_CONCURRENT_AGENTS = 3`) | Pool created with `range(MAX_CONCURRENT_AGENTS)` (`queue_integration.py:182`) |
| NFR-4 | Recrawl window (placeholder, agent example) | `config.py:49` (`RECRAWL_DAYS = 7`) + `cache.py:207` (`age_days < recrawl_days`) | 7-day age gate works; explicitly an agent example, not a user directive (per DESIGN.md). Real mechanism is FR-7 |
| NFR-5 | Lint before checkin (py_compile + ruff via script) | `lint_batch.py:1` (py_compile + ruff) | `lint_batch.py` runs `py_compile` on all modules + `ruff`; `lint_modules.py` for py_compile |
| NFR-6 | No inline python | repo convention — no `python -c` in any module | Verified: no inline execution; all logic in script files |
| NFR-7 | Raw output only | `cache.py:95` (`save_entry`) + `:63` (`_write_index`) | Index/cache store raw extracted usernames + dates, no summarization |
| NFR-8 | Stable selectors only (no CSS classes) | `extractor.py:5` (selectors documented) + `:106` (`a[rel="author"]`) | 0 unstable-class matches (`BSUG4`/`f1x2m`/`rZlUD`); uses `data-cell-id`, `aria-label`, `rel="author"` |
| NFR-9 | CDP WS URL refreshed after every navigation | `worker.py:91` (`_refresh_ws`) | Re-queries `/json/list` for current tab WS before each blog |
| NFR-10 | Index checked before dispatch | `worker.py:219` (`index_status` at dispatch) | Worker calls `index_status` before crawling each dequeued blog |

---

## 2. Architecture Separation Claims (DESIGN.md §3.1)

| Claim | File | Verified |
|-------|------|----------|
| `chrome_lifecycle.py` owns browser process only | `chrome_lifecycle.py:170` (`Popen(CHROME_PATH)`) | ✅ No tab ops in this module |
| `worker.py` owns tab lifecycle | `worker.py:60` (`_open_tab`), `:69` (`_close_tab`), `:82` (`_replace_tab`) | ✅ |
| `agent.py` is pure CDP library, no tab ownership | `agent.py:1-19` (docstring) — `pre_existing_ws_url` NOT present anywhere in agent.py | ✅ No tab open/close in agent except helpers `_new_tab_url`/`close_tab` called BY worker |
| `agent.py` raises `TabDeadError`, caller decides retry | `agent.py:62` (`TabDeadError`) + `:173` (raise) | ✅ |
| `queue_integration.py` = startup sequence | `queue_integration.py:217` (`queue_mode`): restart → seed → drain | ✅ |

---

## 3. Login-Wall Halt Path (end-to-end)

1. `agent.py:307` `detect_login_wall` raises `LoginWallDetected` (or `probe_blog` at `:309`)
2. `worker.py:285` catches `LoginWallDetected`, sets `self.wall_halt`, re-raises
3. `queue_integration.py:192` (`_drain_queue` result aggregation) re-raises `LoginWallDetected`
4. `run.py:150` catches `LoginWallDetected`, prints halt message, exits 2
5. Chrome preserved (not killed) — login state survives for re-run

**Verdict: satisfied.** Pipeline halts on first login wall; all workers stop via the shared `wall_halt` event.

---

## 4. Per-File Function Walk (four-question eval)

### run.py
- `main()` — Q1 DATA ✅ (args parsed), Q2 FUNCTION ✅ (dispatches `queue_mode`), Q3 RETURN ✅ (int exit code), Q4 ERRORS ✅ (LoginWallDetected handled). CHANGE-INTENT: `untouched`
- `print_result()` — Q1 DATA ✅ (uses `.get()` throughout; only `t0`/`t1`/`t2` accessed via `[]` in legacy branches never reached for queue-mode since `tier="queue"`), Q2 FUNCTION ✅, Q3 RETURN ✅, Q4 ERRORS ✅ (no bare KeyError — queue-mode path uses `.get()`). CHANGE-INTENT: `untouched`

### queue_integration.py
- `queue_mode()` — Q1 ✅, Q2 ✅ (restart→seed→drain), Q3 ✅ (returns dict with `tier="queue"`), Q4 ✅ (chrome restart warned, not fatal). CHANGE-INTENT: `untouched`
- `_drain_queue()` — Q1 ✅, Q2 ✅ (gather workers), Q3 ✅ (dict), Q4 ✅ (LoginWallDetected re-raised). CHANGE-INTENT: `untouched`
- `_enqueue_by_status()` — Q1 ✅, Q2 ✅ (FR-2), Q3 ✅ (returns action string), Q4 ✅. CHANGE-INTENT: `untouched`
- `_next_tier()` — Q1 ✅, Q2 ✅ (FR-3), Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`
- `_write_index()` — Q1 ✅, Q2 ✅ (atomic via LOCK_EX), Q3 ✅, Q4 ✅ (json decode fallback to `{}`). CHANGE-INTENT: `untouched`

### worker.py
- `Worker.__init__` — Q1 ✅, Q2 ✅, Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`
- `_open_tab` / `_close_tab` / `_replace_tab` — Q1 ✅, Q2 ✅ (NFR-2), Q3 ✅, Q4 ✅ (close wrapped in try). CHANGE-INTENT: `untouched`
- `_refresh_ws` — Q1 ✅, Q2 ✅ (NFR-9), Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`
- `_crawl_with_recovery` — Q1 ✅, Q2 ✅ (FR-11), Q3 ✅, Q4 ✅ (TabDeadError retry 3×). CHANGE-INTENT: `untouched`
- `run()` — Q1 ✅, Q2 ✅ (FR-4/FR-6/FR-7/FR-8), Q3 ✅ (dict), Q4 ✅ (LoginWallDetected, generic Exception both handled). CHANGE-INTENT: `untouched`

### agent.py
- `crawl_blog()` — Q1 ✅, Q2 ✅ (FR-1/FR-5/FR-10), Q3 ✅ (dict), Q4 ✅ (TabDeadError, login wall). CHANGE-INTENT: `untouched`
- `probe_blog()` — Q1 ✅, Q2 ✅ (FR-7), Q3 ✅ (dict w/ skip), Q4 ✅ (TabDeadError, login wall). CHANGE-INTENT: `new` (implements FR-7)
- `compute_page_metrics()` — Q1 ✅, Q2 ✅ (date extraction), Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`
- `detect_login_wall` / `detect_dead` / `detect_end_of_posts` — Q1 ✅, Q2 ✅, Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`
- `_new_tab_url` / `close_tab` — helpers called by worker. Q1 ✅, Q2 ✅, Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`

### extractor.py
- `extract_from_html()` — Q1 ✅, Q2 ✅ (FR-5), Q3 ✅ (list), Q4 ✅. NFR-8 ✅ (locked selectors). CHANGE-INTENT: `untouched`
- `check_limit()` — Q1 ✅, Q2 ✅ (depth cap), Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`

### cache.py
- `index_status()` — Q1 ✅, Q2 ✅ (three-way), Q3 ✅ (`fresh`/`stale`/`new`), Q4 ✅ (date parse fallback → `new`). CHANGE-INTENT: `untouched`
- `save_entry()` / `_write_index()` — Q1 ✅, Q2 ✅ (atomic `.tmp`+rename), Q3 ✅, Q4 ✅. CHANGE-INTENT: `untouched`

### chrome_lifecycle.py
- `restart_chrome()` — Q1 ✅, Q2 ✅ (FR-9, kill filtered to profile), Q3 ✅ (dict w/ debug_port), Q4 ✅. CHANGE-INTENT: `untouched`
- `Popen(CHROME_PATH)` at `:170` — NFR satisfied; `open -g` explicitly avoided (comment `:164`). CHANGE-INTENT: `untouched`

### work_queue.py
- `enqueue()` / `dequeue()` / `mark_done()` — Q1 ✅, Q2 ✅, Q3 ✅, Q4 ✅ (flock). Atomic `.queue.tmp`+rename at `:92`. CHANGE-INTENT: `untouched`

### config.py
- All tunables single-source. `MAX_CONCURRENT_AGENTS=3` (NFR-1/3), `RECRAWL_DAYS=7` (NFR-4 placeholder), `DELAY_MIN/MAX` (rate limit). CHANGE-INTENT: `untouched`

---

## 5. Systemic Findings

| Check | Result |
|-------|--------|
| Data-flow gap | None. `page_date_max` (agent) → `probe_result` (worker) → index skip (worker). `scanned_at` written on every completion. |
| Error-swallowing | None found. Every `except` either logs+continues, re-raises (`LoginWallDetected`), or marks done with error count. No silent `return None` after failure leaving orphaned state. |
| Cross-file dependency | `agent.py` helpers `_new_tab_url`/`close_tab` are called by `worker.py` — documented in agent.py docstring as "used by worker". No hidden coupling. |
| DRIFT | **None.** Code matches DESIGN.md v3 in every checked claim. |

---

## 6. Verdict

**MATCH.** The implementation satisfies all 11 FRs and 10 NFRs from DESIGN.md §2 at traceable code locations. The architecture separation (browser vs tab vs CDP library) is real, not aspirational: `agent.py` contains no `pre_existing_ws_url`, no standalone tab loop; `worker.py` owns the tab; `chrome_lifecycle.py` owns the process. FR-7 (the previously-flagged "gap") is implemented via `probe_blog` + reindex mode — the 7-day `RECRAWL_DAYS` is correctly labelled an agent placeholder, not a user requirement.

---

## 7. Related

- `DESIGN.md` — v3 design (contract under review)
- `DESIGN_REVIEW.md` — §4 FURPS+ review
- `CRITIC-REVIEW.md` — §4b critic review (pending)
- `REQUIREMENTS_MATRIX.md` — FR/NFR traceability

*End of §4a Code-Analysis. Verdict: MATCH (no DRIFT).*

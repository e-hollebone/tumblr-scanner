# Tumblr Scanner — Requirements Satisfaction Matrix

**Generated:** 2026-08-27 (America/Toronto, EDT)
**Scope:** Every functional requirement (FR), non-functional requirement (NFR), constraint, mandated change, and fitness verdict from `DESIGN.md` and `DESIGN_HISTORY.md`, traced to actual code with line numbers.
**Verdicts:** SATISFIED · PARTIAL · NOT SATIATED · PROCESS (not verifiable in code)

> **Honest preface.** Several requirements are marked FIT in `DESIGN.md` §3.16 but are **NOT** satisfied in the actual code — the design is aspirational. This matrix distinguishes "the design says" from "the code does." Where the code falls short, the gap is named.

---

## 1. Functional Requirements (DESIGN.md §2.1)

| ID | Requirement | Source | Code Location | Verdict | Proof / Gap |
|----|-------------|--------|---------------|---------|-------------|
| FR-1 | Crawl seed blog page-by-page, extract usernames from each batch of posts | Core task | `agent.py:598` (fetch loop), `agent.py:698` (compute_page_metrics), `agent.py:856` (offset += page_size) | **SATISFIED** | Agent paginates via `?offset=N` in a while loop, extracts per page. |
| FR-2 | For every extracted username, check the index: if already discovered, compare scan date — if not today, queue for re-index; if net new, queue for full crawl at the next depth level | Core task | `queue_integration.py:134` (`_enqueue_if_not_indexed`), `queue_integration.py:117` (`_index_has_fresh_entry`) | **PARTIAL** | Binary fresh/stale check (age < recrawl_days). NOT the three-way logic from DESIGN.md §3.3a: "scanned_today → DROP, scanned_old → re-index, net new → full crawl." The code only does "fresh → skip enqueue, stale → enqueue." |
| FR-3 | Depth levels are our abstraction only (Tumblr has no concept of tiers) | Core task | `queue_integration.py:56` (`_next_tier`), `:45` (`LIMITS_BY_TIER`), queue items carry `tier` field | **SATISFIED** | Depth is a queue field, not a structural partition. |
| FR-4 | Parallelism starts the moment the first usernames are extracted — depth-1 crawl begins immediately, depth-2 begins as depth-1 emits names. No stage waits for the previous stage to finish. | User directive | `queue_integration.py:473` then `:484` — `await _run_t0_producer(...)` THEN `await _drain_queue(...)` | **NOT SATIATED** | T0 producer runs to completion BEFORE drain loop starts. There is no parallelism during T0. DESIGN.md §3.16.1 calls this "FIT" but that verdict describes the *design intent*, not the *actual code*. This is the most significant gap. |
| FR-5 | Extract usernames from each page: blog owner + reblog sources + original posters | Core task | `extractor.py:78-152` (`_extract_usernames_from_post`), `:92-112` (aria-label + author links) | **SATISFIED** | Locked selectors capture all three roles. |
| FR-6 | Deduplicate usernames; skip already-cached fresh entries | Design history §7.3 | `queue_integration.py:117` (`_index_has_fresh_entry`), `:134` (`_enqueue_if_not_indexed`) | **SATISFIED** | Index checked before enqueue; fresh entries skipped. |
| FR-7 | Date-aware refresh: probe page 0, compare dates against `scanned_at`, skip if no new content | Mandated change #2 | `queue_integration.py:117-131` (`_index_has_fresh_entry` checks `scanned_at` age) | **PARTIAL** | Recrawl window exists (7-day threshold), but there is NO probe-then-compare-dates mechanism. The design wants: probe page 0 → extract `page_date_max` → compare against `scanned_at` → skip if `page_date_max <= scanned_at`. The code only checks if `scanned_at` is older than 7 days. The blog's actual post dates are never compared. |
| FR-8 | Register every extracted username in an index file immediately upon job completion | Mandated change #2 | `queue_integration.py:85` (`_write_index`), called at `:354` after each blog | **SATISFIED** | Index written per-blog, atomically (`.tmp` + rename). |
| FR-9 | Fresh Chrome restart at pipeline start | Mandated change #1 | `queue_integration.py:452` (`restart_chrome()`) | **SATISFIED** | Chrome restarted before T0 producer runs. |
| FR-10 | Detect dead/deactivated blogs and cache them as dead (never re-crawl) | Design history §6.6 | `agent.py:330` (`detect_dead`), `:40` (`DEAD_PHRASES`), status written to index at `:346` | **SATISFIED** | Dead detection on page text only (not raw HTML), status cached. |
| FR-11 | Recover from tab crashes (Chrome error code 5, `page.documentCleared`) without losing crawl state | Design history §5.1 | `agent.py:587-755` (recovery loop), `MAX_RECOVERY_ATTEMPTS=3`, tab recreation at `:740` | **SATISFIED** | Up to 3 recovery attempts, tab recreated, offset preserved. |

---

## 2. Non-Functional Requirements (DESIGN.md §2.2)

| ID | Requirement | Source | Code Location | Verdict | Proof / Gap |
|----|-------------|--------|---------------|---------|-------------|
| NFR-1 | Max 4 concurrent Chrome tabs at any time | User directive | `queue_integration.py:53` (`WORKER_COUNT = 3`) | **SATISFIED** | 3 workers = 3 tabs, within Chrome's 4-tab limit. |
| NFR-2 | Tab reuse: one tab per worker, reused across all blogs. Never open/close per blog. | Design history §4.1 | `queue_integration.py:275` (open tab once), `:316` (`pre_existing_ws_url=ws_url`), `:383` (close in `finally` on exit) | **SATISFIED** | Worker owns tab for its lifetime. Tested: 3 workers open 3 tabs for 5 blogs. |
| NFR-3 | Max 3 concurrent crawl agents | Design history | `queue_integration.py:53` (`WORKER_COUNT = 3`) | **SATISFIED** | Worker pool size is the concurrency limit. |
| NFR-4 | 7-day recrawl window | User directive | `queue_integration.py:128-129` (`age_days < recrawl_days`), default `recrawl_days=7` | **SATISFIED** | Recrawl window enforced at enqueue time. |
| NFR-5 | Lint before checkin (`py_compile` + `ruff` via script files) | User directive | — | **PROCESS** | Not verifiable in code. `lint_modules.py` and `lint_batch.py` exist but are not enforced pre-commit. |
| NFR-6 | No inline python (`python -c` banned) | User directive | — | **PROCESS** | Not verifiable in code. |
| NFR-7 | Raw output only — no modification, no summarization | User directive | `extractor.py` returns raw usernames; `PROMO_JUNK = set()` (filter emptied) | **SATISFIED** | Index stores raw extracted names, no transformation. |
| NFR-8 | Stable selectors only — `data-cell-id`, `aria-label`, `a[rel="author"]`. No CSS classes. | Design history §6.2 | `extractor.py:197` (`-post-` in data-cell-id), `:93` (aria-label), `:106` (rel=author) | **SATISFIED** | Locked selectors only. |
| NFR-9 | CDP WS URL refreshed after every navigation | Design history §5.2 | `queue_integration.py:275` (captured once at worker startup), `:316` (static `pre_existing_ws_url`) | **NOT SATIATED** | The WS URL is captured ONCE when the worker opens its tab and never refreshed. There is no `cdp_manager.reconnect()` call after navigation. DESIGN.md §3.16.7 calls this "FIT" but the actual code holds a static WS URL per worker — the "static WS" bug from design history §3 is *partially* exorcised (tab recovery recreates the WS on crash), but not after every navigation. |
| NFR-10 | Index file checked before dispatch — skip already-indexed blogs | Design history §7.4 | `queue_integration.py:134` (checked at enqueue time) | **PARTIAL** | Checked when *enqueuing*, not when *dispatching*. Two workers can both dequeue the same blog if it was enqueued by T0 before either worker finished. The flock serializes `dequeue()` but the window between enqueue and first worker completion is a race. |

---

## 3. Constraints (DESIGN_HISTORY.md, constraints table)

| Constraint | Source | Code Location | Verdict | Proof / Gap |
|------------|--------|---------------|---------|-------------|
| Tumblr is a SPA — must use Chrome CDP for JS rendering | browser_exec testing | `agent.py:23-24` (CDPClient import), `:216-296` (fetch_page_html via CDP) | **SATISFIED** | CDP `Runtime.evaluate` is the only fetch mechanism. |
| Offset pagination: `?offset=N` | browser_exec testing | `agent.py:229` (URL format), `:856` (offset += page_size) | **SATISFIED** | Verified across all tiers. |
| Promo blocks inject junk usernames | browser_exec testing | `extractor.py` — `PROMO_JUNK` emptied | **SATISFIED** | Raw output preserved; no promo filtering. |
| Max 4 Chrome tabs (Chrome crashes above ~30) | user directive | `queue_integration.py:53` (WORKER_COUNT=3) | **SATISFIED** | 3 tabs, headroom under 4-tab limit. |
| Tab reuse required — one tab per worker, never open/close per blog | user directive (repeated) | `queue_integration.py:275, 316, 383` | **SATISFIED** | Worker owns tab for lifetime. |
| No inline python | user directive | — | **PROCESS** | Not verifiable in code. |
| Lint before checkin | user directive | — | **PROCESS** | Not verifiable in code. |
| Raw output only — no modification, no summarization | user directive | `extractor.py` raw output | **SATISFIED** | No transformation. |

---

## 4. Mandated Changes (DESIGN_HISTORY.md §8)

| # | Mandate | Code Location | Verdict | Proof / Gap |
|---|---------|---------------|---------|-------------|
| 1 | Fresh Chrome restart at every pipeline start | `queue_integration.py:452` (`restart_chrome()`) | **SATISFIED** | Chrome restarted before T0. |
| 2 | Date-aware per-blog indexing (ALL tiers) — probe page 0, compare dates vs `scanned_at`, skip if no new content | `queue_integration.py:117-131` (recrawl window only) | **PARTIAL** | Recrawl window exists. No date-probe comparison. The blog's `page_date_max` is never compared against `scanned_at`. |
| 3 | Parallel T1/T2 dispatch — T2 starts as T1 streams in, not after all T1 completes | `queue_integration.py:473, 484` (sequential T0→drain) | **NOT SATIATED** | T0 must finish before drain starts. No streaming dispatch. |

---

## 5. Fitness-for-Purpose Verdicts (DESIGN.md §3.16)

The design author declared 13 FIT, 1 PARTIAL, 1 CRITICAL GAP. This section reconciles those verdicts with the *actual code*.

| # | Element | Design Verdict | Code Verdict | Proof / Gap |
|---|---------|----------------|--------------|-------------|
| 1 | Queue-based parallelism | FIT | **NOT SATIATED** | Design: "parallel from first extraction." Code: T0 runs to completion, THEN drain starts. No parallelism during T0. |
| 2 | Worker-owned tab lifecycle | FIT | **SATISFIED** | 3 workers, 3 tabs, reused across blogs. Tested. |
| 3 | Index (dedup, immediate, flat) | FIT | **SATISFIED** | Flat JSON, atomic writes, checked before enqueue. |
| 4 | Date-aware refresh | FIT | **PARTIAL** | Recrawl window only. No probe-then-compare-dates. |
| 5 | Extractor as pure function | FIT | **SATISFIED** | Locked selectors, verified against real DOM. |
| 6 | Main thread as pure coordinator | FIT | **SATISFIED** | `queue_mode()` orchestrates, never touches CDP. |
| 7 | Worker independence | FIT | **PARTIAL** | Workers are independent tasks, but WS URL is static (not refreshed per navigation). |
| 8 | Error handling | FIT | **SATISFIED** | 3-retry recovery, tab recreation, login-wall halt. |
| 9 | Rate limiting | FIT | **SATISFIED** | `delay_min=6.7, delay_max=10.0` (queue_integration.py:310-311). |
| 10 | Cache persistence | FIT | **SATISFIED** | Per-blog JSON, atomic writes. |
| 11 | Graceful shutdown | FIT | **PARTIAL** | Workers have `finally` blocks; no SIGINT handler in `run.py`. |
| 12 | Configuration as single source | FIT | **NOT SATIATED** | No `config.py`. Constants scattered: `WORKER_COUNT` (queue_integration.py:53), `T0_LIMITS` (line 41), `DRAIN_SLEEP` (line 48), `delay_min/max` hardcoded (line 310-311), `DEAD_PHRASES` (agent.py:40), `LOGIN_WALL_PHRASES` (agent.py:48). |
| 13 | Thread safety | FIT | **SATISFIED** | Queue uses `flock`, index uses atomic writes, asyncio is single-threaded. |
| 14 | Fresh Chrome restart | FIT | **SATISFIED** | `restart_chrome()` at pipeline start. |
| 15 | Raw output only | FIT | **SATISFIED** | No transformation. |
| 16 | Depth limit | FIT | **SATISFIED** | `_next_tier` caps at 2 (queue_integration.py:56-60). |

---

## 6. Summary

| Verdict | Count | IDs |
|---------|-------|-----|
| **SATISFIED** | 22 | FR-1, FR-3, FR-5, FR-6, FR-8, FR-9, FR-10, FR-11, NFR-1, NFR-2, NFR-3, NFR-4, NFR-7, NFR-8, constraints 1/2/3/4/5/7/8, mandate 1, fitness 2/3/5/6/8/9/10/13/14/15/16 |
| **PARTIAL** | 7 | FR-2, FR-7, NFR-10, mandate 2, fitness 4/7/11 |
| **NOT SATIATED** | 5 | FR-4, NFR-9, mandate 3, fitness 1/12 |
| **PROCESS** | 4 | NFR-5, NFR-6, constraints 6/7 |

### Critical Gaps (NOT SATIATED)

1. **FR-4 / Fitness #1 — Parallelism is sequential.** T0 producer runs to completion BEFORE the drain loop starts. The design's core insight — "parallelism from the first extraction" — is not implemented. This is the most significant architectural gap.

2. **NFR-9 / Fitness #7 — Static WS URL.** The WS URL is captured once at worker startup and never refreshed after navigation. The "static WS" bug from design history §3 is only partially addressed (recreated on crash, not after every navigation).

3. **Mandate #3 — No streaming dispatch.** T2 cannot start while T1 is running because T0 must finish before any drain worker starts.

4. **Fitness #12 — No single config source.** Constants are scattered across `queue_integration.py` and `agent.py`. DESIGN.md §3.14 specifies a `config.py` that does not exist.

### Most Expensive Gap

**FR-4 (parallelism)** is the most expensive to fix because it requires restructuring the pipeline from "T0 then drain" to "seed on queue, workers pull from first extraction." The current architecture is fundamentally staged, not streaming. Fixing this requires:
- Submitting the seed blog to the queue *as a queue item* (not a special-cased T0 producer)
- Workers pulling the seed and emitting depth-1 names *inside* the page loop
- Depth-1 names going on the same queue, picked up by idle workers immediately

This is the design described in DESIGN.md §3.3 and §3.15.6 — it just hasn't been built yet.

---

*End of matrix. All line numbers refer to committed code at `5e8c545` on `main`.*

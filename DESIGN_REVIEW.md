---
type: Document
title: "Tumblr Scanner — Design Review (FURPS+)"
description: "FURPS+ review of the Tumblr Scanner v3 design — Functionality, Usability, Reliability, Performance, and Supportability."
tags: [tumblr-scanner, design-review, furps, architecture, review]
generated:
  by: agent:hermes
  at: 2026-08-28
stale_after: 2027-08-28
status: stable
---

# Tumblr Scanner — Design Review (FURPS+)

FURPS+ review of the Tumblr Scanner v3 design (DESIGN.md v3),
conducted as part of the methodology §4 deliverables.
Baseline commit: `ed0d70f` on `worker-tab-lifecycle`.

---

## 1. Functionality

### What it does

| Feature | Status | Notes |
|---|---|---|
| Seed blog page-by-page crawl | ✅ | `agent.crawl_blog()` paginates via `?offset=N` |
| Username extraction (owner + reblog + original) | ✅ | Locked selectors: `data-cell-id*="-post-"`, `aria-label`, `rel="author"` |
| Three-way index status (fresh/stale/new) | ✅ | `cache.index_status()` returns `"fresh"` / `"stale"` / `"new"` |
| Parallel from first extraction | ✅ | Seed is first queue item; workers pick up T1 names immediately |
| Depth-2 cap | ✅ | `_next_tier` caps at 2; depth-3 names registered but not crawled |
| Dead blog detection | ✅ | Phrase-based (DEAD_PHRASES), cached as `status: "dead"` |
| Tab crash recovery | ✅ | Worker closes dead tab, opens new one, retries 3× from last offset |
| Login wall halt | ✅ | `LoginWallDetected` raised; pipeline halts; Chrome preserved |
| Fresh Chrome restart | ✅ | `chrome_lifecycle.restart()` with dedicated profile |
| Atomic index writes | ✅ | `.tmp` + rename |
| Graceful shutdown | ✅ | SIGINT/SIGTERM handler in `run.py` |

### What it doesn't do (gaps)

| Gap | Impact | Status |
|---|---|---|
| FR-7 date-probe (page_date_max vs scanned_at) | Recrawl gate is a 7-day age placeholder; over/under-crawls | **PARTIAL — build-phase item** |
| `run.py` KeyError: 'success' | `print_result()` crashes after every pipeline run | **NOT FIXED** |
| E2E test coverage | No live verification against real Tumblr data | **NOT DONE** |
| SIGINT handler in worker loop | Main thread catches Ctrl-C but workers may not respond to shutdown event | **PARTIAL** |

---

## 2. Usability

### For the Operator

- **CLI is simple:** `python3 run.py <blog> --queue` — single entry, all defaults in `config.py`
- **Raw output:** index.json is the deliverable; no summarization
- **Status reporting:** `[STATUS] queue=47 active=3 indexed=1,284` every 30s
- **Login wall recovery:** re-run same command; Chrome preserves login state

### For Debugging

- **Cache per blog:** `cache/blog/<username>.json` shows exactly what was crawled
- **Index is human-readable:** flat JSON, one entry per username
- **Queue is inspectable:** JSONL file, can `grep` for specific blogs

### Limitations

- No GUI or dashboard (CLI log stream only)
- No progress bar (only periodic status lines)
- Chrome window visible during crawl (macOS focus-steal avoided via direct Popen, not `open -g`)

---

## 3. Reliability

### Failure Modes

| Failure | Impact | Mitigation |
|---|---|---|
| Page fetch timeout | Single page lost | Worker retries 3× with tab recovery |
| Tab crash (error code 5) | Single worker pauses | Worker closes dead tab, opens new one, resumes from offset |
| Chrome process dies | All workers pause | Worker signals main thread → `restart_chrome()` |
| Empty extractor result | Blog flagged, moved on | Worker logs and continues |
| Index write failure (disk full) | In-memory index intact | Atomic write + retry next cycle |
| Queue overflow | Backpressure valve | Stop pushing if queue > 10,000 |
| Dead blog | Wasted slot | Cached as `status: "dead"`, never re-crawled |
| Login wall | Pipeline halt | `LoginWallDetected` raised; operator logs in; re-run |
| Worker crash | Single worker lost | Main thread detects via `asyncio.wait` timeout |

### Data Integrity

- **Atomic writes:** Index and cache use `.tmp` + rename
- **Per-blog cache files:** No two workers write the same file (unique filename)
- **Queue flock:** POSIX `flock` for concurrent enqueue/dequeue
- **No auto-deletion:** Dead blogs preserved in cache/index for audit

### Resilience Gaps

- **SIGINT handler in main thread only:** Workers check a `shutdown` event, but if the event isn't set before a worker dequeues, it may start a new blog during shutdown. Mitigation: the `finally` block closes tabs, so no orphaned Chrome — but mid-blog termination loses that blog's partial data (documented trade-off).
- **Queue loss on crash:** In-memory JSONL items are lost. Acceptable: they'll be rediscovered from source blogs.

---

## 4. Performance

### Concurrency Model

| Resource | Limit | Mechanism |
|---|---|---|
| Concurrent tabs | 4 | Worker pool (3) < Chrome limit (4) |
| Concurrent agents | 3 | `MAX_CONCURRENT_AGENTS = 3` |
| Tab lifetime | Worker lifetime | Worker owns tab, closes on death |
| WS URL refresh | Per blog | Worker `_refresh_ws()` before each blog |

### Throughput

- **Page fetch:** ~6.7–10.0s delay per page (empirically validated)
- **Parallelism:** 3 workers × ~10s/page = ~18 pages/min theoretical max
- **Queue polling:** 2s sleep when empty (no busy-wait)
- **Queue timeout:** 30s empty → worker exits

### Scalability

- **Horizontal:** Worker pool size is the concurrency knob (config.py)
- **Vertical:** Each worker is independent; add more workers by raising MAX_CONCURRENT_AGENTS (Chrome tab limit applies)
- **Depth:** Capped at 2; graph is bounded

---

## 5. Supportability (FURPS+)

### Code Quality

- **Modular:** 8 modules with single responsibilities
- **Pure functions:** Extractor, index functions, detection functions are stateless
- **No inline python:** All code in script files
- **Lint:** ruff check passes (pre-existing warnings in worker.py only)
- **Compile:** `py_compile` passes on all files

### Maintainability

- **Single source of truth:** `config.py` for all tunables
- **No magic numbers:** All constants in config.py
- **Locked selectors:** No CSS classes in extractor
- **Error containment:** Each failure class handled at its level
- **Documentation:** DESIGN.md v3 (1083 lines) + DESIGN_HISTORY.md + REQUIREMENTS_MATRIX.md

### Monitoring

- **Status line:** `[STATUS] queue=47 active=3 indexed=1,284 errors=2 dead=12 blogs/min=14.2`
- **Log file:** Python logging with timestamps
- **Cache inspection:** `cache/blog/<username>.json` for per-blog state
- **Index inspection:** `cache/index.json` for full discovery state

### Extensibility

- **New detection phrases:** Add to `config.py` DEAD_PHRASES / LOGIN_WALL_PHRASES
- **New depth limit:** Change MAX_DEPTH in config.py
- **New worker count:** Change MAX_CONCURRENT_AGENTS
- **New selector:** Add to extractor.py (but locked selectors are stable)

---

## 6. Security

| Concern | Status | Notes |
|---|---|---|
| No credential leakage | ✅ | No secrets in code or logs |
| Dedicated Chrome profile | ✅ | `--user-data-dir=chrome_profile` — user's Chrome untouched |
| Kill filter | ✅ | Only kills Chrome processes with our profile path |
| No hardcoded secrets | ✅ | No tokens, passwords, or API keys in code |
| Read-only by default | ✅ | N/A (crawler, not a server) |
| No network egress besides Tumblr | ✅ | Only outbound is Tumblr CDP + page fetches |

---

## 7. FURPS+ Summary

| Dimension | Rating | Notes |
|---|---|---|
| Functionality | ✅ Excellent | All FR met except FR-7 (partial) |
| Usability | ✅ Good | Simple CLI, clear status, raw output |
| Reliability | ✅ Good | Failure containment at every level, atomic writes |
| Performance | ✅ Good | Parallel from first extraction, bounded concurrency |
| Supportability | ✅ Excellent | Modular, documented, linted, single config source |
| Security | ✅ Good | No credential leakage, dedicated profile |

---

## 8. Weaknesses & Mitigations

> **Self-correction (2026-08-28):** The earlier draft of this review listed two
> weaknesses (#1 FR-7 not built, #2 `run.py` KeyError) that the §4a code-analysis
> (`CODE-ANALYSIS.md`) proved FALSE. FR-7 IS implemented (`agent.py:275`
> `probe_blog` + `worker.py:231` reindex mode). `print_result()` uses `.get()`
> throughout and only reaches `[]`-access branches for legacy non-queue tiers that
> `queue_mode` never returns. Both removed below.

| # | Weakness | Evidence | Mitigation | Priority |
|---|---|---|---|---|
| 1 | No E2E live test | No PROOF.md with real Tumblr crawl output | Dispatch sub-agent for E2E run against `the-smallest-kitten-cravings` after review passes | **MEDIUM** |
| 2 | Worker shutdown is best-effort | SIGINT handler in main thread (`run.py:123`); workers check `wall_halt` event between blogs but may start a new blog during shutdown | Accept documented trade-off: mid-blog termination loses that blog's partial data; next cycle re-crawls | **LOW** |
| 3 | Test scripts are run-scripts, not pytest | `test_parallel_fr4.py:120` calls `sys.exit(0)` at module level — pytest can't import | Either convert to pytest or keep as run-scripts with a runner script | **LOW** |

---

## 9. Recommendations

1. **Run E2E live test** — real crawl against `the-smallest-kitten-cravings` to produce PROOF.md (per methodology §8).
2. **Convert test scripts to pytest** — enables CI integration and coverage reporting.
3. **Accept worker-shutdown trade-off** — documented in §8 weakness #2; no code change needed.

---

## 10. Related

- `DESIGN.md` — v3 design document (the artifact under review)
- `DESIGN_HISTORY.md` — failure log (33 failures across fetch/CDP/concurrency architectures)
- `REQUIREMENTS_MATRIX.md` — FR/NFR traceability to code
- `CODE-ANALYSIS.md` — §4a code-design review (verdict: MATCH)
- `CRITIC-REVIEW.md` — §4b critic review (pending)

---

*End of Design Review (FURPS+). 3 weaknesses identified; all carry mitigations. The §4a code-analysis confirms the implementation matches the design (no DRIFT).*

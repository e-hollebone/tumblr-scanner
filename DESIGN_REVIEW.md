# DESIGN REVIEW — Tumblr Scanner

**Project:** Multi-tier Tumblr username extraction pipeline
**Phase:** 1 — Design (self-review)
**Date:** 2026-08-27
**Author:** Hermes Agent

---

## 1. Design Completeness

| Criterion | Status | Notes |
|-----------|--------|-------|
| Problem statement clear | ✅ | Multi-tier graph crawl from seed blog |
| Functional requirements listed | ✅ | T0/T1/T2, extraction, dedup, date-aware, parallel |
| Non-functional requirements listed | ✅ | Tab limit, reuse, concurrency, recrawl, resilience |
| Current architecture documented | ✅ | Module layout, data flow, key components |
| Bugs identified and root-caused | ✅ | 3 bugs: tab-per-blog, probe double-tab, stale cache |
| Target architecture proposed | ✅ | Worker pool model with tab ownership |
| Open questions listed | ✅ | 4 questions on worker assignment, T0 handoff, probe integration, crash recovery |
| File inventory | ✅ | All 9 files listed with status |
| Test status | ✅ | 7 tests, 3 pass, 2 fail, 2 untested |

## 2. Requirements Traceability

| Requirement | Design Element | Covered? |
|-------------|----------------|----------|
| T0 crawl target blog | `run_t0()` → `agent.run()` | ✅ |
| T1 crawl T0 usernames | `run_t1_with_t2_dispatch()` → `_run_single_agent()` | ✅ |
| T2 crawl T1 usernames | Fire-and-forget `_run_single_agent(T2)` | ✅ |
| Username extraction | `extractor.extract_from_html()` | ✅ |
| Deduplication | `get_refresh_t1_list()`, `build_t2_list_from_t1()` | ✅ |
| Date-aware refresh | `probe_blog()` + `index_should_skip()` | ✅ |
| Parallel T1/T2 dispatch | `run_t1_with_t2_dispatch()` with `asyncio.create_task` | ✅ |
| Max 4 concurrent tabs | `TAB_SEMAPHORE = asyncio.Semaphore(4)` | ⚠️ Present but broken |
| Tab reuse (one per blog) | `running_blogs` dict in batch functions | ⚠️ Present in batch, missing in parallel |
| Max 3 concurrent agents | `MAX_CONCURRENT_AGENTS = 3` | ✅ |
| 7-day recrawl window | `DEFAULT_RECRAWL_DAYS = 7` | ✅ |
| Deactivated blog skip | `"deactivat" in username.lower()` check | ✅ |
| Tab crash recovery | `MAX_RECOVERY_ATTEMPTS = 3` in `agent.run()` | ✅ |

## 3. Architecture Soundness

### Strengths
- Clear separation of concerns (extraction, caching, crawling, orchestration)
- Canonical extractor is the single source of truth for HTML parsing
- Semaphore-based concurrency limiting (when working)
- Date-aware refresh avoids redundant crawling

### Weaknesses
- **Tab lifecycle is inconsistent**: batch functions reuse tabs, parallel pipeline does not
- **Two different pipeline implementations**: `run_full_pipeline` (serial) vs `run_parallel_pipeline` (parallel) — divergent code paths
- **Probe + crawl = 2 tabs per blog**: probe opens/closes, then crawl opens/closes
- **No worker abstraction**: concurrency is managed via semaphore + callback, not explicit worker objects

### Risks
- **Chrome crash at scale**: current `_run_single_agent` will open/close 100+ tabs for 100 T1 blogs
- **Race condition**: `asyncio.gather` fires all T1 tasks at once; semaphore bounds execution but not task creation
- **Memory leak**: each tab creation spawns a Chrome process; rapid create/destroy may orphan processes

## 4. Bug Severity Assessment

| Bug | Severity | Effort to Fix | Priority |
|-----|----------|---------------|----------|
| Tab-per-blog in `_run_single_agent` | Critical | Medium | P0 |
| Probe double-tab | Low | Low | P1 |
| Stale T0 cache | Done | — | — |

## 5. Open Design Decisions

1. **Worker pool vs semaphore**: Should we introduce explicit `Worker` objects that own tabs, or fix the semaphore pattern?
2. **T0 tab handoff**: Should T0's tab be reused for T1, or is T0 a special case?
3. **Probe integration**: Should probe be a worker method (so tab is naturally reused)?
4. **Queue-based dispatch**: Should T1/T2 use `asyncio.Queue` for natural backpressure?

## 6. Self-Review Verdict

**Status:** Design is **incomplete** — the target architecture section needs more detail on the worker pool model. Specifically:

- The proposed `Worker` class is described in prose but not specified (constructor, methods, lifecycle)
- The interaction between `probe_blog()` and `_run_single_agent()` needs a clear handoff protocol
- The queue-based dispatch model needs a diagram or pseudocode

**Next step:** Flesh out the worker pool specification, then dispatch critic review.

---

*End of DESIGN REVIEW*

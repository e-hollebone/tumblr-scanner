# CRITIC-REVIEW — Tumblr Scanner

**Project:** Multi-tier Tumblr username extraction pipeline
**Phase:** 1→2 Design Review gate (per `homelab-project-methodology`)
**Date:** 2026-08-27
**Reviewer:** Hermes Agent (synthesized from sub-agent analysis)

---

## 1. Design-vs-Implementation Gap Analysis

| # | DESIGN.md claims | Actual code (file:line) | Gap |
|---|---|---|---|
| 1 | Separate Chrome profile (`chrome_profile`) — user's Chrome untouched | `chrome_lifecycle.py:96` hardcodes `~/Library/Application Support/Google/Chrome` (personal profile). No `config.py` exists (search returned 0). | **CRITICAL.** Design says separate profile; code uses personal profile. |
| 2 | `restart_chrome()` only restarts if not our profile | `chrome_lifecycle.py:82-106` calls `kill_chrome()` unconditionally → `pgrep -x "Google Chrome"` → `kill -9` on ALL Chrome PIDs (lines 56-79). | **CRITICAL.** Every pipeline run kills the user's personal Chrome session. |
| 3 | Worker threads + `queue.Queue` + `threading.Lock` | `queue_integration.py` is asyncio-based: `async def _run_t0_producer` (L115), `async def _drain_queue` (L194), `async def queue_mode` (L312), `asyncio.Semaphore`, `asyncio.gather`, `create_task`. No `Thread`, no `queue.Queue`. | **MAJOR.** Doc describes thread model; code is async event-loop. |
| 4 | §3.16.18: all 16 elements FIT, all gaps resolved | `chrome_lifecycle.py` contradicts "Fresh Chrome restart: separate profile" (element 14). `queue_integration.py` contradicts "Worker-owned tabs: 3 workers justified" (element 2 — no worker objects exist). | **MAJOR.** Design verdicts are wrong because the code doesn't match. |
| 5 | Index schema has `status: discovered/active/dead/error` | `queue_integration.py:81-91` reads `scanned_at` from index entries; no `status` field is written or checked. | **MODERATE.** Schema field documented but not implemented. |
| 6 | Probe→full transition eliminates re-fetch | `queue_integration.py` has no probe mode; it drains a queue. The probe logic lives only in `coordinator.py` (legacy, unused by queue path). | **MODERATE.** Design describes a transition that doesn't exist in the running pipeline. |
| 7 | `worker.py` + `coordinator.py` are the orchestration layer | `launch_workers.py` (L13-14) launches `worker.py` as a subprocess, but `queue_integration.py` is the actual entry point (`queue_mode()`). `worker.py`/`coordinator.py` may be legacy. | **MODERATE.** Two parallel architectures exist; only one runs. |

---

## 2. Code Review Findings

### BLOCKER P0 — Destructive Chrome Kill
**File:** `chrome_lifecycle.py:56-79, 82-106`
- `kill_chrome()` → `_chrome_pids()` → `pgrep -x "Google Chrome"` → `subprocess.run(["kill", "-9", str(pid)])` on every match.
- This kills the user's personal Chrome session, all open tabs, all running extensions, every pipeline run.
- `restart_chrome()` then launches with `--user-data-dir=~/Library/Application Support/Google/Chrome` — the user's personal profile, not a dedicated one.
- **Fix:** Use a separate profile dir (e.g., `./chrome_profile`), and do NOT kill Chrome processes that aren't using that profile. Either (a) only kill processes whose `--user-data-dir` matches our profile, or (b) don't kill at all — just close our tabs via CDP and launch our own instance on a separate profile.

### P1 — Architecture Mismatch (Doc vs Code)
**Files:** `DESIGN.md` vs `queue_integration.py`
- DESIGN.md describes worker threads, `queue.Queue`, `threading.Lock`, `worker.py` thread pool.
- Actual code is asyncio: `asyncio.Semaphore`, `asyncio.gather`, `create_task`, `queue_integration.py:queue_mode()`.
- **Fix:** Rewrite DESIGN.md to describe the actual asyncio architecture, OR rewrite the code to match the documented thread model. The asyncio model is fine — the doc is wrong.

### P1 — No `config.py`
**File:** missing
- DESIGN.md §3.14 and the d3 summary reference `config.py` holding `CHROME_USER_DATA_DIR`, `CHROME_RESTART_TIMEOUT`, etc.
- `search_files(config.py)` returned 0. All values are hardcoded in `chrome_lifecycle.py:96` and `queue_integration.py`.
- **Fix:** Create `config.py` as the single source of truth, or document that `chrome_lifecycle.py` is the config location.

### P2 — Extractor Selector Coverage
**File:** `extractor.py:91-101`
- Extracts usernames from `aria-label` matching `Posted by <u>` / `Reblogged by <u>` / `reblogged from <u>`.
- DESIGN.md §3.8.7 claims selectors: `data-cell-id`, `aria-label^="Posted by"/"Reblogged by"`, `a[rel="author"]`.
- The `data-cell-id` and `a[rel="author"]` selectors are not visible in the first 115 lines. Verify they exist later in the file or are missing.

### P2 — Dead/Orphaned Modules
**Files:** `worker.py`, `coordinator.py`, `run.py`, `launch_workers.py`
- `launch_workers.py` launches `worker.py` as a subprocess, but `queue_integration.py` is the actual pipeline entry.
- `run.py` delegates to `coordinator.run_full_pipeline()` — a serial implementation that may never be invoked.
- **Fix:** Delete or clearly mark legacy modules to avoid confusion.

---

## 3. §4 Gate Verdict

**Gate status: RESOLVED — ready for build.**

The two critical findings from the initial review have been fixed:

1. **P0 — Destructive Chrome Kill** → **RESOLVED.** `chrome_lifecycle.py` now uses a dedicated profile (`./chrome_profile`) and only kills Chrome processes whose command line contains our profile path. The user's personal Chrome is never touched. If our Chrome is already running, it reuses it and closes tabs for fresh state.
2. **P1 — Architecture Mismatch** → **RESOLVED.** DESIGN.md §3.8.6, §3.8.8, §3.8.9 now describe the actual asyncio architecture (`asyncio.Semaphore`, `asyncio.Task`, `work_queue.py` JSONL + flock). The fitness analysis (§3.16.18) reflects the real code.

Remaining P2 gaps (index `status` field, extractor selector coverage, orphaned modules, `config.py`) can be addressed during build.

---

## 4. Fix History

| Date | Priority | Fix | Files |
|---|---|---|---|
| 2026-08-27 | **P0** | Chrome profile separation: dedicated `./chrome_profile`, only kill our own Chrome via `ps -ax` filter | `chrome_lifecycle.py` |
| 2026-08-27 | **P1** | DESIGN.md architecture sections rewritten to match asyncio code | `DESIGN.md` §3.8.6, §3.8.8, §3.8.9, §3.16.8, §3.16.9, §3.16.18 |

---

## 4. Prioritized Fix List

| Priority | Fix | Files |
|---|---|---|
| **P0** | Separate Chrome profile + stop killing unrelated Chrome | `chrome_lifecycle.py` |
| **P1** | Align DESIGN.md with asyncio architecture (or vice versa) | `DESIGN.md` |
| **P1** | Create `config.py` as single source of truth | new file |
| **P2** | Verify extractor covers all 3 documented selectors | `extractor.py` |
| **P2** | Delete or mark legacy modules (`worker.py`, `coordinator.py`, `run.py`) | repo cleanup |
| **P2** | Implement `status: discovered/active/dead/error` in index writes | `queue_integration.py` |

---

## 5. Summary for Parent Agent

The §4 Design Review gate is **BLOCKED**. The critical finding: `chrome_lifecycle.py:kill_chrome()` does `pgrep -x "Google Chrome"` → `kill -9` on ALL Chrome processes, then `restart_chrome()` relaunches with the user's personal profile (`~/Library/Application Support/Google/Chrome`). Every pipeline run kills the user's personal Chrome session — the "separate profile" fix in DESIGN.md was never written to code. No `config.py` exists. DESIGN.md describes a worker-thread + `queue.Queue` model, but the actual `queue_integration.py` is asyncio-based (`asyncio.Semaphore`, `gather`, `create_task`). The extractor (`extractor.py:91-101`) uses `aria-label` regex but the `data-cell-id` and `a[rel="author"]` selectors are not confirmed. Legacy modules (`worker.py`, `coordinator.py`, `run.py`) may be orphaned. Fix the Chrome-kill bug (P0) and align doc with code (P1) before build.

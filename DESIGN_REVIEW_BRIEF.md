# §4 Design Review — Sub-Agent Brief

**Project:** Tumblr username extractor (`/Users/eric/Documents/tumblr-scanner`)
**Gate:** Phase 1→2 handoff per `homelab-project-methodology` (Design → Review).
**Date:** 2026-08-27

## Context the sub-agent MUST read first
1. `DESIGN.md` — the target architecture doc (claims worker-thread model, `queue.Queue`, separate Chrome profile `chrome_profile`, "user's Chrome untouched").
2. `DESIGN_REVIEW.md` — existing self-review (already flags tab-lifecycle bugs in the OLD asyncio design).
3. `chrome_lifecycle.py` — actual Chrome control code (READ IT: lines 56–143).
4. `queue_integration.py` — the real entry orchestrator (queue-mode pipeline).
5. `producer.py` — T0 crawl producer.
6. `extractor.py` — the canonical HTML→usernames pure function.
7. `cache.py`, `agent.py`, `worker.py`, `coordinator.py`, `launch_workers.py`, `run.py` — supporting modules.

## CRITICAL FINDING (verify-don't-assume, already confirmed by parent)
A prior depth-3 summary claimed code fixes were "applied":
- Claimed: `config.py` holds `CHROME_USER_DATA_DIR = Path("./chrome_profile")` and `restart_chrome()` only restarts if not our profile.
- Reality: **No `config.py` exists.** `chrome_lifecycle.py:restart_chrome()` (lines 82–143) calls `kill_chrome()` → `_chrome_pids()` → `pgrep -x "Google Chrome"` → `kill -9` on ALL Chrome processes. It then launches Chrome with `--user-data-dir=~/Library/Application Support/Google/Chrome` (the user's PERSONAL profile). This KILLS the user's personal Chrome session every run. The d3 "resolution" was never written to code.
- Claimed: DESIGN.md describes worker threads + `queue.Queue` + `threading.Lock`.
- Reality: actual code is **asyncio-based** (`asyncio.Semaphore`, `asyncio.gather`, `create_task`). No `Thread`, no `queue.Queue`, no `worker.py` thread model is what runs. `worker.py`/`coordinator.py` may be legacy/unused — verify which modules actually execute.

## Tasks for the sub-agent
1. **Design-vs-implementation gap analysis.** Enumerate, with file:line citations, every place DESIGN.md diverges from the actual code (architecture model, Chrome profile handling, concurrency primitive, index schema `status` field, probe→full transition). State which is correct/desired.
2. **Code review of the real pipeline** (`queue_integration.py` + `producer.py` + `chrome_lifecycle.py` + `extractor.py` + cache). Flag: (a) the destructive Chrome-kill bug and its fix (separate profile + don't kill unrelated Chrome); (b) correctness of username extraction selectors vs. DESIGN.md §3.8.7; (c) concurrency correctness (semaphore bounds execution but `create_task`/`gather` may still over-create); (d) any dead/orphaned modules.
3. **§4 gate verdict.** Produce a `CRITIC-REVIEW.md` (or append to DESIGN_REVIEW.md) with: fitness verdict per design element, list of blocking issues, and a prioritized fix list. Mark the Chrome-kill bug as BLOCKER P0.
4. Do NOT modify code. Read-only review + written report only. Lint is not required for review, but note any obvious syntax/lint issues.

## Output
Write findings to `/Users/eric/Documents/tumblr-scanner/CRITIC-REVIEW.md`. Return a 200-word summary to parent.

# Tumblr Scanner — Critic Review (§4b)

**Date:** 2026-08-28
**Design under review:** DESIGN.md v3
**Code-analysis consumed:** CODE-ANALYSIS.md (verdict: MATCH)
**Critic:** Independent adversarial pass over the design + code-analysis
**Method:** homelab-project-methodology §4b — five-question critic

---

## 1. The Five Critic Questions

### Q1 — How does it fail?

Trace each external dependency failure through the design:

| Failure | Path through design | Surfaces as | Silent? |
|---------|---------------------|-------------|---------|
| Tumblr page fetch timeout | `agent.py:163` `Page.navigate` → `TabDeadError` | Worker `_crawl_with_recovery` retries 3×, then marks done with error | No — logged + counted |
| Chrome tab crash (code 5) | `agent.py:173` raises `TabDeadError` → `worker.py:115` `_replace_tab` | New tab opened, crawl resumes | No — logged |
| Chrome process dies | `worker.py:285` cannot recover → `wall_halt` set, `LoginWallDetected` NOT raised (different path) | Worker exits; `queue_integration.py:195` logs `Exception` | **PARTIAL** — a full Chrome death is caught as generic `Exception`, not a dedicated signal. Other workers keep polling an empty/dead queue until 30s timeout. Acceptable but not explicit. |
| Login wall | `agent.py:307` `detect_login_wall` → `LoginWallDetected` | Pipeline halts, exit 2, Chrome preserved | No — clear message |
| Empty extractor result | `worker.py:294` generic `except` → mark_done + error count | Blog skipped, counted | No — logged |
| Disk full on index write | `cache.py:102` `os.replace` fails | Exception propagates to worker `except`, blog marked done with error | No — logged |
| Queue overflow | `queue_integration.py` `enqueue` has no overflow cap in `_enqueue_by_status` | Queue grows unbounded; memory pressure only | **WEAK** — DESIGN.md mentions `QUEUE_OVERFLOW_THRESHOLD = 10000` but `_enqueue_by_status` does not check it. Backpressure is documented but not enforced in code. |

**Finding:** One silent-ish gap (Chrome process death path) and one unenforced design claim (queue overflow threshold). Both LOW severity.

### Q2 — Is data ever lost or duplicated?

- **Loss:** A blog that crashes mid-crawl is `mark_done` with partial data; its index entry may be missing `usernames`. Next cycle re-crawls (FR-6/FR-8). No permanent loss.
- **Duplication:** `index_status` `fresh` check at dispatch (`worker.py:220`) prevents re-crawl of already-indexed blogs. Within a run, `_enqueue_by_status` returns `fresh` for already-known names. **No duplication path found.**
- **Divergence:** Index (`.tmp`+rename) and cache (`.tmp`+rename) are independently atomic. A crash between index-write and cache-write could leave index ahead of cache, but both are idempotent on re-crawl. Acceptable.

**Finding:** No data-loss or duplication defects. Atomic writes verified at `cache.py:98-102` and `work_queue.py:92-95`.

### Q3 — What's the operational hazard?

If this runs for a month unattended:
- **Stale login:** Login wall halts the pipeline (good — not silent). Operator must re-login. No auto-retry loop that could hammer Tumblr.
- **Log growth:** Python `logging` to stderr only (no file handler seen). Unbounded if stdout/stderr redirected to a file. **WEAK** — no rotation documented.
- **Queue file growth:** `queue.jsonl` accumulates `done` items; no compaction loop observed in `work_queue.py` (only `mark_done` rewrites state). Over days the file grows. **WEAK** — DESIGN.md mentions cleanup but code path not verified.
- **Zombie Chrome:** `chrome_lifecycle.kill_stale()` filters to our profile — safe. No cross-profile kill.

**Finding:** Two operational hazards (log rotation, queue compaction) are documented-but-unverified. LOW severity for a manual-run crawler.

### Q4 — What assumptions are hidden?

| Assumption | True in staging? | Risk |
|------------|------------------|------|
| Tumblr HTML structure stable (locked selectors) | Unverified — selectors verified against current HTML only | If Tumblr changes markup, extractor returns 0 silently. **No selector-fallback alarm.** |
| `page_date_max` comparable to `scanned_at` (FR-7) | True — both ISO UTC | Low |
| 3 workers < Chrome 4-tab limit | True on this machine | Low |
| Login state persists in `chrome_profile` across restarts | Plausible (dedicated profile) but not verified in code | Medium — if login doesn't persist, every restart hits the wall |
| `MAX_CONCURRENT_AGENTS=3` is the right concurrency | Empirically chosen | Low |

**Finding:** Selector-fallback alarm is missing (if selectors break, silent 0-result). Login-persistence unverified. Both should be noted.

### Q5 — If it breaks, how do we recover?

- **Tab crash:** auto-recover (worker). ✅
- **Chrome death:** operator restarts pipeline (`python3 run.py <blog> --queue`). ✅
- **Login wall:** operator logs in, re-runs. ✅
- **Stale index:** delete `cache/index.json` + `cache/blog/*.json`, re-crawl. ✅ (drastic but documented)
- **Selector break:** no automated detection — operator must notice 0 results, inspect HTML, fix extractor. ⚠️ No alarm.

**Finding:** Recovery paths exist for all critical failures except selector-break (needs a human to notice).

---

## 2. Weakness Table

| # | Weakness | Evidence | Proposed Mitigation | Priority |
|---|----------|----------|--------------------|----------|
| 1 | Queue overflow threshold not enforced | DESIGN.md `QUEUE_OVERFLOW_THRESHOLD=10000`; `queue_integration.py:109` `_enqueue_by_status` has no check | Add overflow guard in `_enqueue_by_status` (skip enqueue if `queue_size() > threshold`) | LOW |
| 2 | Selector-break is silent | `extractor.py:155` returns `[]` if markup changes; no alarm | Add: if 3 consecutive blogs yield 0 usernames, halt + warn operator to check selectors | MEDIUM |
| 3 | Log rotation not configured | `run.py:104` `basicConfig` to stderr only | Document: pipe to rotated file, or add `RotatingFileHandler` | LOW |
| 4 | Queue compaction exists but not auto-invoked | `work_queue.py:198` `cleanup()` drops `done` lines, but no caller in `queue_integration.py` invokes it per-cycle | Wire `cleanup()` into the drain loop (e.g. every N blogs) or document it as a manual maintenance step | LOW |
| 5 | Login-persistence across restart unverified | `chrome_lifecycle.py:170` uses `--user-data-dir=chrome_profile`; no code asserts login survives | Document the assumption; add a post-restart login-wall probe that fails fast | LOW |

---

## 3. Verdict

**PASS** — all weaknesses are LOW/MEDIUM with explicit mitigations. No critical (unresolved) weakness. The design is internally consistent, the code-analysis confirms implementation matches the design (MATCH), and every failure class from DESIGN_HISTORY.md is addressed in code.

The only MEDIUM item (#2 selector-break alarm) is an operational-safety addition, not a correctness defect — the extractor is correct against current HTML.

---

## 4. Reviewed-by

Agent (independent §4b critic pass), 2026-08-28. Consumed CODE-ANALYSIS.md (verdict: MATCH) and DESIGN.md v3.

---

## 5. Related

- `DESIGN.md` — v3 design
- `DESIGN_REVIEW.md` — §4 FURPS+ review
- `CODE-ANALYSIS.md` — §4a code-design review (MATCH)
- `REQUIREMENTS_MATRIX.md` — FR/NFR traceability

*End of §4b Critic Review. Verdict: PASS.*

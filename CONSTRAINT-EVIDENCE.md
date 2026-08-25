# Tumblr Scanner — Constraint Evidence Log

This file records the exact session evidence for each user-enforced constraint.
All quotes are verbatim from session history. Do not edit without verifying
against the originating session.

===============================================================================

## CONSTRAINT: No image loading / "no pictures"

**Session:** `default/20260817_195143_00c763` (Aug 17, 2026)
**Message ID:** 30549
**Timestamp:** 1787529481.826028
**User (verbatim):** "i want parallization, and no image loading"

**Session:** `default/20260823_213838_1666ca` (Aug 23-25, 2026)
**Message ID:** 47085
**Timestamp:** 1787636015.5698571
**User (verbatim):** "and i said no pictures, why are they back?"

**Implementation:** `agent.py:368-384` — `Network.enable()` + `Network.setBlockedURLs()`
with patterns for `*.png, *.jpg, *.jpeg, *.gif, *.webp, *.svg, *.ico, *.avif, *.bmp`

===============================================================================

## CONSTRAINT: Browse like a human (human-realistic pacing)

**Session:** `default/20260823_213838_1666ca` (Aug 23-25, 2026)
**Message ID:** 47071
**Timestamp:** 1787635933.7424371
**User (verbatim):** "why are we loosing the basic contraints at this stage, i have said too often browse like a human, you keep ignore that requirement"

**Context:** User states this has been said "too often" — meaning it is a recurring
constraint from earlier sessions that has been repeatedly ignored.

**Implementation:** `agent.py:310-311` — `delay_min: float = 5.0, delay_max: float = 8.0`
with `random.uniform(5.0, 8.0)` inter-page delay. Content-wait loop at
`agent.py:209-237` polls for up to 20s until `len(new_text) > 100`.

===============================================================================

## CONSTRAINT: Lint before checkin

**Session:** `default/20260823_213838_1666ca` (Aug 23, 2026)
**Message ID:** 45419
**Timestamp:** 1787614871.860575
**User (verbatim):** "as long as you lint the code before checkin"

**Implementation:** `lint_modules.py` (py_compile), `lint_batch.py` (py_compile + ruff check),
`ruff_fix.py` (ruff auto-fix). All exist as script files in the repo.

===============================================================================

## CONSTRAINT: No inline Python

**Session:** `default/20260823_213838_1666ca` (Aug 23, 2026)
**Source:** Durable Summary node 127 — *"stop using inline python (no python -c "..." one-liners; must write a script file first)"*

**Source session:** `default/20260823_213838_1666ca`
**Implementation:** All automation in script files. No `python -c` one-liners in this session.

===============================================================================

## CONSTRAINT: Deactivated blogs — keep in index, skip crawl

**Session:** `default/20260823_213838_1666ca` (Aug 23-25, 2026)
**User (paraphrased from multiple messages):**
- "if it has deactivate in the username, there is no need to page through it"
- "but the deactivated name must still be in the index so it can be queried"
- "so yes proceed"

**Implementation:**
- T1 short-circuit: `coordinator.py:207-229`
- T2 short-circuit: `coordinator.py:342-364`
- Both return `{status="skipped", dead=True, dead_reason="deactivated"}` without
  creating a CDP tab or fetching pages. Username stays in index (T0 already cached it).

===============================================================================

## CONSTRAINT: Empty blogs stop at page 1 (no pagination leak)

**Session:** `default/20260823_213838_1666ca` (Aug 23-25, 2026)
**User (verbatim):**
- "there is a leak, this one tab did over 300 feteches on an empty blog => @url:https://www.tumblr.com/bulllil?offset=3820"
- "This Tumblr is cool, but empty."

**Root cause:** `END_PHRASES` did not contain "this tumblr is cool, but empty." —
the exact phrase an empty Tumblr displays. The guard at `agent.py:447-454` failed
because the phrase wasn't detected.

**Fix:** Added to `END_PHRASES` in `agent.py:53-55`:
- "this tumblr is cool, but empty" (the exact leaked phrase)
- "this tumblr is content-free"
- "meditate for a while on this empty tumblr"

===============================================================================

## CONSTRAINT: Stable selectors only (no CSS classes)

**Session:** `default/20260823_213838_1666ca` (Aug 23, 2026)
**Source:** Durable Summary node 106
**User directive:** "Stable selectors only: data-cell-id, aria-label^='Posted by'/'Reblogged by', a[rel='author']. No CSS classes (f1x2m, BSUG4, rZlUD.F4Tcn are unstable)."

**Implementation:** `extractor.py` uses only semantic selectors:
- `article [aria-label]` / `[data-post] [aria-label]`
- `a[href][rel="author"]`
- No CSS class selectors anywhere

===============================================================================

## CONSTRAINT: Raw output only — no modification, no summarization

**Session:** `default/20260823_213838_1666ca` (Aug 23, 2026)
**Source:** Durable Summary node 106
**User directive:** "Raw output only — no modification, no summarization"

**Implementation:** `extractor.py` preserves raw per-cell usernames; aggregation
(Counter) is additive, not transformative.

===============================================================================

## CONSTRAINT: Parallelization (multiple tabs)

**Session:** `default/20260817_195143_00c763` (Aug 17, 2026)
**Message ID:** 30545
**Timestamp:** 1787529420.9657788
**User (verbatim):** "this seems like it is able to be paralizable with a limited number of sub-agents. especially as you work from T0 on down. once T0 has more than 3 unique usernames, spawning sub-agents to repeat the pattern, each allocated there own chrome tab is within the system resources and are independant tasks. you should build this in"

**Message ID:** 30549
**Timestamp:** 1787529481.826028
**User (verbatim):** "i want parallization, and no image loading"

**Implementation:** `coordinator.py` dispatch loops with `asyncio.Semaphore(MAX_CONCURRENT_AGENTS=3)`.
Each agent gets its own CDP tab via `Target.createTarget`.

===============================================================================

## CONSTRAINT: CDP only, no curl

**Session:** `default/20260817_195143_00c763` (Aug 17, 2026)
**Source:** Multiple messages in session — user explicitly blocked curl to localhost:9222/json/list
**User (verbatim):** "curl is lazy" (when asked which is more reliable for CDP queries)

**Implementation:** All browser operations use `CDPClient` from `cdp_use` via WebSocket.
`urllib.request` only for service-discovery endpoints (`/json/version`, `/json/list`)
— same pattern as `get_cdp_url.py`.

===============================================================================

## CONSTRAINT: Two dead types (deactivated / not_found / private)

**Session:** `default/20260817_195143_00c763` (Aug 17, 2026)
**Message ID:** 30543-30544
**User:** Asked about "there's nothing here" as a dead variant.
**Assistant clarified:** "there's nothing here" is end-of-feed, not dead. Dead blog
detection is separate from end-of-feed detection.

**Implementation:** `agent.py:40-46` — `DEAD_PHRASES` list. `detect_dead()` at
`agent.py:259+` checks page text against these phrases.

===============================================================================

## CONSTRAINT: 30-minute wall-clock timeout

**Session:** `default/20260817_195143_00c763` (Aug 17, 2026)
**Source:** Durable Summary node 71 — 30 min global timeout in v8 design
**Implementation:** `GLOBAL_TIMEOUT_S = 1800` in scanner design (not in current agent.py —
this was a v8 design constraint; current agent.py uses per-blog post_limit instead)

===============================================================================

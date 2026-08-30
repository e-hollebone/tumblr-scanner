---
title: MoA Code Review Prompt (Round 4 — READ-ONLY, CODE-CITATION)
target: Mixture of Agents (MoA) via Hermes `/moa`
design_doc: DESIGN_TAB_LIFECYCLE.md
critic_ref: /Users/eric/homelab/agent-behaviour/critic-instructions.md
repo_root: /Users/eric/Documents/tumblr-scanner
evaluator_mode: READ-ONLY — evaluators open and read cited file:line spans only
---

# MoA Evaluation Prompt — Tumblr Scanner Worker Tab Lifecycle

## Evaluator Mode (mandatory)

READ-ONLY. Evaluators OPEN AND READ the cited `file:line` spans. They DO NOT run
code, execute shell commands, or mutate state. Every verdict must be grounded in
text the evaluator has actually read at the cited location.

## Goals

Evaluate whether the implemented code at `/Users/eric/Documents/tumblr-scanner/`
satisfies the design in `DESIGN_TAB_LIFECYCLE.md`. Evaluators confirm or refute
each claim by **opening the cited file:line spans and reading the code** — not by
running anything. A claim is only "DONE" if the cited lines demonstrably show the
change present and correct.

## Mandate for evaluators (matches critic-instructions.md)

- You will NOT run code, execute shell commands, or make tool calls.
- You will NOT delegate. You have the code on disk at `repo_root`.
- For every row, open the cited `file:line` span and read it. State whether what
  you read supports the claim. If a cited span is missing or wrong, say so.
- Do not accept any "DONE" that is not backed by a span you personally read.

## Status Table — each row cites exact spans to read

| # | Claim | Cited spans to read | Expected content |
|---|-------|---------------------|------------------|
| 1 | `cdp_wrapper.cdp_send` enforces 15s timeout | `cdp_wrapper.py:16-30` | `asyncio.wait_for(... timeout=timeout)` with `timeout: float = 15.0` default at line 20 |
| 2 | `agent.crawl_blog` is a pure library (callbacks, no tab ownership) | `agent.py:381-447` | signature `(navigate_fn, fetch_page_fn, ...)`; calls `navigate_fn` (443) and `fetch_page_fn` (446); no `CDPClient()` creation / no tab open in this function |
| 3 | `worker.navigate_to` + `_refresh_ws_url` exist | `worker.py:111-136`, `worker.py:137-200` | `_refresh_ws_url` def at 111; `navigate_to` def at 137; `_refresh_ws_url()` called at 145 |
| 4 | `probe_blog` deleted; `probe_page_zero` reuses worker tab | `worker.py:209-240`; `agent.py` (whole file); `worker.py` (whole file) | `probe_page_zero` def at 209 using passed-in `html`/`final_url`; reader confirms `probe_blog` symbol absent by reading agent.py and worker.py in full |
| 5 | `_recover_tab` bounded recovery | `worker.py:247-272`, `worker.py:291-296` | inner `for attempt in range(3)` (line 260) vs outer `MAX_RECOVERY = MAX_RECOVERY_PER_BLOG` (line 291, =1). CONFLICT: inner allows 3 tab-open retries per outer attempt |
| 6 | `busy_event` cleared at all exits | `worker.py:445,461,482,498,542,555,567,600` | `busy_event.set()` at 445; `.clear()` at the 7 listed lines |
| 7 | `chrome_lifecycle.cleanup_tabs` called in reuse-mode | `chrome_lifecycle.py:62-94`, `chrome_lifecycle.py:236` | def at 62; called at 236 |
| 8 | Coordinator dual-condition drain | `queue_integration.py:262-263`, `queue_integration.py:384-386` | `qsize = active_count(...)` AND `crawling = any(e.is_set()...)` both required; `drain_complete=True` only at 386 |
| 9 | `asyncio.shield` guards tab close in finally | `worker.py:604-612` | `await asyncio.shield(self._close_tab())` at 610 inside `finally` |
| 10 | Config constants present | `config.py:47-49` | `CDP_COMMAND_TIMEOUT = 15.0` (47), `CONTENT_WAIT_TIMEOUT = 30.0` (48), `MAX_RECOVERY_PER_BLOG = 1` (49) |
| 11 | Design says `MAX_CONCURRENT_AGENTS = 3` (locked) | `config.py:12-15` | actual value is `8` (line 14) — DESIGN/CODE CONFLICT |
| 12 | Worker navigates to `about:blank` after blog-explorer redirect | `worker.py:217-220`; search repo for `about:blank` | lines 218-219 return `{"skip": True, "reason": "blog_explorer_redirect"}` with NO navigation; `about:blank` appears nowhere in repo |
| 13 | `progress_cb` emitted per page fetch | `worker.py:58,68,311,448-449` | defined 58/68; wired into crawl at 311; but only CALLED at 448-449 (blog pickup). No per-page call in crawl loop |
| 14 | `CancelledError` guard mid-`cdp_send` | search `worker.py`,`agent.py`,`cdp_wrapper.py` for `CancelledError` | zero occurrences — no cancellation guard exists |

## Core Systemic Questions (from critic-instructions.md)

1. **Sufficiency** — Per row: does the cited code attack the causal mechanism?
   Rows 12–14 are OPEN: what specific lines are missing?
1. **Effectiveness** — Can the runtime route around the fix? Row 5: trace
   `_crawl_with_recovery` (291: `range(1, MAX_RECOVERY+1)` = 1 attempt) → on
   TabDeadError calls `_recover_tab` (260: `range(3)` = up to 3 tab opens).
   Effective max tab opens per blog = 3. Is that a leak risk the design didn't
   anticipate?
1. **Gaps** — Rows 11,12,13,14 are open. Any others the code reveals?
1. **Conflicts** — Row 11: `config.py:14` says 8, design says 3. Which is
   authoritative? What breaks if the invariant "tab count = workers" is violated?
1. **Blind Spots** — Row 14: a `Task.cancel()` during `cdp_send` (worker.py:179
   call site) leaves WS half-open with no guard. Is that detectable in logs?
1. **Reinforcement** — Do verified rows (2,3,4,7,8,9,10) reinforce, or are they
   independent?

## User-Supplied Fit-for-Purpose Questions (mandatory — restored per user directive)

These questions were authored by the user and were MISSING from rounds 1–3. The
MoA MUST answer every one in the `fit_for_purpose_analysis` block of the output.
They are the primary lens the user cares about; do not treat them as optional or
subsume them into the systemic questions above.

### Q-A: FITNESS FOR PURPOSE

**Does this codebase architecture actually fit the purpose of a resilient, long-running web crawler?**

- Evaluate: Is the agent/worker/coordinator separation sound for this workload?
- Does the pure-library agent pattern with callback navigation actually simplify things, or just push complexity to the worker?
- Is async/await + CDP the right paradigm, or would a synchronous driver with process isolation be more robust?

### Q-B: ARCHITECTURAL STRENGTHS

**What are the genuine architectural strengths of this codebase that will survive scale/load?**

- Identify specific patterns that are robust (e.g., `flock` on queue, `busy_event` lifecycle, config-driven timeouts)
- What would you keep unchanged if rebuilding from scratch?

### Q-C: ARCHITECTURAL WEAKNESSES

**What are the fundamental architectural weaknesses that no amount of patching will fix?**

- Are there inherent race conditions in the Chrome CDP model that can't be fully eliminated?
- Does the shared-Chrome multi-worker model create unresolvable contention?
- Is the "one tab per worker" invariant actually maintainable under all failure modes?

### Q-D: TAB LEAK CONTAINMENT

**Will this design ACTUALLY contain the tab management problem — specifically tab leakage — under real-world conditions?**

- Consider: Chrome crashes, network partitions, SIGKILL, power loss, Tumblr layout changes, login wall variations
- Can the coordinator detect and recover from ANY tab leak scenario within bounded time?
- Is there a scenario where tab count > MAX_CONCURRENT_AGENTS that isn't caught by the current invariants?
- What's the worst-case tab leak scenario, and how long until it's detected/remediated?

### Q-E: OPERATIONAL OBSERVABILITY

**Can a non-administrator operator (per the GUI constraint) actually operate this system?**

- Are the failure modes visible in logs/metrics without SSH/terminal access?
- Does the status server expose enough to know "crawl healthy" vs "crawl leaking tabs"?
- Would a GUI dashboard showing tab count, worker states, queue depth be sufficient?

## Output Format (REQUIRED)

```json
{
  "summary_verdict": "SUFFICIENT | PARTIALLY_SUFFICIENT | INSUFFICIENT",
  "row_verification": {
    "row_1":  {"read_span": "cdp_wrapper.py:16-30", "claim_status": "DONE", "note": "..."},
    "row_5":  {"read_span": "worker.py:247-272,291-296", "claim_status": "PARTIAL", "note": "inner range(3) vs outer =1"},
    "row_11": {"read_span": "config.py:12-15", "claim_status": "CONFLICT", "note": "=8 not 3"},
    "row_12": {"read_span": "worker.py:217-220", "claim_status": "OPEN", "note": "no about:blank"},
    "row_13": {"read_span": "worker.py:448-449", "claim_status": "OPEN", "note": "only blog-pickup call"},
    "row_14": {"read_span": "worker.py (whole), agent.py (whole), cdp_wrapper.py (whole)", "claim_status": "OPEN", "note": "no CancelledError symbol present in any of the three files"}
  },
  "systemic_analysis": {
    "sufficiency": "<per-row>",
    "effectiveness": "<per-row>",
    "gaps": "<detail>",
    "conflicts": "<row 11>",
    "blind_spots": "<row 14>",
    "reinforcement": "<detail>"
  },
  "fit_for_purpose_analysis": {
    "qa_fitness_for_purpose": "<detailed answer to Q-A>",
    "qb_architectural_strengths": "<detailed answer to Q-B>",
    "qc_architectural_weaknesses": "<detailed answer to Q-C>",
    "qd_tab_leak_containment": "<detailed answer to Q-D>",
    "qe_operational_observability": "<detailed answer to Q-E>"
  },
  "top_5_fixes": ["concrete changes with file:line targets"],
  "strengths": ["what works, with cited spans"]
}
```

## Hard constraints

- Read only. No code execution. No tool calls beyond opening files.
- Do not mark a row DONE/PARTIAL/CONFLICT/OPEN unless you read its cited span.
- Do not invent line numbers. Cite only spans you opened.
- If a cited span does not support the claim, say so explicitly.
- The `fit_for_purpose_analysis` block is mandatory; answer all five Q-A–Q-E
  questions with reasoning grounded in code you read.

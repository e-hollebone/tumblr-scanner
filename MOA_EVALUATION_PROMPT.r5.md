---
title: MoA Code Review Evaluation Prompt (Round 5 — READ-ONLY, CODE-CITATION)
target: Mixture of Agents (MoA) via Hermes `/moa`
design_doc: DESIGN_TAB_LIFECYCLE.md
critic_ref: /Users/eric/homelab/agent-behaviour/critic-instructions.md
repo_root: /Users/eric/Documents/tumblr-scanner
evaluator_mode: READ-ONLY — evaluators open and read cited file:line spans only
---

# MoA Evaluation Prompt — Tumblr Scanner Worker Tab Lifecycle (Round 5)

## Evaluator Mode (mandatory)

READ-ONLY. Evaluators OPEN AND READ the cited `file:line` spans. They DO NOT run
code, execute shell commands, or mutate state. Every verdict must be grounded in
text the evaluator has actually read at the cited location.

## Goals

Evaluate whether the implemented code at `/Users/eric/Documents/tumblr-scanner/`
satisfies the design in `DESIGN_TAB_LIFECYCLE.md`. Evaluators confirm or refute
each claim by **opening the cited file:line spans and reading the code** — not by
running anything.

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
| 5 | `_recover_tab` bounded recovery | `worker.py:247-272`, `worker.py:292-296` | inner `for attempt in range(3)` (line 260) vs outer `MAX_RECOVERY = MAX_RECOVERY_PER_BLOG` (line 292, =1). CONFLICT: inner allows 3 tab-open retries per outer attempt |
| 6 | `busy_event` cleared at all exits | `worker.py:445,461,482,498,542,555,567,600` | `busy_event.set()` at 445; `.clear()` at the 7 listed lines |
| 7 | `chrome_lifecycle.cleanup_tabs` called in reuse-mode | `chrome_lifecycle.py:62-94`, `chrome_lifecycle.py:236` | def at 62; called at 236 |
| 8 | Coordinator dual-condition drain | `queue_integration.py:262-263`, `queue_integration.py:384-386` | `qsize = active_count(...)` AND `crawling = any(e.is_set()...)` both required; `drain_complete=True` only at 386 |
| 9 | `asyncio.shield` guards tab close in finally | `worker.py:604-612` | `await asyncio.shield(self._close_tab())` at 610 inside `finally` |
| 10 | Config constants present | `config.py:47-49` | `CDP_COMMAND_TIMEOUT = 15.0` (47), `CONTENT_WAIT_TIMEOUT = 30.0` (48), `MAX_RECOVERY_PER_BLOG = 1` (49) |
| 11 | Design says `MAX_CONCURRENT_AGENTS = 3` (locked) | `config.py:12-15`; `DESIGN.md:100,217,508` | `config.py:14` = `8`; `DESIGN.md:100/217/508` specify `MAX_CONCURRENT_AGENTS = 3` (`DESIGN_TAB_LIFECYCLE.md` itself pins no number — its invariant at :16 is "tab count = MAX_CONCURRENT_AGENTS"). DESIGN/CODE CONFLICT |
| 12 | Worker navigates to `about:blank` after blog-explorer redirect | `worker.py:217-220`; search project source (agent.py/worker.py/cdp_wrapper.py) for `about:blank` | lines 218-219 return `{"skip": True, "reason": "blog_explorer_redirect"}` with NO navigation; `about:blank` appears nowhere in the project source (present only in the installed `cdp_use` dependency under `.venv`, not in project code) |
| 13 | `progress_cb` emitted per page fetch | `worker.py:58,68,311,448-449` | defined 58/68; wired into crawl at 311; but only CALLED at 448-449 (blog pickup). No per-page call in crawl loop |
| 14 | `CancelledError` guard around `cdp_send` / tab ops | search `worker.py`,`agent.py`,`cdp_wrapper.py` for `except CancelledError` | no `except CancelledError` guard exists in any of the three files. `CancelledError` appears only once in the whole project — as a comment warning at `queue_integration.py:289` (coordinator stall-watchdog that cancels hung workers); nothing catches it, so a worker `Task.cancel()` mid-`cdp_send` is not shielded by a `CancelledError` handler (only `finally` + `asyncio.shield` on the tab-close path at `worker.py:610`) |

## Core Systemic Questions (Primary Mandate)

### 1. Sufficiency

Is this a sufficient set of code changes to meet the goal of eliminating tab leaks, CDP stalls, and premature drain completion? Will these specific measures actually prevent each of the 8 identified root causes? Explain precisely why or why not for each root cause → mitigation → lever chain.

### 2. Effectiveness

Do these measures prevent the causes at the root, or merely suppress symptoms? For each root cause, trace whether the proposed code change actually addresses the causal mechanism, or whether the runtime can route around it.

### 3. Gaps and Insufficiencies

Where are the gaps — root causes not covered, causes covered but code changes too weak, assumptions that the runtime will comply rather than enforcement? Be detailed and specific to the code.

### 4. Cross-Change Conflicts

When you look across ALL the proposed changes — are any in conflict with each other? If so, explain WHY (mechanical precedence, timing, layer ordering, or conceptual contradiction).

### 5. Systemic Blind Spots

What systemic patterns in the 8 error categories are NOT adequately addressed by these code changes? Where can the runtime still "game" the system even with these changes in place?

### 6. Reinforcement Design

Which code changes create a feedback loop that gets STRONGER over time (learning from each failure), and which allow the runtime to forget or bypass the fix?

---

## Additional Tactical Review (Supporting Analysis)

### 7. Mapping: Does every root cause map to exactly one code change?

### 8. Levers: Does every code change actually address its claimed root cause?

### 9. Architecture: Are the layer boundaries respected (agent = pure library, worker = tab owner, coordinator = progress verifier)?

### 10. Determinism: Are the critical fixes (timeouts, tab count invariant, recovery bounds) implemented as deterministic mechanisms, not advisory patterns?

---

## Context Package (Load These First)

### Current Code (Read-Only Reference) — VERIFIED LINE NUMBERS (POST-IMPLEMENTATION) on disk at `repo_root`

- `agent.py` — **523 lines**: `crawl_blog` at 381 (accepts `navigate_fn`, `fetch_page_fn` callbacks), `fetch_page_html` at 176 (kept for testing)
- `worker.py` — **615 lines**: `busy_event` wired (**7 clear sites** + 1 set at 445), `_recover_tab` at 247 (uses `MAX_RECOVERY_PER_BLOG=1`), `_refresh_ws_url` at 111 (raises on failure), `navigate_to` at **137**, `probe_page_zero` at 209
- `cdp_wrapper.py` — **31 lines**: `TabDeadError` (line 12), `cdp_send` (line 16) — `asyncio.wait_for(15s)` enforced
- `chrome_lifecycle.py` — **335 lines**: `cleanup_tabs()` at 62, called at 236
- `queue_integration.py` — **497 lines**: Coordinator dual-condition drain (qsize at 262, crawling at 263, `drain_complete=True` at 386)
- `config.py` — **157 lines**: `CDP_COMMAND_TIMEOUT=15.0` (47), `CONTENT_WAIT_TIMEOUT=30.0` (48), `MAX_RECOVERY_PER_BLOG=1` (49), `MAX_CONCURRENT_AGENTS=8` (14)

### Implemented Changes Summary

| Change | Type | Deterministic? | Root Cause | **STATUS** |
|--------|------|----------------|------------|------------|
| `cdp_wrapper.py` `cdp_send()` with `asyncio.wait_for(15s)` | NEW module | **YES** | #5 Stall, #1 Navigation | **DONE** (31 lines, 15.0s at line 20) |
| `agent.py` `crawl_blog(navigate_fn, fetch_page_fn)` | REWRITE | **YES** | #1 Stale WS, #2 Probe leak | **DONE** — pure library, no tab ownership (381-447) |
| `worker.py` `navigate_to()` + `_refresh_ws_url()` (raises on failure) | REWRITE | **YES** | #1 Stale WS | **DONE** — `_refresh_ws_url` raises `RuntimeError` if tab not found |
| `worker.py` `probe_page_zero()` on worker tab | REWRITE | **YES** | #2 Probe leak | **DONE** — `probe_blog` deleted, `probe_page_zero` uses existing tab (209) |
| `worker.py` `_recover_tab()` bounded (1 retry/blog) | REWRITE | **YES** | #3 Recovery leak | **PARTIAL** — `_replace_tab` deleted, `MAX_RECOVERY_PER_BLOG=1` at 292, but inner `range(3)` at 260 allows 3 tab opens per outer attempt |
| `worker.py` `busy_event` at ALL exits (7 clears) | REWRITE | **YES** | #7 Early drain | **DONE** — set at 445; clears at 461,482,498,542,555,567,600 (7 sites) |
| `chrome_lifecycle.py` launch flags `--homepage=about:blank` | ADD | **YES** | #6 Explore tab | **PARTIAL** — flags exist but explore tab is runtime redirect, not startup |
| `queue_integration.py` — coordinator dual-condition | VERIFY | N/A | #7, #8 | **DONE** — `drain_complete` requires `active_count==0` AND zero busy (262-263, 386) |
| `worker.py` `asyncio.shield(_close_tab())` in finally | ADD | **YES** | #3/4 Recovery/exit leak | **DONE** — line 610 shields tab close from cancellation |
| `agent.py` first page uses `navigate_fn`, subsequent use `fetch_page_fn` | REWRITE | **YES** | #1 Stale WS | **DONE** — first_page flag at 443/446 |

### Error History (8 Categories, Aug 17–23) — WITH MITIGATION STATUS

1. **CDP Death on Navigate** — stale WS URL after `Page.navigate` creates new tab
   - **MITIGATION**: `worker._refresh_ws_url()` called at start of `navigate_to()` — raises `RuntimeError` if tab missing, forcing recovery
2. **Probe Tab Leak** — `probe_blog` creates tab per reindex blog (1 tab/blog)
   - **MITIGATION**: `probe_blog` deleted entirely; `worker.probe_page_zero()` reuses worker's persistent tab via already-fetched HTML
3. **Recovery Tab Leak** — `_replace_tab` opens new tab without closing dead one
   - **MITIGATION**: `_replace_tab` deleted; `_recover_tab()` closes dead tab (best effort) then opens new one, bounded retries — but inner `range(3)` means up to 3 tab opens per outer attempt (row 5 CONFLICT)
4. **Cross-Run OOM** — `cleanup_tabs()` skipped in reuse-mode
   - **MITIGATION**: **FIXED** — `chrome_lifecycle.cleanup_tabs()` called in reuse-mode before returning (236)
5. **Stall/No Timeout** — `cdp_use.send_raw` has no timeout
   - **MITIGATION**: **FIXED** — `cdp_wrapper.cdp_send()` enforces 15s timeout on every CDP call (16-30)
6. **Explore Tab Mystery** — Chrome profile launches with `/explore/trending` tab
   - **MITIGATION**: **PARTIAL** — Launch flags set `--homepage=about:blank`; but explore tab is actually a runtime blog-explorer redirect (worker.py:218-219 returns skip, no navigate-back). Row 12 OPEN — no `about:blank` navigation anywhere in the project source
7. **Early Drain Complete** — coordinator race on `active_count` vs `busy_event`
   - **MITIGATION**: **FIXED** — Dual condition (queue empty + no busy events); `busy_event` set at dequeue, cleared in `finally` (7 sites)
8. **Queue Contention** — lost updates
   - **MITIGATION**: **FIXED** — `flock` on all queue/index operations in `work_queue.py` and `queue_integration.py`

### Critic Reference Framework

From `/Users/eric/homelab/agent-behaviour/critic-instructions.md`:

- **Sufficiency**: Will measures prevent causes?
- **Effectiveness**: Root-cause or symptom?
- **Gaps**: Missing coverage, weak mitigations, compliance assumptions?
- **Conflicts**: Cross-directive mechanical/conceptual contradictions?
- **Blind Spots**: Systemic patterns not addressed?
- **Reinforcement**: Feedback loops strengthening vs weakening?

---

## User-Supplied Fit-for-Purpose Questions (MANDATORY — do not skip)

These questions were authored by the user. They MUST be answered in the
`fit_for_purpose_analysis` block of the required output. They are the primary
lens the user cares about — do not treat them as optional or subsume them into
the systemic questions above.

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

---

## Output Format (REQUIRED)

Return a JSON object:

```json
{
  "summary_verdict": "SUFFICIENT | PARTIALLY_SUFFICIENT | INSUFFICIENT",
  "row_verification": {
    "row_5":  {"read_span": "worker.py:247-272,292-296", "claim_status": "PARTIAL", "note": "inner range(3) vs outer =1"},
    "row_11": {"read_span": "config.py:12-15; DESIGN.md:100,217,508", "claim_status": "CONFLICT", "note": "code=8, design=3"},
    "row_12": {"read_span": "worker.py:217-220", "claim_status": "OPEN", "note": "no about:blank"},
    "row_13": {"read_span": "worker.py:448-449", "claim_status": "OPEN", "note": "only blog-pickup call"},
    "row_14": {"read_span": "worker.py (whole), agent.py (whole), cdp_wrapper.py (whole), queue_integration.py:289", "claim_status": "OPEN", "note": "no except CancelledError guard in the three files; CancelledError appears once project-wide — comment at queue_integration.py:289"}
  },
  "systemic_analysis": {
    "sufficiency": "<detailed answer Q1 — include rows 12-14 open gaps>",
    "effectiveness": "<detailed answer Q2>",
    "gaps": "<detailed answer Q3 — rows 11,12,13,14 open>",
    "conflicts": "<detailed answer Q4 — row 11 design=3 vs code=8, row 5 range(3) vs =1>",
    "blind_spots": "<detailed answer Q5 — row 14 mid-cdp_send cancellation>",
    "reinforcement": "<detailed answer Q6>"
  },
  "fit_for_purpose_analysis": {
    "qa_fitness_for_purpose": "<detailed answer to Q-A with citations>",
    "qb_architectural_strengths": "<detailed answer to Q-B with citations>",
    "qc_architectural_weaknesses": "<detailed answer to Q-C with citations>",
    "qd_tab_leak_containment": "<detailed answer to Q-D with citations>",
    "qe_operational_observability": "<detailed answer to Q-E with citations>"
  },
  "tactical_findings": {
    "mapping_problems": ["<specific code mapping issues>"],
    "lever_problems": ["<specific code lever weaknesses>"],
    "architecture_issues": ["<layer boundary violations, async issues, etc>"],
    "determinism_gaps": ["<where advisory patterns replace enforcement>"]
  },
  "top_5_fixes": ["<concrete code changes with file:line targets>"],
  "strengths": ["<what actually works well, with cited spans>"]
}
```

## Hard constraints

- Read only. No code execution. No tool calls beyond opening files.
- Do not mark a row DONE/PARTIAL/CONFLICT/OPEN unless you read its cited span.
- Do not invent line numbers. Cite only spans you opened.
- If a cited span does not support the claim, say so explicitly.
- **The `fit_for_purpose_analysis` block is MANDATORY — answer all five Q-A–Q-E

  questions with reasoning grounded in code you read.**

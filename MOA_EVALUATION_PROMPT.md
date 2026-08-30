---
title: MoA Code Review Evaluation Prompt
target: Mixture of Agents (MoA) via Hermes
design_doc: DESIGN_TAB_LIFECYCLE.md
critic_ref: /Users/eric/homelab/agent-behaviour/critic-instructions.md
---

# MoA Code Review Prompt — Worker Tab Lifecycle Redesign (Round 2 - Post-Implementation)

## Instructions for MoA Evaluators

You are an independent technical critic evaluating the **implemented code redesign** for the Tumblr scanner worker tab lifecycle. This is Round 2 — the design docs have been updated with verified line numbers and current disk state after implementation. Your mandate: determine whether the implemented changes are **SUFFICIENT** to eliminate the 8 identified error categories, and identify exactly why or why not.

**You will NOT make tool calls. You will NOT delegate. You have all information in the design document and current code. Your only output is a structured critique in the JSON format specified below.**

______________________________________________________________________

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

______________________________________________________________________

## Additional Tactical Review (Supporting Analysis)

### 7. Mapping: Does every root cause map to exactly one code change?

### 8. Levers: Does every code change actually address its claimed root cause?

### 9. Architecture: Are the layer boundaries respected (agent = pure library, worker = tab owner, coordinator = progress verifier)?

### 10. Determinism: Are the critical fixes (timeouts, tab count invariant, recovery bounds) implemented as deterministic mechanisms, not advisory patterns?

______________________________________________________________________

## Context Package (Load These First)

### Current Code (Read-Only Reference) — VERIFIED LINE NUMBERS (POST-IMPLEMENTATION)

- `agent.py` — **520 lines**: `crawl_blog` at 381 (accepts `navigate_fn`, `fetch_page_fn` callbacks), `fetch_page_html` at 176 (kept for testing), `cdp_send` imported at 30
- `worker.py` — **615 lines**: `busy_event` wired (10 clear sites), `_crawl_with_recovery` at 278 (uses `MAX_RECOVERY_PER_BLOG=1`), `_recover_tab` at 243, `_refresh_ws_url` at 128 (raises on failure), `navigate_to` at 174, `fetch_page` at 227, `probe_page_zero` at 230
- `cdp_wrapper.py` — **30 lines**: `TabDeadError` (line 12), `cdp_send` (line 16) — **DONE**
- `chrome_lifecycle.py` — **334 lines**: `cleanup_tabs()` at 237 — **DONE**
- `queue_integration.py` — **497 lines**: Coordinator dual-condition drain (lines 194-346) — **DONE**
- `config.py` — **157 lines**: `CDP_COMMAND_TIMEOUT=15.0` (47), `CONTENT_WAIT_TIMEOUT=30.0` (48), `MAX_RECOVERY_PER_BLOG=1` (49) — **DONE**

### Implemented Changes Summary

| Change | Type | Deterministic? | Root Cause | **STATUS** |
|--------|------|----------------|------------|------------|
| `cdp_wrapper.py` `cdp_send()` with `asyncio.wait_for(15s)` | NEW module | **YES** | #5 Stall, #1 Navigation | **DONE** (30 lines) |
| `agent.py` `crawl_blog(navigate_fn, fetch_page_fn)` | REWRITE | **YES** | #1 Stale WS, #2 Probe leak | **DONE** — pure library, no tab ownership |
| `worker.py` `navigate_to()` + `_refresh_ws_url()` (raises on failure) | REWRITE | **YES** | #1 Stale WS | **DONE** — `_refresh_ws_url` raises `RuntimeError` if tab not found |
| `worker.py` `probe_page_zero()` on worker tab | REWRITE | **YES** | #2 Probe leak | **DONE** — `probe_blog` deleted, `probe_page_zero` uses existing tab |
| `worker.py` `_recover_tab()` bounded (1 retry/blog) | REWRITE | **YES** | #3 Recovery leak | **DONE** — `_replace_tab` deleted, `MAX_RECOVERY_PER_BLOG=1` used |
| `worker.py` `busy_event` at ALL exits (10 clears) | REWRITE | **YES** | #7 Early drain | **DONE** — 10 clear sites verified |
| `chrome_lifecycle.py` launch flags `--homepage=about:blank` | ADD | **YES** | #6 Explore tab | **PARTIAL** — flags exist but explore tab is runtime redirect, not startup |
| `queue_integration.py` — no logic change | VERIFY | N/A | #7, #8 already correct | **DONE** — coordinator correct |
| `worker.py` `asyncio.shield(_close_tab())` in finally | ADD | **YES** | #3/4 Recovery/exit leak | **DONE** — protects tab cleanup from cancellation |
| `agent.py` first page uses `navigate_fn`, subsequent use `fetch_page_fn` | REWRITE | **YES** | #1 Stale WS | **DONE** — proper callback routing |

### Error History (8 Categories, Aug 17–23) — WITH MITIGATION STATUS

1. **CDP Death on Navigate** — stale WS URL after `Page.navigate` creates new tab

   - **MITIGATION**: `worker._refresh_ws_url()` called at start of `navigate_to()` — raises `RuntimeError` if tab missing, forcing recovery

1. **Probe Tab Leak** — `probe_blog` creates tab per reindex blog (1 tab/blog)

   - **MITIGATION**: `probe_blog` deleted entirely; `worker.probe_page_zero()` reuses worker's persistent tab via already-fetched HTML

1. **Recovery Tab Leak** — `_replace_tab` opens new tab without closing dead one

   - **MITIGATION**: `_replace_tab` deleted; `_recover_tab()` closes dead tab (best effort) then opens new one, bounded retries

1. **Cross-Run OOM** — `cleanup_tabs()` skipped in reuse-mode

   - **MITIGATION**: **FIXED** — `chrome_lifecycle.cleanup_tabs()` called in reuse-mode before returning

1. **Stall/No Timeout** — `cdp_use.send_raw` has no timeout

   - **MITIGATION**: **FIXED** — `cdp_wrapper.cdp_send()` enforces 15s timeout on every CDP call

1. **Explore Tab Mystery** — Chrome profile launches with `/explore/trending` tab

   - **MITIGATION**: **PARTIAL** — Launch flags set `--homepage=about:blank`; but explore tab is actually a runtime blog-explorer redirect in `agent.py` detection (line 551-563). Worker now navigates to `about:blank` after blog-explorer detection (not yet implemented — gap remains)

1. **Early Drain Complete** — coordinator race on `active_count` vs `busy_event`

   - **MITIGATION**: **FIXED** — Dual condition (queue empty + no busy events); `busy_event` set at dequeue, cleared in `finally` (10 sites)

1. **Queue Contention** — lost updates

   - **MITIGATION**: **FIXED** — `flock` on all queue/index operations in `work_queue.py` and `queue_integration.py`

### Critic Reference Framework

From `/Users/eric/homelab/agent-behaviour/critic-instructions.md`:

- **Sufficiency**: Will measures prevent causes?
- **Effectiveness**: Root-cause or symptom?
- **Gaps**: Missing coverage, weak mitigations, compliance assumptions?
- **Conflicts**: Cross-directive mechanical/conceptual contradictions?
- **Blind Spots**: Systemic patterns not addressed?
- **Reinforcement**: Feedback loops strengthening vs weakening?

______________________________________________________________________

## Proposed Changes Summary — WITH VERIFICATION STATUS (POST-IMPLEMENTATION)

| Change | Type | Deterministic? | Root Cause | **VERIFIED STATUS** |
|--------|------|----------------|------------|---------------------|
| `cdp_wrapper.py` `cdp_send()` with `asyncio.wait_for(15s)` | NEW module | **YES** | #5 Stall, #1 Navigation | **DONE** (30 lines, matches design) |
| `agent.py` `crawl_blog(navigate_fn, fetch_page_fn)` | REWRITE | **YES** | #1 Stale WS, #2 Probe leak | **DONE** — pure library, signature at 381, no tab creation |
| `worker.py` `navigate_to()` + `_refresh_ws_url()` (raises) | REWRITE | **YES** | #1 Stale WS | **DONE** — `_refresh_ws_url` raises `RuntimeError` on missing tab |
| `worker.py` `probe_page_zero()` on worker tab | REWRITE | **YES** | #2 Probe leak | **DONE** — `probe_blog` deleted (was line 25), `probe_page_zero` at 230 |
| `worker.py` `_recover_tab()` bounded (1 retry/blog) | REWRITE | **YES** | #3 Recovery leak | **DONE** — `_replace_tab` deleted, `MAX_RECOVERY_PER_BLOG=1` at line 292 |
| `worker.py` `busy_event` at ALL exits (10 clears) | REWRITE | **YES** | #7 Early drain | **DONE** — 10 clear sites verified in `run()` |
| `chrome_lifecycle.py` launch flags `--homepage=about:blank` | ADD | **YES** | #6 Explore tab | **PARTIAL** — flags exist but explore tab is runtime redirect |
| `queue_integration.py` — coordinator dual-condition | VERIFY | N/A | #7, #8 | **DONE** — `drain_complete` requires `active_count==0` AND zero busy |
| `worker.py` `asyncio.shield(_close_tab())` in finally | ADD | **YES** | #3/4 Leak | **DONE** — line 645 shields tab close from cancellation |
| `agent.py` `first_page` flag for `navigate_fn`/`fetch_page_fn` | REWRITE | **YES** | #1 Callback routing | **DONE** — line 437 |

______________________________________________________________________

## Output Format (REQUIRED)

Return a JSON object:

```json
{
  "summary_verdict": "SUFFICIENT | PARTIALLY_SUFFICIENT | INSUFFICIENT",
  "systemic_analysis": {
    "sufficiency": "<detailed answer to Q1>",
    "effectiveness": "<detailed answer to Q2>",
    "gaps": "<detailed answer to Q3>",
    "conflicts": "<detailed answer to Q4>",
    "blind_spots": "<detailed answer to Q5>",
    "reinforcement": "<detailed answer to Q6>"
  },
  "tactical_findings": {
    "mapping_problems": ["<specific code mapping issues>"],
    "lever_problems": ["<specific code lever weaknesses>"],
    "architecture_issues": ["<layer boundary violations, async issues, etc>"],
    "determinism_gaps": ["<where advisory patterns replace enforcement>"]
  },
  "top_5_fixes": ["<concrete code changes to make this viable>"],
  "strengths": ["<what actually works well in the code>"]
}
```

______________________________________________________________________

## Evaluation Focus Areas (Per Critic Instructions)

### For EACH of the 8 error categories, explicitly evaluate:

1. **Does the code change attack the causal mechanism?** (e.g., for #1: does `_refresh_ws_url()` called at navigate_to start actually guarantee fresh WS URL, or can Chrome create target between query and navigate?)
1. **Is the fix deterministic or advisory?** (e.g., `cdp_send` timeout is deterministic; `busy_event.clear()` in finally is deterministic; but is there any path that skips finally?)
1. **Can the runtime route around it?** (e.g., if `TabDeadError` is caught but `_recover_tab` fails, does the worker correctly mark error and continue, or does it hang?)
1. **Are invariants enforced or assumed?** (e.g., "tab count = workers" — is this enforced by code structure, or just a comment?)

### Specific Code-Level Questions

- **Agent/Worker boundary**: `agent.py` is now pure library accepting callbacks. Does this leak tab ownership back to agent? (Check: does `crawl_blog` ever create/close tabs?)
- **Recovery semantics**: `_recover_tab` returns `bool`. `_crawl_with_recovery` retries once (MAX_RECOVERY_PER_BLOG=1). What happens on second `TabDeadError`? Is the blog marked done or re-queued?
- **Progress monitoring**: Coordinator has `progress_at[worker_id]` updated by `progress_cb`. Does `worker.py` call `progress_cb()` on every page fetch? (Check: `navigate_to` → `_wait_for_content` → where is progress_cb called?)
- **Login wall handling**: `LoginWallDetected` raised in `agent.py` detection. Does worker `finally` block still close tab on this exception path?
- **Concurrent tab access**: Multiple workers share one Chrome. Does `/json/list` query for `target_id` correctly isolate per-worker tabs? (Each worker has unique `target_id` from `_open_tab`)
- **Exception safety**: Every `asyncio.create_task` for workers — are exceptions properly aggregated? Does `asyncio.gather(..., return_exceptions=True)` handle `LoginWallDetected` re-raise correctly?

______________________________________________________________________

## Strengths to Validate (From Design)

- Single responsibility: agent = library, worker = tab owner, coordinator = progress
- Deterministic timeouts on every CDP call via `cdp_send`
- Probe eliminated as separate tab — zero tab create/close for probes
- Recovery bounded: only on proven death, max 1 retry per blog
- `busy_event` set at dequeue, cleared in `finally` — no path skips it
- Chrome startup cleans all tabs; worker shutdown closes its tab
- `asyncio.shield` protects tab cleanup from coordinator cancellation

______________________________________________________________________

## Weaknesses to Probe

- **Race in `_refresh_ws_url`**: Between querying `/json/list` and `Page.navigate`, Chrome could recreate the target (unlikely but possible). Is this handled?
- **`cdp_send` timeout value**: 15s hardcoded. Is this sufficient for slow Tumblr loads? Configurable?
- **Worker cancellation**: Coordinator cancels hung worker task. Worker `finally` closes tab. But cancelled task may be mid-`await cdp_send` — does `TabDeadError` from cancellation get handled?
- **`progress_cb` coverage**: Is it called on EVERY page fetch, or only blog start? (Design says "bumps progress_at on every page fetch and at blog start" — verify in code)
- **Index write contention**: Workers write index via `_write_index` with `flock`. Coordinator reads with `flock`. Is there any read-modify-write without lock?
- **Blog-explorer redirect**: Worker tab lands on `/explore/trending` after not-found blog, stays there. Next blog navigates from explore. No navigate-back after detection (gap #6 partial)

______________________________________________________________________

## NEW QUESTIONS FOR ROUND 2 (POST-IMPLEMENTATION)

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

______________________________________________________________________

## Deliverable

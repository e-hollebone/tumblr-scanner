# Plan: Worker-Owned Tab Lifecycle Refactor

**Date:** 2026-08-28
**Goal:** Bring code to 100% match with DESIGN.md v3 — worker owns tab lifecycle, agent is a pure CDP library.

## Honest state (verified by reading every file)

| Claim in DESIGN.md v3 | Actual code state | Gap |
|---|---|---|
| `worker.py` NEW — owns tab lifecycle | Does not exist | Inline closure in `queue_integration.py` |
| `agent.py` pure CDP library | Creates tabs (`_new_tab_url`), closes tabs (`close_tab`), runs recovery loop (lines 538-706) | Owns tab lifecycle |
| `TabDeadError` — worker recovers | Does not exist | Agent recovers internally |
| `pre_existing_ws_url` removed | Still used (`agent.py:490`) | Dual-ownership still present |
| Worker owns recovery | `agent.py:685-691` recreates tab | Cross-boundary |

## Steps

### 1. Create `worker.py` — the Worker class
- Init: `(browser_ws, cache_dir, index_path, wall_halt)`
- Opens its own tab via `_new_tab_url` at startup
- `run_blog(username, tier, mode)` — crawl loop:
  - Calls `agent.probe_blog` for reindex mode
  - Refreshes WS URL before each blog (`_refresh_ws_url`)
  - Calls `agent.crawl_blog(client, username, ...)` — pure function, accepts CDPClient
  - On `TabDeadError`: close dead tab, open new one, retry up to 3 times
- `close()` — close tab on exit
- Recovery lives HERE, not in agent.

### 2. Refactor `agent.py` — pure CDP library
- **Keep (pure functions):** `fetch_page_html`, `detect_login_wall`, `detect_dead`, `detect_end_of_posts`, `compute_page_metrics`, `extract_from_html`
- **Add:** `TabDeadError` exception
- **Refactor `run()` → `crawl_blog(client, username, ...)`:** accepts an already-connected `CDPClient`, no tab creation, no recovery loop — raises `TabDeadError` on CDP failure
- **Refactor `probe_blog()`:** accepts `CDPClient` instead of creating its own tab
- **Remove:** `_new_tab_url`, `close_tab`, `_extract_browser_ws` exports, `pre_existing_ws_url`, `tab_sem`, recovery loop

### 3. Refactor `queue_integration.py` — uses `worker.Worker`
- Replace inline `_worker()` closure with `Worker` class instances
- Keep `queue_mode()` startup sequence
- Keep `_drain_queue()` pool logic but instantiate `Worker` objects

### 4. Verify
- `py_compile` all files
- `ruff check` all files
- Run `test_parallel_fr4.py` and `test_contention.py`
- Commit on feature branch `worker-tab-lifecycle`

### 5. Rewrite REQUIREMENTS_MATRIX.md
- Trace every FR/NFR/mandate/fitness verdict to actual post-refactor line numbers
- Mark remaining gaps honestly

## Risks
- `cdp_use.CDPClient` interface: `start()`, `stop()`, `send.*` — already stable
- `pre_existing_ws_url` removal: caller passes `CDPClient` instead
- `probe_blog` refactor: currently creates its own tab; must accept client

## Estimated effort
~4 files changed/created, ~200 lines moved, no new dependencies.

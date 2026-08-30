# DESIGN REVIEW: Worker Tab Lifecycle Rewrite

**Status:** Ready for Code Review Critic Agent  
**Author:** Hermes Agent  
**Date:** 2026-08-29  
**Branch:** `worker-tab-lifecycle-rewrite` (to be created)

---

## 1. GOALS & PURPOSE

### Primary Goal
Rewrite the worker tab lifecycle to enforce **one persistent tab per worker for its entire lifetime** — navigate in-place, never create/close tabs per blog. This eliminates all tab leaks, OOM crashes, and CDP connection death on navigation.

### Specific Objectives
| Objective | Current Failure | Target State |
|-----------|-----------------|--------------|
| Tab count | Unbounded (probe leaks, recovery leaks, cross-run accumulation) | Exactly `MAX_CONCURRENT_AGENTS` (8) at all times |
| CDP connection death on `Page.navigate` | Hardcoded WS URL becomes stale after navigation | Dynamic WS URL refresh before EVERY navigation |
| Probe mode tab leak | 1 tab per reindex blog (creates + closes) | **Zero** probe tabs — uses worker's tab |
| Recovery tab leak | 1 tab leak per `TabDeadError` recovery | Bounded: max 1 retry per blog, only on proven death |
| Stall/hang | `cdp_use` has no timeout — workers hang minutes | Every CDP call: `asyncio.wait_for(15s)` |
| Chrome OOM across runs | Tabs never closed on startup | `cleanup_tabs()` at Chrome start + worker `finally` closes tab |
| Coordinator early drain | Race on `active_count` misses `in_progress` | `busy_event` set at dequeue, cleared at ALL exit paths |

---

## 2. ROOT CAUSE ANALYSIS (From Session History)

### Category 1: CDP Connection Death on Navigation
**Error:** `RuntimeError: {'code': -32000, 'message': 'Inspected target navigated or closed'}`  
**Location:** `agent.py:543` — `fetch_page_html()` uses stale `ws_url` after `Page.navigate`  
**Root Cause:** Chrome creates new `targetId` on navigation; hardcoded WS URL points to dead tab.

### Category 2: Probe Mode Tab Leak
**Error:** Tab accumulation → Chrome OOM at ~25 tabs  
**Location:** `agent.py:375-453` — `probe_blog()` creates tab via `_new_tab_url()`, closes in `finally` (but was missing before fix)  
**Root Cause:** Every reindex blog = 1 tab create + 1 close. With 8 workers × hundreds of reindex blogs = thousands of tab cycles.

### Category 3: Recovery Tab Leak
**Error:** 1 tab leaked per `TabDeadError` recovery  
**Location:** `worker.py:103-118` — `_replace_tab()` opens new tab without guaranteed close of old  
**Root Cause:** Recovery path unconditionally creates new tab.

### Category 4: No CDP Timeout
**Error:** Workers stall for minutes inside `client.send_raw()`  
**Location:** `cdp_use/client.py:389` — `await future` with no timeout  
**Root Cause:** `cdp_use` library has no timeout on CDP commands.

### Category 5: Coordinator Early Drain Complete
**Error:** `drain_complete` declared with 2,493 pending items  
**Location:** `queue_integration.py:358-367` — `active_count` misses `in_progress` window  
**Root Cause:** File read race between `dequeue` and `mark_done`.

---

## 3. PROPOSED CHANGES

### 3.1 New File: `cdp_wrapper.py`
**Purpose:** Timeout-wrapped CDP client — every CDP call bounded by 15s.

```python
# cdp_wrapper.py
from __future__ import annotations
import asyncio
import logging
from cdp_use import CDPClient

logger = logging.getLogger("cdp-wrapper")

class TabDeadError(Exception):
    """Raised when CDP connection dies or times out."""

async def cdp_send(
    client: CDPClient,
    method: str,
    params: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    """Send CDP command with timeout. Raises TabDeadError on timeout/connection loss."""
    try:
        return await asyncio.wait_for(
            client.send_raw(method, params or {}),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        raise TabDeadError(f"CDP command {method} timed out after {timeout}s")
    except (ConnectionError, OSError) as exc:
        raise TabDeadError(f"CDP connection lost: {exc}") from exc
```

### 3.2 Rewrite: `agent.py`
**Key Changes:**
- Remove `probe_blog()` entirely (lines 375-453) — probe uses worker's tab
- Rewrite `crawl_blog()` to accept `navigate_fn` and `fetch_page_fn` callbacks (worker owns navigation)
- Add `cdp_send()` wrapper to ALL CDP calls in `fetch_page_html()`
- Keep pure detection helpers (`detect_login_wall`, `detect_dead`, `detect_end_of_posts`, `compute_page_metrics`)
- Keep `close_tab()` with HTTP fallback (already correct)
- Keep `_new_tab_url()` for worker startup/recovery only

**New `crawl_blog` signature:**
```python
async def crawl_blog(
    browser_ws: str,
    navigate_fn: Callable[[str, int], Awaitable[tuple[str, str]]],  # (username, offset) -> (html, final_url)
    fetch_page_fn: Callable[[int], Awaitable[tuple[str, str]]],    # offset -> (html, final_url)
    username: str,
    tier: int,
    *,
    unique_limit: int = 100,
    total_limit: int = 250,
    post_limit: int = 250,
    delay_min: float = DELAY_MIN,
    delay_max: float = DELAY_MAX,
    source_blog: str | None = None,
    cache_dir: Path | None = None,
    on_page: callable | None = None,
    on_progress: callable | None = None,
) -> dict[str, Any]:
```

### 3.3 Rewrite: `worker.py`
**Key Changes:**
- Replace `_refresh_ws()` with `_refresh_ws_url()` — raises `TabDeadError` if target_id missing
- Add `navigate_to(username, offset)` — refreshes WS URL, connects, navigates, waits for content, returns html/url
- Add `fetch_page(offset)` — uses same tab for pagination
- Add `probe_page_zero(username)` — calls `navigate_to(username, 0)` then extracts
- Replace `_crawl_with_recovery()` — max 1 retry after recovery, not 3
- Add `_recover_tab()` — only called when `TabDeadError` caught, max 2 recoveries per blog
- `busy_event.set()` at dequeue, `clear()` in `finally` block (ALL exit paths)
- `run()` opens tab in `try`, closes in `finally`

**New Worker State Machine:**
```
STARTING → _open_tab() → IDLE
    ↓ dequeue()
NAVIGATING → navigate_to() → PROBING (reindex only) → CRAWLING → FINALIZE → IDLE
    ↓ TabDeadError
RECOVERING → _recover_tab() → NAVIGATING (retry once)
    ↓ LoginWallDetected
LOGIN_WALL → wall_halt.set() → SHUTDOWN
```

### 3.4 Minor Updates: `queue_integration.py`
- Add logging for `busy_event` state transitions (observability)
- No logic changes — coordinator already uses dual condition correctly

### 3.5 Config Updates: `config.py`
- Add `CONTENT_WAIT_TIMEOUT = 30.0` (was hardcoded)
- Add `CDP_COMMAND_TIMEOUT = 15.0` (was hardcoded)
- Add `MAX_RECOVERY_PER_BLOG = 1` (was hardcoded 3)

---

## 4. CODE-LEVEL FIX MAPPING

| Category | Old Code (File:Line) | New Code (File:Function) | Fix Mechanism |
|----------|---------------------|-------------------------|---------------|
| **1. CDP death on navigate** | `agent.py:519` `client = CDPClient(ws_url)` | `worker.py:navigate_to()` creates fresh client per navigation after `_refresh_ws_url()` | Dynamic WS URL lookup before EVERY navigation |
| **2. Probe tab leak** | `agent.py:391` `_new_tab_url()` for probe | **DELETED** `probe_blog()`; worker calls `navigate_to(username, 0)` | Zero probe tabs — uses worker's persistent tab |
| **3. Recovery tab leak** | `worker.py:103` `_replace_tab()` | `worker.py:_recover_tab()` — only on proven death, max 1 retry/blog | Bounded recovery, not per-error |
| **4. No CDP timeout** | `cdp_use/client.py:389` `await future` | `cdp_wrapper.py:cdp_send()` with `asyncio.wait_for(15s)` | Every CDP call bounded |
| **5. OOM across runs** | `chrome_lifecycle.py:236` skip cleanup | `chrome_lifecycle.py:236` `await cleanup_tabs()` + worker `finally` | Clean startup + guaranteed close |
| **6. Early drain** | `queue_integration.py:358` `active_count` race | `worker.py:236` `busy_event.set()` at dequeue + `finally` clear | True busy state visible |

---

## 5. RESOURCE GUARANTEES

| Resource | Guarantee | Enforcement |
|----------|-----------|-------------|
| **Tabs** | Exactly `MAX_CONCURRENT_AGENTS` (8) | `cleanup_tabs()` at startup + worker `finally` closes tab + recovery bounded |
| **CDP Connections** | 0 or 1 per worker at any moment | Client created per navigation, stopped in `finally` |
| **Memory** | Bounded by page HTML size | Streamed, processed, discarded per page |
| **CPU** | Only during active navigation | `asyncio.sleep` between pages, polling 1s during wait |
| **Network** | Localhost (Chrome) + Tumblr only | No external deps in worker loop |

---

## 6. RETRY BOUNDS

| Operation | Max Retries | Conditions |
|-----------|-------------|------------|
| Tab open at startup | 3 | Exponential backoff (2s, 4s, 8s) |
| Tab recovery per blog | 1 | Only after `TabDeadError` caught |
| CDP command timeout | 0 | 15s timeout → `TabDeadError` immediately |
| Content wait | 0 | 30s deadline → `TabDeadError` |
| Login wall | 0 | Immediate pipeline halt |

---

## 7. FILES TO CREATE/MODIFY

### New Files
1. **`cdp_wrapper.py`** — Timeout-wrapped CDP client (~50 lines)

### Modified Files
2. **`agent.py`** — Rewrite `crawl_blog()` to use callbacks, remove `probe_blog()`, add `cdp_send()` to all CDP calls (~400 lines changed)
3. **`worker.py`** — Complete rewrite with state machine, `navigate_to()`, `fetch_page()`, `probe_page_zero()`, `_recover_tab()` (~300 lines changed)
4. **`config.py`** — Add timeout constants (~5 lines)

### Unchanged (Already Correct)
- `chrome_lifecycle.py` — `cleanup_tabs()` at startup, launch flags
- `queue_integration.py` — Coordinator dual-condition drain logic
- `work_queue.py` — `flock` serialization
- `extractor.py` — Pure extraction, no CDP

---

## 8. TEST PLAN FOR CODE REVIEW CRITIC

### Unit Tests (Mock CDP)
```python
# test_worker_tab_lifecycle.py
async def test_worker_opens_one_tab_only():
    """Verify worker opens exactly 1 tab at startup, closes at exit."""
    worker = Worker(...)
    await worker.run(queue_path)
    assert open_tab_call_count == 1
    assert close_tab_call_count == 1

async def test_navigate_refreshes_ws_url():
    """Verify navigate_to() calls _refresh_ws_url() before navigation."""
    worker = Worker(...)
    await worker.navigate_to("testblog", 0)
    assert refresh_ws_url_called == 1

async def test_probe_uses_worker_tab():
    """Verify probe_page_zero() uses existing tab, doesn't create new one."""
    worker = Worker(...)
    await worker.navigate_to("testblog", 0)
    result = await worker.probe_page_zero("testblog")
    assert new_tab_created == 0

async def test_recovery_only_on_proven_death():
    """Verify _recover_tab() only called when target_id missing from /json/list."""
    worker = Worker(...)
    worker.target_id = "dead-tab"
    with patch("/json/list returns no matching target"):
        await worker._recover_tab()
    assert recover_tab_called == 1

async def test_cdp_timeout():
    """Verify cdp_send() raises TabDeadError after 15s."""
    with patch("client.send_raw hangs forever"):
        with pytest.raises(TabDeadError):
            await cdp_send(client, "Page.navigate", {...})

async def test_busy_event_lifecycle():
    """Verify busy_event set at dequeue, cleared at ALL exits."""
    worker = Worker(...)
    item = dequeue(queue_path)
    assert worker.busy_event.is_set()
    # success path
    await worker._crawl_with_recovery(...)
    assert not worker.busy_event.is_set()
    # error path
    with pytest.raises(Exception):
        await worker._crawl_with_recovery(...)
    assert not worker.busy_event.is_set()
```

### Integration Test (Real Chrome)
```bash
# Run 2 workers × 3 blogs, verify tab count = 2 throughout
cd /Users/eric/Documents/tumblr-scanner
python3 -m pytest test_integration_tab_count.py -v
```

### Soak Test
```bash
# Run full crawl for 30 minutes, monitor:
# - Tab count stays at 8
# - RSS stays < 8 GB
# - No TabDeadError spam
# - No stall watchdog triggers
```

---

## 9. RISK ASSESSMENT

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| `Page.navigate` still creates new targetId in Chrome 130+ | Low | High | `_refresh_ws_url()` detects and recovers |
| `asyncio.wait_for(15s)` too aggressive for slow pages | Medium | Medium | Monitor logs, tune `CDP_COMMAND_TIMEOUT` |
| Worker `finally` block not reached on `SIGKILL` | Low | Medium | `cleanup_tabs()` at next startup catches orphaned tabs |
| Queue race between workers enqueueing discoveries | Low | Low | `flock` on `work_queue.py` already serializes |
| Login wall detection misses new Tumblr patterns | Medium | Medium | URL-based detection is primary; phrases are secondary |

---

## 10. ROLLBACK PLAN

If critical failure:
1. `git checkout main -- agent.py worker.py config.py`
2. Delete `cdp_wrapper.py`
3. Restart crawl — old code runs (with known leaks, but functional)

---

## 11. ACCESS FOR CODE REVIEW CRITIC AGENT

**Repository:** `/Users/eric/Documents/tumblr-scanner`  
**Branch:** `worker-tab-lifecycle-rewrite` (create before review)  
**Files to Review:**
- `cdp_wrapper.py` (new)
- `agent.py` (rewritten)
- `worker.py` (rewritten)
- `config.py` (updated)

**Run Commands:**
```bash
cd /Users/eric/Documents/tumblr-scanner
git checkout -b worker-tab-lifecycle-rewrite
# Apply changes (see patches below)
python3 -m py_compile agent.py worker.py cdp_wrapper.py config.py
python3 -m pytest test_worker_tab_lifecycle.py -v
```

---

## 12. PATCHES (Exact Diffs)

### Patch 1: `cdp_wrapper.py` (NEW FILE)
```diff
--- /dev/null
+++ b/cdp_wrapper.py
@@ -0,0 +1,50 @@
+from __future__ import annotations
+import asyncio
+import logging
+from cdp_use import CDPClient
+
+logger = logging.getLogger("cdp-wrapper")
+
+
+class TabDeadError(Exception):
+    """Raised when CDP connection dies or times out."""
+
+
+async def cdp_send(
+    client: CDPClient,
+    method: str,
+    params: dict | None = None,
+    timeout: float = 15.0,
+) -> dict:
+    """Send CDP command with timeout. Raises TabDeadError on timeout/connection loss."""
+    try:
+        return await asyncio.wait_for(
+            client.send_raw(method, params or {}),
+            timeout=timeout
+        )
+    except asyncio.TimeoutError:
+        raise TabDeadError(f"CDP command {method} timed out after {timeout}s")
+    except (ConnectionError, OSError) as exc:
+        raise TabDeadError(f"CDP connection lost: {exc}") from exc
```

### Patch 2: `config.py` (ADDITIONS)
```diff
--- a/config.py
+++ b/config.py
@@ -40,6 +40,11 @@
 WORKER_STALL_TIMEOUT = 180.0
 
 # --------------------------------------------------------------------------- # noqa
+# Timeouts
+# ---------------------------------------------------------------------------
+
+CDP_COMMAND_TIMEOUT = 15.0
+CONTENT_WAIT_TIMEOUT = 30.0
+MAX_RECOVERY_PER_BLOG = 1
+
+# --------------------------------------------------------------------------- # noqa
 # Windows
 # ---------------------------------------------------------------------------
```

### Patch 3: `agent.py` (KEY CHANGES ONLY — full rewrite attached separately)
```diff
--- a/agent.py
+++ b/agent.py
@@ -1,50 +1,55 @@
+from cdp_wrapper import cdp_send, TabDeadError
+
 # ... imports ...
 
-# probe_blog() REMOVED ENTIRELY (lines 375-453)
-
-async def crawl_blog(
-    browser_ws: str,
-    ws_url: str,                    # REMOVED: worker owns navigation
-    username: str,
+async def crawl_blog(
+    browser_ws: str,
+    navigate_fn: Callable[[str, int], Awaitable[tuple[str, str]]],  # NEW
+    fetch_page_fn: Callable[[int], Awaitable[tuple[str, str]]],    # NEW
+    username: str,
     tier: int,
     *,
     unique_limit: int = 100,
@@ -180,7 +185,7 @@ async def fetch_page_html(
-    try:
-        await client.send.Page.navigate(params={"url": url, "timeout": timeout_ms})
+    try:
+        await cdp_send(client, "Page.navigate", {"url": url, "timeout": timeout_ms})
     except Exception as exc:
         logger.warning("Page.navigate failed for %s offset %d: %s", username, offset, exc)
         raise TabDeadError(f"Page.navigate failed: {exc}") from exc
@@ -200,13 +205,13 @@ async def fetch_page_html(
         res = await client.send.Runtime.evaluate(
+        res = await cdp_send(client, "Runtime.evaluate", {
             params={
                 "expression": "JSON.stringify({url: location.href, text: (document.body ? document.body.innerText : '').slice(0, 800)})",
                 "returnByValue": True,
             }
         })
@@ -227,7 +232,7 @@ async def fetch_page_html(
             await client.send.Runtime.evaluate(
+            await cdp_send(client, "Runtime.evaluate", {
                 params={
                     "expression": (
                         "(function() {"
@@ -242,7 +247,7 @@ async def fetch_page_html(
             res = await client.send.Runtime.evaluate(
+            res = await cdp_send(client, "Runtime.evaluate", {
                 params={
                     "expression": "document.querySelectorAll('[data-cell-id]').length",
                     "returnByValue": True,
@@ -265,7 +270,7 @@ async def fetch_page_html(
             result = await client.send.Runtime.evaluate(
+            result = await cdp_send(client, "Runtime.evaluate", {
                 params={
                     "expression": "document.body ? document.body.innerText : ''",
                     "returnByValue": True,
@@ -288,7 +293,7 @@ async def fetch_page_html(
         result = await client.send.Runtime.evaluate(
+        result = await cdp_send(client, "Runtime.evaluate", {
             params={
                 "expression": "JSON.stringify({html: document.documentElement.outerHTML, url: location.href})",
                 "returnByValue": True,
```

### Patch 4: `worker.py` (KEY CHANGES ONLY — full rewrite attached separately)
```diff
--- a/worker.py
+++ b/worker.py
@@ -21,7 +21,7 @@
 from agent import (
     LoginWallDetected,
     TabDeadError,
-    crawl_blog,
-    probe_blog,          # REMOVED
+    crawl_blog,
 )
 from cdp_wrapper import cdp_send  # NEW
@@ -79,6 +79,35 @@ class Worker:
     async def _open_tab(self) -> None:
         # ... unchanged ...
 
+    async def _refresh_ws_url(self) -> None:
+        """Query /json/list for our target_id's current WS URL. Raises TabDeadError if gone."""
+        import json, urllib.request
+        base = self.browser_ws.replace("ws://", "http://").rstrip("/")
+        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
+            targets = json.loads(resp.read())
+        for t in targets:
+            if t.get("type") == "page" and t.get("id") == self.target_id:
+                ws = t.get("webSocketDebuggerUrl")
+                if ws:
+                    self.ws_url = ws
+                    return
+        raise TabDeadError(f"Target {self.target_id} no longer exists in Chrome")
+
+    async def navigate_to(self, username: str, offset: int = 0) -> tuple[str, str]:
+        """Navigate OUR persistent tab to blog URL. Returns (html, final_url)."""
+        await self._refresh_ws_url()
+        client = CDPClient(self.ws_url)
+        await client.start()
+        try:
+            url = f"https://www.tumblr.com/{username}"
+            if offset:
+                url += f"?offset={offset}"
+            await cdp_send(client, "Page.navigate", {"url": url, "loadResponse": True})
+            html, final_url = await self._wait_for_content(client, username)
+            return html, final_url
+        finally:
+            await client.stop()
+
+    async def _wait_for_content(self, client: CDPClient, username: str) -> tuple[str, str]:
+        # ... content wait logic from fetch_page_html ...
+
+    async def fetch_page(self, offset: int) -> tuple[str, str]:
+        """Fetch a page at offset using OUR tab. Called by crawl_blog for pagination."""
+        # Uses navigate_to internally
+
+    async def probe_page_zero(self, username: str) -> dict:
+        """Probe page 0 using OUR already-navigated tab."""
+        html, _ = await self.navigate_to(username, 0)
+        return compute_page_metrics(html, username)  # + date comparison logic
+
     async def _replace_tab(self) -> None:  # REPLACED by _recover_tab()
         # ... old code removed ...
 
     async def _recover_tab(self) -> bool:
         """Recover ONLY when tab is proven dead. Returns True if recovered."""
         # ... new implementation with bounded retries ...
 
     async def _crawl_with_recovery(self, ...) -> dict:
-        MAX_TAB_RETRIES = 3
+        MAX_TAB_RETRIES = 2  # 1 retry after recovery
         for attempt in range(1, MAX_TAB_RETRIES + 1):
             try:
                 return await crawl_blog(
-                    browser_ws=self.browser_ws,
-                    ws_url=self.ws_url,           # REMOVED
+                    browser_ws=self.browser_ws,
+                    navigate_fn=self.navigate_to,   # NEW
+                    fetch_page_fn=self.fetch_page,  # NEW
                     username=username,
                     tier=tier,
                     ...
                 )
             except TabDeadError as exc:
                 if attempt == 1 and await self._recover_tab():
                     continue
                 return error_result(...)
```

---

## 13. VERIFICATION CHECKLIST FOR CRITIC

- [ ] **Tab invariant:** `len(tabs) == MAX_CONCURRENT_AGENTS` at all times
- [ ] **No probe tabs:** `grep -r "probe_blog" .` returns zero matches
- [ ] **Dynamic WS URL:** Every navigation calls `_refresh_ws_url()` first
- [ ] **CDP timeouts:** Every `client.send.*` wrapped in `cdp_send(timeout=15)`
- [ ] **Recovery bounded:** Max 1 retry per blog, only on `TabDeadError`
- [ ] **Busy event lifecycle:** Set at dequeue, cleared in `finally` block
- [ ] **Cleanup at startup:** `cleanup_tabs()` called in `restart_chrome()` reuse-mode
- [ ] **Cleanup at exit:** Worker `finally` block calls `_close_tab()`
- [ ] **Login wall halts:** `LoginWallDetected` bubbles to coordinator, sets `wall_halt`
- [ ] **Config constants:** All timeouts/retry limits in `config.py`, no magic numbers
- [ ] **Tests pass:** Unit + integration tests green

---

## 14. CALL TO ACTION FOR CRITIC AGENT

**Please review:**
1. **Correctness:** Does the design actually solve each root cause at the code level?
2. **Completeness:** Are there any error paths not handled (exceptions in `finally`, `SIGKILL`, etc.)?
3. **Performance:** Any unnecessary overhead (extra HTTP calls to `/json/list`, CDP connection per navigation)?
4. **Race conditions:** Queue operations, `busy_event` vs coordinator, recovery vs stall watchdog
5. **Observability:** Enough logging/events to debug production issues?

**Deliverable:** Written critique with PASS/FAIL per verification item, plus any additional risks found.

---

**End of Design Review Document**
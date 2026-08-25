# Tumblr Scanner — Design Document

**Project:** Tumblr multi-tier username crawler
**Repo:** `github.com/e-hollebone/tumblr-scanner`
**Target blog:** `the-smallest-kitten-cravings`
**CDP endpoint:** `http://localhost:9222` (user's authenticated Chrome)
**Date:** 2026-08-25
**Status:** Active development — pipeline ready for re-launch

---

## 1. Overview

A three-tier (T0/T1/T2) username discovery pipeline for Tumblr blogs. Starting from a single root blog, the crawler discovers usernames posted by/reblogged by that blog (T0), then crawls each discovered username's own blog (T1), then crawls each username discovered in T1 (T2). Results are cached per-blog as JSON files under `cache/`.

The pipeline uses Chrome DevTools Protocol (CDP) via the `cdp_use` library, operating on the user's existing authenticated Chrome session at port 9222.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Coordinator (coordinator.py)            │
│  T0 crawl → get_t1_list_from_t0 → T1 dispatch              │
│                      → build_t2_list_from_t1 → T2 dispatch │
│  Semaphore-bounded: MAX_CONCURRENT_AGENTS = 3              │
└──────────┬──────────────────────┬───────────────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────┐    ┌──────────────────┐
│  Agent (agent.py)│    │  Agent (agent.py)│  ... up to 3 concurrent
│  One tab/bog     │    │  One tab/bog     │
└────────┬─────────┘    └────────┬─────────┘
         │                        │
         ▼                        ▼
┌─────────────────────────────────────────────────────────────┐
│  CDP (cdp_use.CDPClient) → Chrome at localhost:9222         │
│  • Target.createTarget → new tab per blog                  │
│  • Page.navigate → https://www.tumblr.com/<username>       │
│  • Runtime.evaluate → page HTML + innerText                │
│  • Network.setBlockedURLs → NO IMAGES                      │
│  • Network.enable → required for setBlockedURLs            │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│  Cache (cache.py)                                          │
│  • Per-blog JSON: cache/<tier>/<username>.json            │
│  • Central log: cache/log.json                             │
│  • Recrawl window: 7 days (entry_is_stale)                │
│  • Atomic writes, append-only log                         │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│  Extractor (extractor.py)                                  │
│  • BeautifulSoup + semantic selectors                      │
│  • Raw username extraction, no modification                │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Tier Structure

| Tier | What it crawls | Source of usernames | Limits |
|------|---------------|---------------------|--------|
| T0 | The root blog (`the-smallest-kitten-cravings`) | N/A — this is the entry point | `unique=250, total=500, posts=500` |
| T1 | Every username discovered in T0 | `get_t1_list_from_t0()` from T0 result | `unique=100, total=250, posts=250` |
| T2 | Every username discovered in T1 | `build_t2_list_from_t1()` from T1 results | `unique=75, total=125, posts=125` |

**Note:** These are *per-blog* limits. Each agent stops independently when its assigned blog hits any limit, is detected as dead, or reaches end-of-feed.

### 2.2 Concurrency Model

- `MAX_CONCURRENT_AGENTS = 3` — at most 3 CDP tabs active simultaneously
- Each agent gets its own Chrome tab via `Target.createTarget`
- Coordinator uses `asyncio.Semaphore(3)` to bound concurrency
- Tab lifecycle: coordinator creates tab → agent uses it → coordinator closes it via `close_tab()`

---

## 3. User-Enforced Constraints (Historical)

These constraints were set by the user across multiple sessions. Each is documented with the session evidence and current code status.

### 3.1 No Image Loading ("no pictures" / "no image loading")

**Evidence:**
- Aug 17 19:51 — *"i want parallization, and no image loading"* (`session: default/20260817_195143_00c763`, msg 30549)
- Aug 25 01:30 — *"and i said no pictures, why are they back?"* (`session: default/20260823_213838_1666ca`, msg 47085)

**Meaning:** Browser tabs must not load images or media resources. Tumblr pages should render text-only. This saves bandwidth, speeds up page loads, and respects the user's intent that the crawler is a text extractor, not a media consumer.

**Implementation:** `agent.py:368-384` — After the CDP client connects to a new tab but BEFORE navigating to the Tumblr URL, the agent sends:
```
Network.enable()
Network.setBlockedURLs(urls=[
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
    "*.svg", "*.ico", "*.avif", "*.bmp",
])
```

This blocks all common image formats at the Chrome network layer. The `Network.enable` call is required before `setBlockedURLs` will work. Both calls are wrapped in a try/except (blind except, `# noqa: BLE001`) because image blocking is best-effort — if it fails, the agent still functions, just without the constraint.

**Status: ✅ IMPLEMENTED**

---

### 3.2 Browse Like a Human (Human-Realistic Pacing)

**Evidence:**
- Aug 25 01:47 — *"why are we loosing the basic contraints at this stage, i have said too often browse like a human, you keep ignore that requirement"* (`session: default/20260823_213838_1666ca`, msg 47071)

This is a recurring constraint stated across multiple sessions. The user has said "too often" — meaning this has been set before and ignored repeatedly.

**Meaning:** The crawler must not operate at inhuman speed. Real people don't load page after page instantly. The inter-page delay must be long enough that:
1. Tumblr's server doesn't flag the traffic as automated
2. The page has time to fully render before content is extracted
3. The pacing is indistinguishable from a human browsing

**Implementation:**
- `agent.py:310-311` — `delay_min: float = 5.0, delay_max: float = 8.0`
- Inter-page delay: `random.uniform(5.0, 8.0)` seconds between page fetches
- Content-wait loop in `fetch_page_html()` (`agent.py:209-237`): polls every 1 second for up to 20 seconds until `len(new_text) > 100`, ensuring the page has actually rendered before extraction begins
- Page navigation retry: if `Page.navigate` fails, retry once after a 2-second sleep (`agent.py:197-207`)

A human browsing Tumblr would spend at minimum 5 seconds between pages (reading, scrolling). The 5-8 second range with random jitter is realistic. The 20-second content-wait timeout with 100-character threshold means the agent won't extract from a half-rendered page.

**Status: ✅ IMPLEMENTED**

---

### 3.3 Lint Before Checkin

**Evidence:**
- Aug 23 ~23:01 — *"as long as you lint the code before checkin"* (`session: default/20260823_213838_1666ca`, msg 45419)

**Meaning:** No code is committed to git without first passing `py_compile` + `ruff check`. This is a hard gate.

**Implementation:**
- `lint_modules.py` — runs `py_compile` on all committed modules
- `lint_batch.py` — runs `py_compile` + `ruff check` on all committed modules
- `ruff_fix.py` — runs `ruff check --fix` for auto-fixable issues

**Status: ✅ INFRASTRUCTURE EXISTS.** No checkin has occurred yet in this session; the gate is ready.

---

### 3.4 No Inline Python

**Evidence:**
- Aug 23 ~23:01 — *"stop using inline python"* (no `python -c "..."` one-liners; must write a script file first)

**Meaning:** All automation must be in script files. No `python -c "..."` shell one-liners.

**Implementation:** This session uses `patch`, `read_file`, `terminal` (grep/git), and `session_search` — no inline Python.

**Status: ✅ MET**

---

### 3.5 Deactivated Blogs: Keep in Index, Skip Crawl

**Evidence:**
- Aug 25 — *"if it has deactivate in the username, there is no need to page through it"* + *"the deactivated name must still be in the index so it can be queried"* + *"so yes proceed"*

**Meaning:** This is a two-part constraint:
1. **Don't waste resources crawling deactivated blogs** — if a username contains "deactivat", don't create a CDP tab or fetch pages for it
2. **But keep it queryable** — the username must remain in the cache/index so it can be looked up later

The earlier approach (filtering deactivated usernames out of T1/T2 lists entirely) was rejected by the user. The correct approach is a dispatch-time short-circuit.

**Implementation:**
- T1: `coordinator.py:207-229` — `bounded()` checks `if "deactivat" in username.lower()` and returns `{status="skipped", dead=True, dead_reason="deactivated", ...}` without creating a tab
- T2: `coordinator.py:342-364` — same short-circuit
- The username stays in the index because T0 already cached it; the short-circuit just skips the crawl

**Status: ✅ IMPLEMENTED**

---

### 3.6 Empty Blogs Stop at Page 1 (Pagination Leak Fix)

**Evidence:**
- *"there is a leak, this one tab did over 300 feteches on an empty blog => @url:https://www.tumblr.com/bulllil?offset=3820"*
- *"This Tumblr is cool, but empty."* (user provided the exact phrase)

**Root cause:** `END_PHRASES` in `agent.py` did NOT contain "this tumblr is cool, but empty." — the exact phrase an empty Tumblr blog displays. The guard at `agent.py:447-454` requires all three conditions: `not page_usernames AND posts_processed == 0 AND detect_end_of_posts()`. Since the third condition failed (phrase not in the list), the guard never fired, and the loop ran until `post_limit` (500 pages).

**Fix:** Added the missing phrase plus two more empty-blog signals to `END_PHRASES`:
- `"this tumblr is cool, but empty"` (the exact phrase from the leak)
- `"this tumblr is content-free"`
- `"meditate for a while on this empty tumblr"`

**Status: ✅ IMPLEMENTED** (`agent.py:48-56`)

---

### 3.7 Stable Selectors Only

**Evidence:**
- Durable Summary node 106 — *"Stable selectors only: data-cell-id, aria-label^='Posted by'/'Reblogged by', a[rel='author']. No CSS classes (f1x2m, BSUG4, rZlUD.F4Tcn are unstable)."*

**Meaning:** Tumblr's CSS class names change frequently. Selectors must use semantic HTML attributes that are stable across Tumblr redesigns.

**Implementation:** `extractor.py`:
- `cell.select("article [aria-label], [data-post] [aria-label]")` — aria-label on post elements
- Regex on `aria-label` for "Posted by"/"Reblogged by" pattern (line 81-85)
- `cell.select('a[href][rel="author"]')` — author links with rel="author"
- No CSS class selectors anywhere

**Status: ✅ IMPLEMENTED**

---

### 3.8 Raw Output Only

**Evidence:**
- Durable Summary node 106 — *"Raw output only — no modification, no summarization"*

**Meaning:** The extractor outputs usernames exactly as found. No filtering, no deduplication at extraction time, no summarization. Aggregation (counting, dedup) happens at the cache/analysis layer.

**Implementation:**
- `extractor.py:98-100` — per-cell output preserves raw usernames
- `extractor.py:103-115` — `Counter` aggregation is additive
- Agent commits raw entries to cache after every page

**Status: ✅ IMPLEMENTED**

---

### 3.9 CDP Only, No curl

**Evidence:**
- Earlier session — *"curl is lazy"* — CDP `send_raw` is the direct pipe

**Meaning:** All browser interaction goes through the CDP WebSocket. No `curl` or HTTP REST calls to `localhost:9222/json/*` for browser operations.

**Implementation:**
- All browser operations use `CDPClient` from `cdp_use` via WebSocket
- `urllib.request` is only used for service-discovery endpoints (`/json/version`, `/json/list`) — the same pattern as `get_cdp_url.py` in the project. These are read-only HTTP GETs to discover WebSocket URLs, not browser interaction.
- No `curl` anywhere in the codebase

**Status: ✅ MET**

---

### 3.10 Parallelization

**Evidence:**
- Aug 17 — *"this seems like it is able to be paralizable... you should build this in"* (msg 30545)
- Aug 17 — *"i want parallization, and no image loading"* (msg 30549)

**Meaning:** Multiple CDP tabs, each scanning a different blog simultaneously. The user explicitly directed parallelization.

**Implementation:**
- `coordinator.py` — T1/T2 dispatch loops create one tab per blog via `_new_tab_url()`
- `asyncio.Semaphore(MAX_CONCURRENT_AGENTS)` bounds concurrency to 3
- Each agent runs in its own CDP tab, independently
- Tab lifecycle managed by coordinator: create → agent uses → close_tab()

**Status: ✅ IMPLEMENTED**

---

### 3.11 Status Counting Correctness

**Evidence:**
- *"skipped entries have dead=True → counted in t1_dead / t2_dead via r.get('dead')"*
- *"Error counting: status == 'error' only"*

**Meaning:** The summary statistics must correctly separate:
- `dead` — includes both genuinely dead blogs AND skipped (deactivated) entries (both have `dead=True`)
- `error` — only actual failures (status == "error"), NOT skipped entries (status == "skipped")

**Implementation:**
- `coordinator.py:531` — `t1_dead = [r for r in t1_results if r.get("dead")]` — includes skipped entries ✓
- `coordinator.py:532` — `t1_error = [r for r in t1_results if r.get("status") == "error"]` — skipped entries have `status="skipped"`, excluded ✓

**Status: ✅ IMPLEMENTED**

---

## 4. Key Implementation Decisions

### 4.1 Why Network.setBlockedURLs Instead of Other Approaches

Chrome CDP offers several ways to block resources:
- `Network.setBlockedURLs` — blocks specific URL patterns at the network layer
- `Page.setLifecycle` — would pause script execution but not block images
- `Emulation.setMediaType` — simulates print/media queries, doesn't block images
- `Network.setCacheDisabled` — disables cache but doesn't block loading

`Network.setBlockedURLs` is the correct choice because:
1. It blocks at the network layer — images never leave Chrome
2. It's pattern-based (`*.png`) so it catches all subdomains and paths
3. It doesn't affect page rendering or JS execution
4. It's a one-time setup per tab (call once after connecting, before navigating)

The pattern list covers all common image formats: PNG, JPEG, GIF, WebP, SVG, ICO, AVIF, BMP.

### 4.2 Why Network.enable Before setBlockedURLs

CDP requires `Network.enable` to be called before most `Network.*` commands work. Without it, `setBlockedURLs` silently fails or throws. The agent calls both in sequence immediately after connecting to each tab.

### 4.3 Why Per-Tab Image Blocking (Not Global)

Each agent creates its own tab via `Target.createTarget`. Image blocking is applied to each tab individually after the CDP client connects. This is correct because:
- Each tab has its own network context
- The browser's main CDP WebSocket (used for `Target.createTarget`) is separate from each page's WebSocket
- Blocking must be set on the page's own CDP session

### 4.4 Why Blind Except on Image Blocking

The image blocking calls are wrapped in `except Exception:  # noqa: BLE001`. This is intentional:
- If `Network.enable` or `setBlockedURLs` fails, the agent still functions
- The constraint is best-effort — the agent isn't useless without it
- A failure here shouldn't kill the entire crawl
- The user's pattern for agent.py is blind-except with `# noqa` annotations

### 4.5 The "No Pictures" Constraint vs. HTML Extraction

The extractor (`extractor.py`) parses HTML for usernames. It does NOT process images. The "no pictures" constraint is about the browser not *loading* images, not about the extractor ignoring them. Even if images somehow loaded, the extractor wouldn't care — it only reads text. But blocking images at the network layer is still important for:
- Bandwidth (Tumblr pages can have dozens of images)
- Page load speed (images are the bulk of page weight)
- Respecting the user's explicit intent

### 4.6 Historical Evolution of This Constraint

| Session | Date | What happened |
|---------|------|---------------|
| `default/20260817_195143_00c763` | Aug 17 | User sets "no image loading" alongside parallelization request. A `block_images()` function was designed but the v8 implementation was never completed — the session moved to other work before the code was written. |
| `default/20260823_213838_1666ca` | Aug 23-25 | User reiterates "no pictures" after seeing images load during a pipeline run. The constraint was missing from the current `agent.py`. |

---

## 5. Pipeline Flow (End to End)

```
1. T0 CRAWL
   └─ Coordinator creates 1 tab for the-smallest-kitten-cravings
   └─ Agent paginates ?offset=0,20,40,... until limit or stop
   └─ Agent extracts usernames from each page
   └─ Agent commits cache entries after every page
   └─ Agent stops when: limit reached, dead detected, end-of-feed, or error
   └─ Result: T0 cache entry + log entries

2. T1 LIST BUILD
   └─ get_t1_list_from_t0(t0_result) → all usernames from T0
   └─ Note: deactivated usernames are NOT filtered here (they stay in index)

3. T1 DISPATCH
   └─ Coordinator creates up to 3 tabs (semaphore-bounded)
   └─ For each username:
       ├─ IF "deactivat" in username → skip (status="skipped", dead=True)
       ├─ ELSE → create tab, run agent, close tab
   └─ Each agent: paginate, extract, cache, stop on limit/dead/end
   └─ Summary: total, success, cached, dead (includes skipped), error

4. T2 LIST BUILD
   └─ build_t2_list_from_t1(t1_results) → all usernames from T1 results
   └─ Dedup against existing fresh T2 cache entries

5. T2 DISPATCH
   └─ Same pattern as T1: semaphore-bounded, deactivated short-circuit
   └─ Each agent crawls its assigned blog
   └─ Final summary: T0 + T1 + T2 results
```

---

## 6. Cache Schema

### 6.1 Per-Blog Entry (`cache/<tier>/<username>.json`)

```json
{
    "username": "blog-username",
    "tier": "t1",
    "source_blog": "parent-blog-that-led-here",
    "status": "running|success|cached|dead|error|skipped|limit_reached",
    "unique_count": 42,
    "total_count": 127,
    "posts_processed": 7,
    "usernames": ["user1", "user2", ...],
    "all_occurrences": ["user1", "user2", "user1", ...],
    "per_page": [
        {"page": 0, "offset": 0, "usernames_this_page": [...], "total_this_page": 5},
        ...
    ],
    "dead": false,
    "dead_reason": null,
    "scanned_at": "2026-08-25T01:30:00"
}
```

### 6.2 Log Entry (`cache/log.json`)

Append-only log, one JSON object per line. Each agent action (start, page, complete, error) appends a line.

```json
{"tier": "t1", "username": "example", "status": "running", "unique_count": 5, "total_count": 5, "posts_processed": 1, "dead": false, "dead_reason": null, "ts": "2026-08-25T01:30:00.123456+00:00"}
```

---

## 7. Current File Inventory

| File | Lines | Purpose |
|------|-------|---------|
| `agent.py` | ~576 | CDP agent — one tab per blog, pagination, extraction, caching |
| `coordinator.py` | ~787 | T0/T1/T2 orchestration, dispatch, summary |
| `extractor.py` | ~125 | HTML→usernames extraction (BeautifulSoup + semantic selectors) |
| `cache.py` | ~200 | JSON cache: save/load entries, log, staleness check |
| `run.py` | ~354 | Entry point — parses args, calls coordinator, prints results |
| `lint_modules.py` | ~30 | py_compile checker |
| `lint_batch.py` | ~50 | py_compile + ruff check |
| `ruff_fix.py` | ~30 | ruff auto-fix runner |
| `.gitignore` | ~20 | Standard Python gitignore |

---

## 8. Open Items / Build-Phase Verification

The following should be verified before the next full pipeline run:

1. **Image blocking verification** — confirm `Network.setBlockedURLs` actually prevents image requests in the live Chrome session. This requires a test navigate to a known image-heavy page and checking that image resources are blocked.

2. **Delay reasonableness** — 5-8 second inter-page delay. The user has said "browse like a human" multiple times but hasn't given a specific number. If 5-8s is too fast or too slow, adjust.

3. **Lint gate execution** — run `lint_batch.py` before any commit. Currently there are known ruff findings in `run.py` (unused imports, empty f-strings) and `coordinator.py` (2 BLE001 sites not yet annotated) that need addressing.

4. **Extraction verification** — dry-run the extractor against a saved full-page HTML to confirm non-post cells are excluded from output.

---

## 9. Changelog

### 2026-08-25 — Image blocking added
- `agent.py:368-384` — `Network.enable()` + `Network.setBlockedURLs()` after CDP connect, before any navigation
- Blocks: `*.png, *.jpg, *.jpeg, *.gif, *.webp, *.svg, *.ico, *.avif, *.bmp`
- Best-effort (blind except with warning log)

### 2026-08-25 — END_PHRASES leak fix
- Added `"this tumblr is cool, but empty"`, `"this tumblr is content-free"`, `"meditate for a while on this empty tumblr"` to `END_PHRASES`
- Fixes the `bulllil` pagination leak (300+ pages on an empty blog)

### 2026-08-25 — Deactivated blog short-circuits
- T1: `coordinator.py:207-229` — skip at dispatch, keep in index
- T2: `coordinator.py:342-364` — same pattern

### 2026-08-25 — delay_min parameter fix
- Added `delay_min: float = 5.0` to `agent.py:run()` signature (was only in docstring, causing NameError on all agents)

### 2026-08-23 — Lint infrastructure
- `lint_modules.py`, `lint_batch.py`, `ruff_fix.py` created
- `.gitignore` written

### 2026-08-23 — Separate repo
- `github.com/e-hollebone/tumblr-scanner` created, initial commit `448de36`

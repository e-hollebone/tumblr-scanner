#!/usr/bin/env python3
"""Test the worker-pool drain: 3 workers, each owning ONE persistent tab.

Verifies:
- 3 workers open exactly 3 tabs total (one per worker, not one per blog)
- All blogs get processed
- No per-blog tab churn
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

# --- Mock the heavy CDP / bs4 dependencies ---
CREATE_TARGET_CALLS = []
CLOSE_TARGET_CALLS = []

class FakeClient:
    def __init__(self, *a, **k):
        self.send = mock.MagicMock()
        self.send.Target.createTarget = self._create_target
        self.send.Target.closeTarget = self._close_target
        self.send.Page.navigate = mock.AsyncMock(return_value={})
        self.send.Runtime.evaluate = mock.AsyncMock(
            return_value={"result": {"value": "x" * 200}}
        )
    async def _create_target(self, params=None):
        url = params.get("url") if params else None
        CREATE_TARGET_CALLS.append(url)
        idx = len(CREATE_TARGET_CALLS)
        return {"targetId": f"tab-{idx}"}
    async def _close_target(self, params=None):
        tid = params.get("targetId") if params else None
        CLOSE_TARGET_CALLS.append(tid)
        return {}
    async def start(self):
        pass
    async def stop(self):
        pass

class _CDPModule:
    CDPClient = FakeClient

class _BS4Module:
    BeautifulSoup = object

sys.modules.setdefault("cdp_use", _CDPModule())
sys.modules.setdefault("bs4", _BS4Module())

import agent as real_agent

# Make _extract_browser_ws return a fake WS instead of hitting real localhost
real_agent._extract_browser_ws = lambda browser_ws: "ws://fake-browser"

# Serve /json/list and /json/version from a fake in-memory response
import urllib.request as _urllib

def _fake_urlopen(url, *a, **k):
    class _Resp:
        def __enter__(self_inner):
            return self_inner
        def __exit__(self_inner, *x):
            return False
        def read(self_inner):
            if "json/version" in str(url):
                return b'{"webSocketDebuggerUrl":"ws://fake-browser"}'
            # /json/list → include ALL created targets so concurrent
            # workers can each find their own tab by id
            targets = []
            for idx in range(len(CREATE_TARGET_CALLS)):
                tab_id = f"tab-{idx + 1}"
                targets.append({
                    "type": "page",
                    "id": tab_id,
                    "webSocketDebuggerUrl": f"ws://fake/{tab_id}",
                })
            return json.dumps(targets).encode()
    return _Resp()

_urllib.urlopen = _fake_urlopen

# Stub the page-analysis internals so agent.run runs to a clean "finished" in one page
real_agent.compute_page_metrics = lambda html, src=None: {
    "usernames": [],  # no discoveries — avoid self-enqueue inflation in test
    "page_date_max": None,
    "page_date_min": None,
    "posts_rendered": 20,
}
real_agent.detect_login_wall = lambda *a, **k: False
real_agent.detect_dead = lambda *a, **k: False
real_agent.detect_end_of_posts = lambda *a, **k: True  # finish after first page
real_agent.load_entry = lambda *a, **k: None
real_agent.save_entry = lambda *a, **k: None
real_agent.append_log = lambda *a, **k: None
real_agent.index_should_skip = lambda *a, **k: False

# --- Build a fake queue + index ---
import work_queue as wq

TMP = Path(tempfile.mkdtemp(prefix="tumblr_test_"))
QUEUE = TMP / "queue.jsonl"
INDEX = TMP / "index.json"

BLOGS = ["blog_a", "blog_b", "blog_c", "blog_d", "blog_e"]
for name in BLOGS:
    wq.enqueue(QUEUE, name, "", 1)

from queue_integration import _drain_queue

result = asyncio.run(_drain_queue(
    queue_path=QUEUE,
    index_path=INDEX,
    cache_dir=TMP,
    browser_ws="http://localhost:9222",
    recrawl_days=7,
))

print(f"Blogs in queue : {len(BLOGS)}")
print(f"Blogs processed : {result['processed']}")
print(f"createTarget calls : {len(CREATE_TARGET_CALLS)}")
for u in CREATE_TARGET_CALLS:
    print(f"   - {u}")
print(f"closeTarget calls : {len(CLOSE_TARGET_CALLS)}")
for t in CLOSE_TARGET_CALLS:
    print(f"   - {t}")

# Each worker opens exactly 1 tab → 3 tabs total
if len(CREATE_TARGET_CALLS) == 3 and result["processed"] == len(BLOGS):
    print("\nPASS: 3 workers, 3 tabs, all blogs processed (no per-blog churn)")
    sys.exit(0)
else:
    print(f"\nFAIL: expected 3 tabs + {len(BLOGS)} processed, got {len(CREATE_TARGET_CALLS)} tabs + {result['processed']} processed")
    sys.exit(1)

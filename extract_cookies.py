"""Extract Tumblr session cookies from a running Chrome instance.

One-time extraction: run this while logged into Tumblr in your browser.
Saves cookies to cache/tumblr-cookies.json for injection into the scanner's
dedicated Chrome profile.

Usage:
    python extract_cookies.py [CDP_PORT]
    
Default CDP port: 9222 (Chrome launched with --remote-debugging-port=9222)
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

from config import CACHE_DIR

COOKIE_CACHE_PATH = CACHE_DIR / "tumblr-cookies.json"

# Cookies that matter for authentication — skip analytics/tracking cruft
AUTH_COOKIE_NAMES = {
    "sid", "tmgioct", "pfu", "logged_in", "language", "cl_pref",
    "euconsent-v2-noniab", "euconsent-v2-analytics",
}

def _get_ws_url(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
            info = json.loads(resp.read())
            return info.get("webSocketDebuggerUrl")
    except Exception as exc:
        print(f"ERROR: Cannot reach Chrome on port {port}: {exc}")
        return None


def extract_cookies(port: int = 9222) -> list[dict]:
    """Extract auth-relevant cookies from a running Chrome instance."""
    import websockets
    
    ws_url = _get_ws_url(port)
    if not ws_url:
        sys.exit(1)
    
    cookies = []
    
    async def _extract():
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            resp = json.loads(await ws.recv())
            
            all_cookies = resp.get("result", {}).get("cookies", [])
            for c in all_cookies:
                name = c.get("name", "")
                domain = c.get("domain", "")
                if "tumblr.com" in domain and name in AUTH_COOKIE_NAMES:
                    cookies.append({
                        "name": name,
                        "value": c["value"],
                        "domain": domain,
                        "path": c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", False),
                        "expires": c.get("expires", -1),
                        "sameSite": c.get("sameSite"),
                    })
            return len(all_cookies)
    
    total = asyncio.get_event_loop().run_until_complete(_extract())
    
    if not cookies:
        print(f"WARNING: No auth cookies found (checked {total} total).")
        print("  Make sure you're logged into Tumblr in this Chrome profile.")
        sys.exit(1)
    
    COOKIE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_CACHE_PATH.write_text(json.dumps(cookies, indent=2))
    
    print(f"Extracted {len(cookies)} Tumblr auth cookies from {total} total")
    print(f"Saved to: {COOKIE_CACHE_PATH}")
    print()
    print("Cookies:")
    for c in cookies:
        print(f"  {c['name']:30s} domain={c['domain']}")
    print()
    print("Session valid until next Tumblr logout or cookie expiry (~12 months).")
    return cookies


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    extract_cookies(port)

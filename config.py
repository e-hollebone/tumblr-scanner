"""Single source of truth for all pipeline tunables.

Every module imports from here. No magic numbers scattered across source
files. To change timing, concurrency, or limits — edit this file.
"""
import os
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

# Worker pool size = number of concurrent Chrome tabs. Must stay below the
# Chrome crash threshold (observed ~30, practical max 8). The design locks the
# active worker pool at 3 persistent tabs (DESIGN.md:217, DESIGN_TAB_LIFECYCLE.md
# invariant "tab count = MAX_CONCURRENT_AGENTS"); raise only with explicit reason.
# NOTE: historical config value was 8 — changed to 3 to match the locked design
# (MoA eval Gap 2: code=8 vs design=3 conflict).
WORKER_POOL_SIZE = 3
# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------

# Delay range between page fetches. Empirically validated: 6.7-10.0s.
# Lower values trigger Tumblr rate-limiting.
# Per-page politeness delay (seconds). User directive 2026-08-28: cap the
# max wait at 9s for throughput — the crawl is effective, now it must be fast.
DELAY_MIN = 5.0
DELAY_MAX = 9.0

# How long a worker sleeps (seconds) when the queue is empty before checking
# again. Keeps the worker alive for newly enqueued items without busy-waiting.
QUEUE_POLL_INTERVAL = 2.0

# After this many seconds of an empty queue, the worker considers the drain
# complete and exits. Prevents workers hanging forever if the queue is exhausted
# but more items might be enqueued later by other workers.
QUEUE_EMPTY_TIMEOUT = 30.0

# Coordinator: a worker that holds busy_event but makes NO progress for this
# many seconds is considered HUNG (stuck in a CDP await with no timeout) and
# is force-restarted. This is the active monitor — the coordinator does not
# just trust busy_event, it verifies progress. (User directive 2026-08-28:
# "you need an active tab monitor because you're not following up.")
WORKER_STALL_TIMEOUT = 180.0

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

CDP_COMMAND_TIMEOUT = 15.0
CONTENT_WAIT_TIMEOUT = 30.0
MAX_RECOVERY_PER_BLOG = 1

# Login-wall confirmation (fix A): a detected wall is treated as a *soft*
# signal and retried before halting the whole pipeline. Tumblr's rate-limit /
# "are you human" interstitial routes through a /login URL, which the
# URL-only detector (agent.detect_login_wall) misreads as a hard login wall.
# A transient interstitial clears on retry; a genuine auth gate does not.
# After WALL_RETRY_MAX failed attempts the wall is accepted as real and the
# pipeline halts (so a true gate still tells the user to log in).
WALL_RETRY_MAX = 2
WALL_RETRY_BACKOFF_S = 15.0

# --------------------------------------------------------------------------- # noqa
# Windows
# ---------------------------------------------------------------------------

T0_LIMITS = {"unique": 250, "total": 500, "posts": 500}
T1_LIMITS = {"unique": 100, "total": 250, "posts": 250}
T2_LIMITS = {"unique": 75, "total": 125, "posts": 125}

LIMITS_BY_TIER = {0: T0_LIMITS, 1: T1_LIMITS, 2: T2_LIMITS}

# --------------------------------------------------------------------------- # noqa
# Windows
# ---------------------------------------------------------------------------

# NOTE: There is NO recrawl-window requirement. The user dropped the 7-day
# recrawl concept. Index membership alone governs dedup (new vs already-
# indexed). Date-aware refresh, where needed, is FR-7's page-0 date probe —
# not an age-based skip. Do not reintroduce RECRAWL_DAYS.

# Progress log interval (seconds).
PROGRESS_INTERVAL = 30

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Stop pushing new items to the queue if it exceeds this depth.
QUEUE_OVERFLOW_THRESHOLD = 10_000

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Cache root is overridable via TUMBLR_SCANNER_CACHE_DIR so a clone on another
# machine (or CI) doesn't write into the original author's home directory.
# Falls back to a project-relative ./cache if the env var is unset AND the
# legacy absolute macOS path does not exist.
def _resolve_cache_dir() -> Path:
    env = os.environ.get("TUMBLR_SCANNER_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    legacy = Path("/Users/eric/Documents/tumblr-scanner/cache")
    if legacy.exists() or legacy.parent.exists():
        return legacy
    return Path.cwd() / "cache"


CACHE_DIR = _resolve_cache_dir()
QUEUE_PATH = CACHE_DIR / "queue.jsonl"
INDEX_PATH = CACHE_DIR / "index.json"

# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

def _resolve_chrome_path() -> str:
    """Locate the Chrome/Chromium binary in a platform-portable way.

    Priority:
      1. CHROME_PATH env var — explicit override for any OS (headless servers,
         custom installs, etc.).
      2. shutil.which() on common executable names (Linux/Windows/Homebrew).
      3. macOS Application bundle (the original hardcoded default).
    Raises RuntimeError only if nothing is found, so the failure is explicit
    rather than a confusing "file not found" deep in the launch path.
    """
    env = os.environ.get("CHROME_PATH")
    if env:
        return env
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac_path).exists():
        return mac_path
    raise RuntimeError(
        "Chrome/Chromium not found. Set CHROME_PATH to the binary, "
        "or install Chrome/Chromium on PATH."
    )


CHROME_PATH = _resolve_chrome_path()
# Profile dir is overridable via TUMBLR_SCANNER_CHROME_PROFILE so an external
# clone doesn't write into the original author's home directory.
CHROME_USER_DATA_DIR = Path(
    os.environ.get(
        "TUMBLR_SCANNER_CHROME_PROFILE",
        "/Users/eric/Documents/tumblr-scanner/chrome_profile",
    )
).expanduser()
CDP_PORT = 9222
CHROME_RESTART_TIMEOUT = 10

# Default CDP browser HTTP endpoint (read from /json/version at runtime).
DEFAULT_CDP_BROWSER = f"http://localhost:{CDP_PORT}"

# ---------------------------------------------------------------------------
# Extraction / Detection phrases
# ---------------------------------------------------------------------------

DEAD_PHRASES = [
    "this blog has been deactivated",
    "blog has been deactivated",
    "there's nothing here",
    "this blog doesn't exist",
    "page not found",
]

# Usernames matching these (case-insensitive substring) are skipped BEFORE
# any page fetch — they are deactivated/dead blogs by name convention.
SKIP_USERNAME_PATTERNS = [
    "deactiv",
    "-gone",
    "deleted",
    "decommissioned",
]

LOGIN_WALL_PHRASES = [
    "log in to continue",
    "log in or sign up",
    "this page is for humans only",
    "please verify you're a human",
    "tumblr.com/login",
    "sign up for tumblr",
    "create your account",
    "log in to tumblr",
    "before we go ahead",
    "verify you're human",
    "content_warning_wall",
    "recaptcha",
    "recaptcha.net",
    "i am over eighteen",
    "i am 18 or older",
    "view this blog",
    "this blog contains",
    "mature content",
    "sensitive content",
    "are you over 18",
    "confirm your age",
    "you must be 18",
    "accept the terms",
    "view adult content",
    "continue to blog",
]

END_PHRASES = [
    "no more posts to show",
    "you're all caught up",
    "end of posts",
    "no posts to show",
    "this tumblr is cool, but empty",
    "this tumblr is content-free",
    "meditate for a while on this empty tumblr",
]

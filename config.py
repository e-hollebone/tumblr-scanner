"""Single source of truth for all pipeline tunables.

Every module imports from here. No magic numbers scattered across source
files. To change timing, concurrency, or limits — edit this file.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

# Worker pool size = number of concurrent Chrome tabs. Must stay below the
# Chrome crash threshold (observed ~30, practical max 4). 3 workers + headroom.
MAX_CONCURRENT_AGENTS = 3

# ---------------------------------------------------------------------------
# Timing (seconds)
# ---------------------------------------------------------------------------

# Delay range between page fetches. Empirically validated: 6.7-10.0s.
# Lower values trigger Tumblr rate-limiting.
DELAY_MIN = 6.7
DELAY_MAX = 10.0

# How long a worker sleeps (seconds) when the queue is empty before checking
# again. Keeps the worker alive for newly enqueued items without busy-waiting.
QUEUE_POLL_INTERVAL = 2.0

# After this many seconds of an empty queue, the worker considers the drain
# complete and exits. Prevents workers hanging forever if the queue is exhausted
# but more items might be enqueued later by other workers.
QUEUE_EMPTY_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# Limits per tier
# ---------------------------------------------------------------------------

T0_LIMITS = {"unique": 250, "total": 500, "posts": 500}
T1_LIMITS = {"unique": 100, "total": 250, "posts": 250}
T2_LIMITS = {"unique": 75, "total": 125, "posts": 125}

LIMITS_BY_TIER = {0: T0_LIMITS, 1: T1_LIMITS, 2: T2_LIMITS}

# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# Skip re-crawl if scanned within this many days.
RECRAWL_DAYS = 7

# Progress log interval (seconds).
PROGRESS_INTERVAL = 30

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Stop pushing new items to the queue if it exceeds this depth.
QUEUE_OVERFLOW_THRESHOLD = 2_500

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CACHE_DIR = Path("/Users/eric/Documents/tumblr-scanner/cache")
QUEUE_PATH = CACHE_DIR / "queue.jsonl"
INDEX_PATH = CACHE_DIR / "index.json"

# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = Path("/Users/eric/Documents/tumblr-scanner/chrome_profile")
CDP_PORT = 9222
CHROME_RESTART_TIMEOUT = 10

# Default CDP browser HTTP endpoint (read from /json/version at runtime).
DEFAULT_CDP_BROWSER = f"http://localhost:{CDP_PORT}"

# Default recrawl window (days) used when --recrawl-days is not given.
DEFAULT_RECRAWL_DAYS = 7

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

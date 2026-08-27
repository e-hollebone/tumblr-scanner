#!/usr/bin/env python3
"""Apply ruff auto-fixes to committed modules, then patch the rest manually."""

import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/eric/Documents/tumblr-scanner")
MODULES = [
    "agent.py",
    "cache.py",
    "coordinator.py",
    "extractor.py",
    "run.py",
    "lint_batch.py",
    "lint_modules.py",
]
RUFF = "/Users/eric/.hermes/hermes-agent/venv/bin/ruff"

print("=== ruff check --fix (auto-fixable only) ===")
result = subprocess.run(
    [
        RUFF,
        "check",
        "--fix",
        "--unsafe-fixes",
        "--output-format=concise",
        ".",
        "--force-exit-zero",
    ],
    cwd=str(REPO),
    capture_output=True,
    text=True,
)
# ruff returns 0 when all fixed, 1 when some remain
remaining = result.returncode != 0
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)

if not remaining:
    print("  All auto-fixable issues resolved.")
else:
    print(f"  Some issues remain after auto-fix (exit {result.returncode}).")

print("\n=== Remaining issues by file ===")
result = subprocess.run(
    [RUFF, "check", "--output-format=concise", ".", "--force-exit-zero"],
    cwd=str(REPO),
    capture_output=True,
    text=True,
)
if result.stdout:
    # Filter to committed modules only
    for line in result.stdout.splitlines():
        if any(line.startswith(f"{m}:") for m in MODULES):
            print(f"  {line}")
else:
    print("  Clean — no remaining issues.")

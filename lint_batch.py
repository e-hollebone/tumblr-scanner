#!/usr/bin/env python3
"""Lint tumblr-scanner modules: py_compile for all, ruff for diagnostics."""

import py_compile
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/eric/Documents/tumblr-scanner")
MODULES = [
    "agent.py",
    "cache.py",
    "extractor.py",
    "run.py",
    "lint_modules.py",
]
RUFF = "/Users/eric/.hermes/hermes-agent/venv/bin/ruff"

print("=== py_compile (all modules) ===")
for name in MODULES:
    path = REPO / name
    print(f"  {name}... ", end="", flush=True)
    try:
        py_compile.compile(str(path), doraise=True)
        print("OK")
    except py_compile.PyCompileError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

print("\n=== ruff check ===")
result = subprocess.run(
    [RUFF, "check", "--output-format=concise", "."],
    cwd=str(REPO),
    capture_output=True,
    text=True,
)
if result.returncode == 0:
    print("  clean — no ruff issues")
else:
    print(result.stdout or result.stderr, end="")

print("\n=== ruff format (check only) ===")
result = subprocess.run(
    [RUFF, "format", "--check", "."],
    cwd=str(REPO),
    capture_output=True,
    text=True,
)
if result.returncode == 0:
    print("  clean — no formatting issues")
else:
    print(result.stdout or result.stderr, end="")

print("\nDone.")

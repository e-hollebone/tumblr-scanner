#!/usr/bin/env python3
"""Lint all tumblr-scanner modules with py_compile. Fail on first error."""
import py_compile
import sys
from pathlib import Path

REPO = Path("/Users/eric/Documents/tumblr-scanner")
MODULES = ["agent.py", "cache.py", "coordinator.py", "extractor.py", "run.py"]

for name in MODULES:
    path = REPO / name
    print(f"=== {name} ===")
    try:
        py_compile.compile(str(path), doraise=True)
        print("OK")
    except py_compile.PyCompileError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

print("\nAll modules pass py_compile.")

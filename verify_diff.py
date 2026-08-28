#!/usr/bin/env python3
"""
verify_diff.py — deterministic drift checker for tumblr-scanner.

Runs before commit. Checks staged changes against:
  1. .verify_blocklist — symbols/concepts that were deliberately dropped
  2. config.py — all config.X references must match a real definition
  3. REQUIREMENTS_MATRIX.md — NFR/FR code locations must still exist
  4. New CLI flags in run.py must be documented in DESIGN.md

Exit 0 = clean. Exit 1 = violations (prints report).

This is the "smaller model critic" — deterministic, fast, no LLM.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
BLOCKLIST_FILE = ROOT / ".verify_blocklist"
CONFIG_FILE = ROOT / "config.py"
MATRIX_FILE = ROOT / "REQUIREMENTS_MATRIX.md"
DESIGN_FILE = ROOT / "DESIGN.md"
RUN_FILE = ROOT / "run.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def staged_files() -> list[Path]:
    """Return list of staged file paths."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return [ROOT / p for p in out.stdout.splitlines() if p.strip()]


def staged_diff() -> str:
    """Return the full staged diff text."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return out.stdout


def added_lines_only(diff: str) -> list[tuple[str, int, str]]:
    """
    Parse a unified diff and return (file, line_num, text) for added lines.
    Only captures lines starting with '+' (not '+++') and only counts
    actual new lines in the hunk (skips context).
    """
    results: list[tuple[str, int, str]] = []
    current_file: str = ""
    new_line: int = 0

    for raw in diff.splitlines():
        # File header
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        # Hunk header: @@ -old,count +new,count @@
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            if m:
                new_line = int(m.group(1))
            continue
        # Added line
        if raw.startswith("+") and not raw.startswith("+++"):
            results.append((current_file, new_line, raw[1:]))
        # Count line advances for both added and context lines
        if not raw.startswith("-"):
            new_line += 1

    return results


def read_blocklist() -> list[str]:
    """Read blocklist patterns (one per line, # comments skipped)."""
    if not BLOCKLIST_FILE.exists():
        return []
    patterns: list[str] = []
    for line in BLOCKLIST_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def config_keys() -> set[str]:
    """Extract all UPPER_CASE config keys defined in config.py."""
    keys: set[str] = set()
    if not CONFIG_FILE.exists():
        return keys
    text = CONFIG_FILE.read_text()
    # Match lines like "KEY_NAME = ..." at column 0
    for m in re.finditer(r"^([A-Z][A-Z0-9_]+)\s*=", text, re.MULTILINE):
        keys.add(m.group(1))
    return keys


def config_refs_in_code() -> list[tuple[str, int, str]]:
    """Find all `config.SOMETHING` references across the codebase."""
    refs: list[tuple[str, int, str]] = []
    for py in ROOT.rglob("*.py"):
        if py.name == "verify_diff.py":
            continue
        try:
            text = py.read_text()
        except OSError:
            continue
        # Only match `config.X` where config is the module we import.
        # Avoid json.JSONDecodeError, os.O_APPEND, logging.DEBUG, etc.
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"(?<![.\w])config\.([A-Z][A-Z0-9_]+)", line):
                refs.append((str(py.relative_to(ROOT)), i, m.group(1)))
    return refs


def matrix_code_refs() -> list[tuple[str, str, str]]:
    """
    Parse REQUIREMENTS_MATRIX.md for file:line references.
    Returns list of (id, file, fragment).
    """
    refs: list[tuple[str, str, str]] = []
    if not MATRIX_FILE.exists():
        return refs
    text = MATRIX_FILE.read_text()
    # Match patterns like `agent.py:270` or `config.py:23`
    for m in re.finditer(
        r"`([a-zA-Z0-9_.]+):(\d+)`", text
    ):
        file = m.group(1)
        line = m.group(2)
        # Find the requirement ID on the same line (look backward)
        line_start = text.rfind("\n", 0, m.start())
        line_text = text[line_start : text.find("\n", m.end())]
        id_match = re.search(r"\b(FR|NFR)-(\d+)\b", line_text)
        req_id = f"{id_match.group(1)}-{id_match.group(2)}" if id_match else "?"
        refs.append((req_id, file, line))
    return refs


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_blocklist(
    added: list[tuple[str, int, str]],
) -> list[str]:
    """Flag any added line containing a blocklisted pattern."""
    patterns = read_blocklist()
    if not patterns:
        return []
    violations: list[str] = []
    for file, line, text in added:
        # Skip the blocklist file itself — it defines the patterns.
        if file == ".verify_blocklist":
            continue
        for pat in patterns:
            if pat in text:
                violations.append(
                    f"  BLOCKLIST: '{pat}' in {file}:{line}\n"
                    f"    >> {text.strip()[:80]}"
                )
    return violations


def check_config_refs() -> list[str]:
    """Flag config.X references where X is not defined in config.py."""
    keys = config_keys()
    if not keys:
        return []
    violations: list[str] = []
    for file, line, name in config_refs_in_code():
        if name not in keys:
            violations.append(
                f"  CONFIG: {file}:{line} references config.{name}\n"
                f"    >> but config.py does not define {name}"
            )
    return violations


def check_matrix_refs() -> list[str]:
    """Flag NVR/FR code locations whose referenced FILE no longer exists.

    Line-number drift is editorial noise (matrix line refs shift as code
    edits) — only flag if the whole file is gone.
    """
    violations: list[str] = []
    seen_files: set[str] = set()
    for _req_id, file, _line in matrix_code_refs():
        if file in seen_files:
            continue
        seen_files.add(file)
        if not (ROOT / file).exists():
            violations.append(
                f"  MATRIX: references {file} which does not exist"
            )
    return violations


def check_new_cli_flags() -> list[str]:
    """Flag new CLI flags in run.py not documented anywhere.

    A flag is "documented" if it appears in DESIGN.md OR in run.py's own
    module docstring. Only genuinely undocumented flags are flagged.
    """
    if not RUN_FILE.exists() or not DESIGN_FILE.exists():
        return []
    run_text = RUN_FILE.read_text()
    design_text = DESIGN_FILE.read_text()
    # Module docstring is the first '''...''' block
    docstring = ""
    m = re.search(r'"""(.*?)"""', run_text, re.DOTALL)
    if m:
        docstring = m.group(1)
    violations: list[str] = []
    for m in re.finditer(r'"--([a-z][a-z0-9-]*)"', run_text):
        flag = m.group(1)
        if (
            f"--{flag}" not in design_text
            and flag not in design_text
            and f"--{flag}" not in docstring
            and flag not in docstring
        ):
            violations.append(
                f"  CLI: run.py has --{flag} but neither DESIGN.md nor its docstring documents it"
            )
    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 60)
    print("verify_diff.py — drift checker")
    print("=" * 60)

    # Gather data
    diff = staged_diff()
    added = added_lines_only(diff)

    print(f"\nStaged files: {len(staged_files())}")
    print(f"Added lines:  {len(added)}")
    print(f"Blocklist:    {BLOCKLIST_FILE.name} ({len(read_blocklist())} patterns)")

    # Run checks
    all_violations: list[str] = []
    checks = [
        ("Blocklist (dropped concepts)", check_blocklist(added)),
        ("Config key existence", check_config_refs()),
        ("Matrix code locations", check_matrix_refs()),
        ("CLI flag documentation", check_new_cli_flags()),
    ]

    for name, violations in checks:
        print(f"\n--- {name} ---")
        if violations:
            all_violations.extend(violations)
            for v in violations:
                print(v)
        else:
            print("  OK")

    # Summary
    print("\n" + "=" * 60)
    if all_violations:
        print(f"FAIL: {len(all_violations)} violation(s)")
        print("=" * 60)
        return 1
    print("PASS: no drift detected")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

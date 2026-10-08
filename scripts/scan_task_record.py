#!/usr/bin/env python3
"""Scan a local wp task record for credentials and machine-specific paths.

Exit codes:
    0  clean
    2  review warnings only
    1  hard credential findings

Usage:
    python3 scripts/scan_task_record.py wp-state/repos/<repo-id>/tasks/<task-id>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Hard credential patterns: never acceptable in a committed record.
HARD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "OpenAI-style secret key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "JWT"),
    (re.compile(r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE),
     "credential assignment"),
]

# Absolute machine-local paths: review warnings, not automatic failures.
PATH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/home/[A-Za-z0-9_.-]+"), "home path"),
    (re.compile(r"/lzcapp\b"), "lzcapp path"),
    (re.compile(r"/tmp\b"), "tmp path"),
    (re.compile(r"/usr/local\b"), "usr-local path"),
    (re.compile(r"/usr/bin\b"), "usr-bin path"),
]

# Files that legitimately contain deny-rule mentions of secrets/paths.
_DENY_PATTERN = re.compile(r"\bdeny\b", re.IGNORECASE)
# Soft credential keywords: in a deny-rule line they are a review warning.
_SOFT_KEYWORDS = re.compile(r"secret|token|api[_-]?key|password|\.env", re.IGNORECASE)


def scan_path(root: Path) -> tuple[list[str], list[str]]:
    """Return (hard_failures, review_warnings) with '<rel>:<line>: <label>'."""
    hard: list[str] = []
    warnings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"{path.relative_to(root)}: unreadable ({exc})")
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            rel = f"{path.relative_to(root)}:{idx}"
            is_deny_context = bool(_DENY_PATTERN.search(line))
            if is_deny_context and _SOFT_KEYWORDS.search(line):
                warnings.append(f"{rel}: deny rule context")
            for pattern, label in HARD_PATTERNS:
                if pattern.search(line):
                    if is_deny_context:
                        warnings.append(f"{rel}: {label} (deny rule context)")
                    else:
                        hard.append(f"{rel}: {label}")
            for pattern, label in PATH_PATTERNS:
                if pattern.search(line):
                    warnings.append(f"{rel}: {label}")
    return hard, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("record", type=Path, help="task record directory to scan")
    parser.add_argument(
        "--exit-on-review",
        action="store_true",
        help="treat review warnings as failures (strict mode)",
    )
    args = parser.parse_args(argv)

    if not args.record.is_dir():
        print(f"error: not a directory: {args.record}", file=sys.stderr)
        return 1

    hard, warnings = scan_path(args.record)
    for item in hard:
        print(f"FAIL  {item}")
    for item in warnings:
        print(f"WARN  {item}")

    if hard:
        print("task record blocked: credential patterns found", file=sys.stderr)
        return 1
    if warnings and args.exit_on_review:
        print("task record blocked: review warnings in strict mode", file=sys.stderr)
        return 1
    if warnings:
        print("task record has review warnings", file=sys.stderr)
        return 2
    print("task record clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

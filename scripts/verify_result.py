#!/usr/bin/env python3
"""Validate one skill-local delegated task's terminal artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from protocol import (
    handoff_display_path,
    read_json,
    task_relative_paths,
    validate_terminal_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--require-success",
        action="store_true",
        help="also require the terminal task status to be success",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    paths = task_relative_paths(args.task_id, repo)
    expected_handoff = handoff_display_path(repo, args.task_id)
    try:
        result = read_json(paths["result"])
        errors = validate_terminal_artifacts(
            result,
            paths["handoff"],
            args.task_id,
            expected_handoff,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = None
        errors = [str(exc)]

    status = result.get("status") if isinstance(result, dict) else None
    output = {
        "protocol_valid": not errors,
        "task_id": args.task_id,
        "state_path": expected_handoff.rsplit("/", 1)[0],
        "status": status,
        "errors": errors,
        "independent_acceptance_required": True,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    if errors:
        return 2
    if args.require_success and status != "success":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

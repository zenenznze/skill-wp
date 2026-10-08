#!/usr/bin/env python3
"""Validate one skill-local wp-graph's terminal protocol state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from graph_lib import graph_relative_paths, graph_status, validate_graph
from protocol import (
    TERMINAL_STATUSES,
    handoff_display_path,
    read_json,
    task_relative_paths,
    validate_terminal_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--graph-id", required=True, help="yyyymmdd-lowercase-slug")
    parser.add_argument(
        "--require-success",
        action="store_true",
        help="also require the graph status to be success",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    paths = graph_relative_paths(args.graph_id, repo)
    errors: list[str] = []
    graph = None
    try:
        graph = read_json(paths["graph_json"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))

    status = None
    node_statuses: dict[str, str] = {}
    if graph is not None:
        if not isinstance(graph, dict):
            errors.append("graph must be a JSON object")
        else:
            errors.extend(validate_graph(graph))
            node_statuses = {
                node["id"]: node.get("status", "pending")
                for node in graph.get("nodes", [])
                if isinstance(node, dict) and isinstance(node.get("id"), str)
            }
            for node in graph.get("nodes", []):
                if not isinstance(node, dict) or node.get("kind") != "task":
                    continue
                node_id = node.get("id")
                task_id = node.get("task_id")
                if not isinstance(node_id, str) or not isinstance(task_id, str):
                    continue
                try:
                    task_paths = task_relative_paths(task_id, repo)
                except ValueError as exc:
                    errors.append(f"{node_id}: {exc}")
                    continue
                result_path = task_paths["result"]
                if result_path.is_file():
                    try:
                        result = read_json(result_path)
                        task_errors = validate_terminal_artifacts(
                            result,
                            task_paths["handoff"],
                            task_id,
                            handoff_display_path(repo, task_id),
                        )
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        task_errors = [str(exc)]
                    errors.extend(f"{node_id}: {message}" for message in task_errors)
                elif node.get("status", "pending") == "success":
                    errors.append(
                        f"{node_id}: node claims success but has no result.json"
                    )
            try:
                status = graph_status(graph)
            except Exception as exc:  # noqa: BLE001 - graph may be invalid
                errors.append(f"cannot compute graph status: {exc}")
            stored = graph.get("status")
            if stored in TERMINAL_STATUSES and status is not None and stored != status:
                errors.append(
                    f"stored status {stored} does not match computed graph "
                    f"status {status}"
                )

    output = {
        "protocol_valid": not errors,
        "graph_id": args.graph_id,
        "graph_status": status,
        "node_statuses": node_statuses,
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

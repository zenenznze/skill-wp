#!/usr/bin/env python3
"""Initialize skill-local wp-graph state packages."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from graph_lib import graph_relative_paths, validate_graph
from protocol import (
    assert_isolated_execution_root,
    atomic_write_json,
    now_iso,
    read_json,
    repository_id,
    state_relative_path,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"


def repository_root(path: Path) -> Path:
    candidate = path.resolve()
    process = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ValueError(f"not a Git repository: {candidate}")
    root = Path(process.stdout.strip()).resolve()
    if root != candidate:
        raise ValueError(f"--repo must be the Git root: {root}")
    return root


def render(template_name: str, values: dict[str, str]) -> str:
    text = (ASSETS / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def initialize(
    repo: Path, graph_id: str, goal: str | None, graph_json: Path | None = None
) -> dict[str, str | None]:
    root = repository_root(repo)
    assert_isolated_execution_root(root)
    relative = graph_relative_paths(graph_id, root)
    graph_dir = relative["graph_dir"]
    if graph_dir.exists():
        raise FileExistsError(f"graph already exists: {graph_dir}")

    created_at = now_iso()
    handoff = None
    values: dict[str, str] = {}
    if graph_json is not None:
        graph = read_json(graph_json)
        if not isinstance(graph, dict):
            raise ValueError("graph.json is invalid: graph must be a JSON object")
        # Complete the bookkeeping fields before validation so an import can be
        # authored without machine-local metadata; the completed graph must
        # still satisfy every content rule.
        graph.setdefault("repo_id", repository_id(root))
        graph.setdefault("created_at", created_at)
        graph.setdefault("updated_at", created_at)
        errors = validate_graph(graph)
        if errors:
            raise ValueError("graph.json is invalid: " + "; ".join(errors))
        if graph.get("graph_id") != graph_id:
            raise ValueError(
                f"graph_id in file ({graph.get('graph_id')!r}) does not match "
                f"--graph-id {graph_id}"
            )
    else:
        if not goal or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        graph = {
            "graph_id": graph_id,
            "repo_id": repository_id(root),
            "goal": goal.strip(),
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
            "nodes": [],
            "edges": [],
        }
        values = {
            "GRAPH_ID": graph_id,
            "GOAL": goal.strip(),
            "CREATED_AT": created_at,
            "REPO_ID": graph["repo_id"],
            "ORCHESTRATOR": os.environ.get("WP_ORCHESTRATOR", "current-agent"),
        }
        handoff = relative["graph_handoff"]

    graph_dir.mkdir(parents=True)
    atomic_write_json(relative["graph_json"], graph)
    if handoff is not None:
        handoff.write_text(render("GRAPH_HANDOFF.md.template", values), encoding="utf-8")

    return {
        "graph_id": graph_id,
        "graph_dir": state_relative_path(graph_dir),
        "graph_json": state_relative_path(relative["graph_json"]),
        "graph_handoff": (
            state_relative_path(handoff) if handoff is not None else None
        ),
    }


def check(repo: Path, graph_id: str) -> dict[str, object]:
    root = repository_root(repo)
    relative = graph_relative_paths(graph_id, root)
    try:
        graph = read_json(relative["graph_json"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"protocol_valid": False, "errors": [str(exc)]}
    errors = validate_graph(graph)
    return {"protocol_valid": not errors, "errors": errors}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--graph-id", required=True, help="yyyymmdd-lowercase-slug")
    parser.add_argument("--goal", help="one-paragraph observable outcome")
    parser.add_argument("--graph-json", type=Path, help="path to a graph.json to import")
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-validate an existing stored graph.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        try:
            output = check(args.repo, args.graph_id)
        except (OSError, ValueError) as exc:
            print(f"init_graph: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["protocol_valid"] else 2
    if args.graph_json is None and args.goal is None:
        print("init_graph: --goal is required", file=sys.stderr)
        return 2
    try:
        created = initialize(args.repo, args.graph_id, args.goal, args.graph_json)
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"init_graph: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(created, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Schedule a wp graph: fan out conflict-free batches of run_task.py nodes.

Loads graph.json created by init_graph.py, computes the ready set via
graph_lib.py, batches conflict-free ready task nodes, launches each node as a
bounded run_task.py subprocess, validates each finished node with the existing
terminal-artifact validation, and persists node/graph status atomically after
every change. Supports --dry-run and resumable re-invocation.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from graph_lib import (
    critical_path,
    effective_dependencies,
    graph_relative_paths,
    graph_status,
    plan_batch,
    preflight_task_packages,
    ready_tasks,
    validate_graph,
)
from protocol import (
    TERMINAL_STATUSES,
    assert_isolated_execution_root,
    atomic_write_json,
    atomic_write_text,
    handoff_display_path,
    now_iso,
    read_json,
    state_relative_path,
    task_relative_paths,
    validate_terminal_artifacts,
)

SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
GRAPH_HANDOFF_TEMPLATE = SKILL_ROOT / "assets" / "GRAPH_HANDOFF.md.template"
POLL_INTERVAL = 5.0
PROGRESS_HEADING = "# Execution Progress"


def nodes_by_id(graph: dict) -> dict[str, dict]:
    return {
        node["id"]: node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }


def _node_status(graph: dict, node_id: str) -> str:
    node = nodes_by_id(graph).get(node_id)
    if node is None:
        return "missing"
    return node.get("status", "pending")


def eligible_ready(graph: dict, max_node_attempts: int) -> list[str]:
    """Pending ready tasks plus failed tasks with attempts remaining.

    ``ready_tasks`` already returns pending tasks whose effective dependencies
    are all success. A failed task whose attempt count is still below
    ``max_node_attempts`` becomes eligible again so a bounded retry happens on
    the same invocation; exhausted failures stay failed.
    """
    ready = set(ready_tasks(graph))
    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "task":
            continue
        if node.get("status") != "failed":
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        if int(node.get("attempts", 0)) >= max_node_attempts:
            continue
        if all(
            _node_status(graph, dep) == "success"
            for dep in effective_dependencies(graph, node_id)
        ):
            ready.add(node_id)
    return sorted(ready)


def plan_batches(
    graph: dict, max_parallel: int, max_node_attempts: int
) -> list[list[str]]:
    """Optimistic batch plan: simulate every launched node succeeding."""
    simulated = copy.deepcopy(graph)
    simulated_nodes = nodes_by_id(simulated)
    batches: list[list[str]] = []
    while True:
        ready = eligible_ready(simulated, max_node_attempts)
        if not ready:
            break
        batch = plan_batch(ready, simulated, max_parallel)
        if not batch:
            break
        batches.append(batch)
        for node_id in batch:
            simulated_nodes[node_id]["status"] = "success"
    return batches


def schedule(
    graph: dict,
    *,
    launch,
    validate_node,
    persist=None,
    on_node_finished=None,
    max_parallel: int = 3,
    max_node_attempts: int = 3,
    poll_interval: float = POLL_INTERVAL,
) -> dict:
    """Run the scheduling loop over ``graph``, mutating it in place.

    ``launch(node_id, attempt)`` returns a process-like object exposing
    ``poll()`` (exit code, or None while running). ``validate_node(node_id,
    exit_code)`` returns ``(status, errors)`` with status one of success,
    blocked, or failed. ``persist()`` runs after every node transition and
    completion so a crash mid-batch leaves a truthful graph on disk.
    ``on_node_finished(node_id, attempt, exit_code, status)`` runs once per
    completed node. Returns the terminal summary dict.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    if max_node_attempts < 1:
        raise ValueError("max_node_attempts must be at least 1")
    if persist is None:
        persist = lambda: None
    if on_node_finished is None:
        on_node_finished = lambda *args: None

    by_id = nodes_by_id(graph)
    batches_run = 0
    while True:
        ready = eligible_ready(graph, max_node_attempts)
        if not ready:
            graph["status"] = graph_status(graph)
            persist()
            break
        batch = plan_batch(ready, graph, max_parallel)
        if not batch:
            graph["status"] = graph_status(graph)
            persist()
            break

        processes: dict[str, object] = {}
        for node_id in batch:
            node = by_id[node_id]
            node["status"] = "running"
            node["attempts"] = int(node.get("attempts", 0)) + 1
            persist()
            processes[node_id] = launch(node_id, node["attempts"])
        while processes:
            time.sleep(poll_interval)
            for node_id in list(processes):
                process = processes[node_id]
                exit_code = process.poll()
                if exit_code is None:
                    continue
                del processes[node_id]
                node = by_id[node_id]
                status, errors = validate_node(node_id, exit_code)
                node["status"] = status
                if status == "failed":
                    node["last_error"] = errors
                persist()
                on_node_finished(node_id, node["attempts"], exit_code, status)
        batches_run += 1

    node_statuses = {
        node_id: node.get("status", "pending")
        for node_id, node in sorted(by_id.items())
    }
    return {
        "graph_id": graph.get("graph_id"),
        "graph_status": graph.get("status"),
        "node_statuses": node_statuses,
        "batches_run": batches_run,
    }


def build_launch_command(
    node: dict,
    attempt: int,
    *,
    repo: Path,
    agent: str,
    level: str,
    timeout: float | None,
    roster: Path | None,
    max_node_attempts: int,
    allow_legacy_task_package: bool = False,
) -> list[str]:
    """Build the run_task.py invocation for one graph node."""
    command = [
        sys.executable,
        str(SCRIPT_ROOT / "run_task.py"),
        "--repo",
        str(repo),
        "--task-id",
        node["task_id"],
        "--agent",
        agent,
        "--level",
        level,
        "--attempt",
        str(attempt),
        "--max-attempts",
        str(max_node_attempts),
    ]
    if timeout is not None:
        command.extend(["--timeout-seconds", str(timeout)])
    if roster is not None:
        command.extend(["--roster", str(roster)])
    if allow_legacy_task_package:
        command.append("--allow-legacy-task-package")
    return command


def default_launch(
    node: dict,
    attempt: int,
    *,
    repo: Path,
    agent: str,
    level: str,
    timeout: float | None,
    roster: Path | None,
    max_node_attempts: int,
    allow_legacy_task_package: bool = False,
) -> subprocess.Popen:
    command = build_launch_command(
        node,
        attempt,
        repo=repo,
        agent=agent,
        level=level,
        timeout=timeout,
        roster=roster,
        max_node_attempts=max_node_attempts,
        allow_legacy_task_package=allow_legacy_task_package,
    )
    return subprocess.Popen(command, cwd=repo)


def default_validate_node(repo: Path, node: dict, exit_code: int) -> tuple[str, list[str]]:
    """Validate one finished node against its terminal artifacts.

    The result.json status is authoritative when terminal validation passes;
    a missing or unparseable result.json, or a protocol error, marks the node
    failed with the errors recorded for ``last_error``.
    """
    del exit_code  # terminal artifacts are the source of truth
    task_id = node["task_id"]
    paths = task_relative_paths(task_id, repo)
    try:
        result = read_json(paths["result"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "failed", [f"cannot read result.json: {exc}"]
    errors = validate_terminal_artifacts(
        result,
        paths["handoff"],
        task_id,
        handoff_display_path(repo, task_id),
    )
    if errors:
        return "failed", errors
    status = result.get("status")
    if status not in TERMINAL_STATUSES:
        return "failed", [f"invalid terminal status: {status!r}"]
    return status, []


def ensure_graph_handoff(path: Path, graph: dict) -> None:
    """Create the GRAPH_HANDOFF.md record when a graph was imported without one."""
    if path.exists():
        return
    template = GRAPH_HANDOFF_TEMPLATE.read_text(encoding="utf-8")
    values = {
        "GRAPH_ID": str(graph.get("graph_id", "")),
        "GOAL": str(graph.get("goal", "")),
        "CREATED_AT": str(graph.get("created_at", "")),
        "REPO_ID": str(graph.get("repo_id", "")),
        "ORCHESTRATOR": os.environ.get("WP_ORCHESTRATOR", "current-agent"),
    }
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    atomic_write_text(path, text)


def progress_line(
    node_id: str, task_id: str, attempt: int, exit_code: int, status: str
) -> str:
    return (
        f"- {now_iso()}: node {node_id} finished "
        f"task_id={task_id} attempt={attempt} exit_code={exit_code} status={status}"
    )


def append_execution_progress(path: Path, line: str) -> None:
    """Append one finished-node line to the ``# Execution Progress`` section."""
    text = path.read_text(encoding="utf-8")
    start = text.find(PROGRESS_HEADING)
    if start < 0:
        raise ValueError("GRAPH_HANDOFF has no '# Execution Progress' section")
    body_start = start + len(PROGRESS_HEADING)
    section_end = text.find("\n# ", body_start)
    if section_end < 0:
        section_end = len(text)
    body = text[body_start:section_end].strip("\n")
    if body.strip() == "- Not started.":
        body = ""
    lines = [entry for entry in body.splitlines() if entry.strip()]
    lines.append(line)
    rendered = (
        text[:body_start]
        + "\n\n"
        + "\n".join(lines)
        + "\n\n"
        + text[section_end + 1 :]
    )
    atomic_write_text(path, rendered)


def _persist_graph(graph_json_path: Path, graph: dict) -> None:
    graph["updated_at"] = now_iso()
    atomic_write_json(graph_json_path, graph)


def exit_code_for(status: str) -> int:
    if status == "success":
        return 0
    if status == "blocked":
        return 3
    return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--graph-id", required=True, help="yyyymmdd-lowercase-slug")
    parser.add_argument(
        "--agent",
        "--executor",
        dest="agent",
        choices=("auto", "claude", "codex", "grok", "kimi", "pi"),
        default="auto",
    )
    parser.add_argument(
        "--level",
        "--capability",
        "--tier",
        dest="level",
        choices=("fast", "balanced", "hard"),
        default="balanced",
    )
    parser.add_argument("--roster", type=Path, help="roster file from discover_executors.py")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-parallel", type=int, default=3)
    parser.add_argument("--max-node-attempts", type=int, default=3)
    parser.add_argument(
        "--allow-legacy-task-package",
        action="store_true",
        help="explicitly resume unversioned legacy task packages",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan without launching")
    return parser.parse_args(argv)


def _run_locked(
    args: argparse.Namespace, repo: Path, paths: dict[str, Path], graph: dict
) -> int:
    by_id = nodes_by_id(graph)
    package_errors = preflight_task_packages(
        graph, repo, allow_legacy=args.allow_legacy_task_package
    )
    if package_errors:
        for error in package_errors:
            print(f"run_graph: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        plan = {
            "ready": eligible_ready(graph, args.max_node_attempts),
            "batches": plan_batches(graph, args.max_parallel, args.max_node_attempts),
            "critical_path": critical_path(graph),
            "graph_status": graph_status(graph),
        }
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    graph_json_path = paths["graph_json"]
    handoff_path = paths["graph_handoff"]
    ensure_graph_handoff(handoff_path, graph)
    roster = args.roster.resolve() if args.roster is not None else None

    def launch(node_id: str, attempt: int) -> subprocess.Popen:
        node = by_id[node_id]
        return default_launch(
            node,
            attempt,
            repo=repo,
            agent=args.agent,
            level=args.level,
            timeout=args.timeout_seconds,
            roster=roster,
            max_node_attempts=args.max_node_attempts,
            allow_legacy_task_package=args.allow_legacy_task_package,
        )

    def validate_node(node_id: str, exit_code: int) -> tuple[str, list[str]]:
        return default_validate_node(repo, by_id[node_id], exit_code)

    def on_node_finished(node_id: str, attempt: int, exit_code: int, status: str) -> None:
        node = by_id[node_id]
        append_execution_progress(
            handoff_path,
            progress_line(node_id, node["task_id"], attempt, exit_code, status),
        )

    summary = schedule(
        graph,
        launch=launch,
        validate_node=validate_node,
        persist=lambda: _persist_graph(graph_json_path, graph),
        on_node_finished=on_node_finished,
        max_parallel=args.max_parallel,
        max_node_attempts=args.max_node_attempts,
        poll_interval=POLL_INTERVAL,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return exit_code_for(summary["graph_status"])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_parallel < 1:
        print("run_graph: --max-parallel must be at least 1", file=sys.stderr)
        return 2
    if args.max_node_attempts < 1:
        print("run_graph: --max-node-attempts must be at least 1", file=sys.stderr)
        return 2
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        print("run_graph: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    repo = args.repo.resolve()
    assert_isolated_execution_root(repo)
    paths = graph_relative_paths(args.graph_id, repo)
    try:
        graph = read_json(paths["graph_json"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"run_graph: cannot read graph.json: {exc}", file=sys.stderr)
        return 2
    errors = validate_graph(graph)
    if errors:
        for error in errors:
            print(f"run_graph: {error}", file=sys.stderr)
        return 2

    lock_path = paths["graph_dir"] / "run.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            "run_graph: lock already exists: "
            f"{state_relative_path(lock_path)}. Another run_graph process owns "
            "this graph; if it crashed, remove the stale lock to resume.",
            file=sys.stderr,
        )
        return 2
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} started_at={now_iso()}\n")
        return _run_locked(args, repo, paths, graph)
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

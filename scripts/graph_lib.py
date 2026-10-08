#!/usr/bin/env python3
"""Graph model for wp multi-task delegation.

Pure functions over parsed graph.json structures; no I/O except through paths
injected by callers. A graph is a DAG of task and artifact nodes connected by
one of four edge relations. Every edge carries a non-placeholder reason so the
same file can serve as schedule, context map, and resume state.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from protocol import (
    STATE_ROOT,
    TASK_ID_PATTERN,
    repository_id,
    task_package_error_lines,
    task_relative_paths,
    validate_task_package,
    validate_task_id,
)

GRAPH_STATUSES = {"pending", "running", "success", "blocked", "failed"}
NODE_STATUSES = {"pending", "running", "success", "blocked", "failed"}
ARTIFACT_STATUSES = {"pending", "produced"}
EDGE_RELATIONS = {"depends_on", "produces", "consumes", "relates"}
NODE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PLACEHOLDER_REASON_PATTERN = re.compile(
    r"(?i)^(todo|tbd|tbc|unknown|n/?a|none|xxx|\.)$"
)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_relative_path(value: str) -> bool:
    candidate = PurePosixPath(value)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and value != ""
    )


def _node_kinds(nodes: list[object]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, dict):
            node_id = node.get("id")
            if isinstance(node_id, str):
                kinds[node_id] = node.get("kind")
    return kinds


def validate_graph(graph: object) -> list[str]:
    """Validate a graph.json document, returning error strings (empty = valid)."""
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["graph must be a JSON object"]

    graph_id = graph.get("graph_id")
    if not isinstance(graph_id, str) or not TASK_ID_PATTERN.fullmatch(graph_id):
        errors.append("graph_id must match yyyymmdd-lowercase-slug")
    if not _nonempty_string(graph.get("repo_id")):
        errors.append("repo_id must be a non-empty string")
    if not _nonempty_string(graph.get("goal")):
        errors.append("goal must be a non-empty string")
    if graph.get("status") not in GRAPH_STATUSES:
        errors.append(
            "status must be one of pending, running, success, blocked, failed"
        )

    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        errors.append("nodes must be a list")
        nodes = []
    edges = graph.get("edges")
    if not isinstance(edges, list):
        errors.append("edges must be a list")
        edges = []

    seen_ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{index}] must be an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not NODE_ID_PATTERN.fullmatch(node_id):
            errors.append(f"nodes[{index}].id must match lower-case slug pattern")
        elif node_id in seen_ids:
            errors.append(f"duplicate node id: {node_id}")
        else:
            seen_ids.add(node_id)
        kind = node.get("kind")
        if kind == "task":
            _validate_task_node(node, index, errors)
        elif kind == "artifact":
            _validate_artifact_node(node, index, errors)
        else:
            errors.append(f"nodes[{index}].kind must be task or artifact")

    kinds = _node_kinds(nodes)
    for index, edge in enumerate(edges):
        _validate_edge(edge, index, kinds, errors)

    cycle = _find_cycle(nodes, edges)
    if cycle is not None:
        errors.append("dependency cycle detected: " + " -> ".join(cycle))
    return errors


def preflight_task_packages(
    graph: dict, repo: Path, *, allow_legacy: bool = False
) -> list[str]:
    """Validate every task package and its graph-owned write scope.

    Unversioned HANDOFFs fail by default.  Callers must opt into the bounded
    compatibility path explicitly so a missing version cannot bypass v2
    readiness checks accidentally.
    """
    errors: list[str] = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "task":
            continue
        node_id = node.get("id", "<unknown-node>")
        task_id = node.get("task_id")
        if not isinstance(task_id, str):
            continue
        handoff = task_relative_paths(task_id, repo)["handoff"]
        if not handoff.is_file():
            errors.append(
                f"node {node_id}: missing task package HANDOFF ({task_id})"
            )
            continue
        validation = validate_task_package(handoff)
        if validation.errors:
            errors.extend(
                task_package_error_lines(validation, prefix=f"node {node_id}")
            )
            continue
        if validation.legacy:
            if not allow_legacy:
                errors.append(
                    f"node {node_id}: legacy task package is not allowed by default; "
                    "pass --allow-legacy-task-package to use the compatibility path"
                )
            continue

        contract_scope = validation.contract["write_scope"]
        declared_writes = node.get("writes")
        if not isinstance(declared_writes, list):
            errors.append(
                f"node {node_id}: v2 task must declare writes matching "
                "Atomic Work Contract write_scope"
            )
            continue
        if sorted(declared_writes) != sorted(contract_scope):
            errors.append(
                f"node {node_id}: graph writes must exactly match "
                "Atomic Work Contract write_scope"
            )
    return errors


def _validate_task_node(node: dict, index: int, errors: list[str]) -> None:
    task_id = node.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        errors.append(f"nodes[{index}].task_id must match yyyymmdd-lowercase-slug")
    if not _nonempty_string(node.get("goal")):
        errors.append(f"nodes[{index}].goal must be a non-empty string")
    status = node.get("status", "pending")
    if status not in NODE_STATUSES:
        errors.append(
            f"nodes[{index}].status must be one of pending, running, success, "
            "blocked, failed"
        )
    writes = node.get("writes")
    if writes is not None and not (
        isinstance(writes, list)
        and all(
            isinstance(write, str) and _safe_relative_path(write)
            for write in writes
        )
    ):
        errors.append(
            f"nodes[{index}].writes must be a list of repo-relative glob strings"
        )
    attempts = node.get("attempts", 0)
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
    ):
        errors.append(f"nodes[{index}].attempts must be a non-negative integer")


def _validate_artifact_node(node: dict, index: int, errors: list[str]) -> None:
    path = node.get("path")
    if not isinstance(path, str) or not _safe_relative_path(path):
        errors.append(f"nodes[{index}].path must be a repo-relative path")
    status = node.get("status", "pending")
    if status not in ARTIFACT_STATUSES:
        errors.append(f"nodes[{index}].status must be pending or produced")


def _validate_edge(
    edge: object, index: int, kinds: dict[str, str], errors: list[str]
) -> None:
    if not isinstance(edge, dict):
        errors.append(f"edges[{index}] must be an object")
        return
    from_id = edge.get("from")
    to_id = edge.get("to")
    if not isinstance(from_id, str) or from_id not in kinds:
        errors.append(f"edges[{index}].from must reference an existing node id")
    if not isinstance(to_id, str) or to_id not in kinds:
        errors.append(f"edges[{index}].to must reference an existing node id")
    if isinstance(from_id, str) and isinstance(to_id, str) and from_id == to_id:
        errors.append(f"edges[{index}]: edge from a node to itself is not allowed")

    relation = edge.get("relation")
    if relation not in EDGE_RELATIONS:
        errors.append(
            f"edges[{index}].relation must be depends_on, produces, consumes, "
            "or relates"
        )

    reason = edge.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(f"edges[{index}].reason is mandatory")
    else:
        stripped = reason.strip()
        if len(stripped) < 12:
            errors.append(f"edges[{index}].reason must be at least 12 characters")
        elif PLACEHOLDER_REASON_PATTERN.fullmatch(stripped):
            errors.append(f"edges[{index}].reason is a placeholder")

    if relation in EDGE_RELATIONS and isinstance(from_id, str) and isinstance(
        to_id, str
    ):
        from_kind = kinds.get(from_id)
        to_kind = kinds.get(to_id)
        if from_kind and to_kind:
            if relation == "depends_on" and not (
                from_kind == "task" and to_kind == "task"
            ):
                errors.append(f"edges[{index}]: depends_on must be task -> task")
            if relation == "produces" and not (
                from_kind == "task" and to_kind == "artifact"
            ):
                errors.append(
                    f"edges[{index}]: produces must be task -> artifact"
                )
            if relation == "consumes" and not (
                from_kind == "artifact" and to_kind == "task"
            ):
                errors.append(
                    f"edges[{index}]: consumes must be artifact -> task"
                )


def _find_cycle(
    nodes: list[object], edges: list[object]
) -> list[str] | None:
    """Detect a cycle in the scheduling subgraph (relates excluded).

    ``consumes`` edges are resolved through their producing tasks, so the
    dependency graph is over task nodes only. Returns the cycle node ids in
    traversal order, or None when the graph is acyclic.
    """
    task_ids = {
        node["id"]
        for node in nodes
        if isinstance(node, dict)
        and isinstance(node.get("id"), str)
        and node.get("kind") == "task"
    }
    producers: dict[str, set[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("relation") != "produces":
            continue
        artifact_id = edge.get("to")
        producer = edge.get("from")
        if isinstance(artifact_id, str) and isinstance(producer, str):
            producers.setdefault(artifact_id, set()).add(producer)

    deps: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        relation = edge.get("relation")
        to_id = edge.get("to")
        if relation == "depends_on" and isinstance(to_id, str) and to_id in deps:
            source = edge.get("from")
            if isinstance(source, str) and source in deps:
                deps[to_id].add(source)
        elif (
            relation == "consumes"
            and isinstance(to_id, str)
            and to_id in deps
        ):
            artifact_id = edge.get("from")
            if isinstance(artifact_id, str):
                for producer in producers.get(artifact_id, ()):
                    if producer in deps:
                        deps[to_id].add(producer)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in task_ids}
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        color[node_id] = GRAY
        stack.append(node_id)
        for dependency in sorted(deps[node_id]):
            if color[dependency] == GRAY:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
            if color[dependency] == WHITE:
                result = visit(dependency)
                if result is not None:
                    return result
        stack.pop()
        color[node_id] = BLACK
        return None

    for node_id in sorted(task_ids):
        if color[node_id] == WHITE:
            result = visit(node_id)
            if result is not None:
                return result
    return None


def effective_dependencies(graph: dict, node_id: str) -> set[str]:
    """Direct ``depends_on`` sources plus producers of consumed artifacts."""
    nodes = graph.get("nodes", [])
    node_ids = {
        node["id"] for node in nodes if isinstance(node, dict) and isinstance(
            node.get("id"), str
        )
    }
    if node_id not in node_ids:
        return set()
    producers: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") != "produces":
            continue
        artifact_id = edge.get("to")
        producer = edge.get("from")
        if isinstance(artifact_id, str) and isinstance(producer, str):
            producers.setdefault(artifact_id, set()).add(producer)
    deps: set[str] = set()
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("to") != node_id:
            continue
        relation = edge.get("relation")
        if relation == "depends_on":
            source = edge.get("from")
            if isinstance(source, str):
                deps.add(source)
        elif relation == "consumes":
            artifact_id = edge.get("from")
            if isinstance(artifact_id, str):
                deps.update(producers.get(artifact_id, ()))
    return {dep for dep in deps if dep in node_ids}


def _node_status(nodes_by_id: dict[str, dict], node_id: str) -> str:
    node = nodes_by_id.get(node_id)
    if node is None:
        return "missing"
    return node.get("status", "pending")


def ready_tasks(graph: dict) -> list[str]:
    """Task nodes pending whose effective dependencies are all success."""
    nodes_by_id = {
        node["id"]: node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    ready: list[str] = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "task":
            continue
        if node.get("status", "pending") != "pending":
            continue
        deps = effective_dependencies(graph, node["id"])
        if all(_node_status(nodes_by_id, dep) == "success" for dep in deps):
            ready.append(node["id"])
    return sorted(ready)


def _static_prefix(pattern: str) -> list[str]:
    """Truncate a glob at the first wildcard, then split into path segments."""
    for index, char in enumerate(pattern):
        if char in "*?":
            pattern = pattern[:index]
            break
    return [segment for segment in pattern.strip("/").split("/") if segment]


def _segment_prefix(prefix: list[str], candidate: list[str]) -> bool:
    return candidate[: len(prefix)] == prefix


def write_conflict(writes_a: list[str], writes_b: list[str]) -> bool:
    """Conservative write-set conflict detection.

    An empty write set means the write scope is unknown and therefore conflicts
    with every other node. Otherwise each glob is reduced to its static prefix
    (truncated at the first ``*`` or ``?``, then normalized to path segments);
    two write sets conflict when any static prefix of one is a complete
    path-segment prefix of the other, or they are equal. This is deliberately
    an over-approximation of real file overlap.
    """
    if not writes_a or not writes_b:
        return True
    prefixes_a = [_static_prefix(write) for write in writes_a]
    prefixes_b = [_static_prefix(write) for write in writes_b]
    for prefix_a in prefixes_a:
        for prefix_b in prefixes_b:
            if _segment_prefix(prefix_a, prefix_b) or _segment_prefix(
                prefix_b, prefix_a
            ):
                return True
    return False


def plan_batch(
    ready: list[str], graph: dict, max_parallel: int
) -> list[str]:
    """Greedy deterministic batch: sorted ready, skip write-conflicts, cap."""
    nodes_by_id = {
        node["id"]: node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }

    def writes_of(node_id: str) -> list[str] | None:
        node = nodes_by_id.get(node_id)
        if node is None:
            return None
        return node.get("writes", [])

    batch: list[str] = []
    for node_id in sorted(ready):
        if len(batch) >= max_parallel:
            break
        writes = writes_of(node_id)
        if writes is None:
            continue
        if any(write_conflict(writes, writes_of(existing)) for existing in batch):
            continue
        batch.append(node_id)
    return batch


def graph_status(graph: dict) -> str:
    """Derive the graph-level status from node statuses.

    success when there is at least one task node and every task node is
    success; failed when any task failed and no ready tasks remain; blocked
    when any task is blocked and nothing failed-or-ready remains; running when
    any node is running; otherwise pending.
    """
    nodes = graph.get("nodes", [])
    task_nodes = [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("kind") == "task"
    ]
    if task_nodes and all(
        node.get("status", "pending") == "success" for node in task_nodes
    ):
        return "success"
    any_failed = any(
        node.get("status", "pending") == "failed" for node in task_nodes
    )
    ready = ready_tasks(graph)
    if any_failed and not ready:
        return "failed"
    any_blocked = any(
        node.get("status", "pending") == "blocked" for node in task_nodes
    )
    if any_blocked and not any_failed and not ready:
        return "blocked"
    if any(
        isinstance(node, dict) and node.get("status") == "running"
        for node in nodes
    ):
        return "running"
    return "pending"


def critical_path(graph: dict) -> list[str]:
    """Longest task chain through effective dependencies (by node count).

    Deterministic tie-break: the lexicographically smallest node-id sequence
    among all longest chains.
    """
    task_ids = [
        node["id"]
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
        and isinstance(node.get("id"), str)
        and node.get("kind") == "task"
    ]
    if not task_ids:
        return []
    dependents: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    for task_id in task_ids:
        for dependency in effective_dependencies(graph, task_id):
            if dependency in dependents:
                dependents[dependency].add(task_id)

    memo: dict[str, int] = {}

    def longest_starting_at(node_id: str) -> int:
        if node_id in memo:
            return memo[node_id]
        best = 1
        for successor in sorted(dependents[node_id]):
            best = max(best, 1 + longest_starting_at(successor))
        memo[node_id] = best
        return best

    length = 0
    start: str | None = None
    for node_id in sorted(task_ids):
        candidate = longest_starting_at(node_id)
        if candidate > length:
            length = candidate
            start = node_id
    if start is None:
        return []
    path = [start]
    while len(path) < length:
        current = path[-1]
        candidates = [
            successor
            for successor in sorted(dependents[current])
            if longest_starting_at(successor) == length - len(path)
        ]
        if not candidates:
            break
        path.append(candidates[0])
    return path


def required_context(graph: dict, node_id: str) -> dict:
    """Context packet a downstream node should load.

    Limited to transitive effective dependencies: upstream task ids, artifacts
    produced by those upstream tasks, and the dependency edges among them.
    """
    nodes = graph.get("nodes", [])
    nodes_by_id = {
        node["id"]: node
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    upstream: set[str] = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for dependency in effective_dependencies(graph, current):
            if dependency not in upstream:
                upstream.add(dependency)
                frontier.append(dependency)
    upstream.discard(node_id)

    artifacts: list[dict[str, str]] = []
    artifact_ids: set[str] = set()
    for edge in graph.get("edges", []):
        if (
            not isinstance(edge, dict)
            or edge.get("relation") != "produces"
            or edge.get("from") not in upstream
        ):
            continue
        artifact_id = edge.get("to")
        artifact = nodes_by_id.get(artifact_id) if isinstance(artifact_id, str) else None
        if artifact is not None:
            artifact_ids.add(artifact_id)
            artifacts.append(
                {
                    "path": artifact.get("path", ""),
                    "produced_by": edge.get("from"),
                }
            )
    artifacts.sort(key=lambda item: (item["path"], item["produced_by"]))

    relevant = set(upstream) | {node_id} | artifact_ids
    edges: list[dict[str, str]] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") == "relates":
            continue
        if edge.get("from") in relevant and edge.get("to") in relevant:
            edges.append(
                {
                    "from": edge.get("from"),
                    "to": edge.get("to"),
                    "relation": edge.get("relation"),
                    "reason": edge.get("reason"),
                }
            )
    edges.sort(key=lambda item: (item["from"], item["to"]))
    return {
        "upstream_tasks": sorted(upstream),
        "artifacts": artifacts,
        "edges": edges,
    }


def graph_relative_paths(graph_id: str, repo: Path) -> dict[str, Path]:
    """Resolve graph artifacts into the skill-local state directory."""
    validate_task_id(graph_id)
    root = STATE_ROOT / "repos" / repository_id(repo)
    graph_dir = root / "graphs" / graph_id
    return {
        "graph_dir": graph_dir,
        "graph_json": graph_dir / "graph.json",
        "graph_handoff": graph_dir / "GRAPH_HANDOFF.md",
    }

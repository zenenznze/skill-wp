#!/usr/bin/env python3
"""Run a durable, single-writer supervisor over an existing wp graph.

The supervisor owns control-plane state below wp-state. Herdr is an execution
plane adapter only: lifecycle state never becomes semantic task success.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from graph_lib import effective_dependencies, plan_batch, validate_graph
from herdr_pi_control import (
    BridgeError,
    CommandError,
    HerdrClient,
    HerdrTimeout,
    ProtocolError,
    discover_workspace,
    herdr_result,
    nested_id,
    require_array,
)
from protocol import (
    STATE_ROOT,
    atomic_write_json,
    assert_isolated_execution_root,
    mark_handoff_started,
    now_iso,
    repository_id,
    synthesize_failure,
    task_package_error_lines,
    validate_task_id,
    validate_task_package,
    validate_terminal_artifacts,
)


SUPERVISOR_SCHEMA = "wp-durable-supervisor-v1"
COLLECTED_RESULT_SCHEMA = "wp-collected-result-v1"
REVIEW_SCHEMA = "wp-review-v1"
TASK_STATUSES = {"pending", "running", "retry", "success", "blocked", "failed"}
ATTEMPT_STATUSES = {
    "spawning",
    "waiting",
    "collecting",
    "reviewed",
    "passed",
    "retry",
    "blocked",
    "failed",
    "interrupted",
}
LIFECYCLE_STATUSES = {"idle", "done", "blocked", "unknown", "timeout"}
REVIEW_VERDICTS = {"PASS", "RETRY"}
RESULT_FIELDS = {
    "task_id", "status", "summary", "handoff_path", "changed_files", "validation",
    "blocker", "recommended_next_action", "observations", "failure_class", "producer", "transport",
}
NODE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SupervisorError(ValueError):
    """A deterministic supervisor/state error."""


class SupervisorLockError(SupervisorError):
    """Another live supervisor owns the state."""


@dataclass
class WorkerHandle:
    node_id: str
    attempt: int
    worker_id: str
    metadata: dict[str, str] = field(default_factory=dict)


class WorkerBackend(Protocol):
    name: str

    def spawn(
        self,
        task: dict[str, Any],
        attempt: int,
        prompt: str,
        feedback: str | None,
        journal: Callable[[dict[str, str]], None] | None = None,
    ) -> WorkerHandle: ...

    def recover(self, attempt: dict[str, Any]) -> WorkerHandle | None: ...

    def poll(self, handle: WorkerHandle) -> str | None: ...

    def collect(self, handle: WorkerHandle | None, attempt: dict[str, Any]) -> None: ...


class Reviewer(Protocol):
    name: str

    def review(
        self,
        task: dict[str, Any],
        attempt: int,
        result: dict[str, Any] | None,
        result_errors: list[str],
        lifecycle_status: str,
    ) -> dict[str, Any]: ...


def _safe_text(value: Any, limit: int = 800) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token|"
        r"refresh[_-]?token)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_data(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_safe_data(item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(key): _safe_data(item) for key, item in list(value.items())[:32]}
    return value


def _state_relative(path: Path, state_root: Path) -> str:
    return "wp-state/" + path.resolve().relative_to(state_root.resolve()).as_posix()


def _repo_state_root(state_root: Path, repo: Path) -> Path:
    return state_root / "repos" / repository_id(repo)


def _graph_path(state_root: Path, repo: Path, graph_id: str) -> Path:
    validate_task_id(graph_id)
    return _repo_state_root(state_root, repo) / "graphs" / graph_id / "graph.json"


def _task_paths(state_root: Path, repo: Path, task_id: str) -> dict[str, Path]:
    validate_task_id(task_id)
    task_dir = _repo_state_root(state_root, repo) / "tasks" / task_id
    return {
        "task_dir": task_dir,
        "handoff": task_dir / "HANDOFF.md",
        "prompt": task_dir / "EXECUTOR_PROMPT.txt",
        "result": task_dir / "result.json",
    }


def _supervisor_root(state_root: Path, repo: Path, supervisor_id: str) -> Path:
    validate_task_id(supervisor_id)
    return _repo_state_root(state_root, repo) / "supervisors" / supervisor_id


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_node_id(value: str) -> str:
    if not isinstance(value, str) or not NODE_ID_PATTERN.fullmatch(value):
        raise SupervisorError(f"invalid graph node id: {value!r}")
    return value


def validate_review(value: Any, task_id: str, attempt: int) -> list[str]:
    """Validate the exact reviewer verdict contract."""
    errors: list[str] = []
    required = {"schema", "task_id", "attempt", "verdict", "summary", "findings", "checked", "reviewer"}
    if not isinstance(value, dict):
        return ["review must be an object"]
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        if missing:
            errors.append("review missing fields: " + ", ".join(missing))
        if extra:
            errors.append("review has unsupported fields: " + ", ".join(extra))
    if value.get("schema") != REVIEW_SCHEMA:
        errors.append(f"review.schema must be {REVIEW_SCHEMA}")
    if value.get("task_id") != task_id:
        errors.append("review.task_id does not match the task")
    if isinstance(value.get("attempt"), bool) or value.get("attempt") != attempt:
        errors.append("review.attempt does not match the attempt")
    if value.get("verdict") not in REVIEW_VERDICTS:
        errors.append("review.verdict must be PASS or RETRY")
    if not isinstance(value.get("summary"), str) or not value.get("summary", "").strip():
        errors.append("review.summary must be a non-empty string")
    for field_name in ("findings", "checked"):
        values = value.get(field_name)
        if not isinstance(values, list) or not values or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            errors.append(f"review.{field_name} must be a non-empty string array")
    if not isinstance(value.get("reviewer"), str) or not value.get("reviewer", "").strip():
        errors.append("review.reviewer must be a non-empty string")
    return errors


def validate_collected_result(value: Any, task_id: str, attempt: int) -> list[str]:
    """Validate the supervisor's result envelope, not just its inner result."""
    required = {
        "schema",
        "task_id",
        "attempt",
        "lifecycle_status",
        "collected_at",
        "result",
        "result_errors",
    }
    if not isinstance(value, dict):
        return ["collected result must be an object"]
    errors: list[str] = []
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        errors.append("collected result missing fields: " + ", ".join(missing))
    if extra:
        errors.append("collected result has unsupported fields: " + ", ".join(extra))
    if value.get("schema") != COLLECTED_RESULT_SCHEMA:
        errors.append(f"collected result schema must be {COLLECTED_RESULT_SCHEMA}")
    if value.get("task_id") != task_id:
        errors.append("collected result task_id does not match the task")
    if isinstance(value.get("attempt"), bool) or value.get("attempt") != attempt:
        errors.append("collected result attempt does not match the attempt")
    if value.get("lifecycle_status") not in LIFECYCLE_STATUSES:
        errors.append("collected result lifecycle_status is invalid")
    if not isinstance(value.get("collected_at"), str) or not value.get("collected_at", "").strip():
        errors.append("collected result collected_at must be non-empty")
    if value.get("result") is not None and not isinstance(value.get("result"), dict):
        errors.append("collected result.result must be an object or null")
    if not isinstance(value.get("result_errors"), list) or not all(
        isinstance(item, str) and item.strip() for item in value.get("result_errors", [])
    ):
        errors.append("collected result.result_errors must be a string array")
    return errors


def _expected_handoff(state_root: Path, repo: Path, task_id: str) -> str:
    return _state_relative(_task_paths(state_root, repo, task_id)["handoff"], state_root)


def _task_state_template(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "wp-supervisor-task-v1",
        "node_id": node["id"],
        "task_id": node["task_id"],
        "status": "pending",
        "attempts": 0,
        "active_attempt": None,
        "feedback": None,
        "last_result_path": None,
        "last_review_path": None,
        "updated_at": now_iso(),
    }


class SupervisorLock:
    """An exclusive process lock with safe stale-owner reclamation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    raw = self.path.read_text(encoding="utf-8")
                    owner = json.loads(raw)
                    pid = owner.get("pid")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise SupervisorLockError(
                        f"lock exists and is unreadable: {self.path}"
                    ) from exc
                if isinstance(pid, bool) or not isinstance(pid, int):
                    raise SupervisorLockError(f"lock has invalid owner: {self.path}")
                if _pid_alive(pid):
                    raise SupervisorLockError(f"live supervisor owns lock: pid={pid}")
                self.path.unlink()
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "started_at": now_iso()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return
        raise SupervisorLockError(f"could not acquire lock: {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


class StateStore:
    """Atomic task/project/checkpoint writes plus an append-only event journal."""

    def __init__(self, root: Path, state_root: Path) -> None:
        self.root = root
        self.state_root = state_root
        self.project_path = root / "project.json"
        self.events_path = root / "events.jsonl"
        self.tasks_dir = root / "tasks"
        self.attempts_dir = root / "attempts"
        self.results_dir = root / "results"
        self.reviews_dir = root / "reviews"
        self.checkpoints_dir = root / "checkpoints"
        self.event_ids: set[str] = set()
        self.event_seq = 0
        self._load_event_index()

    def _load_event_index(self) -> None:
        if not self.events_path.is_file():
            return
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            seq = event.get("seq")
            if isinstance(event_id, str):
                self.event_ids.add(event_id)
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.event_seq = max(self.event_seq, seq)

    def task_path(self, node_id: str) -> Path:
        return self.tasks_dir / f"{_safe_node_id(node_id)}.json"

    def attempt_path(self, node_id: str, attempt: int) -> Path:
        return self.attempts_dir / _safe_node_id(node_id) / f"attempt-{attempt:02d}.json"

    def result_path(self, node_id: str, attempt: int) -> Path:
        return self.results_dir / _safe_node_id(node_id) / f"attempt-{attempt:02d}.json"

    def review_path(self, node_id: str, attempt: int) -> Path:
        return self.reviews_dir / _safe_node_id(node_id) / f"attempt-{attempt:02d}.json"

    def read_project(self) -> dict[str, Any]:
        try:
            value = json.loads(self.project_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorError(f"cannot read project.json: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != SUPERVISOR_SCHEMA:
            raise SupervisorError("project.json schema mismatch")
        return value

    def read_tasks(self, node_ids: list[str]) -> dict[str, dict[str, Any]]:
        tasks: dict[str, dict[str, Any]] = {}
        for node_id in node_ids:
            path = self.task_path(node_id)
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise SupervisorError(f"cannot read task state {path}: {exc}") from exc
            if not isinstance(value, dict) or value.get("node_id") != node_id:
                raise SupervisorError(f"task state schema mismatch: {path}")
            if value.get("status") not in TASK_STATUSES:
                raise SupervisorError(f"invalid task status in {path}")
            tasks[node_id] = value
        return tasks

    def read_attempt(self, node_id: str, attempt: int) -> dict[str, Any]:
        path = self.attempt_path(node_id, attempt)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorError(f"cannot read attempt state {path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("node_id") != node_id or value.get("attempt") != attempt:
            raise SupervisorError(f"attempt state schema mismatch: {path}")
        return value

    def _append_event(
        self,
        event_type: str,
        *,
        node_id: str | None = None,
        attempt: int | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> int:
        stable_id = event_id or f"{event_type}:{node_id or '-'}:{attempt or '-'}"
        if stable_id in self.event_ids:
            return self.event_seq
        self.event_seq += 1
        event = {
            "seq": self.event_seq,
            "event_id": stable_id,
            "at": now_iso(),
            "type": event_type,
            "node_id": node_id,
            "attempt": attempt,
            "data": _safe_data(data or {}),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.event_ids.add(stable_id)
        return self.event_seq

    def commit(
        self,
        project: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        *,
        event_type: str | None = None,
        node_id: str | None = None,
        attempt: int | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> None:
        if event_type is not None:
            self._append_event(
                event_type,
                node_id=node_id,
                attempt=attempt,
                data=data,
                event_id=event_id,
            )
        for task_id, task in tasks.items():
            task["updated_at"] = now_iso()
            atomic_write_json(self.task_path(task_id), task)
        project["event_seq"] = self.event_seq
        project["checkpoint_seq"] = int(project.get("checkpoint_seq", 0)) + 1
        project["last_checkpoint"] = _state_relative(
            self.checkpoints_dir / f"checkpoint-{project['checkpoint_seq']:06d}.json",
            self.state_root,
        )
        project["updated_at"] = now_iso()
        checkpoint = {
            "schema": "wp-supervisor-checkpoint-v1",
            "checkpoint_seq": project["checkpoint_seq"],
            "created_at": project["updated_at"],
            "project_status": project.get("status"),
            "event_seq": self.event_seq,
            "tasks": copy.deepcopy(tasks),
        }
        atomic_write_json(self.checkpoints_dir / f"checkpoint-{project['checkpoint_seq']:06d}.json", checkpoint)
        atomic_write_json(self.project_path, project)


def _load_graph(repo: Path, graph_id: str, state_root: Path) -> dict[str, Any]:
    path = _graph_path(state_root, repo, graph_id)
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"cannot read graph.json: {exc}") from exc
    errors = validate_graph(graph)
    if errors:
        raise SupervisorError("graph validation failed: " + "; ".join(errors))
    if graph.get("repo_id") != repository_id(repo):
        raise SupervisorError("graph repo_id does not match the execution root")
    return graph


def _validate_task_packages(repo: Path, graph: dict[str, Any], state_root: Path) -> None:
    errors: list[str] = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "task":
            continue
        node_id = node["id"]
        task_id = node["task_id"]
        handoff = _task_paths(state_root, repo, task_id)["handoff"]
        validation = validate_task_package(handoff)
        if validation.errors:
            errors.extend(task_package_error_lines(validation, prefix=f"node {node_id}"))
            continue
        if validation.legacy:
            errors.append(f"node {node_id}: legacy task packages are not supported by durable supervisor")
            continue
        writes = node.get("writes")
        if not isinstance(writes, list) or sorted(writes) != sorted(validation.contract["write_scope"]):
            errors.append(f"node {node_id}: graph writes must exactly match Atomic Work Contract write_scope")
    if errors:
        raise SupervisorError("task package preflight failed: " + "; ".join(errors))


def initialize_supervisor(
    repo: Path,
    graph_id: str,
    supervisor_id: str,
    *,
    max_parallel: int = 3,
    max_attempts: int = 3,
    backend: str = "herdr",
    state_root: Path = STATE_ROOT,
) -> dict[str, str | int]:
    if max_parallel < 1 or max_attempts < 1:
        raise SupervisorError("max_parallel and max_attempts must be positive")
    if backend != "herdr":
        raise SupervisorError("the CLI backend must be herdr; tests may inject a fake backend")
    repo = repo.resolve()
    graph = _load_graph(repo, graph_id, state_root)
    _validate_task_packages(repo, graph, state_root)
    root = _supervisor_root(state_root, repo, supervisor_id)
    if root.exists():
        raise FileExistsError(f"supervisor already exists: {root}")
    task_nodes = [node for node in graph["nodes"] if node.get("kind") == "task"]
    if not task_nodes:
        raise SupervisorError("graph must contain at least one task node")
    root.mkdir(parents=True)
    for directory in (root / "tasks", root / "attempts", root / "results", root / "reviews", root / "checkpoints"):
        directory.mkdir()
    (root / "events.jsonl").touch()
    project: dict[str, Any] = {
        "schema": SUPERVISOR_SCHEMA,
        "supervisor_id": supervisor_id,
        "repo_id": repository_id(repo),
        "graph_id": graph_id,
        "graph_digest": _canonical_digest(graph),
        "backend": backend,
        "status": "pending",
        "max_parallel": max_parallel,
        "max_attempts": max_attempts,
        "task_nodes": sorted(node["id"] for node in task_nodes),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "event_seq": 0,
        "checkpoint_seq": 0,
        "last_checkpoint": None,
    }
    tasks = {node["id"]: _task_state_template(node) for node in task_nodes}
    store = StateStore(root, state_root)
    atomic_write_json(store.project_path, project)
    store.commit(project, tasks, event_type="supervisor_initialized", event_id="supervisor_initialized")
    return {
        "supervisor_id": supervisor_id,
        "state_dir": _state_relative(root, state_root),
        "project": _state_relative(store.project_path, state_root),
        "graph": _state_relative(_graph_path(state_root, repo, graph_id), state_root),
        "max_parallel": max_parallel,
        "max_attempts": max_attempts,
    }


class DeterministicReviewer:
    """Independent, read-only acceptance gate for worker terminal artifacts."""

    name = "deterministic-readonly-reviewer-v1"

    def review(
        self,
        task: dict[str, Any],
        attempt: int,
        result: dict[str, Any] | None,
        result_errors: list[str],
        lifecycle_status: str,
    ) -> dict[str, Any]:
        findings = [_safe_text(error) for error in result_errors]
        checked = ["result-contract", "handoff-terminal-state", "validation-records", "lifecycle-separation"]
        if lifecycle_status in {"blocked", "unknown", "timeout"}:
            findings.append(f"worker lifecycle is {lifecycle_status}; lifecycle is not semantic success")
        if result is None:
            findings.append("worker did not provide a structured result")
        elif result.get("status") != "success":
            findings.append(f"worker result status is {result.get('status')!r}, not success")
        verdict = "PASS" if not findings and result is not None and result.get("status") == "success" else "RETRY"
        return {
            "schema": REVIEW_SCHEMA,
            "task_id": task["task_id"],
            "attempt": attempt,
            "verdict": verdict,
            "summary": "Independent reviewer accepted the strict terminal contract." if verdict == "PASS" else "Independent reviewer rejected the attempt and requested bounded retry.",
            "findings": findings or ["none"],
            "checked": checked,
            "reviewer": self.name,
        }


class HerdrBackend:
    """Herdr execution-plane adapter; it never decides task success."""

    name = "herdr"

    def __init__(
        self,
        repo: Path,
        state_root: Path,
        controller_tab_id: str,
        *,
        binary: str | None = None,
        wait_timeout_ms: int = 250,
    ) -> None:
        if not controller_tab_id.strip():
            raise SupervisorError("controller_tab_id is required for Herdr backend")
        if wait_timeout_ms < 1:
            raise SupervisorError("wait_timeout_ms must be positive")
        self.repo = repo.resolve()
        self.state_root = state_root
        self.client = HerdrClient(binary)
        self.controller_tab_id = controller_tab_id
        self.wait_timeout_ms = wait_timeout_ms
        self.workspace_id: str | None = None

    def _workspace(self) -> str:
        if self.workspace_id is not None:
            return self.workspace_id
        plan = {"space_mode": "cwd", "cwd": str(self.repo)}
        workspace = discover_workspace(plan, self.client)
        if workspace is None:
            created = herdr_result(
                self.client.call(
                    "workspace",
                    "create",
                    "--cwd",
                    str(self.repo),
                    "--label",
                    "wp-supervisor",
                    "--no-focus",
                )
            )
            workspace = nested_id(created, "workspace", "workspace_id")
        self.workspace_id = workspace
        return workspace

    @staticmethod
    def _agent_name(node_id: str, attempt: int) -> str:
        compact = re.sub(r"[^a-z0-9-]", "-", node_id.lower()).strip("-")[:18] or "task"
        return f"wp-{compact}-a{attempt}"

    def spawn(
        self,
        task: dict[str, Any],
        attempt: int,
        prompt: str,
        feedback: str | None,
        journal: Callable[[dict[str, str]], None] | None = None,
    ) -> WorkerHandle:
        workspace_id = self._workspace()
        node_id = task["node_id"]
        tab_label = f"执行-{node_id}-a{attempt}"
        created = herdr_result(
            self.client.call(
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--cwd",
                str(self.repo),
                "--label",
                tab_label,
                "--no-focus",
            )
        )
        tab_id = nested_id(created, "tab", "tab_id")
        pane_id = nested_id(created, "root_pane", "pane_id")
        if journal:
            journal({"workspace_id": workspace_id, "tab_id": tab_id, "pane_id": pane_id, "tab_label": tab_label})
        agent_name = self._agent_name(node_id, attempt)
        started = herdr_result(
            self.client.call("agent", "start", agent_name, "--kind", "pi", "--pane", pane_id)
        )
        returned_agent = (
            nested_id(started, "agent", "name")
            if isinstance(started.get("agent"), dict)
            else nested_id(started, "agent_name")
        )
        if returned_agent != agent_name:
            raise ProtocolError("Herdr returned an inconsistent agent ID")
        if journal:
            journal({"agent_id": returned_agent})
        prompt_text = prompt
        if feedback:
            prompt_text += (
                "\n\nDURABLE SUPERVISOR RETRY FEEDBACK (address this before finishing):\n"
                + _safe_text(feedback, 4000)
            )
        self.client.call("agent", "prompt", returned_agent, prompt_text)
        return WorkerHandle(
            node_id=node_id,
            attempt=attempt,
            worker_id=returned_agent,
            metadata={
                "workspace_id": workspace_id,
                "tab_id": tab_id,
                "pane_id": pane_id,
                "tab_label": tab_label,
            },
        )

    def recover(self, attempt: dict[str, Any]) -> WorkerHandle | None:
        worker = attempt.get("worker")
        if not isinstance(worker, dict) or not isinstance(worker.get("agent_id"), str):
            return None
        worker_id = worker["agent_id"]
        try:
            self.client.call("agent", "get", worker_id)
        except (BridgeError, CommandError, ProtocolError, HerdrTimeout, OSError):
            result_path = attempt.get("result_path")
            if isinstance(result_path, str):
                candidate = Path(result_path)
                if not candidate.is_absolute() and candidate.parts and candidate.parts[0] == "wp-state":
                    candidate = self.state_root.parent / Path(*candidate.parts[1:])
                if candidate.is_file():
                    return WorkerHandle(
                        attempt["node_id"],
                        attempt["attempt"],
                        worker_id,
                        {"recovered_result": "true"},
                    )
            return None
        metadata = {
            key: value
            for key, value in worker.items()
            if key in {"workspace_id", "tab_id", "pane_id", "tab_label"} and isinstance(value, str)
        }
        return WorkerHandle(attempt["node_id"], attempt["attempt"], worker_id, metadata)

    def poll(self, handle: WorkerHandle) -> str | None:
        try:
            response = self.client.call(
                "agent",
                "wait",
                handle.worker_id,
                "--until",
                "idle",
                "--until",
                "done",
                "--until",
                "blocked",
                "--timeout",
                str(self.wait_timeout_ms),
            )
        except HerdrTimeout:
            return None
        status = nested_id(herdr_result(response), "status")
        if status not in LIFECYCLE_STATUSES:
            raise ProtocolError(f"unsupported Herdr lifecycle status: {status}")
        return status

    def collect(self, handle: WorkerHandle | None, attempt: dict[str, Any]) -> None:
        if handle is None:
            return
        try:
            # Read is deliberately observational; its output is not persisted as
            # semantic evidence and Herdr lifecycle states remain inconclusive.
            self.client.call("agent", "read", handle.worker_id, "--source", "recent-unwrapped", "--lines", "20")
        except (BridgeError, CommandError, ProtocolError, HerdrTimeout, OSError):
            pass


class DurableSupervisor:
    """The single writer for one durable supervisor state directory."""

    def __init__(
        self,
        repo: Path,
        supervisor_id: str,
        backend: WorkerBackend,
        *,
        reviewer: Reviewer | None = None,
        state_root: Path = STATE_ROOT,
        poll_seconds: float = 1.0,
    ) -> None:
        self.repo = repo.resolve()
        self.supervisor_id = supervisor_id
        self.state_root = state_root
        self.root = _supervisor_root(state_root, self.repo, supervisor_id)
        self.store = StateStore(self.root, state_root)
        self.project = self.store.read_project()
        if self.project.get("supervisor_id") != supervisor_id:
            raise SupervisorError("supervisor_id does not match project.json")
        self.graph = _load_graph(self.repo, self.project["graph_id"], state_root)
        if self.project.get("graph_digest") != _canonical_digest(self.graph):
            raise SupervisorError("graph.json changed after supervisor initialization")
        self.nodes = {
            node["id"]: node
            for node in self.graph.get("nodes", [])
            if isinstance(node, dict) and node.get("kind") == "task"
        }
        expected_nodes = sorted(self.nodes)
        if sorted(self.project.get("task_nodes", [])) != expected_nodes:
            raise SupervisorError("project task_nodes do not match graph.json")
        self.tasks = self.store.read_tasks(expected_nodes)
        self.backend = backend
        self.reviewer = reviewer or DeterministicReviewer()
        self.poll_seconds = poll_seconds
        if poll_seconds < 0:
            raise SupervisorError("poll_seconds cannot be negative")
        self.active: dict[str, WorkerHandle] = {}

    def _runtime_graph(self) -> dict[str, Any]:
        graph = copy.deepcopy(self.graph)
        for node in graph["nodes"]:
            if not isinstance(node, dict) or node.get("kind") != "task":
                continue
            task = self.tasks[node["id"]]
            node["status"] = "pending" if task["status"] in {"pending", "retry"} else task["status"]
        return graph

    def _project_status(self) -> str:
        statuses = [task["status"] for task in self.tasks.values()]
        if statuses and all(status == "success" for status in statuses):
            return "success"
        if any(status == "failed" for status in statuses):
            return "failed"
        if any(status == "blocked" for status in statuses):
            return "blocked"
        if any(status == "running" for status in statuses):
            return "running"
        return "pending"

    def _commit(
        self,
        event_type: str | None = None,
        *,
        node_id: str | None = None,
        attempt: int | None = None,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> None:
        self.store.commit(
            self.project,
            self.tasks,
            event_type=event_type,
            node_id=node_id,
            attempt=attempt,
            data=data,
            event_id=event_id,
        )

    def _write_attempt(self, attempt: dict[str, Any]) -> None:
        atomic_write_json(self.store.attempt_path(attempt["node_id"], attempt["attempt"]), attempt)

    def _read_prompt(self, task_id: str, attempt: int, feedback: str | None) -> str:
        paths = _task_paths(self.state_root, self.repo, task_id)
        try:
            prompt = paths["prompt"].read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SupervisorError(f"cannot read executor prompt: {paths['prompt']}: {exc}") from exc
        if not prompt:
            raise SupervisorError(f"executor prompt is empty: {paths['prompt']}")
        directives = (
            "\n\nDURABLE SUPERVISOR CONTRACT:\n"
            f"- This is attempt {attempt}; task id is {task_id}.\n"
            f"- Read HANDOFF at {paths['handoff'].resolve()} and write the strict terminal result to {paths['result'].resolve()}.\n"
            "- Herdr idle/done/blocked and process exit are lifecycle evidence only; semantic success requires a valid result and independent review.\n"
            "- At terminal completion remove runner_sentinel, set HANDOFF status, and replace the provisional result.\n"
            "- Do not write credentials, tokens, cookies, or raw authentication output to any state file.\n"
        )
        if feedback:
            directives += "- Previous attempt feedback: " + _safe_text(feedback, 4000) + "\n"
        return prompt + directives

    def _journal_worker(self, attempt: dict[str, Any], values: dict[str, str]) -> None:
        worker = attempt.setdefault("worker", {})
        worker.update(values)
        self._write_attempt(attempt)
        self._commit(
            "worker_resource_journaled",
            node_id=attempt["node_id"],
            attempt=attempt["attempt"],
            data={"fields": sorted(values)},
            event_id=f"worker_resource_journaled:{attempt['node_id']}:{attempt['attempt']}:{','.join(sorted(values))}",
        )

    def _start(self, node_id: str) -> None:
        task_state = self.tasks[node_id]
        node = self.nodes[node_id]
        attempt_number = int(task_state["attempts"]) + 1
        task_state.update(
            {
                "status": "running",
                "attempts": attempt_number,
                "active_attempt": attempt_number,
            }
        )
        attempt = {
            "schema": "wp-supervisor-attempt-v1",
            "node_id": node_id,
            "task_id": node["task_id"],
            "attempt": attempt_number,
            "status": "spawning",
            "started_at": now_iso(),
            "finished_at": None,
            "feedback": task_state.get("feedback"),
            "worker": {},
            "lifecycle_status": None,
            "result_path": None,
            "review_path": None,
            "result_errors": [],
            "error": None,
        }
        self._write_attempt(attempt)
        task_paths = _task_paths(self.state_root, self.repo, node["task_id"])
        invocation_id = f"{self.supervisor_id}-{node_id}-attempt-{attempt_number}"
        provisional = synthesize_failure(
            node["task_id"],
            invocation_id,
            "Herdr worker started but has not produced terminal artifacts.",
            _state_relative(self.store.attempt_path(node_id, attempt_number), self.state_root),
            "started",
            None,
            "task_failure",
            handoff_path=_expected_handoff(self.state_root, self.repo, node["task_id"]),
        )
        atomic_write_json(task_paths["result"], provisional)
        mark_handoff_started(task_paths["handoff"], invocation_id, attempt_number, "herdr-pi")
        self._commit(
            "attempt_started",
            node_id=node_id,
            attempt=attempt_number,
            data={"max_attempts": self.project["max_attempts"]},
            event_id=f"attempt_started:{node_id}:{attempt_number}",
        )
        try:
            prompt = self._read_prompt(node["task_id"], attempt_number, task_state.get("feedback"))
            handle = self.backend.spawn(
                {**node, "node_id": node_id},
                attempt_number,
                prompt,
                task_state.get("feedback"),
                lambda values: self._journal_worker(attempt, values),
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # transport failures become a bounded retry
            attempt["status"] = "interrupted"
            attempt["error"] = _safe_text(exc)
            attempt["finished_at"] = now_iso()
            self._write_attempt(attempt)
            self._commit(
                "worker_spawn_failed",
                node_id=node_id,
                attempt=attempt_number,
                data={"error": attempt["error"]},
                event_id=f"worker_spawn_failed:{node_id}:{attempt_number}",
            )
            self._finish(node_id, attempt, "unknown", [f"worker spawn failed: {attempt['error']}"])
            return
        attempt["worker"] = {"agent_id": handle.worker_id, **handle.metadata}
        attempt["status"] = "waiting"
        self._write_attempt(attempt)
        self._commit(
            "worker_spawned",
            node_id=node_id,
            attempt=attempt_number,
            data={"worker_id": handle.worker_id, "resources": sorted(handle.metadata)},
            event_id=f"worker_spawned:{node_id}:{attempt_number}",
        )
        self.active[node_id] = handle

    def _recover(self) -> None:
        for node_id, task in self.tasks.items():
            if task["status"] != "running":
                continue
            active_attempt = task.get("active_attempt")
            if not isinstance(active_attempt, int):
                raise SupervisorError(f"running task has no active_attempt: {node_id}")
            attempt = self.store.read_attempt(node_id, active_attempt)
            if attempt.get("status") in {"reviewed", "passed", "retry", "blocked", "failed"}:
                self._apply_decision(node_id, attempt)
                continue
            handle = self.backend.recover(attempt)
            if handle is None:
                self._finish(node_id, attempt, "unknown", ["controller restarted and worker could not be recovered"])
                continue
            self.active[node_id] = handle
            self._commit(
                "attempt_recovered",
                node_id=node_id,
                attempt=active_attempt,
                data={"worker_id": handle.worker_id},
                event_id=f"attempt_recovered:{node_id}:{active_attempt}",
            )

    def _read_worker_result(self, task_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        result_path = _task_paths(self.state_root, self.repo, task_id)["result"]
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, [f"result.json is missing: {result_path}"]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, [f"result.json is malformed: {_safe_text(exc)}"]
        if not isinstance(raw, dict):
            return None, ["result.json must be a JSON object"]
        errors = validate_terminal_artifacts(
            raw,
            _task_paths(self.state_root, self.repo, task_id)["handoff"],
            task_id,
            _expected_handoff(self.state_root, self.repo, task_id),
        )
        extra = sorted(set(raw) - RESULT_FIELDS)
        if extra:
            errors.append("result.json has unsupported fields: " + ", ".join(extra))
        return raw, errors

    def _review(
        self,
        task_node: dict[str, Any],
        attempt: dict[str, Any],
        result: dict[str, Any] | None,
        result_errors: list[str],
        lifecycle_status: str,
    ) -> dict[str, Any]:
        try:
            verdict = self.reviewer.review(
                task_node,
                attempt["attempt"],
                result,
                result_errors,
                lifecycle_status,
            )
        except Exception as exc:
            verdict = {
                "schema": REVIEW_SCHEMA,
                "task_id": task_node["task_id"],
                "attempt": attempt["attempt"],
                "verdict": "RETRY",
                "summary": "Reviewer failed closed; bounded retry is required.",
                "findings": [f"reviewer error: {_safe_text(exc)}"],
                "checked": ["reviewer-contract"],
                "reviewer": "supervisor-reviewer-failed-closed",
            }
        review_errors = validate_review(verdict, task_node["task_id"], attempt["attempt"])
        if review_errors:
            verdict = {
                "schema": REVIEW_SCHEMA,
                "task_id": task_node["task_id"],
                "attempt": attempt["attempt"],
                "verdict": "RETRY",
                "summary": "Reviewer verdict failed strict schema validation.",
                "findings": [_safe_text(error) for error in review_errors],
                "checked": ["reviewer-contract"],
                "reviewer": "supervisor-reviewer-failed-closed",
            }
        return verdict

    def _feedback(self, attempt: dict[str, Any], review: dict[str, Any]) -> str:
        pieces = [review.get("summary", "retry requested")]
        pieces.extend(review.get("findings", []))
        pieces.extend(attempt.get("result_errors", []))
        return _safe_text("; ".join(str(piece) for piece in pieces), 2000)

    def _apply_decision(self, node_id: str, attempt: dict[str, Any]) -> None:
        if attempt.get("status") in {"passed", "retry", "blocked", "failed"}:
            self.tasks[node_id]["status"] = attempt["status"] if attempt["status"] != "passed" else "success"
            self.tasks[node_id]["active_attempt"] = None
            return
        review_path = self.store.review_path(node_id, attempt["attempt"])
        result_path = self.store.result_path(node_id, attempt["attempt"])
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            review = {
                "schema": REVIEW_SCHEMA,
                "task_id": attempt["task_id"],
                "attempt": attempt["attempt"],
                "verdict": "RETRY",
                "summary": "Review artifact is unavailable; retry is required.",
                "findings": [_safe_text(exc)],
                "checked": ["reviewer-contract"],
                "reviewer": "supervisor-reviewer-failed-closed",
            }
        result = None
        try:
            collected = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(collected, dict):
                result = collected.get("result")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        valid_pass = (
            validate_review(review, attempt["task_id"], attempt["attempt"]) == []
            and review.get("verdict") == "PASS"
            and isinstance(result, dict)
            and not attempt.get("result_errors")
            and result.get("status") == "success"
        )
        if valid_pass:
            outcome = "success"
            attempt_status = "passed"
            feedback = None
        elif isinstance(result, dict) and result.get("status") == "blocked" and not attempt.get("result_errors"):
            outcome = "blocked"
            attempt_status = "blocked"
            feedback = None
        elif attempt["attempt"] < int(self.project["max_attempts"]):
            outcome = "retry"
            attempt_status = "retry"
            feedback = self._feedback(attempt, review)
        else:
            outcome = "failed"
            attempt_status = "failed"
            feedback = self._feedback(attempt, review)
        attempt["status"] = attempt_status
        attempt["finished_at"] = attempt.get("finished_at") or now_iso()
        attempt["review_verdict"] = review.get("verdict")
        self._write_attempt(attempt)
        task = self.tasks[node_id]
        task["status"] = outcome
        task["active_attempt"] = None
        task["feedback"] = feedback
        task["last_result_path"] = _state_relative(result_path, self.state_root)
        task["last_review_path"] = _state_relative(review_path, self.state_root)
        self._commit(
            "task_decided",
            node_id=node_id,
            attempt=attempt["attempt"],
            data={"outcome": outcome, "review_verdict": review.get("verdict"), "feedback": bool(feedback)},
            event_id=f"task_decided:{node_id}:{attempt['attempt']}",
        )

    def _finish(
        self,
        node_id: str,
        attempt: dict[str, Any],
        lifecycle_status: str,
        extra_errors: list[str] | None = None,
        handle: WorkerHandle | None = None,
    ) -> None:
        attempt["status"] = "collecting"
        attempt["lifecycle_status"] = lifecycle_status
        self._write_attempt(attempt)
        self._commit(
            "worker_lifecycle_terminal",
            node_id=node_id,
            attempt=attempt["attempt"],
            data={"lifecycle_status": lifecycle_status},
            event_id=f"worker_lifecycle_terminal:{node_id}:{attempt['attempt']}",
        )
        errors = list(extra_errors or [])
        try:
            self.backend.collect(handle, attempt)
        except Exception as exc:
            errors.append(f"worker collection failed: {_safe_text(exc)}")
        result, contract_errors = self._read_worker_result(attempt["task_id"])
        errors.extend(contract_errors)
        attempt["result_errors"] = [_safe_text(error) for error in errors]
        collected = {
            "schema": COLLECTED_RESULT_SCHEMA,
            "task_id": attempt["task_id"],
            "attempt": attempt["attempt"],
            "lifecycle_status": lifecycle_status,
            "collected_at": now_iso(),
            "result": result,
            "result_errors": attempt["result_errors"],
        }
        collected_path = self.store.result_path(node_id, attempt["attempt"])
        atomic_write_json(collected_path, collected)
        if validate_collected_result(collected, attempt["task_id"], attempt["attempt"]):
            raise SupervisorError("internal collected-result contract violation")
        attempt["result_path"] = _state_relative(collected_path, self.state_root)
        attempt["status"] = "reviewed"
        self._write_attempt(attempt)
        self._commit(
            "result_collected",
            node_id=node_id,
            attempt=attempt["attempt"],
            data={"valid": not errors, "error_count": len(errors)},
            event_id=f"result_collected:{node_id}:{attempt['attempt']}",
        )
        review = self._review(self.nodes[node_id] | {"node_id": node_id}, attempt, result, errors, lifecycle_status)
        review_path = self.store.review_path(node_id, attempt["attempt"])
        atomic_write_json(review_path, review)
        attempt["review_path"] = _state_relative(review_path, self.state_root)
        self._write_attempt(attempt)
        self._commit(
            "review_verdict_recorded",
            node_id=node_id,
            attempt=attempt["attempt"],
            data={"verdict": review["verdict"], "reviewer": review["reviewer"]},
            event_id=f"review_verdict_recorded:{node_id}:{attempt['attempt']}",
        )
        self._apply_decision(node_id, attempt)

    def _poll_one(self) -> bool:
        for node_id, handle in list(self.active.items()):
            poll_error: str | None = None
            try:
                lifecycle = self.backend.poll(handle)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                lifecycle = "unknown"
                poll_error = f"worker wait failed: {_safe_text(exc)}"
            if lifecycle is None:
                continue
            del self.active[node_id]
            attempt_number = self.tasks[node_id]["active_attempt"]
            if not isinstance(attempt_number, int):
                raise SupervisorError(f"active task lost attempt number: {node_id}")
            attempt = self.store.read_attempt(node_id, attempt_number)
            self._finish(
                node_id,
                attempt,
                lifecycle,
                [poll_error] if poll_error else None,
                handle=handle,
            )
            return True
        return False

    def run(self) -> dict[str, Any]:
        lock = SupervisorLock(self.root / "run.lock")
        with lock:
            self._recover()
            try:
                while True:
                    capacity = int(self.project["max_parallel"]) - len(self.active)
                    if capacity > 0:
                        runtime = self._runtime_graph()
                        ready = [
                            node_id
                            for node_id in sorted(
                                node_id
                                for node_id in self.nodes
                                if self.tasks[node_id]["status"] in {"pending", "retry"}
                            )
                            if all(
                                runtime_node_status(runtime, dep, self.tasks) == "success"
                                for dep in effective_dependencies(runtime, node_id)
                            )
                        ]
                        batch = plan_batch(ready, runtime, min(capacity, len(ready))) if ready else []
                        for node_id in batch:
                            self._start(node_id)
                    if self.active:
                        if self._poll_one():
                            continue
                        if self.poll_seconds:
                            time.sleep(self.poll_seconds)
                        continue
                    status = self._project_status()
                    self.project["status"] = status
                    self._commit(
                        "supervisor_finished",
                        data={"status": status},
                        event_id=f"supervisor_finished:{status}",
                    )
                    return self.summary()
            except KeyboardInterrupt:
                self.project["status"] = "running"
                self._commit("controller_interrupted", data={"active": sorted(self.active)})
                raise

    def summary(self) -> dict[str, Any]:
        return {
            "supervisor_id": self.supervisor_id,
            "status": self.project.get("status"),
            "task_statuses": {node_id: self.tasks[node_id]["status"] for node_id in sorted(self.tasks)},
            "attempts": {node_id: self.tasks[node_id]["attempts"] for node_id in sorted(self.tasks)},
            "active": sorted(self.active),
            "state_dir": _state_relative(self.root, self.state_root),
            "project": _state_relative(self.store.project_path, self.state_root),
        }


def runtime_node_status(graph: dict[str, Any], node_id: str, tasks: dict[str, dict[str, Any]]) -> str:
    node = next((node for node in graph.get("nodes", []) if isinstance(node, dict) and node.get("id") == node_id), None)
    if node is None:
        return "missing"
    return node.get("status", tasks.get(node_id, {}).get("status", "pending"))


def load_supervisor_summary(repo: Path, supervisor_id: str, state_root: Path = STATE_ROOT) -> dict[str, Any]:
    root = _supervisor_root(state_root, repo.resolve(), supervisor_id)
    store = StateStore(root, state_root)
    project = store.read_project()
    tasks = store.read_tasks(project["task_nodes"])
    return {
        "supervisor_id": supervisor_id,
        "status": project.get("status"),
        "task_statuses": {node_id: tasks[node_id]["status"] for node_id in sorted(tasks)},
        "state_dir": _state_relative(root, state_root),
        "project": _state_relative(store.project_path, state_root),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a durable supervisor state package")
    init.add_argument("--repo", type=Path, default=Path.cwd())
    init.add_argument("--graph-id", required=True)
    init.add_argument("--supervisor-id", required=True)
    init.add_argument("--max-parallel", type=int, default=3)
    init.add_argument("--max-attempts", type=int, default=3)

    run = sub.add_parser("run", help="resume or run the durable supervisor")
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument("--supervisor-id", required=True)
    run.add_argument("--controller-tab-id", required=True)
    run.add_argument("--herdr-bin")
    run.add_argument("--wait-timeout-ms", type=int, default=250)
    run.add_argument("--poll-seconds", type=float, default=1.0)

    check = sub.add_parser("check", help="read durable supervisor state")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    check.add_argument("--supervisor-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo = args.repo.resolve()
        assert_isolated_execution_root(repo)
        if args.command == "init":
            output = initialize_supervisor(
                repo,
                args.graph_id,
                args.supervisor_id,
                max_parallel=args.max_parallel,
                max_attempts=args.max_attempts,
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "check":
            print(json.dumps(load_supervisor_summary(repo, args.supervisor_id), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if os.environ.get("HERDR_ENV") != "1":
            raise SupervisorError("HERDR_ENV must be exactly 1 for the Herdr execution plane")
        store_root = _supervisor_root(STATE_ROOT, repo, args.supervisor_id)
        project = StateStore(store_root, STATE_ROOT).read_project()
        if project.get("backend") != "herdr":
            raise SupervisorError("project backend is not herdr")
        backend = HerdrBackend(
            repo,
            STATE_ROOT,
            args.controller_tab_id,
            binary=args.herdr_bin,
            wait_timeout_ms=args.wait_timeout_ms,
        )
        supervisor = DurableSupervisor(
            repo,
            args.supervisor_id,
            backend,
            poll_seconds=args.poll_seconds,
        )
        print(json.dumps(supervisor.run(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if supervisor.project.get("status") == "success" else 3 if supervisor.project.get("status") == "blocked" else 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "resume": "rerun the same durable_supervisor.py run command"}), file=sys.stderr)
        return 130
    except (OSError, ValueError, SupervisorError, BridgeError, CommandError, ProtocolError, HerdrTimeout) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": _safe_text(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

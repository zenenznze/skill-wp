#!/usr/bin/env python3
"""Run a resumable native Codex Goal through the App Server JSONL protocol."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from codex_app_server import (
    AppServerError,
    AppServerRequestTimeout,
    AppServerRpcError,
    CodexAppServer,
    inherited_environment,
)
from budget_checkpoints import (
    BudgetCheckpointController,
    new_checkpoint_state,
    protocol_evidence,
    validate_checkpoint_state,
)
from protocol import (
    assert_isolated_execution_root,
    handoff_display_path,
    resolve_task_path,    atomic_write_json,
    mark_handoff_blocked,
    mark_handoff_failed,
    mark_handoff_started,
    now_iso,
    read_json,
    task_relative_paths,
    update_handoff_frontmatter,
    validate_frontmatter_scalar,
    validate_terminal_artifacts,
)


TERMINAL_GOAL_STATUSES = {
    "paused",
    "blocked",
    "usageLimited",
    "budgetLimited",
    "complete",
}
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "xhigh"
MAX_MODEL_LIST_PAGES = 32
THREAD_REASONING_CONFIG_KEY = "model_reasoning_effort"
REQUEST_USER_INPUT_POLICY = (
    "Do not wait for interactive user input. Resolve this from HANDOFF, repository "
    "evidence, existing conventions, or the safest reversible default. Record the "
    "decision. If no defensible default exists and external input is strictly "
    "required, document the exact blocker and mark the active goal blocked."
)


class RunnerInterrupted(Exception):
    pass


class GoalStalled(RuntimeError):
    pass


class GoalTimeout(GoalStalled):
    """Raised only when the overall authorized execution window expires."""


class ModelSelectionError(ValueError):
    """Raised when the native model catalog cannot satisfy the request."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=86400)
    parser.add_argument("--idle-timeout-seconds", type=float, default=900)
    parser.add_argument("--request-timeout-seconds", type=float, default=60)
    parser.add_argument("--token-budget", type=int)
    parser.add_argument(
        "--execution-mode",
        choices=("bounded", "sustained"),
        default="bounded",
        help="select bounded behavior or the sustained timebox policy",
    )
    parser.add_argument("--resume", action="store_true", help="resume the latest prior thread")
    parser.add_argument("--revision", type=Path, help="repository-relative verifier note")
    return parser.parse_args()


def relative_to_repo(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().relative_to(Path(__file__).resolve().parent.parent).as_posix()


def failure_result(
    task_id: str,
    invocation_id: str,
    summary: str,
    stderr_log: str,
    transport_state: str,
    exit_code: int | None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "failed",
        "summary": summary,
        "handoff_path": os.environ.get("WP_HANDOFF_RELATIVE", f"wp-state/tasks/{task_id}/HANDOFF.md"),
        "changed_files": [],
        "validation": [],
        "blocker": None,
        "recommended_next_action": "inspect_executor_failure",
        "producer": "runner-synthesized",
        "transport": {
            "invocation_id": invocation_id,
            "executor": "codex-goal",
            "state": transport_state,
            "exit_code": exit_code,
            "stderr_log": stderr_log,
        },
    }


def provisional_result(
    task_id: str, invocation_id: str, stderr_log: str
) -> dict[str, Any]:
    value = failure_result(
        task_id,
        invocation_id,
        "Codex Goal started but has not produced terminal artifacts.",
        stderr_log,
        "started",
        None,
    )
    value["producer"] = "runner-provisional"
    return value


def resume_command(
    task_id: str,
    attempt: int,
    token_budget: bool = False,
    sustained: bool = False,
) -> str:
    command = (
        "python3 <skill-root>/scripts/run_task.py --repo . "
        f"--task-id {task_id} --agent codex --attempt {attempt} --resume"
    )
    if sustained:
        command += " --sustained-goal"
    if token_budget:
        command += " --token-budget <authorized-total-budget>"
    return command


def blocked_result(
    task_id: str,
    invocation_id: str,
    goal_status: str,
    goal_path: str,
    next_attempt: int,
    sustained: bool = False,
) -> tuple[dict[str, Any], str]:
    needs_budget = goal_status == "budgetLimited"
    command = resume_command(
        task_id,
        next_attempt,
        token_budget=needs_budget,
        sustained=sustained,
    )
    if sustained:
        if not needs_budget:
            command += " --token-budget <authorized-total-budget>"
        command += " --no-time-pressure --timeout-seconds <next-window>"
    if goal_status == "usageLimited":
        unblock = f"Wait for the usage limit to reset, then run: {command}"
    elif goal_status == "budgetLimited":
        unblock = (
            "The controller Agent must review progress and explicitly authorize a total token budget, "
            f"then run: {command}"
        )
    elif goal_status == "paused":
        unblock = f"The controller Agent must review why the Goal paused, then run: {command}"
    else:
        unblock = f"Resolve the external blocker recorded by Codex, then run: {command}"
    result = {
        "task_id": task_id,
        "status": "blocked",
        "summary": f"Codex Goal reached resumable status {goal_status}.",
        "handoff_path": os.environ.get("WP_HANDOFF_RELATIVE", f"wp-state/tasks/{task_id}/HANDOFF.md"),
        "changed_files": [],
        "validation": [],
        "blocker": {
            "where": f"native Codex Goal status {goal_status}",
            "attempted": "The Goal continued until App Server reported a resumable terminal state.",
            "evidence": goal_path,
            "unblock_action": unblock,
            "resume_from": command,
        },
        "recommended_next_action": "resume_same_codex_thread",
        "producer": "runner-synthesized",
        "transport": {
            "invocation_id": invocation_id,
            "executor": "codex-goal",
            "state": goal_status,
        },
    }
    return result, command


def timebox_blocked_result(
    task_id: str,
    invocation_id: str,
    attempt: int,
    token_budget: int,
    timeout_seconds: float,
    evidence: str,
) -> tuple[dict[str, Any], str]:
    """Build the resumable result for an authorized sustained timebox expiry."""
    command = resume_command(task_id, attempt + 1, sustained=True)
    command += (
        " --no-time-pressure "
        f"--token-budget {token_budget} --timeout-seconds {timeout_seconds:g}"
    )
    summary = "Sustained Codex Goal reached the authorized overall timebox."
    result = {
        "task_id": task_id,
        "status": "blocked",
        "summary": summary,
        "handoff_path": os.environ.get("WP_HANDOFF_RELATIVE", f"wp-state/tasks/{task_id}/HANDOFF.md"),
        "changed_files": [],
        "validation": [],
        "blocker": {
            "where": "sustained timebox expired (overall timeout)",
            "attempted": (
                "The native Goal continued on the same thread until the authorized "
                "overall execution window expired."
            ),
            "evidence": evidence,
            "unblock_action": (
                "The controller Agent must review progress and authorize the next timebox, then run: "
                f"{command}"
            ),
            "resume_from": command,
        },
        "recommended_next_action": "resume_same_codex_thread",
        "producer": "runner-synthesized",
        "transport": {
            "invocation_id": invocation_id,
            "executor": "codex-goal",
            "state": "sustained_timebox_expired",
        },
    }
    return result, command


def latest_thread(run_root: Path, before_attempt: int) -> dict[str, Any] | None:
    candidates: list[tuple[int, Path]] = []
    if run_root.is_dir():
        for directory in run_root.glob("attempt-*"):
            match = re.fullmatch(r"attempt-(\d+)", directory.name)
            if match and int(match.group(1)) < before_attempt:
                candidates.append((int(match.group(1)), directory / "thread.json"))
    for _, path in sorted(candidates, reverse=True):
        try:
            value = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("thread_id"), str):
            if not isinstance(value.get("goal_status"), str):
                try:
                    goal_value = read_json(path.parent / "goal.json")
                except (OSError, json.JSONDecodeError):
                    goal_value = None
                if isinstance(goal_value, dict) and isinstance(
                    goal_value.get("status"), str
                ):
                    value["goal_status"] = goal_value["status"]
            return value
    return None


def _model_matches(candidate: Any, requested: str) -> bool:
    if not isinstance(candidate, dict):
        return False
    return any(candidate.get(key) == requested for key in ("id", "model"))


def _reasoning_efforts(model: dict[str, Any]) -> list[str]:
    advertised = model.get("supportedReasoningEfforts")
    if advertised is None:
        advertised = model.get("reasoningEfforts")
    if not isinstance(advertised, list):
        return []
    efforts: list[str] = []
    for item in advertised:
        value: Any = item
        if isinstance(item, dict):
            value = item.get("reasoningEffort")
            if value is None:
                value = item.get("reasoning_effort")
            if value is None:
                value = item.get("effort")
        if isinstance(value, str) and value and value not in efforts:
            efforts.append(value)
    return efforts


def _model_list_page(
    active: CodexAppServer,
    request: Any,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    response = request(active, "model/list", params)
    models = response.get("data")
    if not isinstance(models, list):
        raise ModelSelectionError("model/list returned no data array")
    normalized_models = [model for model in models if isinstance(model, dict)]
    if len(normalized_models) != len(models):
        raise ModelSelectionError("model/list returned an invalid model entry")
    next_cursor = response.get("nextCursor")
    if next_cursor is None:
        next_cursor = response.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise ModelSelectionError("model/list returned an invalid next cursor")
    return normalized_models, next_cursor or None


def discover_model(
    active: CodexAppServer,
    request: Any,
    model: str,
    reasoning_effort: str,
) -> tuple[dict[str, Any], int]:
    if not model.strip():
        raise ModelSelectionError("requested Codex model must not be empty")
    if not reasoning_effort.strip():
        raise ModelSelectionError("requested reasoning effort must not be empty")

    pages = 0
    cursor: str | None = None
    seen_cursors: set[str] = set()
    selected: dict[str, Any] | None = None
    while pages < MAX_MODEL_LIST_PAGES:
        page_models, next_cursor = _model_list_page(active, request, cursor)
        pages += 1
        for candidate in page_models:
            if _model_matches(candidate, model):
                selected = candidate
                break
        if selected is not None:
            break
        if next_cursor is None:
            break
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise ModelSelectionError("model/list returned a repeated pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ModelSelectionError(
            f"model/list exceeded the {MAX_MODEL_LIST_PAGES}-page discovery limit"
        )

    if selected is None:
        raise ModelSelectionError(
            f"requested Codex model '{model}' is not advertised by model/list"
        )
    efforts = _reasoning_efforts(selected)
    if reasoning_effort not in efforts:
        supported = ", ".join(efforts) if efforts else "none"
        raise ModelSelectionError(
            f"reasoning effort '{reasoning_effort}' is not advertised for model "
            f"'{model}' (supported: {supported})"
        )
    return selected, pages


def server_request_handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "item/tool/requestUserInput":
        answers = {
            question["id"]: {"answers": [REQUEST_USER_INPUT_POLICY]}
            for question in params.get("questions", [])
            if isinstance(question, dict) and isinstance(question.get("id"), str)
        }
        return {"answers": answers}
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "decline"}
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {
            "decision": {
                "denied": {
                    "rejection": "The unattended runner never expands executor permissions."
                }
            }
        }
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    raise ValueError(f"unsupported App Server request: {method}")


def goal_snapshot(goal_id: str, goal: dict[str, Any]) -> dict[str, Any]:
    return {"goal_id": goal_id, **goal}


def goal_updated_at(goal: dict[str, Any] | None) -> float | None:
    if not isinstance(goal, dict):
        return None
    value = goal.get("updatedAt")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def stale_goal_event(goal: dict[str, Any], minimum_updated_at: float | None) -> bool:
    updated_at = goal_updated_at(goal)
    return (
        minimum_updated_at is not None
        and updated_at is not None
        and updated_at < minimum_updated_at
    )


def terminal_artifact_errors(
    result_path: Path,
    handoff: Path,
    task_id: str,
    goal_status: str,
    expected_handoff_path: str,
) -> tuple[Any, list[str]]:
    try:
        result = read_json(result_path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"result.json is missing or malformed: {exc}"]
    if isinstance(result, dict) and result.get("producer") == "runner-provisional":
        return result, ["Codex did not replace the provisional result"]
    errors = validate_terminal_artifacts(
        result, handoff, task_id, expected_handoff_path
    )
    expected_status = "success" if goal_status == "complete" else "blocked"
    if isinstance(result, dict) and result.get("status") != expected_status:
        errors.append(
            f"Goal status {goal_status} requires task result status {expected_status}"
        )
    return result, errors


def archive_invalid(result_path: Path, archive_path: Path) -> None:
    if result_path.exists():
        try:
            shutil.copyfile(result_path, archive_path)
        except OSError:
            pass


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    assert_isolated_execution_root(repo)
    if not repo.is_dir():
        raise ValueError(f"repository does not exist: {repo}")
    if (
        args.attempt < 1
        or args.timeout_seconds <= 0
        or args.idle_timeout_seconds <= 0
        or args.request_timeout_seconds <= 0
    ):
        raise ValueError("attempt and timeout values must be positive")
    if args.token_budget is not None and args.token_budget <= 0:
        raise ValueError("token-budget must be positive")
    if args.execution_mode == "sustained" and args.token_budget is None:
        raise ValueError("sustained mode requires --token-budget")
    validate_frontmatter_scalar("--model", args.model)
    validate_frontmatter_scalar("--reasoning-effort", args.reasoning_effort)

    paths = task_relative_paths(args.task_id, repo)
    handoff = repo / paths["handoff"]
    goal_path = repo / paths["codex_goal"]
    result_path = paths["result"]
    os.environ["WP_HANDOFF_RELATIVE"] = handoff_display_path(repo, args.task_id)
    for required in (handoff, goal_path):
        if not required.is_file():
            raise ValueError(f"required task file does not exist: {required}")
    objective = goal_path.read_text(encoding="utf-8").strip()
    if not objective or len(objective) > 4000:
        raise ValueError("CODEX_GOAL.txt must contain 1 to 4000 characters")

    revision_instruction = ""
    if args.revision:
        revision_path = resolve_task_path(args.revision, repo)
        task_root = (repo / paths["task_dir"]).resolve()
        if task_root not in revision_path.parents or not revision_path.is_file():
            raise ValueError("--revision must be an existing file inside the task directory")
        revision_instruction = (
            " Also read the focused verifier note at "
            f"{relative_to_repo(revision_path, repo)}; HANDOFF remains authoritative."
        )

    run_root = repo / paths["run_root"]
    prior_thread = latest_thread(run_root, args.attempt) if args.resume else None
    if args.resume and prior_thread is None:
        raise ValueError("--resume requested but no prior resumable thread.json exists")
    if prior_thread is not None and prior_thread.get("model") != args.model:
        raise ValueError("--model must match the persisted thread model when resuming")
    if prior_thread is not None and prior_thread.get("reasoning_effort") != args.reasoning_effort:
        raise ValueError(
            "--reasoning-effort must match the persisted thread reasoning effort when resuming"
        )
    if (
        prior_thread is not None
        and prior_thread.get("execution_mode", "bounded") != args.execution_mode
    ):
        raise ValueError(
            "--execution-mode must match the persisted thread execution mode when resuming"
        )
    if (
        prior_thread is not None
        and prior_thread.get("resumable") is False
        and not revision_instruction
    ):
        raise ValueError("the latest thread is complete; --resume requires --revision")
    if (
        prior_thread is not None
        and prior_thread.get("goal_status") == "budgetLimited"
        and args.token_budget is None
    ):
        raise ValueError("resuming a budget-limited Goal requires --token-budget")

    prior_authorized_budget = prior_thread.get("authorized_token_budget") if prior_thread else None
    if prior_authorized_budget is not None and (
        not isinstance(prior_authorized_budget, int) or isinstance(prior_authorized_budget, bool)
        or prior_authorized_budget <= 0
    ):
        raise ValueError("persisted authorized token budget is invalid")
    authorized_token_budget = (
        args.token_budget if args.token_budget is not None else prior_authorized_budget
    )

    try:
        version_process = subprocess.run(
            [args.codex_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"codex --version failed: {exc}") from exc
    if version_process.returncode != 0:
        raise ValueError(
            f"codex --version failed with exit code {version_process.returncode}"
        )

    attempt_dir = run_root / f"attempt-{args.attempt:02d}"
    if attempt_dir.exists():
        raise ValueError(f"attempt directory already exists; choose a new attempt: {attempt_dir}")
    attempt_dir.mkdir(parents=True)
    event_log = attempt_dir / "codex-app-server.jsonl"
    stderr_log = attempt_dir / "codex.stderr.log"
    invocation_path = attempt_dir / "invocation.json"
    thread_path = attempt_dir / "thread.json"
    persisted_goal_path = attempt_dir / "goal.json"
    token_usage_path = attempt_dir / "token-usage.json"
    checkpoint_state_path = paths["budget_checkpoints"]
    invalid_result_path = attempt_dir / "executor-result.invalid.json"
    invocation_id = str(uuid.uuid4())
    if prior_thread:
        goal_id = str(prior_thread.get("goal_id") or uuid.uuid4())
    else:
        goal_id = str(uuid.uuid4())
    stderr_relative = relative_to_repo(stderr_log, repo)
    goal_relative = relative_to_repo(persisted_goal_path, repo)

    requested_command = [args.codex_bin, "app-server", "--stdio"]
    command = list(requested_command)
    invocation: dict[str, Any] = {
        "invocation_id": invocation_id,
        "task_id": args.task_id,
        "attempt": args.attempt,
        "started_at": now_iso(),
        "phase": "preparing",
        "executor": "codex-goal",
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "execution_mode": args.execution_mode,
        "model_selection": "native-model-list",
        "approval_policy": "never",
        "sandbox": "workspace-write",
        "timeout_seconds": args.timeout_seconds,
        "idle_timeout_seconds": args.idle_timeout_seconds,
        "request_timeout_seconds": args.request_timeout_seconds,
        "restart_count": 0,
        "requested_command": requested_command,
        "command": command,
        "event_log": relative_to_repo(event_log, repo),
        "stderr_log": stderr_relative,
    }
    if authorized_token_budget is not None:
        invocation["authorized_token_budget"] = authorized_token_budget
    atomic_write_json(invocation_path, invocation)
    atomic_write_json(token_usage_path, {"observed": False})
    atomic_write_json(persisted_goal_path, {"goal_id": goal_id, "status": "not_set"})

    invocation["codex_version"] = version_process.stdout.strip()
    invocation["protocol_evidence"] = protocol_evidence()
    invocation["budget_checkpoint_state"] = relative_to_repo(checkpoint_state_path, repo)
    invocation["phase"] = "starting"
    atomic_write_json(invocation_path, invocation)
    atomic_write_json(
        result_path, provisional_result(args.task_id, invocation_id, stderr_relative)
    )
    update_handoff_frontmatter(
        handoff,
        {
            "executor": "codex-goal",
            "executor_model": args.model,
            "executor_effort": args.reasoning_effort,
        },
    )
    mark_handoff_started(handoff, invocation_id, args.attempt, "codex-goal")

    environment = inherited_environment(
        AGENT_TASK_ID=args.task_id,
        AGENT_HANDOFF_PATH=str(handoff),
        AGENT_HANDOFF_RELATIVE=handoff_display_path(repo, args.task_id),
        AGENT_RESULT_PATH=paths["result"].as_posix(),
        AGENT_THREAD_STATE_PATH=relative_to_repo(thread_path, repo),
        AGENT_INVOCATION_ID=invocation_id,
    )

    deadline = time.monotonic() + args.timeout_seconds
    thread_id: str | None = (
        str(prior_thread["thread_id"]) if prior_thread is not None else None
    )
    thread_context: dict[str, Any] = {}
    checkpoint_controller: BudgetCheckpointController | None = None
    final_goal: dict[str, Any] | None = None
    final_result: Any = None
    failure_summary: str | None = None
    sustained_timebox_result: dict[str, Any] | None = None
    transport_state = "starting"
    transport_exit_code: int | None = None
    idle_recovery_used = False
    artifact_repair_used = False
    stale_goal_events_ignored = 0
    client: CodexAppServer | None = None
    interrupted: list[str] = []
    old_handlers: dict[int, Any] = {}

    def on_signal(signum: int, _frame: Any) -> None:
        interrupted.append(f"runner interrupted by signal {signum}")
        raise RunnerInterrupted(interrupted[-1])

    def request(active: CodexAppServer, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return active.request(method, params, timeout=args.request_timeout_seconds)

    def connect_once(active_command: list[str]) -> CodexAppServer:
        active = CodexAppServer(
            active_command,
            repo,
            event_log,
            stderr_log,
            server_request_handler,
            environment,
        )
        try:
            active.start()
            request(
                active,
                "initialize",
                {
                    "clientInfo": {
                        "name": "pi-wp",
                        "title": "Controller Agent delegation",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            active.notify("initialized")
        except Exception:
            active.close()
            raise
        return active

    def connect() -> CodexAppServer:
        return connect_once(command)

    def persist_thread(
        resumable: bool = True, goal_status: str | None = None
    ) -> None:
        assert thread_id is not None
        value: dict[str, Any] = {
            "thread_id": thread_id,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "execution_mode": args.execution_mode,
            "goal_id": goal_id,
            "attempt": args.attempt,
            "resumable": resumable,
            "updated_at": now_iso(),
        }
        if goal_status is not None:
            value["goal_status"] = goal_status
        if authorized_token_budget is not None:
            value["authorized_token_budget"] = authorized_token_budget
        if checkpoint_controller is not None:
            value["budget_checkpoint_generation"] = checkpoint_controller.generation["generation_id"]
        value["budget_checkpoint_state"] = relative_to_repo(checkpoint_state_path, repo)
        atomic_write_json(thread_path, value)

    def load_checkpoint_state() -> dict[str, Any]:
        if not checkpoint_state_path.is_file():
            return new_checkpoint_state(args.task_id)
        try:
            value = read_json(checkpoint_state_path)
        except (OSError, json.JSONDecodeError) as exc:
            invocation["checkpoint_state_error"] = str(exc)
            atomic_write_json(invocation_path, invocation)
            raise ValueError(f"budget checkpoint state is unreadable: {exc}") from exc
        state_error = validate_checkpoint_state(value, args.task_id)
        if state_error is not None:
            invocation["checkpoint_state_error"] = state_error
            atomic_write_json(invocation_path, invocation)
            raise ValueError(state_error)
        return value

    def persist_checkpoint_state(state: dict[str, Any]) -> None:
        state["updated_at"] = now_iso()
        atomic_write_json(checkpoint_state_path, state)

    def ensure_checkpoint_controller() -> None:
        nonlocal checkpoint_controller
        if checkpoint_controller is not None or authorized_token_budget is None or thread_id is None:
            return
        state = load_checkpoint_state()
        checkpoint_controller = BudgetCheckpointController(
            state,
            args.task_id,
            thread_id,
            goal_id,
            authorized_token_budget,
            lambda: persist_checkpoint_state(state),
            now_iso,
        )
        invocation["checkpoint_generation_id"] = checkpoint_controller.generation["generation_id"]
        invocation["checkpoint_thresholds"] = checkpoint_controller.generation.get("thresholds", {})
        atomic_write_json(invocation_path, invocation)

    def set_goal(active: CodexAppServer, status: str = "active") -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "objective": objective,
            "status": status,
        }
        if authorized_token_budget is not None:
            params["tokenBudget"] = authorized_token_budget
        response = request(active, "thread/goal/set", params)
        goal = response.get("goal")
        if not isinstance(goal, dict):
            raise AppServerError("thread/goal/set returned no goal object")
        atomic_write_json(persisted_goal_path, goal_snapshot(goal_id, goal))
        return goal

    def get_goal(active: CodexAppServer) -> dict[str, Any] | None:
        response = request(active, "thread/goal/get", {"threadId": thread_id})
        goal = response.get("goal")
        if goal is None:
            return None
        if not isinstance(goal, dict):
            raise AppServerError("thread/goal/get returned an invalid goal")
        atomic_write_json(persisted_goal_path, goal_snapshot(goal_id, goal))
        return goal

    def start_turn(active: CodexAppServer, text: str) -> None:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        model = thread_context.get("model")
        if not isinstance(model, str) or not model:
            thread = thread_context.get("thread")
            if isinstance(thread, dict):
                model = thread.get("model")
        if isinstance(model, str) and model:
            settings: dict[str, Any] = {"model": model, "developer_instructions": None}
            effort = thread_context.get("reasoningEffort")
            if not isinstance(effort, str) or not effort:
                effort = thread_context.get("reasoning_effort")
            if (not isinstance(effort, str) or not effort) and isinstance(
                thread_context.get("thread"), dict
            ):
                effort = thread_context["thread"].get("reasoningEffort")
            if isinstance(effort, str) and effort:
                settings["reasoning_effort"] = effort
            params["collaborationMode"] = {"mode": "default", "settings": settings}
        try:
            request(active, "turn/start", params)
        except AppServerRpcError as exc:
            error_text = json.dumps(exc.error, ensure_ascii=False)
            if "activeTurn" not in error_text and "active turn" not in error_text.lower():
                raise

    def steer_checkpoint(instruction: str, expected_turn_id: str) -> tuple[bool, str | None]:
        if thread_id is None:
            return False, "cannot steer without an active thread id"
        active = client
        if active is None:
            return False, "cannot steer without an active App Server"
        try:
            response = request(
                active,
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": expected_turn_id,
                    "input": [{"type": "text", "text": instruction}],
                },
            )
            if not isinstance(response.get("turnId"), str) or not response["turnId"]:
                return False, "turn/steer returned an invalid turnId"
            return True, None
        except AppServerError as exc:
            return False, str(exc)

    def monitor(
        active: CodexAppServer, minimum_goal_updated_at: float | None
    ) -> dict[str, Any]:
        nonlocal idle_recovery_used, stale_goal_events_ignored
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GoalTimeout("Codex Goal exceeded the overall timeout")
            try:
                event = active.next_event(min(args.idle_timeout_seconds, remaining))
            except AppServerRequestTimeout:
                goal = get_goal(active)
                if (
                    goal is not None
                    and not stale_goal_event(goal, minimum_goal_updated_at)
                    and goal.get("status") in TERMINAL_GOAL_STATUSES
                ):
                    return goal
                if idle_recovery_used:
                    raise GoalStalled("Codex Goal remained idle after one continuation")
                idle_recovery_used = True
                start_turn(
                    active,
                    "Continue the active Goal from HANDOFF and current repository state. "
                    "Do not stop until the Goal reaches a justified terminal state.",
                )
                continue

            method = event.get("method")
            params = event.get("params")
            if not isinstance(params, dict):
                params = {}
            if method == "thread/goal/updated":
                goal = params.get("goal")
                if isinstance(goal, dict) and stale_goal_event(
                    goal, minimum_goal_updated_at
                ):
                    stale_goal_events_ignored += 1
                    invocation["stale_goal_events_ignored"] = stale_goal_events_ignored
                    atomic_write_json(invocation_path, invocation)
                    continue
                if isinstance(goal, dict):
                    atomic_write_json(
                        persisted_goal_path, goal_snapshot(goal_id, goal)
                    )
                if checkpoint_controller is not None:
                    checkpoint_result = checkpoint_controller.process(
                        params,
                        steer_checkpoint,
                    )
                    invocation["last_checkpoint_event"] = checkpoint_result
                    invocation["checkpoint_generation_id"] = (
                        checkpoint_controller.generation["generation_id"]
                    )
                    invocation["checkpoint_thresholds"] = (
                        checkpoint_controller.generation.get("thresholds", {})
                    )
                    atomic_write_json(invocation_path, invocation)
                else:
                    invocation["checkpoint_skipped"] = (
                        "no explicit or persisted positive token budget"
                    )
                    atomic_write_json(invocation_path, invocation)
                if (
                    isinstance(goal, dict)
                    and goal.get("status") in TERMINAL_GOAL_STATUSES
                ):
                    return goal
            elif method == "thread/tokenUsage/updated":
                # Raw model token totals are diagnostic only. Native Goal
                # tokensUsed/tokenBudget is the checkpoint accounting source.
                atomic_write_json(token_usage_path, {"observed": True, **params})
            elif method == "turn/completed":
                invocation["last_turn"] = params.get("turn")
                atomic_write_json(invocation_path, invocation)
                goal = get_goal(active)
                if (
                    goal is not None
                    and not stale_goal_event(goal, minimum_goal_updated_at)
                    and goal.get("status") in TERMINAL_GOAL_STATUSES
                ):
                    return goal
            elif method == "error":
                invocation["last_error"] = params
                atomic_write_json(invocation_path, invocation)
                goal = get_goal(active)
                if (
                    goal is not None
                    and not stale_goal_event(goal, minimum_goal_updated_at)
                    and goal.get("status") in TERMINAL_GOAL_STATUSES
                ):
                    return goal

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, on_signal)

    try:
        for process_attempt in range(2):
            try:
                client = connect()
                transport_state = "running"
                invocation["phase"] = "running"
                atomic_write_json(invocation_path, invocation)

                _, model_catalog_pages = discover_model(
                    client, request, args.model, args.reasoning_effort
                )
                invocation["model_catalog_pages"] = model_catalog_pages
                atomic_write_json(invocation_path, invocation)

                if thread_id is None:
                    start_params: dict[str, Any] = {
                        "cwd": str(repo),
                        "model": args.model,
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "runtimeWorkspaceRoots": [str(repo)],
                        "ephemeral": False,
                        "config": {
                            THREAD_REASONING_CONFIG_KEY: args.reasoning_effort
                        },
                    }
                    response = request(client, "thread/start", start_params)
                    thread = response.get("thread")
                    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                        raise AppServerError("thread/start returned no thread id")
                    thread_id = thread["id"]
                    thread_context = response
                    persist_thread()
                    final_goal = set_goal(client)
                else:
                    persist_thread()
                    resume_params: dict[str, Any] = {
                        "threadId": thread_id,
                        "cwd": str(repo),
                        "model": args.model,
                        "approvalPolicy": "never",
                        "sandbox": "workspace-write",
                        "runtimeWorkspaceRoots": [str(repo)],
                        "config": {
                            THREAD_REASONING_CONFIG_KEY: args.reasoning_effort
                        },
                    }
                    response = request(client, "thread/resume", resume_params)
                    thread_context = response
                    final_goal = get_goal(client)
                    should_reactivate = args.resume and process_attempt == 0
                    if final_goal is None or (
                        should_reactivate
                        and final_goal.get("status") in TERMINAL_GOAL_STATUSES
                    ):
                        final_goal = set_goal(client)
                    elif (
                        final_goal is not None
                        and prior_authorized_budget is not None
                        and args.token_budget is not None
                        and args.token_budget > prior_authorized_budget
                    ):
                        # A larger explicit budget is a new native authorization
                        # and therefore must be reflected before monitoring usage.
                        final_goal = set_goal(client)

                ensure_checkpoint_controller()
                persist_thread(
                    resumable=True,
                    goal_status=final_goal.get("status") if final_goal is not None else None,
                )

                if final_goal is None or final_goal.get("status") == "active":
                    turn_text = (
                        "Begin executing the active Goal now. Read HANDOFF completely, "
                        "implement, validate, repair failures, and finish terminal artifacts."
                        + revision_instruction
                    )
                    if process_attempt > 0:
                        turn_text = (
                            "Resume the same active Goal from HANDOFF, repository state, and "
                            "persisted thread context. Continue implementation and validation."
                        )
                    start_turn(client, turn_text)
                    final_goal = monitor(client, goal_updated_at(final_goal))

                while final_goal.get("status") == "complete":
                    final_result, errors = terminal_artifact_errors(
                        result_path,
                        handoff,
                        args.task_id,
                        "complete",
                        handoff_display_path(repo, args.task_id),
                    )
                    if not errors:
                        break
                    archive_invalid(result_path, invalid_result_path)
                    if artifact_repair_used:
                        raise GoalStalled(
                            "Goal completed twice with invalid terminal artifacts: "
                            + "; ".join(errors)
                        )
                    artifact_repair_used = True
                    final_goal = set_goal(client)
                    repair_goal_updated_at = goal_updated_at(final_goal)
                    start_turn(
                        client,
                        "The Goal was marked complete, but terminal artifacts are invalid: "
                        + "; ".join(errors)
                        + ". Only repair HANDOFF and result.json, then mark the Goal complete.",
                    )
                    final_goal = monitor(client, repair_goal_updated_at)
                break
            except (AppServerError, GoalStalled, RunnerInterrupted) as exc:
                transport_exit_code = client.return_code if client is not None else None
                if client is not None:
                    client.close()
                    client = None
                if (
                    isinstance(exc, GoalTimeout)
                    and args.execution_mode == "sustained"
                ):
                    assert args.token_budget is not None
                    if thread_id is not None:
                        persist_thread(resumable=True, goal_status="active")
                    sustained_timebox_result, command_text = timebox_blocked_result(
                        args.task_id,
                        invocation_id,
                        args.attempt,
                        args.token_budget,
                        args.timeout_seconds,
                        goal_relative,
                    )
                    atomic_write_json(result_path, sustained_timebox_result)
                    mark_handoff_blocked(
                        handoff,
                        invocation_id,
                        sustained_timebox_result["summary"],
                        goal_relative,
                        command_text,
                    )
                    transport_state = "sustained_timebox_expired"
                    break
                if (
                    isinstance(exc, AppServerError)
                    and thread_id is not None
                    and process_attempt == 0
                ):
                    invocation["restart_count"] = 1
                    invocation["last_transport_error"] = str(exc)
                    atomic_write_json(invocation_path, invocation)
                    continue
                failure_summary = str(exc)
                transport_state = (
                    "interrupted" if isinstance(exc, RunnerInterrupted) else "failed"
                )
                break
        else:
            failure_summary = "Codex App Server failed after one same-thread restart"
            transport_state = "failed"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        failure_summary = str(exc)
        transport_state = "failed"
    finally:
        if client is not None:
            transport_exit_code = client.return_code
            client.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)

    if sustained_timebox_result is not None:
        final_result = sustained_timebox_result
    elif failure_summary:
        archive_invalid(result_path, invalid_result_path)
        synthesized = failure_result(
            args.task_id,
            invocation_id,
            failure_summary,
            stderr_relative,
            transport_state,
            transport_exit_code,
        )
        atomic_write_json(result_path, synthesized)
        mark_handoff_failed(handoff, invocation_id, failure_summary, stderr_relative)
        final_result = synthesized
    elif final_goal is None:
        raise OSError("Codex Goal ended without a persisted Goal state")
    else:
        goal_status = str(final_goal.get("status"))
        transport_state = "goal_terminal"
        persist_thread(
            resumable=goal_status != "complete", goal_status=goal_status
        )
        if goal_status == "complete":
            final_result, errors = terminal_artifact_errors(
                result_path,
                handoff,
                args.task_id,
                goal_status,
                handoff_display_path(repo, args.task_id),
            )
            if errors:
                archive_invalid(result_path, invalid_result_path)
                summary = "; ".join(errors)
                final_result = failure_result(
                    args.task_id,
                    invocation_id,
                    summary,
                    stderr_relative,
                    "invalid_terminal_artifacts",
                    transport_exit_code,
                )
                atomic_write_json(result_path, final_result)
                mark_handoff_failed(handoff, invocation_id, summary, stderr_relative)
        elif goal_status == "blocked":
            final_result, errors = terminal_artifact_errors(
                result_path,
                handoff,
                args.task_id,
                goal_status,
                handoff_display_path(repo, args.task_id),
            )
            if errors:
                archive_invalid(result_path, invalid_result_path)
                summary = "blocked Goal produced invalid terminal artifacts: " + "; ".join(errors)
                final_result = failure_result(
                    args.task_id,
                    invocation_id,
                    summary,
                    stderr_relative,
                    "invalid_terminal_artifacts",
                    transport_exit_code,
                )
                atomic_write_json(result_path, final_result)
                mark_handoff_failed(handoff, invocation_id, summary, stderr_relative)
        elif goal_status in {"usageLimited", "budgetLimited", "paused"}:
            final_result, command_text = blocked_result(
                args.task_id,
                invocation_id,
                goal_status,
                goal_relative,
                args.attempt + 1,
                sustained=args.execution_mode == "sustained",
            )
            atomic_write_json(result_path, final_result)
            mark_handoff_blocked(
                handoff,
                invocation_id,
                final_result["summary"],
                goal_relative,
                command_text,
            )
        else:
            summary = f"Codex Goal stopped in unexpected status {goal_status}"
            final_result = failure_result(
                args.task_id,
                invocation_id,
                summary,
                stderr_relative,
                "unexpected_goal_status",
                transport_exit_code,
            )
            atomic_write_json(result_path, final_result)
            mark_handoff_failed(handoff, invocation_id, summary, stderr_relative)

    invocation.update(
        {
            "phase": "complete",
            "finished_at": now_iso(),
            "thread_id": thread_id,
            "goal_id": goal_id,
            "goal_status": final_goal.get("status") if final_goal else None,
            "transport_state": transport_state,
            "transport_exit_code": transport_exit_code,
            "task_status": final_result["status"],
            "artifact_repair_used": artifact_repair_used,
            "idle_recovery_used": idle_recovery_used,
            "protocol_exit_code": 0,
        }
    )
    atomic_write_json(invocation_path, invocation)
    print(
        json.dumps(
            {
                "task_id": args.task_id,
                "status": final_result["status"],
                "executor": "codex-goal",
                "thread_id": thread_id,
                "result_path": relative_to_repo(paths["result"], repo),
                "invocation_path": relative_to_repo(invocation_path, repo),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"run_codex_goal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

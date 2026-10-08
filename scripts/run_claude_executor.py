#!/usr/bin/env python3
"""Run Claude Code while guaranteeing a durable terminal task result when writable."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from protocol import (
    assert_isolated_execution_root,
    handoff_display_path,
    resolve_task_path,    atomic_write_json,
    mark_handoff_failed,
    mark_handoff_started,
    next_revision_number,
    now_iso,
    read_json,
    repair_command,
    synthesize_failure,
    task_relative_paths,
    update_handoff_frontmatter,
    validate_terminal_artifacts,
    write_revision_note,
)


ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"


class RunnerInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"runner interrupted by signal {signum}")
        self.signum = signum


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--effort", choices=("high", "max"), default="high")
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--model", help="resolved Claude model alias")
    parser.add_argument(
        "--model-source",
        choices=("override", "alias", "discovered", "preferred", "inherited"),
        default="override",
    )
    parser.add_argument("--permission-mode", choices=("dontAsk", "bypassPermissions"), default="dontAsk")
    parser.add_argument("--isolated", action="store_true", help="confirm a disposable container or VM")
    parser.add_argument("--max-budget-usd", type=float)
    parser.add_argument("--revision", type=Path, help="repository-relative focused revision note")
    return parser.parse_args()


def supports_flag(binary: str, flag: str) -> bool:
    try:
        process = subprocess.run(
            [binary, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in process.stdout or flag in process.stderr


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def provisional_result(
    task_id: str, invocation_id: str, stderr_log: str
) -> dict[str, Any]:
    value = synthesize_failure(
        task_id,
        invocation_id,
        "Claude Code invocation started but has not produced terminal artifacts.",
        stderr_log,
        "started",
        None,
        "task_failure",
    )
    value["producer"] = "runner-provisional"
    return value


def relative_to_repo(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().relative_to(ASSET_ROOT.parent.resolve()).as_posix()


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    assert_isolated_execution_root(repo)
    if args.attempt < 1 or args.max_turns < 1 or args.timeout_seconds <= 0:
        raise ValueError("attempt, max-turns, and timeout-seconds must be positive")
    if args.permission_mode == "bypassPermissions" and not args.isolated:
        raise ValueError("bypassPermissions requires --isolated for a disposable container or VM")
    if not repo.is_dir():
        raise ValueError(f"repository does not exist: {repo}")

    paths = task_relative_paths(args.task_id, repo)
    handoff = repo / paths["handoff"]
    prompt_path = repo / paths["claude_prompt"]
    settings = repo / paths["claude_settings"]
    result_path = paths["result"]
    os.environ["WP_HANDOFF_RELATIVE"] = handoff_display_path(repo, args.task_id)
    for required in (handoff, prompt_path, settings):
        if not required.is_file():
            raise ValueError(f"required task file does not exist: {required}")
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        settings_value = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read task prompt or settings: {exc}") from exc
    if not isinstance(settings_value, dict):
        raise ValueError("claude-settings.json must contain a JSON object")

    revision_path: Path | None = None
    if args.revision:
        revision_path = resolve_task_path(args.revision, repo)
        task_root = (repo / paths["task_dir"]).resolve()
        if task_root not in revision_path.parents or not revision_path.is_file():
            raise ValueError("--revision must be an existing file inside the task directory")
        prompt += (
            "\nFor this correction attempt, also read the focused verifier note at:\n"
            f"{relative_to_repo(revision_path, repo)}\n"
            "HANDOFF remains the complete task source.\n"
        )

    attempt_dir = repo / paths["run_root"] / f"attempt-{args.attempt:02d}"
    if attempt_dir.exists():
        raise ValueError(f"attempt directory already exists; choose a new attempt: {attempt_dir}")
    attempt_dir.mkdir(parents=True)

    invocation_id = str(uuid.uuid4())
    stdout_path = attempt_dir / "claude.jsonl"
    stderr_path = attempt_dir / "claude.stderr.log"
    invocation_path = attempt_dir / "invocation.json"
    stdout_relative = relative_to_repo(stdout_path, repo)
    stderr_relative = relative_to_repo(stderr_path, repo)

    invocation: dict[str, Any] = {
        "invocation_id": invocation_id,
        "task_id": args.task_id,
        "attempt": args.attempt,
        "started_at": now_iso(),
        "phase": "preparing",
        "repo": str(repo),
        "effort": args.effort,
        "permission_mode": args.permission_mode,
        "requested_model": args.model,
        "configured_model": "inherited",
        "model_source": "inherited",
        "requested_max_turns": args.max_turns,
        "timeout_seconds": args.timeout_seconds,
        "stdout_log": stdout_relative,
        "stderr_log": stderr_relative,
    }
    atomic_write_json(invocation_path, invocation)
    atomic_write_json(
        result_path, provisional_result(args.task_id, invocation_id, stderr_relative)
    )
    mark_handoff_started(handoff, invocation_id, args.attempt, "claude-code")

    max_turns_supported = supports_flag(args.claude_bin, "--max-turns")
    model_supported = args.model is not None and supports_flag(args.claude_bin, "--model")
    configured_model = args.model if model_supported else "inherited"
    model_source = args.model_source if model_supported else "inherited"
    update_handoff_frontmatter(handoff, {"executor_model": configured_model})
    command = [
        args.claude_bin,
        "-p",
        "--effort",
        args.effort,
        "--permission-mode",
        args.permission_mode,
        "--output-format",
        "stream-json",
        "--verbose",
        "--settings",
        str(settings),
    ]
    if args.permission_mode == "bypassPermissions":
        command.append("--allow-dangerously-skip-permissions")
    if max_turns_supported:
        command.extend(["--max-turns", str(args.max_turns)])
    if model_supported:
        command.extend(["--model", args.model])
    if args.max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(args.max_budget_usd)])
    command.append(prompt)

    invocation["phase"] = "running"
    invocation["max_turns_supported"] = max_turns_supported
    invocation["model_flag_supported"] = model_supported
    invocation["configured_model"] = configured_model
    invocation["model_source"] = model_source
    invocation["command"] = command[:-1] + ["<executor-prompt>"]
    atomic_write_json(invocation_path, invocation)

    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_TASK_ID": args.task_id,
            "AGENT_HANDOFF_PATH": str(handoff),
            "AGENT_HANDOFF_RELATIVE": handoff_display_path(repo, args.task_id),
            "AGENT_RESULT_PATH": paths["result"].as_posix(),
            "AGENT_INVOCATION_ID": invocation_id,
        }
    )

    child: subprocess.Popen[Any] | None = None
    transport_state = "launch_error"
    transport_exit_code: int | None = None
    failure_summary: str | None = None
    old_handlers: dict[int, Any] = {}

    def on_signal(signum: int, _frame: Any) -> None:
        raise RunnerInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, on_signal)

    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            try:
                child = subprocess.Popen(
                    command,
                    cwd=repo,
                    env=environment,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
                transport_exit_code = child.wait(timeout=args.timeout_seconds)
                transport_state = "exited"
            except OSError as exc:
                failure_summary = f"Claude Code could not start: {exc}"
                transport_state = "launch_error"
            except subprocess.TimeoutExpired:
                terminate(child)
                transport_exit_code = child.returncode if child else None
                failure_summary = f"Claude Code exceeded the {args.timeout_seconds:g}s timeout."
                transport_state = "timeout"
            except RunnerInterrupted as exc:
                terminate(child)
                transport_exit_code = child.returncode if child else None
                failure_summary = str(exc)
                transport_state = "interrupted"
    except OSError as exc:
        failure_summary = f"Claude Code transport logs could not be opened: {exc}"
        transport_state = "launch_error"
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)

    errors: list[str] = []
    executor_artifact_produced = False
    executor_result: Any = None
    try:
        executor_result = read_json(result_path)
    except json.JSONDecodeError as exc:
        executor_artifact_produced = result_path.is_file()
        errors.append(f"result.json is missing or malformed: {exc}")
    except OSError as exc:
        errors.append(f"result.json is missing or malformed: {exc}")
    else:
        if isinstance(executor_result, dict) and executor_result.get("producer") == "runner-provisional":
            errors.append("Claude Code did not replace the provisional result")
        else:
            executor_artifact_produced = True
            errors.extend(validate_terminal_artifacts(
                executor_result,
                handoff,
                args.task_id,
                handoff_display_path(repo, args.task_id),
            ))

    if failure_summary:
        errors.insert(0, failure_summary)

    generated_revision: str | None = None
    repair: str | None = None
    failure_class: str | None = None
    if errors:
        if result_path.exists():
            try:
                shutil.copyfile(result_path, attempt_dir / "executor-result.invalid.json")
            except OSError:
                pass
        summary = "; ".join(errors)
        failure_class = (
            "protocol_error"
            if failure_summary is None and executor_artifact_produced
            else "task_failure"
        )
        synthesized = synthesize_failure(
            args.task_id,
            invocation_id,
            summary,
            stderr_relative,
            transport_state,
            transport_exit_code,
            failure_class,
        )
        atomic_write_json(result_path, synthesized)
        mark_handoff_failed(handoff, invocation_id, summary, stderr_relative)
        if failure_class == "protocol_error":
            task_dir = repo / paths["task_dir"]
            revision_number = next_revision_number(task_dir)
            revision_path = write_revision_note(
                ASSET_ROOT / "REVISION.md.template",
                task_dir,
                revision_number,
                findings=errors,
                evidence=[
                    f"Attempt {args.attempt} executor result failed terminal artifact validation.",
                    f"Archived executor artifact: {relative_to_repo(attempt_dir / 'executor-result.invalid.json', repo)}",
                ],
                corrections=[
                    "Repair HANDOFF.md and result.json so every terminal artifact validation error is resolved.",
                    "Preserve all task constraints and rerun the complete acceptance suite before replacing result.json.",
                ],
                revalidation=[
                    f"python3 scripts/verify_result.py --repo . --task-id {args.task_id}"
                ],
            )
            revision_relative = Path(relative_to_repo(revision_path, repo))
            generated_revision = revision_relative.as_posix()
            repair = repair_command(
                args.task_id, "claude", args.attempt + 1, revision_relative
            )
        final_result = synthesized
    else:
        final_result = executor_result

    invocation.update(
        {
            "phase": "complete",
            "finished_at": now_iso(),
            "transport_state": transport_state,
            "transport_exit_code": transport_exit_code,
            "task_status": final_result["status"],
            "protocol_errors": errors,
            "protocol_exit_code": 0,
            "failure_class": failure_class,
            "generated_revision": generated_revision,
        }
    )
    atomic_write_json(invocation_path, invocation)
    if repair is not None:
        print(f"Repair command: {repair}")
    print(
        json.dumps(
            {
                "task_id": args.task_id,
                "status": final_result["status"],
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
    except (OSError, ValueError) as exc:
        print(f"run_claude_executor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

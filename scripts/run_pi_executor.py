#!/usr/bin/env python3
"""Run the current Pi client as a bounded prompt agent."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from protocol import (
    atomic_write_json,
    handoff_display_path,
    mark_handoff_failed,
    mark_handoff_started,
    now_iso,
    read_json,
    state_relative_path,
    synthesize_failure,
    task_relative_paths,
    validate_terminal_artifacts,
)


class RunnerInterrupted(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if args.attempt < 1 or args.timeout_seconds <= 0:
        raise ValueError("attempt and timeout-seconds must be positive")
    paths = task_relative_paths(args.task_id, repo)
    handoff = paths["handoff"]
    prompt_path = paths["claude_prompt"]
    result_path = paths["result"]
    for required in (handoff, prompt_path):
        if not required.is_file():
            raise ValueError(f"required task file does not exist: {required}")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"executor prompt is empty: {prompt_path}")

    attempt_dir = paths["run_root"] / f"attempt-{args.attempt:02d}"
    if attempt_dir.exists():
        raise ValueError(f"attempt directory already exists; choose a new attempt: {attempt_dir}")
    attempt_dir.mkdir(parents=True)
    invocation_id = str(uuid.uuid4())
    stdout_path = attempt_dir / "pi.stdout.log"
    stderr_path = attempt_dir / "pi.stderr.log"
    invocation_path = attempt_dir / "invocation.json"
    stdout_relative = state_relative_path(stdout_path)
    stderr_relative = state_relative_path(stderr_path)
    handoff_relative = handoff_display_path(repo, args.task_id)

    provisional = synthesize_failure(
        args.task_id,
        invocation_id,
        "Pi invocation started but has not produced terminal artifacts.",
        stderr_relative,
        "started",
        None,
        "task_failure",
        handoff_path=handoff_relative,
    )
    invocation = {
        "invocation_id": invocation_id,
        "task_id": args.task_id,
        "attempt": args.attempt,
        "agent": "pi",
        "started_at": now_iso(),
        "phase": "preparing",
        "model": args.model or "inherited",
        "timeout_seconds": args.timeout_seconds,
        "stdout_log": stdout_relative,
        "stderr_log": stderr_relative,
        "command": [args.pi_bin, "-p", "--no-session", "<executor-prompt>"],
    }
    atomic_write_json(invocation_path, invocation)
    atomic_write_json(result_path, {**provisional, "producer": "runner-provisional"})
    mark_handoff_started(handoff, invocation_id, args.attempt, "pi-prompt")

    command = [args.pi_bin, "-p", "--no-session"]
    if args.model:
        command.extend(["--model", args.model])
    command.append(prompt)
    invocation["phase"] = "running"
    invocation["command"] = command[:-1] + ["<executor-prompt>"]
    atomic_write_json(invocation_path, invocation)
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_TASK_ID": args.task_id,
            "AGENT_HANDOFF_PATH": str(handoff),
            "AGENT_HANDOFF_RELATIVE": handoff_relative,
            "AGENT_RESULT_PATH": str(result_path),
            "AGENT_INVOCATION_ID": invocation_id,
        }
    )

    child: subprocess.Popen[Any] | None = None
    state = "launch_error"
    exit_code: int | None = None
    failure: str | None = None
    old_handlers: dict[int, Any] = {}

    def on_signal(signum: int, _frame: Any) -> None:
        raise RunnerInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, on_signal)
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                child = subprocess.Popen(
                    command,
                    cwd=repo,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                )
                exit_code = child.wait(timeout=args.timeout_seconds)
                state = "exited"
                if exit_code != 0:
                    failure = f"Pi exited with code {exit_code}."
            except OSError as exc:
                failure = f"Pi could not start: {exc}"
            except subprocess.TimeoutExpired:
                terminate(child)
                exit_code = child.returncode if child else None
                state = "timeout"
                failure = f"Pi exceeded the {args.timeout_seconds:g}s timeout."
            except RunnerInterrupted as exc:
                terminate(child)
                exit_code = child.returncode if child else None
                state = "interrupted"
                failure = str(exc)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)

    errors: list[str] = []
    try:
        result = read_json(result_path)
    except (OSError, json.JSONDecodeError) as exc:
        result = None
        errors.append(f"result.json is missing or malformed: {exc}")
    else:
        if isinstance(result, dict) and result.get("producer") == "runner-provisional":
            errors.append("Pi did not replace the provisional result")
        else:
            errors.extend(
                validate_terminal_artifacts(
                    result, handoff, args.task_id, handoff_relative
                )
            )
    if failure:
        errors.insert(0, failure)
    if errors:
        synthesized = synthesize_failure(
            args.task_id,
            invocation_id,
            "; ".join(errors),
            stderr_relative,
            state,
            exit_code,
            "task_failure" if failure else "protocol_error",
            handoff_path=handoff_relative,
        )
        atomic_write_json(result_path, synthesized)
        mark_handoff_failed(handoff, invocation_id, "; ".join(errors), stderr_relative)
        result = synthesized
    invocation.update(
        {
            "phase": "complete",
            "finished_at": now_iso(),
            "transport_state": state,
            "transport_exit_code": exit_code,
            "task_status": result.get("status") if isinstance(result, dict) else "failed",
            "protocol_errors": errors,
        }
    )
    atomic_write_json(invocation_path, invocation)
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        print(f"run_pi_executor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

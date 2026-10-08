#!/usr/bin/env python3
"""Run Antigravity (agy) CLI in non-interactive prompt mode with a durable terminal result.

The runner treats one `agy -p` invocation as a bounded single-shot execution: the
executor prompt (EXECUTOR_PROMPT.txt) instructs agy to implement the HANDOFF
and atomically replace result.json. The runner prewrites a provisional failed
result, monitors the invocation, and synthesizes a normalized failure when the
executor did not replace the terminal artifacts. A runner exit code of zero
means terminal protocol state was persisted; task success still requires
`result.json.status == "success"` and independent controller-Agent acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from protocol import (
    assert_isolated_execution_root,
    atomic_write_json,
    handoff_display_path,
    mark_handoff_failed,
    mark_handoff_started,
    next_revision_number,
    now_iso,
    read_json,
    repair_command,
    resolve_task_path,
    synthesize_failure,
    task_relative_paths,
    validate_terminal_artifacts,
    write_revision_note,
)


ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"
MAX_USAGE_LINE_BYTES = 64 * 1024
MAX_USAGE_SNIPPET_CHARS = 2000
USAGE_SIGNAL = re.compile(
    r"(?i)(?:\busage\b|(?:^|[^A-Za-z0-9])(?:cost|tokens?)(?:$|[^A-Za-z0-9]))"
)
SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)"
)


class RunnerInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"runner interrupted by signal {signum}")
        self.signum = signum


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agy-bin", default="agy")
    parser.add_argument("--model", default="gemini-3.7-flash-high", help="Antigravity model alias")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default=None)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="prepend prior attempt HANDOFF/result context before starting a new attempt",
    )
    parser.add_argument("--revision", type=Path, help="repository-relative focused revision note")
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


def provisional_result(
    task_id: str, invocation_id: str, stderr_log: str
) -> dict[str, Any]:
    value = synthesize_failure(
        task_id,
        invocation_id,
        "Antigravity (agy) invocation started but has not produced terminal artifacts.",
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


def _markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^# {re.escape(heading)}\s*\n(?P<body>.*?)(?=^# |\Z)", text
    )
    return match.group("body").strip() if match else "(not recorded)"


def _handoff_status(text: str) -> str:
    match = re.search(r"(?m)^status:\s*(\S+)\s*$", text)
    return match.group(1) if match else "unknown"


def latest_prior_attempt(run_root: Path, current_attempt: int) -> int | None:
    prior = []
    if run_root.is_dir():
        for candidate in run_root.iterdir():
            match = re.fullmatch(r"attempt-(\d+)", candidate.name)
            if (
                candidate.is_dir()
                and match
                and int(match.group(1)) < current_attempt
                and (candidate / "invocation.json").is_file()
            ):
                prior.append(int(match.group(1)))
    return max(prior, default=None)


def continuation_prompt(
    handoff_text: str, prior_result: dict[str, Any], prior_attempt: int
) -> str:
    summary = prior_result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("--resume requires the previous result.json to contain a summary")
    progress = _markdown_section(handoff_text, "Execution Progress")
    final_result = _markdown_section(handoff_text, "Final Result")
    return (
        "## Previous Attempt Continuation Context\n\n"
        f"- Previous attempt: {prior_attempt}\n"
        f"- HANDOFF status: {_handoff_status(handoff_text)}\n"
        f"- result.json status: {prior_result.get('status', 'unknown')}\n"
        f"- result.json summary: {summary.strip()}\n\n"
        "### Previous HANDOFF Execution Progress\n\n"
        f"{progress}\n\n"
        "### Previous HANDOFF Final Result\n\n"
        f"{final_result}\n\n"
        "Continue from this evidence in the new attempt. HANDOFF.md remains the "
        "complete authoritative task source.\n"
    )


def _safe_usage_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key): _safe_usage_value(item, depth + 1)
            for key, item in list(value.items())[:100]
            if not SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_usage_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _usage_from_json(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if not isinstance(value, dict) or depth > 3:
        return None
    captured: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if SENSITIVE_KEY.search(key_text):
            continue
        if key_text == "usage" and isinstance(item, dict):
            captured.update(_safe_usage_value(item, depth + 1))
        elif re.search(r"(?i)(cost|tokens?)", key_text):
            captured[key_text] = _safe_usage_value(item, depth + 1)
    if captured:
        return captured
    for key in ("result", "data", "metrics"):
        nested = _usage_from_json(value.get(key), depth + 1)
        if nested:
            return nested
    return None


def _redact_usage_snippet(line: str) -> str:
    snippet = " ".join(line.split())[:MAX_USAGE_SNIPPET_CHARS]
    snippet = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", snippet)
    return re.sub(
        r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token|"
        r"refresh[_-]?token)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        snippet,
    )


def capture_usage(stdout_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    usage: dict[str, Any] | None = None
    raw_candidate: str | None = None
    try:
        with stdout_path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_USAGE_LINE_BYTES + 1)
                if not raw:
                    break
                sample = raw[:MAX_USAGE_LINE_BYTES]
                truncated = len(raw) > MAX_USAGE_LINE_BYTES and not raw.endswith(b"\n")
                if truncated:
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_USAGE_LINE_BYTES + 1)
                line = sample.decode("utf-8", errors="replace").strip()
                if not line or not USAGE_SIGNAL.search(line):
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    raw_candidate = _redact_usage_snippet(line)
                    continue
                parsed_usage = _usage_from_json(parsed)
                if parsed_usage:
                    usage = parsed_usage
                else:
                    raw_candidate = _redact_usage_snippet(line)
    except OSError as exc:
        return None, f"agy usage could not be read from its stdout log: {type(exc).__name__}."
    if usage is not None:
        return usage, None
    if raw_candidate is not None:
        return (
            {"raw_snippet": raw_candidate},
            "agy emitted usage/cost text, but its structure was not parseable as usage JSON.",
        )
    return None, None


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    assert_isolated_execution_root(repo)
    if args.attempt < 1 or args.timeout_seconds <= 0:
        raise ValueError("attempt and timeout-seconds must be positive")
    if not repo.is_dir():
        raise ValueError(f"repository does not exist: {repo}")

    paths = task_relative_paths(args.task_id, repo)
    handoff = repo / paths["handoff"]
    prompt_path = repo / paths["claude_prompt"]
    result_path = paths["result"]
    os.environ["WP_HANDOFF_RELATIVE"] = handoff_display_path(repo, args.task_id)
    for required in (handoff, prompt_path):
        if not required.is_file():
            raise ValueError(f"required task file does not exist: {required}")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"executor prompt is empty: {prompt_path}")

    prior_attempt: int | None = None
    if args.resume:
        if args.attempt < 2:
            raise ValueError("--resume requires --attempt 2 or greater")
        prior_attempt = latest_prior_attempt(repo / paths["run_root"], args.attempt)
        if prior_attempt is None:
            raise ValueError("--resume requested but no prior agy attempt exists")
        try:
            prior_result = read_json(result_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"--resume cannot read the previous result.json: {exc}") from exc
        if not isinstance(prior_result, dict):
            raise ValueError("--resume requires the previous result.json to be an object")
        handoff_text = handoff.read_text(encoding="utf-8")
        prompt = continuation_prompt(handoff_text, prior_result, prior_attempt) + "\n" + prompt

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
    stdout_path = attempt_dir / "agy.stdout.log"
    stderr_path = attempt_dir / "agy.stderr.log"
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
        "model": args.model,
        "effort": args.effort,
        "timeout_seconds": args.timeout_seconds,
        "stdout_log": stdout_relative,
        "stderr_log": stderr_relative,
        "resume": args.resume,
        "resume_from_attempt": prior_attempt,
    }
    atomic_write_json(invocation_path, invocation)
    atomic_write_json(
        result_path, provisional_result(args.task_id, invocation_id, stderr_relative)
    )
    mark_handoff_started(handoff, invocation_id, args.attempt, "agy-prompt")

    command = [
        args.agy_bin,
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--output-format",
        "text",
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.effort:
        command.extend(["--effort", args.effort])

    invocation["phase"] = "running"
    invocation_cmd = [args.agy_bin, "-p", "<executor-prompt>", "--dangerously-skip-permissions", "--output-format", "text"]
    if args.model:
        invocation_cmd.extend(["--model", args.model])
    if args.effort:
        invocation_cmd.extend(["--effort", args.effort])
    invocation["command"] = invocation_cmd
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
                failure_summary = f"Antigravity (agy) could not start: {exc}"
                transport_state = "launch_error"
            except subprocess.TimeoutExpired:
                terminate(child)
                transport_exit_code = child.returncode if child else None
                failure_summary = f"Antigravity (agy) exceeded the {args.timeout_seconds:g}s timeout."
                transport_state = "timeout"
            except RunnerInterrupted as exc:
                terminate(child)
                transport_exit_code = child.returncode if child else None
                failure_summary = str(exc)
                transport_state = "interrupted"
    except OSError as exc:
        failure_summary = f"agy transport logs could not be opened: {exc}"
        transport_state = "launch_error"
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)

    usage, usage_note = capture_usage(stdout_path)

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
            errors.append("Antigravity (agy) did not replace the provisional result")
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
                args.task_id, "agy", args.attempt + 1, revision_relative, resume=True
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
    if usage is not None:
        invocation["usage"] = usage
    if usage_note is not None:
        invocation["usage_note"] = usage_note
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
    except (ValueError, OSError) as exc:
        print(f"run_agy_executor: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    main()

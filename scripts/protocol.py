#!/usr/bin/env python3
"""Shared protocol validation and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


TASK_ID_PATTERN = re.compile(r"^[0-9]{8}-[a-z0-9][a-z0-9-]*$")
TERMINAL_STATUSES = {"success", "blocked", "failed"}
VALIDATION_RESULTS = {"passed", "failed", "not_run"}
FAILURE_CLASSES = {"protocol_error", "task_failure"}
BLOCKER_FIELDS = {"where", "attempted", "evidence", "unblock_action", "resume_from"}
WORKFLOW_MAIN_ROOT_ENV = "PI_WORKFLOW_MAIN_ROOT"
WORKFLOW_DELIVERY_ROOT_ENV = "PI_WORKFLOW_DELIVERY_ROOT"
SKILL_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = SKILL_ROOT / "wp-state"
CAPABILITY_ALIASES = {"frontier": "hard"}


def _configured_protected_roots() -> list[Path]:
    """Protected checkouts are opt-in via environment variables.

    No machine-local defaults are baked into the skill; when the variables
    are unset, no checkout is protected.
    """
    values = (
        os.environ.get(WORKFLOW_MAIN_ROOT_ENV, ""),
        os.environ.get(WORKFLOW_DELIVERY_ROOT_ENV, ""),
    )
    return [Path(value).expanduser().resolve() for value in values if value]


def _git_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _is_linked_worktree(root: Path) -> bool:
    try:
        git_dir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        common_dir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if git_dir.returncode != 0 or common_dir.returncode != 0:
        return False
    return _git_path(git_dir.stdout.strip(), root) != _git_path(
        common_dir.stdout.strip(), root
    )


def assert_isolated_execution_root(root: Path) -> None:
    """Reject the canonical Workflow checkouts as executor roots.

    A registered linked worktree is allowed, including one located below the
    main checkout. The canonical main checkout, its ordinary submodules, and
    the permanent delivery checkout are rejected.
    """

    candidate = root.resolve()
    for protected in _configured_protected_roots():
        if candidate == protected or protected not in candidate.parents:
            continue
        if _is_linked_worktree(candidate):
            continue
        raise ValueError(
            "executor root is inside a protected Workflow checkout but is not a "
            "linked worktree: "
            f"{candidate}. Create a task worktree with `git worktree add` first."
        )
    if candidate in _configured_protected_roots():
        raise ValueError(
            "executor root is a protected Workflow checkout: "
            f"{candidate}. Create and pass a dedicated linked worktree instead."
        )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(
            "task_id must match yyyymmdd-lowercase-slug, for example "
            "20260803-auth-refactor"
        )


def normalize_capability(value: str) -> str:
    """Normalize the public level name while accepting the old alias."""
    return CAPABILITY_ALIASES.get(value, value)


def repository_id(repo: Path) -> str:
    """Return a stable, local-only identifier without storing the repo path."""
    resolved = repo.resolve()
    label = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{label}-{digest}"


def state_relative_path(path: Path) -> str:
    """Render a skill-state path relative to the skill checkout."""
    return path.resolve().relative_to(SKILL_ROOT.resolve()).as_posix()


def task_relative_paths(task_id: str, repo: Path) -> dict[str, Path]:
    """Resolve task artifacts into the skill-local state directory."""
    validate_task_id(task_id)
    root = STATE_ROOT / "repos" / repository_id(repo)
    task_dir = root / "tasks" / task_id
    run_root = root / "runs" / task_id
    return {
        "task_dir": task_dir,
        "handoff": task_dir / "HANDOFF.md",
        "claude_prompt": task_dir / "EXECUTOR_PROMPT.txt",
        "claude_settings": task_dir / "claude-settings.json",
        "codex_goal": task_dir / "CODEX_GOAL.txt",
        "result": task_dir / "result.json",
        "run_root": run_root,
    }


def handoff_display_path(repo: Path, task_id: str) -> str:
    return state_relative_path(task_relative_paths(task_id, repo)["handoff"])


def resolve_task_path(value: str | Path, repo: Path) -> Path:
    """Resolve a revision or task path from either skill-state or execution root."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if candidate.parts and candidate.parts[0] == "wp-state":
        return (SKILL_ROOT / candidate).resolve()
    return (repo / candidate).resolve()


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: Path, text: str) -> None:
    _atomic_replace(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, payload)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def synthesize_failure(
    task_id: str,
    invocation_id: str,
    summary: str,
    stderr_log: str,
    transport_state: str,
    transport_exit_code: int | None,
    failure_class: str,
    handoff_path: str | None = None,
) -> dict[str, Any]:
    """Build the shared terminal failure shape used by prompt runners."""

    validate_task_id(task_id)
    if failure_class not in FAILURE_CLASSES:
        raise ValueError("failure_class must be protocol_error or task_failure")
    return {
        "task_id": task_id,
        "status": "failed",
        "summary": summary,
        "handoff_path": handoff_path
        or os.environ.get("WP_HANDOFF_RELATIVE")
        or f"wp-state/tasks/{task_id}/HANDOFF.md",
        "changed_files": [],
        "validation": [],
        "blocker": None,
        "recommended_next_action": (
            "repair_protocol_error"
            if failure_class == "protocol_error"
            else "inspect_task_failure"
        ),
        "producer": "runner-synthesized",
        "failure_class": failure_class,
        "transport": {
            "invocation_id": invocation_id,
            "state": transport_state,
            "exit_code": transport_exit_code,
            "stderr_log": stderr_log,
        },
    }


def _safe_diagnostic_text(value: str, limit: int = 2000) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token|"
        r"refresh[_-]?token)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def next_revision_number(task_dir: Path) -> int:
    numbers = []
    for candidate in task_dir.glob("revision-*.md"):
        match = re.fullmatch(r"revision-(\d+)\.md", candidate.name)
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def write_revision_note(
    template_path: Path,
    task_dir: Path,
    number: int,
    findings: list[str],
    evidence: list[str],
    corrections: list[str],
    revalidation: list[str],
) -> Path:
    """Render a focused verifier note without overwriting an earlier attempt."""

    if number < 1:
        raise ValueError("revision number must be positive")
    if not all((findings, evidence, corrections, revalidation)):
        raise ValueError("revision note sections must all be non-empty")
    template = template_path.read_text(encoding="utf-8")
    revision_path = task_dir / f"revision-{number:02d}.md"
    if revision_path.exists():
        raise ValueError(f"revision note already exists: {revision_path}")

    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {_safe_diagnostic_text(value)}" for value in values)

    numbered = "\n".join(
        f"{index}. {_safe_diagnostic_text(value)}"
        for index, value in enumerate(corrections, start=1)
    )
    commands = "\n".join(_safe_diagnostic_text(value) for value in revalidation)
    rendered = template.replace("{{REVISION_NUMBER}}", f"{number:02d}")
    rendered = rendered.replace(
        "- TODO: criterion and observed mismatch", bullets(findings)
    )
    rendered = rendered.replace(
        "- TODO: command, exit code, diff location, or runtime observation",
        bullets(evidence),
    )
    rendered = rendered.replace("1. TODO", numbered)
    rendered = rendered.replace("```bash\nTODO\n```", f"```bash\n{commands}\n```")
    atomic_write_text(revision_path, rendered)
    return revision_path


def repair_command(
    task_id: str,
    executor: str,
    next_attempt: int,
    revision_path: Path,
    resume: bool = False,
) -> str:
    validate_task_id(task_id)
    if executor not in {"claude", "grok", "kimi"}:
        raise ValueError("repair executor must be claude, grok, or kimi")
    if next_attempt < 2:
        raise ValueError("repair attempt must be at least 2")
    resume_flag = " --resume" if resume else ""
    return (
        "python3 <skill-root>/scripts/run_task.py --repo <execution-root> "
        f"--task-id {task_id} --agent {executor} --attempt {next_attempt} "
        f"--revision {revision_path.as_posix()}{resume_flag}"
    )


def _frontmatter(text: str) -> tuple[str, str, str]:
    match = re.match(r"\A---\n(?P<body>.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError("HANDOFF.md must start with YAML frontmatter")
    return text[: match.start("body")], match.group("body"), text[match.end("body") :]


def _frontmatter_value(body: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", body)
    return match.group(1) if match else None


def read_handoff_state(path: Path) -> tuple[str | None, str | None]:
    _, body, _ = _frontmatter(path.read_text(encoding="utf-8"))
    return _frontmatter_value(body, "status"), _frontmatter_value(body, "runner_sentinel")


def validate_frontmatter_scalar(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty value without outer whitespace")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a single-line value")


def update_handoff_frontmatter(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    prefix, body, suffix = _frontmatter(text)
    for key, value in values.items():
        validate_frontmatter_scalar(key, value)
        if re.search(rf"(?m)^{re.escape(key)}:", body):
            body = re.sub(
                rf"(?m)^{re.escape(key)}:\s*.*$",
                f"{key}: {value}",
                body,
                count=1,
            )
        else:
            body += f"\n{key}: {value}"
    atomic_write_text(path, prefix + body + suffix)


def mark_handoff_started(
    path: Path, invocation_id: str, attempt: int, executor: str
) -> None:
    text = path.read_text(encoding="utf-8")
    prefix, body, suffix = _frontmatter(text)
    body = re.sub(r"(?m)^status:\s*.*$", "status: failed", body, count=1)
    body = re.sub(r"(?m)^updated_at:\s*.*$", f"updated_at: {now_iso()}", body, count=1)
    if re.search(r"(?m)^runner_sentinel:", body):
        body = re.sub(
            r"(?m)^runner_sentinel:\s*.*$",
            f"runner_sentinel: {invocation_id}",
            body,
            count=1,
        )
    else:
        body = re.sub(
            r"(?m)^(status:\s*failed)$",
            rf"\1\nrunner_sentinel: {invocation_id}",
            body,
            count=1,
        )
    record = (
        f"\n<!-- runner-start:{invocation_id} -->\n"
        "## Runner Safeguard Record\n\n"
        f"- Attempt: {attempt}\n"
        f"- Executor: {executor}\n"
        f"- Started at: {now_iso()}\n"
        "- Provisional terminal state: failed until the executor replaces both "
        "terminal artifacts.\n"
    )
    atomic_write_text(path, prefix + body + suffix + record)


def mark_handoff_blocked(
    path: Path,
    invocation_id: str,
    reason: str,
    evidence_path: str,
    resume_command: str,
) -> None:
    text = path.read_text(encoding="utf-8")
    prefix, body, suffix = _frontmatter(text)
    body = re.sub(r"(?m)^status:\s*.*$", "status: blocked", body, count=1)
    body = re.sub(r"(?m)^updated_at:\s*.*$", f"updated_at: {now_iso()}", body, count=1)
    body = re.sub(r"(?m)^runner_sentinel:\s*.*\n?", "", body, count=1)
    record = (
        f"\n<!-- runner-blocked:{invocation_id} -->\n"
        "## Runner Resumable Blocker\n\n"
        f"- Recorded at: {now_iso()}\n"
        f"- Reason: {reason}\n"
        f"- Evidence: `{evidence_path}`\n"
        f"- Resume command: `{resume_command}`\n"
    )
    atomic_write_text(path, prefix + body + suffix + record)


def mark_handoff_failed(path: Path, invocation_id: str, reason: str, log_path: str) -> None:
    text = path.read_text(encoding="utf-8")
    prefix, body, suffix = _frontmatter(text)
    body = re.sub(r"(?m)^status:\s*.*$", "status: failed", body, count=1)
    body = re.sub(r"(?m)^updated_at:\s*.*$", f"updated_at: {now_iso()}", body, count=1)
    body = re.sub(r"(?m)^runner_sentinel:\s*.*\n?", "", body, count=1)
    record = (
        f"\n<!-- runner-failed:{invocation_id} -->\n"
        "## Runner Terminal Failure\n\n"
        f"- Recorded at: {now_iso()}\n"
        f"- Reason: {reason}\n"
        f"- Diagnostic log: `{log_path}`\n"
        "- Resume: inspect the invocation record and log, correct the transport "
        "or protocol failure, then start a new numbered attempt.\n"
    )
    atomic_write_text(path, prefix + body + suffix + record)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_relative_path(value: str) -> bool:
    candidate = PurePosixPath(value)
    return not candidate.is_absolute() and ".." not in candidate.parts and value != ""


def validate_result(
    value: Any,
    task_id: str,
    expected_handoff_path: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["result must be a JSON object"]

    required = {
        "task_id",
        "status",
        "summary",
        "handoff_path",
        "changed_files",
        "validation",
        "blocker",
        "recommended_next_action",
    }
    missing = sorted(required - value.keys())
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")

    if value.get("task_id") != task_id:
        errors.append("task_id does not match the invocation")

    status = value.get("status")
    if status not in TERMINAL_STATUSES:
        errors.append("status must be success, blocked, or failed")

    failure_class = value.get("failure_class")
    if failure_class is not None and failure_class not in FAILURE_CLASSES:
        errors.append("failure_class must be protocol_error or task_failure")

    if not _nonempty_string(value.get("summary")):
        errors.append("summary must be a non-empty string")

    expected_handoff = expected_handoff_path or value.get("handoff_path")
    if not isinstance(expected_handoff, str) or not expected_handoff.startswith("wp-state/"):
        errors.append("handoff_path must point into the skill-local wp-state directory")
    elif value.get("handoff_path") != expected_handoff:
        errors.append(f"handoff_path must be {expected_handoff}")

    changed_files = value.get("changed_files")
    if not isinstance(changed_files, list) or not all(
        isinstance(item, str) and _safe_relative_path(item) for item in changed_files
    ):
        errors.append("changed_files must contain repository-relative strings")
    elif len(changed_files) != len(set(changed_files)):
        errors.append("changed_files must not contain duplicates")

    validation = value.get("validation")
    if not isinstance(validation, list):
        errors.append("validation must be an array")
    else:
        for index, item in enumerate(validation):
            if not isinstance(item, dict):
                errors.append(f"validation[{index}] must be an object")
                continue
            if not _nonempty_string(item.get("command")):
                errors.append(f"validation[{index}].command must be non-empty")
            if item.get("exit_code") is not None and not isinstance(item.get("exit_code"), int):
                errors.append(f"validation[{index}].exit_code must be integer or null")
            if item.get("result") not in VALIDATION_RESULTS:
                errors.append(f"validation[{index}].result is invalid")
        if status == "success":
            if not validation:
                errors.append("success requires at least one recorded validation")
            elif any(item.get("result") != "passed" for item in validation if isinstance(item, dict)):
                errors.append("success requires every recorded validation to pass")

    blocker = value.get("blocker")
    if status == "blocked":
        if not isinstance(blocker, dict):
            errors.append("blocked requires a blocker object")
        else:
            for field in sorted(BLOCKER_FIELDS):
                if not _nonempty_string(blocker.get(field)):
                    errors.append(f"blocker.{field} must be a non-empty string")
    elif blocker is not None:
        errors.append("blocker must be null unless status is blocked")

    if not _nonempty_string(value.get("recommended_next_action")):
        errors.append("recommended_next_action must be a non-empty string")
    if status == "success" and value.get("recommended_next_action") != "orchestrator_verify":
        errors.append("success recommended_next_action must be orchestrator_verify")

    return errors


def validate_terminal_artifacts(
    result: Any,
    handoff_path: Path,
    task_id: str,
    expected_handoff_path: str | None = None,
) -> list[str]:
    errors = validate_result(result, task_id, expected_handoff_path)
    try:
        handoff_status, sentinel = read_handoff_state(handoff_path)
    except (OSError, ValueError) as exc:
        return errors + [f"cannot read HANDOFF terminal state: {exc}"]
    if isinstance(result, dict) and handoff_status != result.get("status"):
        errors.append("HANDOFF status does not match result status")
    if sentinel is not None:
        errors.append("HANDOFF still contains runner_sentinel")
    return errors

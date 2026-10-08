#!/usr/bin/env python3
"""Initialize durable planner-executor-verifier task state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from model_resolver import CAPABILITIES
from protocol import (
    assert_isolated_execution_root,
    handoff_display_path,
    normalize_capability,
    now_iso,
    state_relative_path,
    task_relative_paths,
    validate_frontmatter_scalar,
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
    repo: Path,
    task_id: str,
    goal: str,
    executor: str = "auto",
    codex_model: str = "gpt-5.6-luna",
    codex_reasoning_effort: str = "xhigh",
    kimi_model: str = "kimi-code/k3",
    planner_model: str = "gpt-5.6-sol",
    planner_effort: str = "xhigh",
    capability: str | None = None,
) -> dict[str, str]:
    root = repository_root(repo)
    assert_isolated_execution_root(root)
    for name, value in {
        "codex_model": codex_model,
        "codex_reasoning_effort": codex_reasoning_effort,
        "kimi_model": kimi_model,
        "planner_model": planner_model,
        "planner_effort": planner_effort,
    }.items():
        validate_frontmatter_scalar(name, value)
    if capability is not None:
        capability = normalize_capability(capability)
        if capability not in CAPABILITIES:
            raise ValueError(f"unsupported level: {capability}")
    relative = task_relative_paths(task_id, root)
    task_dir = relative["task_dir"]
    if task_dir.exists():
        raise FileExistsError(f"task already exists: {task_dir}")

    created_at = now_iso()
    executor_name = {
        "auto": "auto",
        "claude": "claude-code",
        "codex": "codex-goal",
        "grok": "grok-prompt",
        "kimi": "kimi-prompt",
        "pi": "pi-prompt",
    }[executor]
    executor_model = {
        "auto": "auto",
        "claude": "inherited",
        "codex": codex_model,
        "grok": "inherited",
        "kimi": kimi_model,
        "pi": "inherited",
    }[executor]
    executor_effort = {
        "auto": "auto",
        "claude": "inherited",
        "codex": codex_reasoning_effort,
        "grok": "inherited",
        "kimi": "inherited",
        "pi": "inherited",
    }[executor]
    values = {
        "TASK_ID": task_id,
        "GOAL": goal.strip(),
        "CREATED_AT": created_at,
        "HANDOFF_PATH": handoff_display_path(root, task_id),
        "RESULT_PATH": state_relative_path(relative["result"]),
        "RESULT_SCHEMA_PATH": state_relative_path(relative["task_dir"] / "result.schema.json"),
        "ORCHESTRATOR": os.environ.get("WP_ORCHESTRATOR", "current-agent"),
        "PLANNER_MODEL": planner_model,
        "PLANNER_EFFORT": planner_effort,
        "EXECUTOR": executor_name,
        "EXECUTOR_MODEL": executor_model,
        "EXECUTOR_EFFORT": executor_effort,
        "EXECUTOR_CAPABILITY": capability or "auto",
    }

    task_dir.mkdir(parents=True)
    relative["run_root"].mkdir(parents=True)
    (SKILL_ROOT / "wp-state").mkdir(parents=True, exist_ok=True)
    plans_path = SKILL_ROOT / "wp-state" / "PLANS.md"
    if not plans_path.exists():
        plans_path.write_text(render("PLANS.md.template", {}), encoding="utf-8")

    (task_dir / "HANDOFF.md").write_text(
        render("HANDOFF.md.template", values), encoding="utf-8"
    )
    (task_dir / "EXECUTOR_PROMPT.txt").write_text(
        render("EXECUTOR_PROMPT.txt.template", values), encoding="utf-8"
    )
    (task_dir / "CODEX_GOAL.txt").write_text(
        render("CODEX_GOAL.txt.template", values), encoding="utf-8"
    )
    shutil.copyfile(
        ASSETS / "claude-settings.json.template", task_dir / "claude-settings.json"
    )
    shutil.copyfile(ASSETS / "result.schema.json", task_dir / "result.schema.json")

    return {
        "task_id": task_id,
        "handoff_path": state_relative_path(relative["handoff"]),
        "prompt_path": state_relative_path(relative["claude_prompt"]),
        "codex_goal_path": state_relative_path(relative["codex_goal"]),
        "settings_path": state_relative_path(relative["claude_settings"]),
        "result_schema_path": state_relative_path(relative["task_dir"] / "result.schema.json"),
        "result_path": state_relative_path(relative["result"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True, help="yyyymmdd-lowercase-slug")
    parser.add_argument("--goal", required=True, help="one-paragraph observable outcome")
    parser.add_argument(
        "--agent",
        "--executor",
        dest="executor",
        choices=("auto", "claude", "codex", "grok", "kimi", "pi"),
        default="auto",
    )
    parser.add_argument(
        "--level",
        "--capability",
        "--tier",
        dest="capability",
        choices=tuple(CAPABILITIES) + ("frontier",),
        help="task difficulty: fast, balanced, or hard (frontier is a legacy alias)",
    )
    parser.add_argument("--codex-model", default="gpt-5.6-luna")
    parser.add_argument("--codex-reasoning-effort", default="xhigh")
    parser.add_argument("--kimi-model", default="kimi-code/k3")
    parser.add_argument(
        "--controller-model",
        "--planner-model",
        dest="planner_model",
        default=os.environ.get("PI_MODEL", "gpt-5.6-sol"),
    )
    parser.add_argument(
        "--controller-effort",
        "--planner-effort",
        dest="planner_effort",
        default=os.environ.get("PI_REASONING_LEVEL", "xhigh"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        created = initialize(
            args.repo,
            args.task_id,
            args.goal,
            executor=args.executor,
            codex_model=args.codex_model,
            codex_reasoning_effort=args.codex_reasoning_effort,
            kimi_model=args.kimi_model,
            planner_model=args.planner_model,
            planner_effort=args.planner_effort,
            capability=args.capability,
        )
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"init_task: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(created, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

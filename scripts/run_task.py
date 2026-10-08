#!/usr/bin/env python3
"""Route one durable implementation task to a supported bounded executor."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from model_resolver import CAPABILITIES, resolve_model
from protocol import (
    assert_isolated_execution_root,
    normalize_capability,
    read_json,
    task_relative_paths,
    update_handoff_frontmatter,
)


SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_KIMI_MODEL = "kimi-code/k3"
DEFAULT_EXECUTOR_BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "kimi": "kimi",
    "pi": "pi",
}
EXECUTOR_BINARY_ATTRIBUTES = {
    executor: f"{executor}_bin" for executor in DEFAULT_EXECUTOR_BINARIES
}


VALIDATION_SECTION_MARKER = "# Validation Commands"


class ExplicitBinaryAction(argparse.Action):
    """Store a binary value and remember that the CLI explicitly supplied it."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_explicit", True)


def validation_commands(handoff_text: str) -> list[str]:
    """Extract executable lines from the HANDOFF Validation Commands bash block."""
    start = handoff_text.find(VALIDATION_SECTION_MARKER)
    if start < 0:
        return []
    section = handoff_text[start:]
    fence_start = section.find("```bash")
    if fence_start < 0:
        return []
    body = section[fence_start + len("```bash") :]
    fence_end = body.find("```")
    if fence_end < 0:
        return []
    return [
        line.strip()
        for line in body[:fence_end].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def allow_prefixes(handoff_text: str) -> list[str]:
    """Command prefixes (first token) of validation commands, deduplicated."""
    seen: set[str] = set()
    prefixes: list[str] = []
    for line in validation_commands(handoff_text):
        token = line.split(None, 1)[0] if line.split(None, 1) else line
        if token not in seen:
            seen.add(token)
            prefixes.append(token)
    return prefixes


def merge_bash_allow(settings: dict, handoff_text: str) -> bool:
    """Add Bash(<prefix> *) allow rules for validation commands without
    weakening any deny rule. Returns True when settings changed."""
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return False
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        return False
    deny = permissions.get("deny")
    deny_prefixes: set[str] = set()
    if isinstance(deny, list):
        for rule in deny:
            if not isinstance(rule, str):
                continue
            if rule.startswith("Bash(") and rule.endswith("*)"):
                deny_prefixes.add(rule[len("Bash(") : -len("*)")].strip())
    existing = {rule for rule in allow if isinstance(rule, str)}
    changed = False
    for prefix in allow_prefixes(handoff_text):
        if prefix in deny_prefixes:
            continue
        rule = f"Bash({prefix} *)"
        if rule not in existing:
            allow.append(rule)
            existing.add(rule)
            changed = True
    return changed


def tailor_claude_settings(repo: Path, task_id: str, handoff_text: str) -> None:
    """Update the task claude-settings.json allowlist from HANDOFF validation
    commands so the executor can actually run them (deny rules untouched)."""
    paths = task_relative_paths(task_id, repo)
    settings_path = paths["task_dir"] / "claude-settings.json"
    try:
        settings = read_json(settings_path)
    except (OSError, ValueError):
        return
    if merge_bash_allow(settings, handoff_text):
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--agent",
        "--executor",
        dest="executor",
        choices=("auto", "claude", "codex", "grok", "kimi", "pi"),
        default="auto",
    )
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--revision", type=Path)
    parser.add_argument(
        "--continue",
        "--resume",
        dest="resume",
        action="store_true",
        help="continue the latest bounded attempt when the selected client supports it",
    )

    routing = parser.add_argument_group("auto routing evidence")
    routing.add_argument(
        "--level",
        "--capability",
        "--tier",
        dest="capability",
        choices=tuple(CAPABILITIES) + ("frontier",),
        help="task difficulty: fast, balanced, or hard (frontier is a legacy alias)",
    )
    routing.add_argument("--information-retrieval", "--research", dest="information_retrieval", action="store_true")
    routing.add_argument("--time-sensitive", action="store_true")
    routing.add_argument("--long-task", action="store_true", help=argparse.SUPPRESS)
    routing.add_argument("--no-time-pressure", action="store_true", help=argparse.SUPPRESS)
    routing.add_argument(
        "--sustained-goal",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    routing.add_argument(
        "--roster",
        type=Path,
        help="roster file from discover_executors.py; degraded clients are rejected",
    )

    claude = parser.add_argument_group("Claude executor")
    claude.add_argument("--effort", choices=("high", "max"), default="high")
    claude.add_argument("--max-turns", type=int, default=80)
    claude.add_argument(
        "--claude-bin", default="claude", action=ExplicitBinaryAction
    )
    claude.add_argument("--claude-model", help="override the capability model alias")
    claude.add_argument(
        "--permission-mode",
        choices=("dontAsk", "bypassPermissions"),
        default="dontAsk",
    )
    claude.add_argument("--isolated", action="store_true")
    claude.add_argument("--max-budget-usd", type=float)

    codex = parser.add_argument_group("Codex Goal executor")
    codex.add_argument("--model", default=DEFAULT_CODEX_MODEL)
    codex.add_argument(
        "--reasoning-effort",
        "--codex-reasoning-effort",
        dest="reasoning_effort",
        default="xhigh",
    )
    codex.add_argument("--codex-bin", default="codex", action=ExplicitBinaryAction)
    codex.add_argument("--idle-timeout-seconds", type=float, default=900)
    codex.add_argument("--request-timeout-seconds", type=float, default=60)
    codex.add_argument("--token-budget", type=int)

    kimi = parser.add_argument_group("Kimi prompt agent")
    kimi.add_argument("--kimi-bin", default="kimi", action=ExplicitBinaryAction)
    kimi.add_argument("--kimi-model", default=DEFAULT_KIMI_MODEL)

    pi = parser.add_argument_group("Pi prompt agent")
    pi.add_argument("--pi-bin", default="pi", action=ExplicitBinaryAction)
    pi.add_argument("--pi-model")

    grok = parser.add_argument_group("Grok prompt agent")
    grok.add_argument("--grok-bin", default="grok", action=ExplicitBinaryAction)
    grok.add_argument("--grok-model")
    grok.add_argument("--grok-effort")
    grok.add_argument("--grok-max-turns", type=int, default=8)
    parser.set_defaults(
        claude_bin_explicit=False,
        codex_bin_explicit=False,
        grok_bin_explicit=False,
        kimi_bin_explicit=False,
        pi_bin_explicit=False,
    )
    return parser.parse_args()


def handoff_ready(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "TODO" in text or "# Acceptance Criteria" not in text:
        return False
    return bool(validation_commands(text))


def validate_sustained_gate(args: argparse.Namespace, handoff: Path) -> None:
    """Require explicit, complete authorization for sustained Goal execution."""
    if not args.sustained_goal:
        return
    if args.executor not in ("auto", "codex"):
        raise ValueError("--sustained-goal supports only --executor auto or codex")
    if not args.no_time_pressure:
        raise ValueError("--sustained-goal requires --no-time-pressure")
    if args.token_budget is None or args.token_budget <= 0:
        raise ValueError("--sustained-goal requires a positive --token-budget")
    if args.timeout_seconds is None or args.timeout_seconds <= 0:
        raise ValueError("--sustained-goal requires a positive --timeout-seconds")
    if not handoff_ready(handoff):
        raise ValueError(
            "--sustained-goal requires a complete HANDOFF without TODO placeholders "
            "and with Validation Commands"
        )


def derive_capability(args: argparse.Namespace, handoff: Path) -> str:
    explicit = getattr(args, "capability", None)
    if explicit is not None:
        return normalize_capability(explicit)
    if args.time_sensitive:
        return "fast"
    if args.sustained_goal:
        return "hard"
    if args.no_time_pressure and args.long_task and handoff_ready(handoff):
        return "hard"
    return "balanced"


def _supports_capability(entry: dict, capability: str) -> bool:
    models = entry.get("models")
    if isinstance(models, dict) and capability in models:
        return True
    tiers = entry.get("tiers_available")
    return isinstance(tiers, list) and capability in tiers


def _entry_capabilities(entry: dict) -> list[str]:
    models = entry.get("models")
    if isinstance(models, dict):
        return [key for key in models if isinstance(key, str)]
    tiers = entry.get("tiers_available")
    if isinstance(tiers, list):
        return [value for value in tiers if isinstance(value, str)]
    return []


def _auto_executor_from_roster(
    roster_path: Path, capability: str, order: tuple[str, ...]
) -> str:
    roster = read_json(roster_path)
    if not isinstance(roster, dict) or not isinstance(roster.get("agents", []), list):
        raise ValueError(f"roster must contain an agents array: {roster_path}")
    entries = {
        entry.get("name"): entry
        for entry in roster.get("agents", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    for executor in order:
        entry = entries.get(executor)
        if entry is None:
            print(
                f"run_task: warning: {executor} is absent from {roster_path}; "
                "skipping it for auto routing",
                file=sys.stderr,
            )
            continue
        if not _supports_capability(entry, capability):
            continue
        health = entry.get("health")
        if health == "degraded":
            continue
        if health == "unknown":
            print(
                f"run_task: warning: {executor} health is unknown per {roster_path}",
                file=sys.stderr,
            )
        return executor

    healthy = sorted(
        f"{name}({','.join(_entry_capabilities(entry)) or 'unknown'})"
        for name, entry in entries.items()
        if name in order
        and entry.get("health") != "degraded"
    )
    options = ", ".join(healthy) if healthy else "none"
    raise ValueError(
        f"no healthy auto executor serves capability {capability} per {roster_path}; "
        f"healthy options: {options}"
    )


def select_executor(args: argparse.Namespace, handoff: Path) -> tuple[str, str]:
    capability = derive_capability(args, handoff)
    if args.sustained_goal:
        return "codex", "authorized Codex Goal continuation"
    if args.executor != "auto":
        return args.executor, "explicit agent selection"
    if getattr(args, "information_retrieval", False):
        order = ("grok", "claude", "pi", "kimi", "codex")
        route_reason = "information retrieval requested; Grok has priority"
    elif capability == "hard":
        order = ("codex", "claude", "pi", "grok", "kimi")
        route_reason = "hard level; Codex Goal has native resumable execution"
    elif capability == "fast":
        order = ("claude", "pi", "grok", "kimi", "codex")
        route_reason = "fast level; prefer low-latency standard agents"
    else:
        order = ("claude", "pi", "grok", "kimi", "codex")
        route_reason = "balanced level; prefer quality/price balance"
    if args.roster is None:
        print(
            "run_task: warning: no roster supplied; using installed-agent order",
            file=sys.stderr,
        )
        selected = order[0]
    else:
        selected = _auto_executor_from_roster(args.roster, capability, order)
    return selected, route_reason


def command_for(
    args: argparse.Namespace,
    executor: str,
    capability: str | None = None,
    model_resolution: dict | None = None,
) -> list[str]:
    timeout = args.timeout_seconds
    capability = normalize_capability(
        capability or getattr(args, "capability", None) or "balanced"
    )
    if executor == "pi":
        command = [
            sys.executable,
            str(SCRIPT_ROOT / "run_pi_executor.py"),
            "--repo",
            str(args.repo.resolve()),
            "--task-id",
            args.task_id,
            "--pi-bin",
            executor_binary(args, "pi"),
            "--timeout-seconds",
            str(timeout if timeout is not None else 7200),
            "--attempt",
            str(args.attempt),
        ]
        if getattr(args, "pi_model", None):
            command.extend(["--model", args.pi_model])
        if args.resume:
            command.append("--resume")
    elif executor == "grok":
        command = [
            sys.executable,
            str(SCRIPT_ROOT / "run_grok_executor.py"),
            "--repo",
            str(args.repo.resolve()),
            "--task-id",
            args.task_id,
            "--grok-bin",
            executor_binary(args, "grok"),
            "--max-turns",
            str(args.grok_max_turns),
            "--timeout-seconds",
            str(timeout if timeout is not None else 7200),
            "--attempt",
            str(args.attempt),
            "--capability",
            capability,
        ]
        if args.grok_model is not None:
            command.extend(["--model", args.grok_model])
        if args.grok_effort is not None:
            command.extend(["--reasoning-effort", args.grok_effort])
        if args.resume:
            command.append("--resume")
    elif executor == "kimi":
        command = [
            sys.executable,
            str(SCRIPT_ROOT / "run_kimi_executor.py"),
            "--repo",
            str(args.repo.resolve()),
            "--task-id",
            args.task_id,
            "--kimi-bin",
            executor_binary(args, "kimi"),
            "--model",
            args.kimi_model,
            "--timeout-seconds",
            str(timeout if timeout is not None else 7200),
            "--attempt",
            str(args.attempt),
        ]
    elif executor == "claude":
        if model_resolution is None:
            model_resolution = resolve_model(
                "claude",
                capability,
                override=getattr(args, "claude_model", None),
            )
        command = [
            sys.executable,
            str(SCRIPT_ROOT / "run_claude_executor.py"),
            "--repo",
            str(args.repo.resolve()),
            "--task-id",
            args.task_id,
            "--effort",
            args.effort,
            "--max-turns",
            str(args.max_turns),
            "--timeout-seconds",
            str(timeout if timeout is not None else 7200),
            "--attempt",
            str(args.attempt),
            "--claude-bin",
            executor_binary(args, "claude"),
            "--permission-mode",
            args.permission_mode,
        ]
        if model_resolution["model"] is not None:
            command.extend(["--model", model_resolution["model"]])
            command.extend(["--model-source", model_resolution["source"]])
        if args.isolated:
            command.append("--isolated")
        if args.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(args.max_budget_usd)])
    elif executor == "codex":
        codex_model = executor_model_resolution(args, executor, capability)['model'] or args.model
        command = [
            sys.executable,
            str(SCRIPT_ROOT / "run_codex_goal.py"),
            "--repo",
            str(args.repo.resolve()),
            "--task-id",
            args.task_id,
            "--model",
            codex_model,
            "--reasoning-effort",
            args.reasoning_effort,
            "--codex-bin",
            executor_binary(args, "codex"),
            "--attempt",
            str(args.attempt),
            "--timeout-seconds",
            str(timeout if timeout is not None else 86400),
            "--idle-timeout-seconds",
            str(args.idle_timeout_seconds),
            "--request-timeout-seconds",
            str(args.request_timeout_seconds),
        ]
        if args.token_budget is not None:
            command.extend(["--token-budget", str(args.token_budget)])
        if args.sustained_goal:
            command.extend(["--execution-mode", "sustained"])
        if args.resume:
            command.append("--resume")
    else:
        raise ValueError(f"unsupported agent route: {executor}")
    if args.revision is not None:
        command.extend(["--revision", str(args.revision)])
    return command


def executor_binary(args: argparse.Namespace, executor: str) -> str:
    attribute = EXECUTOR_BINARY_ATTRIBUTES[executor]
    return getattr(args, attribute, DEFAULT_EXECUTOR_BINARIES[executor])


def executor_model_resolution(
    args: argparse.Namespace, executor: str, capability: str
) -> dict:
    if executor == "pi":
        override = getattr(args, "pi_model", None)
        return {
            "model": override,
            "source": "override" if override is not None else "inherited",
            "catalog": None,
        }
    if executor == "grok":
        override = args.grok_model
        return {
            "model": override,
            "source": "override" if override is not None else "inherited",
            "catalog": None,
        }
    if executor == "claude":
        override = getattr(args, "claude_model", None)
    elif executor == "codex":
        override = args.model if args.model != DEFAULT_CODEX_MODEL else None
    elif executor == "kimi":
        override = args.kimi_model if args.kimi_model != DEFAULT_KIMI_MODEL else None
    else:
        override = None
    return resolve_model(executor, capability, override=override)


def guard_roster_health(args: argparse.Namespace, executor: str) -> None:
    """Refuse to launch a selected executor whose live health is degraded."""

    if args.roster is None:
        return
    roster = read_json(args.roster)
    if not isinstance(roster, dict) or not isinstance(roster.get("agents", []), list):
        raise ValueError(f"roster must contain an agents array: {args.roster}")
    for agent in roster.get("agents", []):
        if agent.get("name") != executor:
            continue
        health = agent.get("health")
        if health == "degraded":
            raise ValueError(
                f"selected executor {executor} is degraded per {args.roster}: "
                f"{agent.get('notes')}; probe again or choose another executor"
            )
        if health == "unknown":
            print(
                f"run_task: warning: {executor} health is unknown per roster; "
                "verify manually before relying on it",
                file=sys.stderr,
            )
        return
    print(
        f"run_task: warning: {executor} is not listed in {args.roster}; "
        "run discover_executors.py --probe for a complete roster",
        file=sys.stderr,
    )


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    assert_isolated_execution_root(repo)
    paths = task_relative_paths(args.task_id, repo)
    handoff = paths["handoff"]
    if not handoff.is_file():
        raise ValueError(f"HANDOFF does not exist: {handoff}")
    max_attempts = getattr(args, "max_attempts", 3)
    if args.attempt < 1 or args.attempt > max_attempts:
        raise ValueError(
            f"attempt must be between 1 and max-attempts ({max_attempts})"
        )
    validate_sustained_gate(args, handoff)
    handoff_text = handoff.read_text(encoding="utf-8")
    capability = normalize_capability(derive_capability(args, handoff))
    executor, reason = select_executor(args, handoff)
    guard_roster_health(args, executor)
    if executor == "claude":
        tailor_claude_settings(repo, args.task_id, handoff_text)
    model_resolution = executor_model_resolution(args, executor, capability)
    model = model_resolution["model"] or "inherited"
    if executor == "grok":
        reasoning_effort = args.grok_effort or "inherited"
    else:
        reasoning_effort = (
            "inherited" if executor in ("claude", "kimi", "pi") else args.reasoning_effort
        )
    executor_frontmatter = {
        "claude": "claude-code",
        "codex": "codex-goal",
        "grok": "grok-prompt",
        "kimi": "kimi-prompt",
        "pi": "pi-prompt",
    }[executor]
    frontmatter = {
        "executor": executor_frontmatter,
        "executor_model": model,
        "executor_effort": reasoning_effort,
        "executor_capability": capability,
        "executor_route_reason": reason,
    }

    if args.sustained_goal:
        frontmatter.update(
            {
                "execution_mode": "sustained-goal",
                "execution_budget": str(args.token_budget),
                "execution_timebox": format(args.timeout_seconds, "g"),
            }
        )
    update_handoff_frontmatter(handoff, frontmatter)
    route_record = {
        "selected_executor": executor,
        "reason": reason,
        "model": model,
        "model_source": model_resolution["source"],
        "capability": capability,
        "level": capability,
        "reasoning_effort": reasoning_effort,
    }
    print(json.dumps(route_record, sort_keys=True), file=sys.stderr)
    completed = subprocess.run(
        command_for(args, executor, capability, model_resolution),
        cwd=repo,
        check=False,
    )
    return completed.returncode


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        print(f"run_task: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

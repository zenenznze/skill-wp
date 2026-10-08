#!/usr/bin/env python3
"""Deterministic workflow governance for wp controllers.

This module is additive to the v2 task-package parser.  It validates a separate
``workflow_governance_version: 1`` control record and does not change HANDOFF,
result, graph, or runner wire formats.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


GOVERNANCE_VERSION = 1
RISK_LEVELS = ("read-only", "tiny", "medium", "high-risk")
RISK_RANK = {name: index for index, name in enumerate(RISK_LEVELS)}
AGENT_ROLES = {"orchestrator", "sole-writer", "reviewer", "scout", "monitor"}
ALL_ROLES = AGENT_ROLES | {"human-approver"}
READ_ONLY_ROLES = {"reviewer", "scout", "monitor"}
MONITOR_INTERVAL_MIN = 60
MONITOR_INTERVAL_MAX = 120
REPEATED_ROOT_CAUSE_LIMIT = 2
ALLOWED_APPROVAL_ACTIONS = {"implement", "verify", "commit", "push", "deploy"}
ALLOWED_DEPLOYMENT_REQUESTERS = {"orchestrator", "controller"}

HIGH_RISK_SIGNALS = {
    "production_change",
    "deployment",
    "destructive_operation",
    "credential_access",
    "security_boundary",
    "permission_expansion",
    "data_migration",
    "external_publication",
    "irreversible_operation",
}
MEDIUM_RISK_SIGNALS = {
    "dependency_change",
    "schema_change",
    "network_write",
    "service_restart",
    "multi_repository_write",
    "unknown_scope",
}
OTHER_RISK_SIGNALS = {
    "generated_files",
    "concurrent_writers",
    "broad_refactor",
}
KNOWN_RISK_SIGNALS = HIGH_RISK_SIGNALS | MEDIUM_RISK_SIGNALS | OTHER_RISK_SIGNALS
TINY_DISQUALIFIERS = KNOWN_RISK_SIGNALS


class GovernanceError(ValueError):
    """Raised for malformed deterministic governance input."""


def _strings(value: Any, field: str, *, non_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GovernanceError(f"{field} must be an array of non-empty strings")
    result = [item.strip() for item in value]
    if non_empty and not result:
        raise GovernanceError(f"{field} must not be empty")
    return result


def _strict_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GovernanceError(f"{field} must be an integer >= {minimum}")
    return value


def classify_risk(task: dict[str, Any]) -> dict[str, Any]:
    """Classify risk conservatively from explicit, machine-checkable signals."""
    if not isinstance(task, dict):
        raise GovernanceError("task must be an object")
    required_fields = {
        "writes", "executes", "signals", "file_count", "bounded_scope", "reversible"
    }
    missing = sorted(required_fields - set(task))
    if missing:
        raise GovernanceError("task is missing required fields: " + ", ".join(missing))
    signals = set(_strings(task["signals"], "signals"))
    writes = task["writes"]
    executes = task["executes"]
    if not isinstance(writes, bool) or not isinstance(executes, bool):
        raise GovernanceError("writes and executes must be booleans")
    file_count = _strict_int(task["file_count"], "file_count")
    bounded_scope = task["bounded_scope"]
    reversible = task["reversible"]
    if not isinstance(bounded_scope, bool) or not isinstance(reversible, bool):
        raise GovernanceError("bounded_scope and reversible must be booleans")
    unknown_signals = signals - KNOWN_RISK_SIGNALS

    reasons: list[str] = []
    if signals & HIGH_RISK_SIGNALS:
        level = "high-risk"
        reasons.append("high-risk signal: " + ", ".join(sorted(signals & HIGH_RISK_SIGNALS)))
    elif unknown_signals:
        level = "medium"
        reasons.append("unknown risk signal: " + ", ".join(sorted(unknown_signals)))
    elif not writes and not executes and not signals:
        level = "read-only"
        reasons.append("no writes, execution, or side-effect signals")
    elif (
        writes
        and not executes
        and bounded_scope
        and reversible
        and 0 < file_count <= 2
        and not signals & TINY_DISQUALIFIERS
    ):
        level = "tiny"
        reasons.append("bounded reversible write affecting at most two files")
    else:
        level = "medium"
        reasons.append("default conservative gate for work with side effects or uncertainty")
        if signals & MEDIUM_RISK_SIGNALS:
            reasons.append("medium-risk signal: " + ", ".join(sorted(signals & MEDIUM_RISK_SIGNALS)))

    requested = task.get("minimum_risk")
    if requested is not None:
        if not isinstance(requested, str) or requested not in RISK_RANK:
            raise GovernanceError("minimum_risk must be read-only, tiny, medium, or high-risk")
        if RISK_RANK[requested] > RISK_RANK[level]:
            reasons.append(f"conservatively raised to requested minimum {requested}")
            level = requested

    return {
        "risk": level,
        "human_gate_required": level in {"medium", "high-risk"},
        "reasons": reasons,
    }


def validate_slots(assignments: list[dict[str, Any]], writes: bool) -> list[str]:
    """Validate role identity and least-privilege slot permissions."""
    errors: list[str] = []
    if not isinstance(assignments, list):
        return ["assignments must be an array"]
    writer_count = 0
    seen_roles: set[str] = set()
    for index, slot in enumerate(assignments):
        if not isinstance(slot, dict):
            errors.append(f"assignments[{index}] must be an object")
            continue
        role = slot.get("role")
        if not isinstance(role, str) or not role.strip() or role not in ALL_ROLES:
            errors.append(f"assignments[{index}].role is invalid")
            continue
        if role in seen_roles:
            errors.append(f"role {role} may be assigned only once")
        seen_roles.add(role)
        actor_type = slot.get("actor_type")
        if not isinstance(actor_type, str) or actor_type not in {"agent", "human"}:
            errors.append(f"assignments[{index}].actor_type is invalid")
            continue
        can_write = slot.get("can_write")
        can_deploy = slot.get("can_deploy")
        if not isinstance(can_write, bool) or not isinstance(can_deploy, bool):
            errors.append(f"assignments[{index}] permissions must be booleans")
            continue
        if role == "sole-writer":
            writer_count += 1
            if actor_type != "agent":
                errors.append("sole-writer must be an agent")
            if not can_write:
                errors.append("sole-writer must have write permission")
        elif role in READ_ONLY_ROLES and (can_write or can_deploy):
            errors.append(f"{role} must be read-only and cannot deploy")
        elif role == "human-approver":
            if actor_type != "human":
                errors.append("human-approver cannot be impersonated by an agent")
            if can_write or can_deploy:
                errors.append("human-approver records decisions but receives no runner permissions")
        elif actor_type != "agent":
            errors.append(f"{role} must be an agent")
        if role != "sole-writer" and can_write:
            errors.append(f"{role} cannot hold repository write permission")
        if can_deploy:
            errors.append("no workflow role receives deployment permission from slot assignment")
    if "orchestrator" not in seen_roles:
        errors.append("every workflow requires one orchestrator")
    if writes and writer_count != 1:
        errors.append("writing work requires exactly one sole-writer")
    if not writes and writer_count > 1:
        errors.append("at most one sole-writer may be assigned")
    return errors


def pending_signoff(scope: list[str]) -> dict[str, Any]:
    """Create the only signoff state an agent may initialize."""
    scopes = _strings(scope, "scope")
    return {"status": "pending", "scope": scopes, "decision_by": None, "statement": None}


def approval_allows(
    approval: dict[str, Any] | None,
    action: str,
    required_scope: list[str],
) -> tuple[bool, list[str]]:
    """Require explicit human approval whose action and scope cover the request."""
    reasons: list[str] = []
    try:
        required = _strings(required_scope, "required_scope", non_empty=True)
    except GovernanceError as exc:
        return False, [str(exc)]
    if not isinstance(action, str) or action not in ALLOWED_APPROVAL_ACTIONS:
        return False, ["action is not an allowed approval action"]
    if not isinstance(approval, dict):
        return False, ["approval is missing"]
    if approval.get("status") != "approved":
        reasons.append("approval status is not approved")
    if approval.get("actor_type") != "human":
        reasons.append("approval must come from a human")
    try:
        actions = _strings(approval.get("actions"), "approval.actions", non_empty=True)
    except GovernanceError as exc:
        actions = []
        reasons.append(str(exc))
    try:
        scopes = _strings(approval.get("scope"), "approval.scope", non_empty=True)
    except GovernanceError as exc:
        scopes = []
        reasons.append(str(exc))
    unknown_actions = set(actions) - ALLOWED_APPROVAL_ACTIONS
    if unknown_actions:
        reasons.append("approval.actions contains unsupported actions: " + ", ".join(sorted(unknown_actions)))
    if actions and action not in actions:
        reasons.append(f"approval does not authorize action {action}")
    if scopes and not set(required).issubset(set(scopes)):
        reasons.append("approval scope does not cover the requested scope")
    if not isinstance(approval.get("statement"), str) or not approval.get("statement", "").strip():
        reasons.append("approval must preserve the human statement")
    return not reasons, reasons


def monitor_policy(interval_seconds: int, consecutive_same_root_cause: int) -> dict[str, Any]:
    """Return deterministic monitor behavior for one observation cycle."""
    interval_seconds = _strict_int(interval_seconds, "interval_seconds")
    if not MONITOR_INTERVAL_MIN <= interval_seconds <= MONITOR_INTERVAL_MAX:
        raise GovernanceError("monitor interval must be between 60 and 120 seconds")
    consecutive_same_root_cause = _strict_int(
        consecutive_same_root_cause, "consecutive_same_root_cause"
    )
    stop = consecutive_same_root_cause >= REPEATED_ROOT_CAUSE_LIMIT
    return {
        "interval_seconds": interval_seconds,
        "automatic_retry_allowed": not stop,
        "status": "blocked" if stop else "continue",
        "escalate": stop,
        "reason": (
            "same root cause observed twice; stop automatic retry and escalate"
            if stop
            else "continue bounded observation"
        ),
    }


def deployment_gate(record: dict[str, Any], required_scope: list[str]) -> dict[str, Any]:
    """Validate the independent deployment authorization and safety gate."""
    if not isinstance(record, dict):
        raise GovernanceError("deployment record must be an object")
    errors: list[str] = []
    requester = record.get("requester")
    if not isinstance(requester, str) or requester not in ALLOWED_DEPLOYMENT_REQUESTERS:
        errors.append("requester must be an allowed verified controller identity")
    allowed, approval_errors = approval_allows(
        record.get("approval"), "deploy", required_scope
    )
    if not allowed:
        errors.extend(approval_errors)
    for field in (
        "preflight_passed",
        "backup_verified",
        "health_check_planned",
        "log_check_planned",
        "rollback_ready",
    ):
        if record.get(field) is not True:
            errors.append(f"{field} must be true")
    return {"allowed": not errors, "errors": errors}


def select_herdr_space(
    cwd: str,
    workspaces: list[dict[str, Any]],
    topic_requires_dedicated_space: bool,
    topic_label: str | None = None,
) -> dict[str, Any]:
    """Select the unique cwd space, or require a dedicated space for a topic."""
    if not isinstance(cwd, str) or not cwd.strip():
        raise GovernanceError("cwd must be a non-empty string")
    if not isinstance(workspaces, list):
        raise GovernanceError("workspaces must be an array")
    if not isinstance(topic_requires_dedicated_space, bool):
        raise GovernanceError("topic_requires_dedicated_space must be a boolean")
    normalized = os.path.realpath(os.path.expanduser(cwd))
    if topic_requires_dedicated_space:
        if not isinstance(topic_label, str) or not topic_label.strip():
            raise GovernanceError("a dedicated topic space requires a semantic label")
        return {"action": "create", "cwd": normalized, "label": topic_label.strip()}
    matches = [
        item for item in workspaces
        if isinstance(item, dict)
        and isinstance(item.get("cwd"), str)
        and os.path.realpath(os.path.expanduser(item["cwd"])) == normalized
    ]
    if len(matches) == 1:
        return {"action": "reuse", "workspace_id": matches[0].get("workspace_id"), "cwd": normalized}
    if len(matches) > 1:
        raise GovernanceError("multiple workspaces claim the same cwd; organize them before dispatch")
    return {"action": "create", "cwd": normalized, "label": Path(normalized).name}


def validate_herdr_plan(plan: dict[str, Any]) -> list[str]:
    """Validate the Pi-only, tab-oriented Herdr controller topology."""
    if not isinstance(plan, dict):
        return ["Herdr plan must be an object"]
    errors: list[str] = []
    controller_tabs = plan.get("controller_tabs")
    if isinstance(controller_tabs, bool) or not isinstance(controller_tabs, int) or controller_tabs != 1:
        errors.append("Herdr plan requires exactly one controller tab")
    split_panes = plan.get("split_panes")
    if not isinstance(split_panes, bool):
        errors.append("split_panes must be a boolean")
    elif split_panes:
        errors.append("default Herdr workflow forbids split panes")
    if plan.get("no_focus") is not True:
        errors.append("background tabs must use no-focus")
    tabs = plan.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        errors.append("Herdr plan requires at least one semantic background tab")
        return errors
    for index, tab in enumerate(tabs):
        if not isinstance(tab, dict):
            errors.append(f"tabs[{index}] must be an object")
            continue
        if tab.get("kind") != "pi":
            errors.append(f"tabs[{index}] must start with --kind pi")
        label = tab.get("label")
        generic_labels = {"worker", "reviewer", "agent", "执行", "任务"}
        if (
            not isinstance(label, str)
            or not label.strip()
            or label.strip().isdigit()
            or label.strip().lower() in generic_labels
        ):
            errors.append(f"tabs[{index}] requires a semantic label")
        pane_count = tab.get("pane_count")
        if isinstance(pane_count, bool) or not isinstance(pane_count, int) or pane_count != 1:
            errors.append(f"tabs[{index}] must contain exactly one pane")
        close_requested = tab.get("close_requested", False)
        if not isinstance(close_requested, bool):
            errors.append(f"tabs[{index}].close_requested must be a boolean")
        elif close_requested:
            if tab.get("accepted") is not True:
                errors.append(f"tabs[{index}] cannot close before independent acceptance")
            completed_label = tab.get("completed_label")
            if not isinstance(completed_label, str) or not completed_label.startswith("完成-"):
                errors.append(f"tabs[{index}] must be renamed 完成-... before close")
        for field in ("workspace_id", "tab_id", "pane_id", "agent_id"):
            if not isinstance(tab.get(field), str) or not tab[field].strip():
                errors.append(f"tabs[{index}].{field} is required for reporting")
    if plan.get("preserve_controller_tab") is not True:
        errors.append("controller tab must be preserved")
    if plan.get("timeout_action") != "get-read-before-retry":
        errors.append("timeout handling must get/read现场 before retry")
    return errors


def validate_governance(record: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete versioned governance record."""
    if not isinstance(record, dict):
        return {"valid": False, "errors": ["governance record must be an object"]}
    errors: list[str] = []
    version = record.get("workflow_governance_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != GOVERNANCE_VERSION:
        errors.append(f"workflow_governance_version must be integer {GOVERNANCE_VERSION}")
    task = record.get("task")
    try:
        if not isinstance(task, dict):
            raise GovernanceError("task must be an object")
        risk = classify_risk(task)
    except GovernanceError as exc:
        risk = None
        errors.append(str(exc))
    writes = task.get("writes", False) if isinstance(task, dict) else False
    errors.extend(validate_slots(record.get("slots", []), writes if isinstance(writes, bool) else False))
    if risk and risk["human_gate_required"]:
        allowed, approval_errors = approval_allows(
            record.get("approval"), "implement", record.get("approval_scope", [])
        )
        if not allowed:
            errors.extend(approval_errors)
    signoff = record.get("signoff")
    if not isinstance(signoff, dict) or signoff.get("status") != "pending":
        errors.append("agent-produced signoff must remain pending")
    else:
        try:
            _strings(signoff.get("scope"), "signoff.scope", non_empty=True)
        except GovernanceError as exc:
            errors.append(str(exc))
    if isinstance(signoff, dict) and signoff.get("status") == "pending" and (
        signoff.get("decision_by") is not None or signoff.get("statement") is not None
    ):
        errors.append("pending signoff cannot contain a human decision")
    return {"valid": not errors, "risk": risk, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="workflow governance JSON record")
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        result = validate_governance(record)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"valid": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("valid") else 2


if __name__ == "__main__":
    raise SystemExit(main())

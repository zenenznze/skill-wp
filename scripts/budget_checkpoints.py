#!/usr/bin/env python3
"""Deterministic token-budget checkpoint decisions for Codex Goals."""

from __future__ import annotations

import re
from typing import Any, Callable


GOAL_UPDATED_METHOD = "thread/goal/updated"
STEERING_METHOD = "turn/steer"
CHECKPOINT_STATE_VERSION = 2

THRESHOLD_SPECS: tuple[dict[str, Any], ...] = (
    {
        "percent": 75,
        "name": "checkpoint",
        "instruction": (
            "Checkpoint now: continue the current work while externalizing material "
            "conclusions, progress, key files, validation evidence, and the next "
            "action into durable state such as HANDOFF and workspace artifacts. "
            "Do not stop; continue the current atomic operation."
        ),
    },
    {
        "percent": 90,
        "name": "convergence",
        "instruction": (
            "Converge now: do not open large new exploration branches. Finish the "
            "current atomic operation, persist the code and HANDOFF state, run the "
            "necessary validation, and leave a precise resume point before yielding."
        ),
    },
)


def protocol_evidence() -> dict[str, Any]:
    """Return the exact installed App Server fields this monitor consumes."""
    return {
        "goal_accounting_notification": GOAL_UPDATED_METHOD,
        "goal_accounting_fields": [
            "params.threadId",
            "params.turnId",
            "params.goal.threadId",
            "params.goal.tokenBudget",
            "params.goal.tokensUsed",
            "params.goal.status",
            "params.goal.updatedAt",
        ],
        "diagnostic_token_notification": "thread/tokenUsage/updated",
        "steering_method": STEERING_METHOD,
        "steering_fields": ["threadId", "expectedTurnId", "input"],
        "verification": "installed Codex App Server generated schema",
    }


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_goal_update(params: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse native Goal budget accounting from ThreadGoalUpdatedNotification."""
    if not isinstance(params, dict):
        return None, "notification params must be an object"
    thread_id = params.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        return None, "params.threadId must be a non-empty string"
    turn_id = params.get("turnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return None, "params.turnId must be a non-empty string or null"
    goal = params.get("goal")
    if not isinstance(goal, dict):
        return None, "params.goal must be an object"
    if goal.get("threadId") != thread_id:
        return None, "params.goal.threadId must match params.threadId"
    token_budget = goal.get("tokenBudget")
    if (
        not isinstance(token_budget, int)
        or isinstance(token_budget, bool)
        or token_budget <= 0
    ):
        return None, "params.goal.tokenBudget must be a positive integer"
    tokens_used = goal.get("tokensUsed")
    if not _nonnegative_integer(tokens_used):
        return None, "params.goal.tokensUsed must be a non-negative integer"
    status = goal.get("status")
    if not isinstance(status, str) or not status:
        return None, "params.goal.status must be a non-empty string"
    updated_at = goal.get("updatedAt")
    if not _nonnegative_integer(updated_at):
        return None, "params.goal.updatedAt must be a non-negative integer"
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "token_budget": token_budget,
        "tokens_used": tokens_used,
        "goal_status": status,
        "goal_updated_at": updated_at,
    }, None


def thresholds_due(
    tokens_used: int, authorized_budget: int, claimed: Any
) -> list[dict[str, Any]]:
    """Return unclaimed thresholds reached by native Goal accounting."""
    if not _nonnegative_integer(tokens_used):
        raise ValueError("tokens_used must be a non-negative integer")
    if not isinstance(authorized_budget, int) or isinstance(authorized_budget, bool):
        raise ValueError("authorized_budget must be an integer")
    if authorized_budget <= 0:
        raise ValueError("authorized_budget must be positive")
    claimed_values = {str(value) for value in claimed} if claimed is not None else set()
    return [
        spec
        for spec in THRESHOLD_SPECS
        if str(spec["percent"]) not in claimed_values
        and tokens_used * 100 >= authorized_budget * int(spec["percent"])
    ]


def new_checkpoint_state(task_id: str) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_STATE_VERSION,
        "task_id": task_id,
        "active_generation_id": None,
        "generations": [],
        "updated_at": None,
    }


def validate_checkpoint_state(state: Any, task_id: str) -> str | None:
    if not isinstance(state, dict):
        return "checkpoint state must be an object"
    if state.get("version") != CHECKPOINT_STATE_VERSION:
        return f"checkpoint state version must be {CHECKPOINT_STATE_VERSION}"
    if state.get("task_id") != task_id:
        return "checkpoint state task_id does not match the task"
    if not isinstance(state.get("generations"), list):
        return "checkpoint state generations must be an array"
    return None


def _generation_number(value: str) -> int:
    match = re.fullmatch(r"generation-(\d+)", value)
    return int(match.group(1)) if match else 0


def ensure_budget_generation(
    state: dict[str, Any],
    task_id: str,
    thread_id: str,
    goal_id: str,
    authorized_budget: int,
    now: str,
) -> dict[str, Any]:
    """Reuse the active same-budget generation or create a new one."""
    state_error = validate_checkpoint_state(state, task_id)
    if state_error is not None:
        raise ValueError(state_error)
    if not isinstance(authorized_budget, int) or isinstance(authorized_budget, bool):
        raise ValueError("authorized_budget must be an integer")
    if authorized_budget <= 0:
        raise ValueError("authorized_budget must be positive")
    generations = state["generations"]
    active_id = state.get("active_generation_id")
    for generation in generations:
        if not isinstance(generation, dict):
            continue
        if (
            generation.get("generation_id") == active_id
            and generation.get("thread_id") == thread_id
            and generation.get("authorized_token_budget") == authorized_budget
        ):
            state["updated_at"] = now
            return generation

    next_number = max(
        (
            _generation_number(generation.get("generation_id", ""))
            for generation in generations
            if isinstance(generation, dict)
        ),
        default=0,
    ) + 1
    generation = {
        "generation_id": f"generation-{next_number:03d}",
        "thread_id": thread_id,
        "goal_id": goal_id,
        "authorized_token_budget": authorized_budget,
        "created_at": now,
        "updated_at": now,
        "thresholds": {},
        "max_observed_tokens_used": 0,
        "last_observation": None,
        "valid_event_count": 0,
        "duplicate_event_count": 0,
        "out_of_order_event_count": 0,
        "malformed_event_count": 0,
        "deferred_event_count": 0,
        "last_malformed_event": None,
        "last_deferred_event": None,
    }
    generations.append(generation)
    state["active_generation_id"] = generation["generation_id"]
    state["updated_at"] = now
    return generation


def record_valid_observation(
    generation: dict[str, Any], observation: dict[str, Any], now: str
) -> None:
    previous = generation.get("last_observation")
    tokens_used = observation["tokens_used"]
    if isinstance(previous, dict):
        if (
            previous.get("tokens_used") == tokens_used
            and previous.get("turn_id") == observation.get("turn_id")
            and previous.get("goal_updated_at") == observation.get("goal_updated_at")
        ):
            generation["duplicate_event_count"] += 1
        if tokens_used < generation["max_observed_tokens_used"]:
            generation["out_of_order_event_count"] += 1
    generation["valid_event_count"] += 1
    generation["max_observed_tokens_used"] = max(
        generation["max_observed_tokens_used"], tokens_used
    )
    generation["last_observation"] = {**observation, "observed_at": now}
    generation["updated_at"] = now


def record_malformed_observation(
    generation: dict[str, Any], reason: str, now: str
) -> None:
    generation["malformed_event_count"] += 1
    generation["last_malformed_event"] = {"reason": reason, "observed_at": now}
    generation["updated_at"] = now


def record_deferred_observation(
    generation: dict[str, Any], observation: dict[str, Any], now: str
) -> None:
    generation["deferred_event_count"] += 1
    generation["last_deferred_event"] = {
        "reason": "threshold reached without an active turnId",
        "tokens_used": observation["tokens_used"],
        "observed_at": now,
    }
    generation["updated_at"] = now


def claim_threshold(
    generation: dict[str, Any],
    spec: dict[str, Any],
    observation: dict[str, Any],
    now: str,
) -> dict[str, Any] | None:
    thresholds = generation["thresholds"]
    key = str(spec["percent"])
    if key in thresholds:
        return None
    record = {
        "threshold_percent": spec["percent"],
        "threshold_name": spec["name"],
        "authorized_token_budget": generation["authorized_token_budget"],
        "observed_tokens_used": observation["tokens_used"],
        "observed_turn_id": observation["turn_id"],
        "claimed_at": now,
        "state": "pending",
        "steering_method": STEERING_METHOD,
        "instruction": spec["instruction"],
    }
    thresholds[key] = record
    generation["updated_at"] = now
    return record


def record_steering_outcome(
    generation: dict[str, Any],
    percent: int,
    outcome: str,
    now: str,
    error: str | None = None,
) -> None:
    record = generation["thresholds"].get(str(percent))
    if not isinstance(record, dict):
        raise ValueError(f"threshold {percent} was not claimed")
    record["state"] = outcome
    record["outcome_at"] = now
    if error:
        record["error"] = error
    generation["updated_at"] = now


class BudgetCheckpointController:
    """Apply native Goal updates and persist exact-once threshold claims."""

    def __init__(
        self,
        state: dict[str, Any],
        task_id: str,
        thread_id: str,
        goal_id: str,
        authorized_budget: int,
        persist: Callable[[], None],
        now: Callable[[], str],
    ) -> None:
        self.state = state
        self.thread_id = thread_id
        self.persist = persist
        self.now = now
        self.generation = ensure_budget_generation(
            state, task_id, thread_id, goal_id, authorized_budget, now()
        )
        persist()

    def process(
        self,
        params: Any,
        steer: Callable[[str, str], tuple[bool, str | None]],
    ) -> dict[str, Any]:
        observation, error = parse_goal_update(params)
        if error is not None:
            record_malformed_observation(self.generation, error, self.now())
            self.persist()
            return {"status": "malformed", "error": error, "signals": []}
        assert observation is not None
        if observation["thread_id"] != self.thread_id:
            error = "params.threadId does not match the active thread"
            record_malformed_observation(self.generation, error, self.now())
            self.persist()
            return {"status": "malformed", "error": error, "signals": []}
        if observation["token_budget"] != self.generation["authorized_token_budget"]:
            error = (
                "native goal tokenBudget does not match the authorized budget "
                f"({observation['token_budget']} != "
                f"{self.generation['authorized_token_budget']})"
            )
            record_malformed_observation(self.generation, error, self.now())
            self.persist()
            return {"status": "budget_mismatch", "error": error, "signals": []}

        observed_at = self.now()
        record_valid_observation(self.generation, observation, observed_at)
        self.persist()
        due = thresholds_due(
            observation["tokens_used"],
            self.generation["authorized_token_budget"],
            self.generation["thresholds"].keys(),
        )
        if not due or observation["goal_status"] != "active":
            return {"status": "observed", "observation": observation, "signals": []}
        turn_id = observation["turn_id"]
        if turn_id is None:
            record_deferred_observation(self.generation, observation, self.now())
            self.persist()
            return {"status": "deferred", "observation": observation, "signals": []}

        signals: list[dict[str, Any]] = []
        for spec in due:
            claim = claim_threshold(self.generation, spec, observation, self.now())
            if claim is None:
                continue
            # Persist the claim before sending so a process restart cannot duplicate it.
            self.persist()
            sent, send_error = steer(claim["instruction"], turn_id)
            outcome = "sent" if sent else "failed"
            record_steering_outcome(
                self.generation,
                int(spec["percent"]),
                outcome,
                self.now(),
                send_error,
            )
            self.persist()
            signal = {**claim, "state": outcome}
            if send_error:
                signal["error"] = send_error
            signals.append(signal)
        return {"status": "observed", "observation": observation, "signals": signals}

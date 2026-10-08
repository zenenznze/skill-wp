from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = str(ROOT / "scripts")
if SCRIPT_PATH not in sys.path:
    sys.path.insert(0, SCRIPT_PATH)

SPEC = importlib.util.spec_from_file_location(
    "codex_goal", ROOT / "scripts" / "run_codex_goal.py"
)
assert SPEC and SPEC.loader
codex_goal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_goal)

import budget_checkpoints  # noqa: E402


class CodexGoalTests(unittest.TestCase):
    def test_resume_command_uses_common_agent_name(self) -> None:
        command = codex_goal.resume_command(
            "20260817-goal", 2, token_budget=True, sustained=True
        )
        self.assertIn("--agent codex", command)
        self.assertIn("--resume", command)
        self.assertIn("--token-budget", command)
        self.assertIn("--sustained-goal", command)

    def test_failure_result_points_to_skill_state(self) -> None:
        os.environ["WP_HANDOFF_RELATIVE"] = (
            "wp-state/repos/demo/tasks/20260817-goal/HANDOFF.md"
        )
        try:
            result = codex_goal.failure_result(
                "20260817-goal", "inv", "failed", "log", "timeout", None
            )
        finally:
            os.environ.pop("WP_HANDOFF_RELATIVE", None)
        self.assertEqual(
            result["handoff_path"],
            "wp-state/repos/demo/tasks/20260817-goal/HANDOFF.md",
        )

    def test_model_selection_rejects_unadvertised_model(self) -> None:
        class FakeServer:
            pass

        def request(_server, method, _params):
            self.assertEqual(method, "model/list")
            return {
                "data": [
                    {"id": "other", "supportedReasoningEfforts": ["xhigh"]}
                ]
            }

        with self.assertRaises(codex_goal.ModelSelectionError):
            codex_goal.discover_model(
                FakeServer(), request, "gpt-5.6-sol", "xhigh"
            )

    @staticmethod
    def goal_update(
        tokens_used: int,
        budget: int = 100,
        thread_id: str = "thread-1",
        turn_id: str | None = "turn-1",
        status: str = "active",
        updated_at: int | None = None,
    ) -> dict:
        return {
            "threadId": thread_id,
            "turnId": turn_id,
            "goal": {
                "threadId": thread_id,
                "objective": "bounded task",
                "status": status,
                "tokenBudget": budget,
                "tokensUsed": tokens_used,
                "timeUsedSeconds": 1,
                "createdAt": 1,
                "updatedAt": tokens_used if updated_at is None else updated_at,
            },
        }

    def controller(
        self, budget: int = 100
    ) -> tuple[
        budget_checkpoints.BudgetCheckpointController,
        dict,
        list,
    ]:
        state = budget_checkpoints.new_checkpoint_state(
            "20260820-budget-checkpoints"
        )
        persisted = []
        clock = iter(
            f"2026-08-20T00:00:{index:02d}+00:00" for index in range(100)
        )
        controller = budget_checkpoints.BudgetCheckpointController(
            state,
            "20260820-budget-checkpoints",
            "thread-1",
            "goal-1",
            budget,
            lambda: persisted.append(copy.deepcopy(state)),
            lambda: next(clock),
        )
        return controller, state, persisted

    def test_goal_schema_and_threshold_math(self) -> None:
        parsed, error = budget_checkpoints.parse_goal_update(
            self.goal_update(74)
        )
        self.assertIsNone(error)
        self.assertEqual(parsed["tokens_used"], 74)
        self.assertEqual(parsed["token_budget"], 100)
        self.assertEqual(
            [
                item["percent"]
                for item in budget_checkpoints.thresholds_due(75, 100, [])
            ],
            [75],
        )
        self.assertEqual(
            [
                item["percent"]
                for item in budget_checkpoints.thresholds_due(90, 100, [])
            ],
            [75, 90],
        )
        self.assertEqual(
            budget_checkpoints.thresholds_due(749, 1000, []), []
        )

    def test_protocol_evidence_uses_native_goal_accounting(self) -> None:
        evidence = budget_checkpoints.protocol_evidence()
        self.assertEqual(
            evidence["goal_accounting_notification"], "thread/goal/updated"
        )
        self.assertIn(
            "params.goal.tokensUsed", evidence["goal_accounting_fields"]
        )
        self.assertIn(
            "params.goal.tokenBudget", evidence["goal_accounting_fields"]
        )
        self.assertEqual(
            evidence["diagnostic_token_notification"],
            "thread/tokenUsage/updated",
        )

    def test_controller_emits_both_thresholds_once_in_order(self) -> None:
        controller, state, _ = self.controller()
        sent = []

        def steer(instruction, turn_id):
            sent.append((instruction, turn_id))
            return True, None

        result = controller.process(self.goal_update(95), steer)
        self.assertEqual(
            [item["threshold_percent"] for item in result["signals"]],
            [75, 90],
        )
        self.assertIn("externalizing", sent[0][0])
        self.assertIn(
            "do not open large new exploration branches", sent[1][0].lower()
        )
        self.assertEqual([item[1] for item in sent], ["turn-1", "turn-1"])

        duplicate = controller.process(self.goal_update(95), steer)
        out_of_order = controller.process(
            self.goal_update(80, turn_id="turn-old"), steer
        )
        self.assertEqual(duplicate["signals"], [])
        self.assertEqual(out_of_order["signals"], [])
        self.assertEqual(len(sent), 2)
        generation = state["generations"][0]
        self.assertEqual(generation["duplicate_event_count"], 1)
        self.assertEqual(generation["out_of_order_event_count"], 1)

    def test_restart_and_same_thread_resume_rehydrate_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget-checkpoints.json"
            controller, state, _ = self.controller()
            first_sent = []
            controller.process(
                self.goal_update(80),
                lambda instruction, _turn: (
                    first_sent.append(instruction) or (True, None)
                ),
            )
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(len(first_sent), 1)

            rehydrated_state = json.loads(path.read_text(encoding="utf-8"))
            restarted_sent = []
            restarted = budget_checkpoints.BudgetCheckpointController(
                rehydrated_state,
                "20260820-budget-checkpoints",
                "thread-1",
                "goal-1",
                100,
                lambda: path.write_text(
                    json.dumps(rehydrated_state), encoding="utf-8"
                ),
                lambda: "2026-08-20T00:01:00+00:00",
            )
            result = restarted.process(
                self.goal_update(95),
                lambda instruction, _turn: (
                    restarted_sent.append(instruction) or (True, None)
                ),
            )
            self.assertEqual(
                [item["threshold_percent"] for item in result["signals"]],
                [90],
            )
            self.assertEqual(len(restarted_sent), 1)
            self.assertEqual(
                rehydrated_state["active_generation_id"], "generation-001"
            )

    def test_threshold_without_active_turn_is_deferred_not_claimed(self) -> None:
        controller, state, _ = self.controller()
        sent = []
        deferred = controller.process(
            self.goal_update(80, turn_id=None),
            lambda instruction, turn: (
                sent.append((instruction, turn)) or (True, None)
            ),
        )
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(state["generations"][0]["thresholds"], {})

        resumed = controller.process(
            self.goal_update(81, turn_id="turn-2"),
            lambda instruction, turn: (
                sent.append((instruction, turn)) or (True, None)
            ),
        )
        self.assertEqual(
            [item["threshold_percent"] for item in resumed["signals"]], [75]
        )
        self.assertEqual(sent[0][1], "turn-2")

    def test_failed_steering_is_recorded_and_not_retried(self) -> None:
        controller, state, _ = self.controller()
        calls = []

        def steer(instruction, turn_id):
            calls.append((instruction, turn_id))
            return False, "turn/steer unavailable"

        first = controller.process(self.goal_update(80), steer)
        second = controller.process(self.goal_update(81), steer)
        self.assertEqual(first["signals"][0]["state"], "failed")
        self.assertEqual(second["signals"], [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            state["generations"][0]["thresholds"]["75"]["error"],
            "turn/steer unavailable",
        )

    def test_larger_budget_creates_generation_and_preserves_evidence(self) -> None:
        controller, state, _ = self.controller(100)
        controller.process(
            self.goal_update(90), lambda _instruction, _turn: (True, None)
        )
        second_sent = []
        larger = budget_checkpoints.BudgetCheckpointController(
            state,
            "20260820-budget-checkpoints",
            "thread-1",
            "goal-1",
            200,
            lambda: None,
            lambda: "2026-08-20T00:02:00+00:00",
        )
        result = larger.process(
            self.goal_update(150, budget=200, turn_id="turn-2"),
            lambda instruction, _turn: (
                second_sent.append(instruction) or (True, None)
            ),
        )
        self.assertEqual(len(state["generations"]), 2)
        self.assertEqual(state["active_generation_id"], "generation-002")
        self.assertEqual(
            set(state["generations"][0]["thresholds"]), {"75", "90"}
        )
        self.assertEqual(
            [item["threshold_percent"] for item in result["signals"]], [75]
        )
        self.assertEqual(len(second_sent), 1)

    def test_malformed_and_budget_mismatch_are_observable(self) -> None:
        controller, state, _ = self.controller()
        raw_usage = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {"total": {"totalTokens": 1_000_000}},
        }
        malformed = controller.process(
            raw_usage, lambda _instruction, _turn: (True, None)
        )
        self.assertEqual(malformed["status"], "malformed")
        self.assertEqual(malformed["signals"], [])

        mismatch = controller.process(
            self.goal_update(95, budget=200),
            lambda _instruction, _turn: (True, None),
        )
        self.assertEqual(mismatch["status"], "budget_mismatch")
        self.assertEqual(mismatch["signals"], [])
        self.assertEqual(
            state["generations"][0]["malformed_event_count"], 2
        )

    def test_terminal_goal_update_does_not_emit_soft_signals(self) -> None:
        controller, state, _ = self.controller()
        sent = []
        result = controller.process(
            self.goal_update(110, status="budgetLimited"),
            lambda instruction, turn: (
                sent.append((instruction, turn)) or (True, None)
            ),
        )
        self.assertEqual(result["signals"], [])
        self.assertEqual(sent, [])
        self.assertEqual(state["generations"][0]["thresholds"], {})

    def test_native_budget_limited_result_remains_blocked(self) -> None:
        result, command = codex_goal.blocked_result(
            "20260820-budget-checkpoints",
            "inv",
            "budgetLimited",
            "goal.json",
            2,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["transport"]["state"], "budgetLimited")
        self.assertIn("--token-budget", command)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "scripts")
import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

SPEC = importlib.util.spec_from_file_location("codex_goal", ROOT / "scripts" / "run_codex_goal.py")
assert SPEC and SPEC.loader
codex_goal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_goal)


class CodexGoalTests(unittest.TestCase):
    def test_resume_command_uses_common_agent_name(self) -> None:
        command = codex_goal.resume_command("20260817-goal", 2, token_budget=True, sustained=True)
        self.assertIn("--agent codex", command)
        self.assertIn("--resume", command)
        self.assertIn("--token-budget", command)
        self.assertIn("--sustained-goal", command)

    def test_failure_result_points_to_skill_state(self) -> None:
        os.environ["WP_HANDOFF_RELATIVE"] = "wp-state/repos/demo/tasks/20260817-goal/HANDOFF.md"
        try:
            result = codex_goal.failure_result("20260817-goal", "inv", "failed", "log", "timeout", None)
        finally:
            os.environ.pop("WP_HANDOFF_RELATIVE", None)
        self.assertEqual(result["handoff_path"], "wp-state/repos/demo/tasks/20260817-goal/HANDOFF.md")

    def test_model_selection_rejects_unadvertised_model(self) -> None:
        class FakeServer:
            pass

        def request(_server, method, _params):
            self.assertEqual(method, "model/list")
            return {"data": [{"id": "other", "supportedReasoningEfforts": ["xhigh"]}]}

        with self.assertRaises(codex_goal.ModelSelectionError):
            codex_goal.discover_model(FakeServer(), request, "gpt-5.6-sol", "xhigh")


if __name__ == "__main__":
    unittest.main()

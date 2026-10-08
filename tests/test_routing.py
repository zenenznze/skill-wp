from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "scripts" / "init_task.py"
RUN_TASK = ROOT / "scripts" / "run_task.py"
sys.path.insert(0, str(ROOT / "scripts"))
from protocol import task_relative_paths  # noqa: E402

SPEC = importlib.util.spec_from_file_location("run_task_module", RUN_TASK)
assert SPEC and SPEC.loader
RUN_TASK_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_TASK_MODULE)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260817-route-task"
        process = subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--task-id", self.task_id,
             "--goal", "Implement and verify a routed task."],
            check=True, capture_output=True, text=True,
        )
        self.paths = task_relative_paths(self.task_id, self.repo)
        self.handoff = self.paths["handoff"]

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def args(self, *extra: str):
        argv = ["run_task.py", "--repo", str(self.repo), "--task-id", self.task_id, *extra]
        with patch.object(sys, "argv", argv):
            return RUN_TASK_MODULE.parse_args()

    def roster(self, *, claude: str = "available", codex: str = "available", grok: str = "available") -> Path:
        path = self.repo / "roster.json"
        path.write_text(json.dumps({"agents": [
            {"name": "claude", "health": claude, "models": {"fast": ["haiku"], "balanced": ["sonnet"], "hard": ["opus"]}},
            {"name": "codex", "health": codex, "models": {"fast": ["gpt-5.6-luna"], "balanced": ["gpt-5.6-luna"], "hard": ["gpt-5.6-sol"]}},
            {"name": "grok", "health": grok, "models": {"fast": ["grok-4.5"], "balanced": ["grok-4.6"], "hard": ["grok-4.6"]}},
            {"name": "pi", "health": "available", "models": {"fast": [], "balanced": [], "hard": []}},
            {"name": "kimi", "health": "available", "models": {"fast": ["kimi-code/k3"], "balanced": ["kimi-code/k3"], "hard": ["kimi-code/k3"]}},
        ]}), encoding="utf-8")
        return path

    def test_level_derivation_and_legacy_alias(self) -> None:
        self.assertEqual(RUN_TASK_MODULE.derive_capability(self.args("--time-sensitive"), self.handoff), "fast")
        self.assertEqual(RUN_TASK_MODULE.derive_capability(self.args("--level", "hard"), self.handoff), "hard")
        self.assertEqual(RUN_TASK_MODULE.derive_capability(self.args("--level", "frontier"), self.handoff), "hard")

    def test_auto_route_prefers_codex_for_hard_and_grok_for_research(self) -> None:
        roster = self.roster()
        hard = self.args("--level", "hard", "--roster", str(roster))
        self.assertEqual(RUN_TASK_MODULE.select_executor(hard, self.handoff)[0], "codex")
        research = self.args("--research", "--roster", str(roster))
        selected, reason = RUN_TASK_MODULE.select_executor(research, self.handoff)
        self.assertEqual(selected, "grok")
        self.assertIn("Grok", reason)

    def test_auto_skips_degraded_preferred_client(self) -> None:
        roster = self.roster(claude="degraded")
        args = self.args("--level", "balanced", "--roster", str(roster))
        self.assertEqual(RUN_TASK_MODULE.select_executor(args, self.handoff)[0], "pi")

    def test_command_routes_pi_and_codex_with_common_continue_flag(self) -> None:
        pi_args = self.args("--agent", "pi", "--continue")
        pi_command = RUN_TASK_MODULE.command_for(pi_args, "pi", "balanced")
        self.assertIn("run_pi_executor.py", pi_command[1])
        self.assertIn("--resume", pi_command)
        codex_args = self.args("--agent", "codex", "--level", "hard")
        codex_command = RUN_TASK_MODULE.command_for(codex_args, "codex", "hard")
        self.assertIn("run_codex_goal.py", codex_command[1])
        self.assertIn("--model", codex_command)

    def test_sol_controller_defaults_codex_writer_to_luna_xhigh(self) -> None:
        with patch.dict(os.environ, {"PI_MODEL": "gpt-5.6-sol"}):
            default_args = self.args("--agent", "codex", "--level", "hard")
            resolved = RUN_TASK_MODULE.executor_model_resolution(
                default_args, "codex", "hard"
            )
            self.assertEqual(resolved["model"], "gpt-5.6-luna")
            self.assertEqual(resolved["source"], "sol-controller-complement")
            self.assertEqual(default_args.reasoning_effort, "xhigh")

            explicit_args = self.args(
                "--agent", "codex", "--level", "hard", "--model", "gpt-5.6-sol"
            )
            explicit = RUN_TASK_MODULE.executor_model_resolution(
                explicit_args, "codex", "hard"
            )
            self.assertEqual(explicit["model"], "gpt-5.6-sol")
            self.assertEqual(explicit["source"], "override")

    def test_attempt_limit_is_explicit(self) -> None:
        args = self.args("--attempt", "4", "--max-attempts", "3")
        with self.assertRaisesRegex(ValueError, "max-attempts"):
            RUN_TASK_MODULE.run(args)

    def test_v2_contract_refuses_before_executor_subprocess(self) -> None:
        args = self.args("--agent", "claude")
        with patch.object(RUN_TASK_MODULE.subprocess, "run") as launch:
            with self.assertRaisesRegex(ValueError, "atomic task preflight"):
                RUN_TASK_MODULE.run(args)
        launch.assert_not_called()

    def test_legacy_package_requires_explicit_compatibility_flag(self) -> None:
        text = self.handoff.read_text(encoding="utf-8")
        text = text.replace("task_protocol_version: 2\n", "", 1)
        start = text.index("# Atomic Work Contract")
        end = text.index("# Current Repository State", start)
        text = text[:start] + text[end:]
        self.handoff.write_text(text, encoding="utf-8")
        args = self.args("--agent", "claude")
        with patch.object(RUN_TASK_MODULE.subprocess, "run") as launch:
            with self.assertRaisesRegex(ValueError, "allow-legacy-task-package"):
                RUN_TASK_MODULE.run(args)
        launch.assert_not_called()

    def test_explicit_legacy_compatibility_flag_allows_routing(self) -> None:
        text = self.handoff.read_text(encoding="utf-8")
        text = text.replace("task_protocol_version: 2\n", "", 1)
        start = text.index("# Atomic Work Contract")
        end = text.index("# Current Repository State", start)
        text = text[:start] + text[end:]
        self.handoff.write_text(text, encoding="utf-8")
        args = self.args("--agent", "claude", "--allow-legacy-task-package")
        with patch.dict(
            os.environ,
            {
                "AGENT_BRIEF_SUPPRESS": "controller-value",
                "WP_ROUTING_TEST_VALUE": "preserved",
            },
        ):
            with patch.object(RUN_TASK_MODULE.subprocess, "run") as launch:
                launch.return_value.returncode = 0
                self.assertEqual(RUN_TASK_MODULE.run(args), 0)
            self.assertEqual(
                os.environ["AGENT_BRIEF_SUPPRESS"], "controller-value"
            )
        launch.assert_called_once()
        executor_env = launch.call_args.kwargs["env"]
        self.assertEqual(executor_env["AGENT_BRIEF_SUPPRESS"], "1")
        self.assertEqual(executor_env["WP_ROUTING_TEST_VALUE"], "preserved")


if __name__ == "__main__":
    unittest.main()

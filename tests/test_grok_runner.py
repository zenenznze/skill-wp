from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "scripts" / "init_task.py"
RUNNER = ROOT / "scripts" / "run_grok_executor.py"
sys.path.insert(0, str(ROOT / "scripts"))
import run_grok_executor as runner_module  # noqa: E402
from protocol import task_relative_paths  # noqa: E402


class GrokRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260814-grok-runner"
        subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--task-id", self.task_id,
             "--goal", "Exercise the Grok runner lifecycle."],
            check=True, capture_output=True, text=True,
        )
        self.paths = task_relative_paths(self.task_id, self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def fake_grok(self, body: str, models: str = "print('grok-4.5')") -> Path:
        executable = self.repo / "fake-grok"
        executable.write_text(
            "#!/usr/bin/env python3\nimport json, os, pathlib, re, sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'models':\n"
            + textwrap.indent(models + "\n", "    ")
            + textwrap.dedent(body), encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def success_body(self) -> str:
        return """
        print('grok-complete', flush=True)
        handoff_path = pathlib.Path(os.environ['AGENT_HANDOFF_PATH'])
        handoff = re.sub(r'^status:.*$', 'status: success', handoff_path.read_text(encoding='utf-8'), count=1, flags=re.M)
        handoff = re.sub(r'^runner_sentinel:.*\\n?', '', handoff, count=1, flags=re.M)
        handoff_path.write_text(handoff, encoding='utf-8')
        result = {'task_id': os.environ['AGENT_TASK_ID'], 'status': 'success', 'summary': 'Grok completed.',
                  'handoff_path': os.environ['AGENT_HANDOFF_RELATIVE'], 'changed_files': [],
                  'validation': [{'command': 'true', 'exit_code': 0, 'result': 'passed'}],
                  'blocker': None, 'recommended_next_action': 'orchestrator_verify', 'producer': 'fake-grok'}
        pathlib.Path(os.environ['AGENT_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')
        """

    def run_runner(self, binary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "--repo", str(self.repo), "--task-id", self.task_id,
             "--grok-bin", str(binary), "--timeout-seconds", "5"],
            check=False, capture_output=True, text=True,
        )

    def test_success_uses_skill_local_state_and_prompt_file(self) -> None:
        process = self.run_runner(self.fake_grok(self.success_body()))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(self.paths["result"].read_text())["status"], "success")
        attempt = self.paths["run_root"] / "attempt-01"
        invocation = json.loads((attempt / "invocation.json").read_text())
        self.assertTrue((attempt / "grok-prompt.txt").is_file())
        self.assertIn("--prompt-file", invocation["command"])

    def test_preflight_failure_is_task_failure(self) -> None:
        process = self.run_runner(self.fake_grok(self.success_body(), "raise SystemExit(7)"))
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(self.paths["result"].read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "task_failure")

    def test_usage_parser_does_not_keep_secrets(self) -> None:
        path = self.paths["run_root"] / "usage.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"usage":{"tokens":12,"api_key":"hidden"}}\n', encoding="utf-8")
        usage, note = runner_module.capture_usage(path)
        self.assertEqual(usage, {"tokens": 12})
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()

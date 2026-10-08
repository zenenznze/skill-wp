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
RUNNER = ROOT / "scripts" / "run_kimi_executor.py"
sys.path.insert(0, str(ROOT / "scripts"))
import run_kimi_executor as runner_module  # noqa: E402
from protocol import task_relative_paths  # noqa: E402


class KimiRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260810-kimi-runner"
        subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--task-id", self.task_id,
             "--goal", "Exercise the Kimi runner lifecycle."],
            check=True, capture_output=True, text=True,
        )
        self.paths = task_relative_paths(self.task_id, self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def fake_kimi(self, body: str) -> Path:
        executable = self.repo / "fake-kimi"
        executable.write_text(
            "#!/usr/bin/env python3\nimport json, os, pathlib, re, sys\n"
            + textwrap.dedent(body), encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def success_body(self) -> str:
        return """
        handoff_path = pathlib.Path(os.environ['AGENT_HANDOFF_PATH'])
        handoff = re.sub(r'^status:.*$', 'status: success', handoff_path.read_text(encoding='utf-8'), count=1, flags=re.M)
        handoff = re.sub(r'^runner_sentinel:.*\\n?', '', handoff, count=1, flags=re.M)
        handoff_path.write_text(handoff, encoding='utf-8')
        result = {'task_id': os.environ['AGENT_TASK_ID'], 'status': 'success', 'summary': 'Kimi completed.',
                  'handoff_path': os.environ['AGENT_HANDOFF_RELATIVE'], 'changed_files': [],
                  'validation': [{'command': 'true', 'exit_code': 0, 'result': 'passed'}],
                  'blocker': None, 'recommended_next_action': 'orchestrator_verify', 'producer': 'fake-kimi'}
        pathlib.Path(os.environ['AGENT_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')
        """

    def run_runner(self, binary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "--repo", str(self.repo), "--task-id", self.task_id,
             "--kimi-bin", str(binary), "--timeout-seconds", "5"],
            check=False, capture_output=True, text=True,
        )

    def test_success_uses_skill_local_state(self) -> None:
        process = self.run_runner(self.fake_kimi(self.success_body()))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(self.paths["result"].read_text())["status"], "success")
        self.assertTrue((self.paths["run_root"] / "attempt-01" / "invocation.json").is_file())

    def test_missing_result_is_task_failure(self) -> None:
        process = self.run_runner(self.fake_kimi("raise SystemExit(7)\n"))
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(self.paths["result"].read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "task_failure")

    def test_usage_parser_redacts_sensitive_fields(self) -> None:
        path = self.paths["run_root"] / "usage.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"usage":{"tokens":12,"secret":"hidden"}}\n', encoding="utf-8")
        usage, note = runner_module.capture_usage(path)
        self.assertEqual(usage, {"tokens": 12})
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()

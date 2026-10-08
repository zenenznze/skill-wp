from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from protocol import task_relative_paths  # noqa: E402

SPEC = importlib.util.spec_from_file_location("run_task_module", ROOT / "scripts" / "run_task.py")
assert SPEC and SPEC.loader
RUN_TASK_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_TASK_MODULE)

HANDOFF_WITH_COMMANDS = """\
# Validation Commands

```bash
python3 scripts/verify_result.py --repo . --task-id 20260810-x
git status --porcelain
pytest -q
```
"""


class AllowlistTailoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260810-tailor"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "init_task.py"), "--repo", str(self.repo),
             "--task-id", self.task_id, "--goal", "tailor settings test"],
            check=True, capture_output=True, text=True,
        )
        self.paths = task_relative_paths(self.task_id, self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def settings(self) -> dict:
        return json.loads(self.paths["claude_settings"].read_text(encoding="utf-8"))

    def test_validation_commands_and_allowlist(self) -> None:
        self.assertEqual(
            RUN_TASK_MODULE.validation_commands(HANDOFF_WITH_COMMANDS),
            [
                "python3 scripts/verify_result.py --repo . --task-id 20260810-x",
                "git status --porcelain",
                "pytest -q",
            ],
        )
        settings = self.settings()
        self.assertTrue(RUN_TASK_MODULE.merge_bash_allow(settings, HANDOFF_WITH_COMMANDS))
        self.assertIn("Bash(git *)", settings["permissions"]["allow"])
        self.assertIn("Bash(python3 *)", settings["permissions"]["allow"])
        deny_before = list(settings["permissions"]["deny"])
        self.assertFalse(RUN_TASK_MODULE.merge_bash_allow(settings, HANDOFF_WITH_COMMANDS))
        self.assertEqual(settings["permissions"]["deny"], deny_before)

    def test_tailor_writes_skill_local_settings(self) -> None:
        self.paths["handoff"].write_text(HANDOFF_WITH_COMMANDS, encoding="utf-8")
        RUN_TASK_MODULE.tailor_claude_settings(self.repo, self.task_id, HANDOFF_WITH_COMMANDS)
        self.assertIn("Bash(pytest *)", self.settings()["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()

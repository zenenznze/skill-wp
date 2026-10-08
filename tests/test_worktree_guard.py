from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from protocol import assert_isolated_execution_root  # noqa: E402


class WorktreeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.main = self.root / "workflow"
        self.delivery = self.root / "delivery"
        self.main.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(self.main)],
            check=True,
            capture_output=True,
        )
        (self.main / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.main), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.main), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )
        self.old_main = os.environ.get("PI_WORKFLOW_MAIN_ROOT")
        self.old_delivery = os.environ.get("PI_WORKFLOW_DELIVERY_ROOT")
        os.environ["PI_WORKFLOW_MAIN_ROOT"] = str(self.main)
        os.environ["PI_WORKFLOW_DELIVERY_ROOT"] = str(self.delivery)

    def tearDown(self) -> None:
        if self.old_main is None:
            os.environ.pop("PI_WORKFLOW_MAIN_ROOT", None)
        else:
            os.environ["PI_WORKFLOW_MAIN_ROOT"] = self.old_main
        if self.old_delivery is None:
            os.environ.pop("PI_WORKFLOW_DELIVERY_ROOT", None)
        else:
            os.environ["PI_WORKFLOW_DELIVERY_ROOT"] = self.old_delivery
        self.temporary.cleanup()

    def test_rejects_main_checkout_and_nested_submodule_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected Workflow checkout"):
            assert_isolated_execution_root(self.main)

        nested = self.main / "daily-customer-support-qa-report"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "not a linked worktree"):
            assert_isolated_execution_root(nested)

    def test_rejects_delivery_checkout(self) -> None:
        self.delivery.mkdir()
        with self.assertRaisesRegex(ValueError, "protected Workflow checkout"):
            assert_isolated_execution_root(self.delivery)

    def test_allows_registered_linked_worktree(self) -> None:
        task = self.main / ".codex-tmp" / "task"
        task.parent.mkdir()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.main),
                "worktree",
                "add",
                "-b",
                "codex/test-task",
                str(task),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        assert_isolated_execution_root(task)


if __name__ == "__main__":
    unittest.main()

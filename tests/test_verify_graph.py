from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_TASK = ROOT / "scripts" / "init_task.py"
VERIFY_GRAPH = ROOT / "scripts" / "verify_graph.py"
sys.path.insert(0, str(ROOT / "scripts"))
from graph_lib import graph_relative_paths  # noqa: E402
from protocol import atomic_write_json, handoff_display_path, task_relative_paths  # noqa: E402


class VerifyGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260819-verify-node"
        process = subprocess.run(
            [sys.executable, str(INIT_TASK), "--repo", str(self.repo), "--task-id", self.task_id,
             "--goal", "A task whose terminal artifacts validate."],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.task_paths = task_relative_paths(self.task_id, self.repo)
        self.graph_id = "20260819-verify-graph"

    def tearDown(self) -> None:
        shutil.rmtree(self.task_paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def finish_success_task(self) -> None:
        handoff = self.task_paths["handoff"]
        text = handoff.read_text(encoding="utf-8")
        text = re.sub(r"^status:.*$", "status: success", text, count=1, flags=re.M)
        handoff.write_text(text, encoding="utf-8")
        result = {
            "task_id": self.task_id,
            "status": "success",
            "summary": "Verified task completion.",
            "handoff_path": handoff_display_path(self.repo, self.task_id),
            "changed_files": [],
            "validation": [{"command": "true", "exit_code": 0, "result": "passed"}],
            "blocker": None,
            "recommended_next_action": "orchestrator_verify",
            "producer": "test-fixture",
        }
        atomic_write_json(self.task_paths["result"], result)

    def write_graph(self, graph: dict) -> None:
        paths = graph_relative_paths(self.graph_id, self.repo)
        paths["graph_dir"].mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths["graph_json"], graph)

    def verify(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VERIFY_GRAPH), "--repo", str(self.repo),
             "--graph-id", self.graph_id, *extra],
            check=False, capture_output=True, text=True,
        )

    def test_success_node_with_valid_artifacts_passes(self) -> None:
        self.finish_success_task()
        graph = {
            "graph_id": self.graph_id,
            "repo_id": "unused",
            "goal": "Graph whose single task succeeded.",
            "status": "success",
            "created_at": "2026-08-19T00:00:00+08:00",
            "updated_at": "2026-08-19T00:00:00+08:00",
            "nodes": [
                {"id": "node-a", "kind": "task", "task_id": self.task_id, "goal": "A.", "status": "success"},
            ],
            "edges": [],
        }
        self.write_graph(graph)
        process = self.verify()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        output = json.loads(process.stdout)
        self.assertTrue(output["protocol_valid"])
        self.assertEqual(output["graph_status"], "success")
        self.assertEqual(output["node_statuses"], {"node-a": "success"})

    def test_success_node_without_result_json_is_error(self) -> None:
        graph = {
            "graph_id": self.graph_id,
            "repo_id": "unused",
            "goal": "Graph whose task claims success without artifacts.",
            "status": "pending",
            "created_at": "2026-08-19T00:00:00+08:00",
            "updated_at": "2026-08-19T00:00:00+08:00",
            "nodes": [
                {"id": "node-a", "kind": "task", "task_id": self.task_id, "goal": "A.", "status": "success"},
            ],
            "edges": [],
        }
        self.write_graph(graph)
        process = self.verify()
        self.assertEqual(process.returncode, 2)
        output = json.loads(process.stdout)
        self.assertFalse(output["protocol_valid"])
        self.assertTrue(any("result.json" in error for error in output["errors"]))

    def test_require_success_exit_codes(self) -> None:
        self.finish_success_task()
        success_graph = {
            "graph_id": self.graph_id,
            "repo_id": "unused",
            "goal": "Success graph.",
            "status": "success",
            "created_at": "2026-08-19T00:00:00+08:00",
            "updated_at": "2026-08-19T00:00:00+08:00",
            "nodes": [
                {"id": "node-a", "kind": "task", "task_id": self.task_id, "goal": "A.", "status": "success"},
            ],
            "edges": [],
        }
        self.write_graph(success_graph)
        process = self.verify("--require-success")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

        pending_graph = {
            "graph_id": self.graph_id,
            "repo_id": "unused",
            "goal": "Pending graph.",
            "status": "pending",
            "created_at": "2026-08-19T00:00:00+08:00",
            "updated_at": "2026-08-19T00:00:00+08:00",
            "nodes": [
                {"id": "node-b", "kind": "task", "task_id": "20260819-other-node", "goal": "B.", "status": "pending"},
            ],
            "edges": [],
        }
        self.write_graph(pending_graph)
        process = self.verify("--require-success")
        self.assertEqual(process.returncode, 3)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "scripts" / "init_graph.py"
sys.path.insert(0, str(ROOT / "scripts"))
from graph_lib import graph_relative_paths  # noqa: E402


class InitGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.graph_id = "20260819-init-graph"
        self.paths = graph_relative_paths(self.graph_id, self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["graph_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def init(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--graph-id", self.graph_id, *extra],
            check=False, capture_output=True, text=True,
        )

    def test_skeleton_and_handoff_created(self) -> None:
        process = self.init("--goal", "Build the example graph foundation.")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(self.paths["graph_json"].is_file())
        self.assertTrue(self.paths["graph_handoff"].is_file())
        graph = json.loads(self.paths["graph_json"].read_text(encoding="utf-8"))
        self.assertEqual(graph["graph_id"], self.graph_id)
        self.assertEqual(graph["status"], "pending")
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        handoff = self.paths["graph_handoff"].read_text(encoding="utf-8")
        self.assertIn(f"graph_id: {self.graph_id}", handoff)
        self.assertIn("orchestrator: current-agent", handoff)
        self.assertIn("# Node Table", handoff)

    def test_graph_json_import_rejects_bad_reason(self) -> None:
        bad = self.repo / "bad-graph.json"
        bad.write_text(
            json.dumps(
                {
                    "graph_id": self.graph_id,
                    "repo_id": "repo-abc",
                    "goal": "Imported graph with a placeholder edge reason.",
                    "status": "pending",
                    "created_at": "2026-08-19T00:00:00+08:00",
                    "updated_at": "2026-08-19T00:00:00+08:00",
                    "nodes": [
                        {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
                        {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md"},
                    ],
                    "edges": [
                        {
                            "from": "task-a",
                            "to": "artifact-x",
                            "relation": "produces",
                            "reason": "TODO",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        process = self.init("--graph-json", str(bad))
        self.assertEqual(process.returncode, 2)
        self.assertIn("reason", process.stderr)
        self.assertFalse(self.paths["graph_dir"].exists())

    def test_graph_json_import_succeeds(self) -> None:
        source = self.repo / "graph.json"
        source.write_text(
            json.dumps(
                {
                    "graph_id": self.graph_id,
                    "goal": "Imported graph that should validate.",
                    "status": "pending",
                    "nodes": [],
                    "edges": [],
                }
            ),
            encoding="utf-8",
        )
        process = self.init("--graph-json", str(source))
        self.assertEqual(process.returncode, 0, process.stderr)
        graph = json.loads(self.paths["graph_json"].read_text(encoding="utf-8"))
        self.assertEqual(graph["graph_id"], self.graph_id)
        self.assertIn("repo_id", graph)
        self.assertIn("created_at", graph)
        self.assertIn("updated_at", graph)

    def test_duplicate_graph_id_refused(self) -> None:
        process = self.init("--goal", "First graph.")
        self.assertEqual(process.returncode, 0, process.stderr)
        process = self.init("--goal", "Second graph.")
        self.assertEqual(process.returncode, 2)
        self.assertIn("already exists", process.stderr)

    def test_check_exit_codes(self) -> None:
        process = self.init("--goal", "Graph to check.")
        self.assertEqual(process.returncode, 0, process.stderr)
        check = self.init("--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        output = json.loads(check.stdout)
        self.assertTrue(output["protocol_valid"])
        self.assertEqual(output["errors"], [])

        graph = json.loads(self.paths["graph_json"].read_text(encoding="utf-8"))
        del graph["goal"]
        self.paths["graph_json"].write_text(json.dumps(graph), encoding="utf-8")
        check = self.init("--check")
        self.assertEqual(check.returncode, 2)
        output = json.loads(check.stdout)
        self.assertFalse(output["protocol_valid"])
        self.assertTrue(output["errors"])

    def test_check_missing_graph_fails(self) -> None:
        process = self.init("--check")
        self.assertEqual(process.returncode, 2)


if __name__ == "__main__":
    unittest.main()

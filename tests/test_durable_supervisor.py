from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import durable_supervisor as ds  # noqa: E402
from protocol import repository_id  # noqa: E402


class FakeBackend:
    name = "fake-herdr"

    def __init__(self, repo: Path, state_root: Path, behaviors: dict[tuple[str, int], dict]):
        self.repo = repo
        self.state_root = state_root
        self.behaviors = behaviors
        self.handles: dict[str, ds.WorkerHandle] = {}
        self.spawned: list[tuple[str, int, str | None]] = []
        self.poll_counts: dict[str, int] = {}

    def _task_paths(self, task_id: str) -> dict[str, Path]:
        return ds._task_paths(self.state_root, self.repo, task_id)

    def spawn(self, task, attempt, prompt, feedback, journal=None):
        worker_id = f"fake-{task['node_id']}-a{attempt}"
        handle = ds.WorkerHandle(task["node_id"], attempt, worker_id, {"tab_id": f"tab-{worker_id}"})
        self.handles[worker_id] = handle
        self.spawned.append((task["node_id"], attempt, feedback))
        if journal:
            journal({"workspace_id": "fake-workspace", "tab_id": f"tab-{worker_id}", "pane_id": f"pane-{worker_id}"})
            journal({"agent_id": worker_id})
        behavior = self.behaviors[(task["node_id"], attempt)]
        if behavior.get("result_file") == "bad-json":
            self._task_paths(task["task_id"])["result"].write_text("{bad", encoding="utf-8")
        elif behavior.get("result") is not None:
            result = dict(behavior["result"])
            result["task_id"] = task["task_id"]
            result["handoff_path"] = ds._expected_handoff(self.state_root, self.repo, task["task_id"])
            self._task_paths(task["task_id"])["result"].write_text(json.dumps(result), encoding="utf-8")
            handoff = self._task_paths(task["task_id"])["handoff"]
            text = handoff.read_text(encoding="utf-8")
            import re
            text = re.sub(r"^status: .*?$", f"status: {result['status']}", text, count=1, flags=re.MULTILINE)
            text = re.sub(r"^runner_sentinel: .*?$\n?", "", text, count=1, flags=re.MULTILINE)
            handoff.write_text(text, encoding="utf-8")
        return handle

    def recover(self, attempt):
        worker = attempt.get("worker", {})
        worker_id = worker.get("agent_id")
        return self.handles.get(worker_id)

    def poll(self, handle):
        count = self.poll_counts.get(handle.worker_id, 0)
        self.poll_counts[handle.worker_id] = count + 1
        behavior = self.behaviors[(handle.node_id, handle.attempt)]
        if behavior.get("interrupt_once") and count == 0:
            raise KeyboardInterrupt
        sequence = behavior.get("poll", [behavior.get("lifecycle", "done")])
        if count >= len(sequence):
            return sequence[-1]
        return sequence[count]

    def collect(self, handle, attempt):
        return None


class ScriptedReviewer:
    name = "fake-independent-reviewer"

    def __init__(self, verdicts: dict[tuple[str, int], str]):
        self.verdicts = verdicts

    def review(self, task, attempt, result, result_errors, lifecycle_status):
        verdict = self.verdicts.get((task["task_id"], attempt), "PASS")
        findings = [] if verdict == "PASS" else ["address the failed acceptance evidence"]
        return {
            "schema": ds.REVIEW_SCHEMA,
            "task_id": task["task_id"],
            "attempt": attempt,
            "verdict": verdict,
            "summary": "fake reviewer verdict",
            "findings": findings or ["none"],
            "checked": ["result-contract", "diff", "validation"],
            "reviewer": self.name,
        }


def success_result() -> dict:
    return {
        "status": "success",
        "summary": "worker passed",
        "changed_files": [],
        "validation": [{"command": "true", "exit_code": 0, "result": "passed"}],
        "blocker": None,
        "recommended_next_action": "orchestrator_verify",
    }


def failed_result() -> dict:
    return {
        "status": "failed",
        "summary": "worker failed focused check",
        "changed_files": [],
        "validation": [{"command": "false", "exit_code": 1, "result": "failed"}],
        "blocker": None,
        "recommended_next_action": "inspect_task_failure",
    }


class DurableSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.state_root = Path(self.tmp.name) / "state"
        self.graph_id = "20260922-supervisor-graph"
        self.supervisor_id = "20260922-supervisor-run"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_task(self, task_id: str, scope: str) -> None:
        paths = ds._task_paths(self.state_root, self.repo, task_id)
        paths["task_dir"].mkdir(parents=True, exist_ok=True)
        paths["handoff"].write_text(
            f"""---
            task_id: {task_id}
            task_protocol_version: 2
            status: planned
            ---

            # Goal

            Complete {task_id}.

            # Atomic Work Contract

            ```json
            {{
              "single_outcome": "Complete the bounded task.",
              "deliverables": ["{scope}"],
              "write_scope": ["{scope}"],
              "read_only": false,
              "acceptance": ["The focused check passes."],
              "resume_boundary": "Resume at the first failed focused check."
            }}
            ```

            # Acceptance Criteria

            - [ ] The focused check passes.

            # Validation Commands

            ```bash
            true
            ```
            """.replace("            ", ""),
            encoding="utf-8",
        )
        paths["prompt"].write_text("Perform the bounded task and write the terminal result.", encoding="utf-8")

    def _write_graph(self, node_specs: list[tuple[str, str]], edges: list[dict] | None = None) -> None:
        nodes = []
        for node_id, scope in node_specs:
            task_id = f"20260922-{node_id}"
            self._write_task(task_id, scope)
            nodes.append({
                "id": node_id,
                "kind": "task",
                "task_id": task_id,
                "goal": f"Complete {node_id}.",
                "status": "pending",
                "attempts": 0,
                "writes": [scope],
            })
        graph = {
            "graph_id": self.graph_id,
            "repo_id": repository_id(self.repo),
            "goal": "durable supervisor test graph",
            "status": "pending",
            "created_at": "2026-09-22T00:00:00+00:00",
            "updated_at": "2026-09-22T00:00:00+00:00",
            "nodes": nodes,
            "edges": edges or [],
        }
        path = ds._graph_path(self.state_root, self.repo, self.graph_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(graph), encoding="utf-8")

    def _make(self, backend, reviewer=None, max_parallel=2, max_attempts=3):
        ds.initialize_supervisor(
            self.repo,
            self.graph_id,
            self.supervisor_id,
            max_parallel=max_parallel,
            max_attempts=max_attempts,
            state_root=self.state_root,
        )
        return ds.DurableSupervisor(
            self.repo,
            self.supervisor_id,
            backend,
            reviewer=reviewer,
            state_root=self.state_root,
            poll_seconds=0,
        )

    def test_pass_persists_result_review_events_and_checkpoint(self):
        self._write_graph([("alpha", "src/alpha.py"), ("beta", "src/beta.py")])
        behaviors = {
            ("alpha", 1): {"lifecycle": "done", "result": success_result()},
            ("beta", 1): {"lifecycle": "idle", "result": success_result()},
        }
        backend = FakeBackend(self.repo, self.state_root, behaviors)
        supervisor = self._make(backend)
        summary = supervisor.run()
        self.assertEqual("success", summary["status"])
        self.assertEqual({"alpha": "success", "beta": "success"}, summary["task_statuses"])
        root = supervisor.root
        self.assertTrue((root / "project.json").is_file())
        self.assertTrue((root / "events.jsonl").read_text(encoding="utf-8").strip())
        self.assertTrue(list((root / "checkpoints").glob("checkpoint-*.json")))
        self.assertTrue(list((root / "results" / "alpha").glob("attempt-01.json")))
        self.assertTrue(list((root / "reviews" / "alpha").glob("attempt-01.json")))

    def test_single_writer_lock_rejects_a_live_second_controller(self):
        self._write_graph([("alpha", "src/alpha.py")])
        backend = FakeBackend(self.repo, self.state_root, {
            ("alpha", 1): {"lifecycle": "done", "result": success_result()},
        })
        supervisor = self._make(backend)
        lock = ds.SupervisorLock(supervisor.root / "run.lock")
        lock.acquire()
        try:
            with self.assertRaises(ds.SupervisorLockError):
                ds.SupervisorLock(supervisor.root / "run.lock").acquire()
        finally:
            lock.release()

    def test_retry_feedback_is_sent_to_next_attempt_then_passes(self):
        self._write_graph([("alpha", "src/alpha.py")])
        task_id = "20260922-alpha"
        behaviors = {
            ("alpha", 1): {"lifecycle": "done", "result": failed_result()},
            ("alpha", 2): {"lifecycle": "done", "result": success_result()},
        }
        backend = FakeBackend(self.repo, self.state_root, behaviors)
        reviewer = ScriptedReviewer({(task_id, 1): "RETRY", (task_id, 2): "PASS"})
        supervisor = self._make(backend, reviewer=reviewer)
        summary = supervisor.run()
        self.assertEqual("success", summary["status"])
        self.assertEqual(2, summary["attempts"]["alpha"])
        self.assertIsNotNone(backend.spawned[1][2])
        self.assertIn("fake reviewer verdict", backend.spawned[1][2])

    def test_interrupted_process_recovers_running_attempt_idempotently(self):
        self._write_graph([("alpha", "src/alpha.py")])
        behaviors = {
            ("alpha", 1): {"lifecycle": "done", "result": success_result(), "interrupt_once": True},
        }
        backend = FakeBackend(self.repo, self.state_root, behaviors)
        first = self._make(backend)
        with self.assertRaises(KeyboardInterrupt):
            first.run()
        self.assertEqual("running", json.loads((first.root / "project.json").read_text())["status"])
        resumed = ds.DurableSupervisor(
            self.repo,
            self.supervisor_id,
            backend,
            state_root=self.state_root,
            poll_seconds=0,
        )
        summary = resumed.run()
        self.assertEqual("success", summary["status"])
        self.assertEqual(1, summary["attempts"]["alpha"])
        self.assertFalse((resumed.root / "run.lock").exists())

    def test_missing_or_bad_result_is_rejected_even_when_lifecycle_is_done(self):
        for result_mode in ("missing", "bad-json"):
            with self.subTest(result_mode=result_mode):
                shutil.rmtree(self.state_root, ignore_errors=True)
                self.state_root.mkdir()
                self._write_graph([("alpha", "src/alpha.py")])
                behaviors = {
                    ("alpha", 1): {"lifecycle": "done", "result_file": result_mode},
                }
                backend = FakeBackend(self.repo, self.state_root, behaviors)
                supervisor = self._make(backend, max_attempts=1)
                summary = supervisor.run()
                self.assertEqual("failed", summary["status"])
                review = list((supervisor.root / "reviews" / "alpha").glob("attempt-01.json"))[0]
                self.assertEqual("RETRY", json.loads(review.read_text())["verdict"])
                collected = list((supervisor.root / "results" / "alpha").glob("attempt-01.json"))[0]
                self.assertTrue(json.loads(collected.read_text())["result_errors"])
    def test_ready_tasks_run_concurrently_and_fast_task_does_not_wait_for_slow_task(self):
        self._write_graph([("slow", "src/slow.py"), ("fast", "src/fast.py")])
        behaviors = {
            ("slow", 1): {"poll": [None, "done"], "result": success_result()},
            ("fast", 1): {"poll": ["done"], "result": success_result()},
        }
        backend = FakeBackend(self.repo, self.state_root, behaviors)
        supervisor = self._make(backend, max_parallel=2)
        summary = supervisor.run()
        self.assertEqual("success", summary["status"])
        self.assertEqual(["fast", "slow"], [item[0] for item in backend.spawned])
        self.assertEqual(1, backend.poll_counts["fake-fast-a1"])
        self.assertGreaterEqual(backend.poll_counts["fake-slow-a1"], 2)


if __name__ == "__main__":
    unittest.main()

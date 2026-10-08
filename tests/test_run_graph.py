from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import graph_lib  # noqa: E402
import init_graph  # noqa: E402
import init_task  # noqa: E402
import run_graph  # noqa: E402
from protocol import (  # noqa: E402
    STATE_ROOT,
    handoff_display_path,
    repository_id,
    task_relative_paths,
    update_handoff_frontmatter,
)


def task_node(
    node_id: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    writes: list[str] | None = None,
) -> dict:
    return {
        "id": node_id,
        "kind": "task",
        "task_id": "20260819-" + node_id,
        "goal": f"Do {node_id}.",
        "status": status,
        "attempts": attempts,
        **(  # noqa: C408
            {"writes": writes} if writes is not None else {}
        ),
    }


def depends_on(from_id: str, to_id: str) -> dict:
    return {
        "from": from_id,
        "to": to_id,
        "relation": "depends_on",
        "reason": f"{to_id} builds on {from_id}",
    }


def make_graph(nodes: list[dict], edges: list[dict]) -> dict:
    return {
        "graph_id": "20260819-run-graph",
        "repo_id": "repo-test",
        "goal": "Test graph for the run_graph scheduler.",
        "status": "pending",
        "created_at": "2026-08-19T00:00:00+08:00",
        "updated_at": "2026-08-19T00:00:00+08:00",
        "nodes": nodes,
        "edges": edges,
    }


def linear_chain(node_ids: list[str]) -> dict:
    nodes = [
        task_node(node_id, writes=[f"{node_id}/**"]) for node_id in node_ids
    ]
    edges = [
        depends_on(source, target)
        for source, target in zip(node_ids, node_ids[1:])
    ]
    return make_graph(nodes, edges)


def diamond_graph() -> dict:
    nodes = [
        task_node("task-a", writes=["a/**"]),
        task_node("task-b", writes=["b/**"]),
        task_node("task-c", writes=["c/**"]),
        task_node("task-d", writes=["d/**"]),
    ]
    edges = [
        depends_on("task-a", "task-b"),
        depends_on("task-a", "task-c"),
        depends_on("task-b", "task-d"),
        depends_on("task-c", "task-d"),
    ]
    return make_graph(nodes, edges)


class FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code

    def poll(self) -> int:
        return self._exit_code


def make_recorder(exit_code: int = 0) -> tuple[list[tuple[str, int]], object]:
    calls: list[tuple[str, int]] = []

    def launch(node_id: str, attempt: int) -> FakeProcess:
        calls.append((node_id, attempt))
        return FakeProcess(exit_code)

    return calls, launch


def validate_success(node_id: str, exit_code: int) -> tuple[str, list[str]]:
    del node_id, exit_code
    return "success", []


def validate_failed(node_id: str, exit_code: int) -> tuple[str, list[str]]:
    del node_id, exit_code
    return "failed", ["simulated failure"]


def validate_with(
    rules: dict[str, tuple[str, list[str]]],
) -> object:
    def validate(node_id: str, exit_code: int) -> tuple[str, list[str]]:
        del exit_code
        return rules.get(node_id, ("success", []))

    return validate


class ScheduleCoreTests(unittest.TestCase):
    def test_linear_chain_runs_in_order(self) -> None:
        graph = linear_chain(["task-a", "task-b", "task-c"])
        calls, launch = make_recorder()
        summary = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_success,
            poll_interval=0,
        )
        self.assertEqual([call[0] for call in calls], ["task-a", "task-b", "task-c"])
        self.assertEqual([call[1] for call in calls], [1, 1, 1])
        self.assertEqual(graph["status"], "success")
        self.assertEqual(summary["graph_status"], "success")
        self.assertEqual(summary["batches_run"], 3)

    def test_diamond_runs_middle_nodes_in_one_batch(self) -> None:
        graph = diamond_graph()
        calls, launch = make_recorder()
        summary = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_success,
            max_parallel=3,
            poll_interval=0,
        )
        self.assertEqual(
            [call[0] for call in calls],
            ["task-a", "task-b", "task-c", "task-d"],
        )
        # b and c are ready together and conflict-free, so a single batch
        # carries both; separate batches would require four batches.
        self.assertEqual(summary["batches_run"], 3)
        self.assertEqual(graph["status"], "success")

    def test_conflicting_writes_serialize_into_separate_batches(self) -> None:
        graph = make_graph(
            [
                task_node("task-a", writes=["src/**"]),
                task_node("task-b", writes=["src/**"]),
            ],
            [],
        )
        calls, launch = make_recorder()
        summary = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_success,
            max_parallel=3,
            poll_interval=0,
        )
        self.assertEqual([call[0] for call in calls], ["task-a", "task-b"])
        self.assertEqual(summary["batches_run"], 2)
        self.assertEqual(graph["status"], "success")

    def test_node_failure_stops_downstream_and_ends_failed(self) -> None:
        graph = linear_chain(["task-a", "task-b"])
        calls, launch = make_recorder()
        summary = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_with({"task-a": ("failed", ["boom"])}),
            max_node_attempts=1,
            poll_interval=0,
        )
        self.assertEqual([call[0] for call in calls], ["task-a"])
        self.assertEqual(graph["status"], "failed")
        self.assertEqual(summary["graph_status"], "failed")
        self.assertEqual(summary["batches_run"], 1)
        by_id = run_graph.nodes_by_id(graph)
        self.assertEqual(by_id["task-a"]["last_error"], ["boom"])
        self.assertEqual(by_id["task-b"]["status"], "pending")

    def test_failed_node_retried_until_exhausted_within_invocation(self) -> None:
        graph = make_graph([task_node("task-a", writes=["a/**"])], [])
        calls, launch = make_recorder()
        run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_failed,
            max_node_attempts=3,
            poll_interval=0,
        )
        self.assertEqual(calls, [("task-a", 1), ("task-a", 2), ("task-a", 3)])
        self.assertEqual(graph["status"], "failed")
        self.assertEqual(graph["nodes"][0]["attempts"], 3)

    def test_failed_node_with_attempts_left_retried_on_reinvocation(self) -> None:
        graph = make_graph(
            [task_node("task-a", writes=["a/**"], status="failed", attempts=2)],
            [],
        )
        calls, launch = make_recorder()
        run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_failed,
            max_node_attempts=3,
            poll_interval=0,
        )
        self.assertEqual(calls, [("task-a", 3)])
        self.assertEqual(graph["status"], "failed")

    def test_success_nodes_skipped_on_reinvocation(self) -> None:
        graph = linear_chain(["task-a", "task-b"])
        calls, launch = make_recorder()
        first = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_success,
            poll_interval=0,
        )
        self.assertEqual([call[0] for call in calls], ["task-a", "task-b"])
        calls.clear()
        second = run_graph.schedule(
            graph,
            launch=launch,
            validate_node=validate_success,
            poll_interval=0,
        )
        self.assertEqual(calls, [])
        self.assertEqual(second["batches_run"], 0)
        self.assertEqual(first["graph_status"], "success")
        self.assertEqual(graph["status"], "success")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True
        )
        self.graph_id = "20260819-run-cli"
        self.task_ids = {
            "task-a": "20260819-cli-task-a",
            "task-b": "20260819-cli-task-b",
        }
        self.repo_state = STATE_ROOT / "repos" / repository_id(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.repo_state, ignore_errors=True)
        self.temporary.cleanup()

    def init_task_packages(self) -> None:
        scopes = {
            self.task_ids["task-a"]: ["docs/a.md"],
            self.task_ids["task-b"]: ["docs/b.md"],
        }
        for task_id in self.task_ids.values():
            init_task.initialize(self.repo, task_id, f"Do {task_id}.")
            handoff = task_relative_paths(task_id, self.repo)["handoff"]
            text = handoff.read_text(encoding="utf-8")
            contract = {
                "single_outcome": f"Complete the bounded work for {task_id}.",
                "deliverables": [f"Owned output for {task_id}"],
                "write_scope": scopes[task_id],
                "read_only": False,
                "acceptance": [f"The focused checks for {task_id} pass."],
                "resume_boundary": f"Resume at the first failing check for {task_id}.",
            }
            match = re.search(r"(?ms)(^```json\n).*?(\n```)", text)
            assert match
            replacement = match.group(1) + json.dumps(contract, indent=2) + match.group(2)
            text = text[:match.start()] + replacement + text[match.end():]
            handoff.write_text(text.replace("TODO", "Completed"), encoding="utf-8")

    def make_legacy_task_packages(self) -> None:
        self.init_task_packages()
        for task_id in self.task_ids.values():
            handoff = task_relative_paths(task_id, self.repo)["handoff"]
            text = handoff.read_text(encoding="utf-8")
            text = text.replace("task_protocol_version: 2\n", "", 1)
            start = text.index("# Atomic Work Contract")
            end = text.index("# Current Repository State", start)
            handoff.write_text(text[:start] + text[end:], encoding="utf-8")

    def cli_graph(self) -> dict:
        return {
            "graph_id": self.graph_id,
            "goal": "CLI test graph for the run_graph scheduler.",
            "status": "pending",
            "nodes": [
                {
                    "id": "task-a",
                    "kind": "task",
                    "task_id": self.task_ids["task-a"],
                    "goal": "Do task A.",
                    "status": "pending",
                    "attempts": 0,
                    "writes": ["docs/a.md"],
                },
                {
                    "id": "task-b",
                    "kind": "task",
                    "task_id": self.task_ids["task-b"],
                    "goal": "Do task B.",
                    "status": "pending",
                    "attempts": 0,
                    "writes": ["docs/b.md"],
                },
            ],
            "edges": [
                {
                    "from": "task-a",
                    "to": "task-b",
                    "relation": "depends_on",
                    "reason": "task b builds on task a",
                }
            ],
        }

    def import_graph(self, graph: dict) -> None:
        source = self.repo / "graph.json"
        source.write_text(json.dumps(graph), encoding="utf-8")
        init_graph.initialize(self.repo, self.graph_id, None, source)

    def run_cli(self, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_graph.main(
                ["--repo", str(self.repo), "--graph-id", self.graph_id, *extra]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_prints_plan_and_launches_nothing(self) -> None:
        self.init_task_packages()
        self.import_graph(self.cli_graph())
        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 0, stderr)
        plan = json.loads(stdout)
        self.assertEqual(plan["ready"], ["task-a"])
        self.assertEqual(plan["batches"], [["task-a"], ["task-b"]])
        self.assertIn("critical_path", plan)
        self.assertEqual(plan["graph_status"], "pending")

    def test_lockfile_refusal_when_lock_exists(self) -> None:
        self.init_task_packages()
        self.import_graph(self.cli_graph())
        graph_dir = graph_lib.graph_relative_paths(self.graph_id, self.repo)["graph_dir"]
        lock = graph_dir / "run.lock"
        lock.write_text("pid=123 started_at=2026-08-19T00:00:00+08:00\n", encoding="utf-8")
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("run.lock", stderr)
        self.assertTrue(lock.is_file())

    def test_preflight_lists_all_missing_task_packages(self) -> None:
        self.import_graph(self.cli_graph())
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("task-a", stderr)
        self.assertIn("task-b", stderr)

    def test_preflight_rejects_all_legacy_packages_by_default(self) -> None:
        self.make_legacy_task_packages()
        self.import_graph(self.cli_graph())
        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("node task-a", stderr)
        self.assertIn("node task-b", stderr)
        self.assertIn("allow-legacy-task-package", stderr)

    def test_explicit_legacy_flag_allows_graph_and_forwards_to_children(self) -> None:
        self.make_legacy_task_packages()
        graph = self.cli_graph()
        self.import_graph(graph)
        code, stdout, stderr = self.run_cli(
            "--dry-run", "--allow-legacy-task-package"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["ready"], ["task-a"])

        command_without_flag = run_graph.build_launch_command(
            graph["nodes"][0],
            1,
            repo=self.repo,
            agent="pi",
            level="balanced",
            timeout=None,
            roster=None,
            max_node_attempts=3,
        )
        command_with_flag = run_graph.build_launch_command(
            graph["nodes"][0],
            1,
            repo=self.repo,
            agent="pi",
            level="balanced",
            timeout=None,
            roster=None,
            max_node_attempts=3,
            allow_legacy_task_package=True,
        )
        self.assertNotIn("--allow-legacy-task-package", command_without_flag)
        self.assertIn("--allow-legacy-task-package", command_with_flag)

    def test_preflight_lists_all_invalid_v2_packages(self) -> None:
        self.init_task_packages()
        for task_id in self.task_ids.values():
            handoff = task_relative_paths(task_id, self.repo)["handoff"]
            text = handoff.read_text(encoding="utf-8")
            text = text.replace("TODO: replace with one observable outcome", "TODO", 1)
            start = text.index("# Atomic Work Contract")
            end = text.index("# Current Repository State", start)
            text = text[:start] + text[start:end].replace(
                "Complete the bounded work", "TODO: replace the outcome"
            )
            handoff.write_text(text, encoding="utf-8")
        self.import_graph(self.cli_graph())
        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("node task-a", stderr)
        self.assertIn("node task-b", stderr)

    def test_preflight_rejects_graph_write_scope_mismatch(self) -> None:
        self.init_task_packages()
        graph = self.cli_graph()
        graph["nodes"][1]["writes"] = ["docs/not-owned.md"]
        self.import_graph(graph)
        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("node task-b", stderr)
        self.assertIn("write_scope", stderr)

    def test_ensure_graph_handoff_creates_missing_handoff(self) -> None:
        self.init_task_packages()
        self.import_graph(self.cli_graph())
        paths = graph_lib.graph_relative_paths(self.graph_id, self.repo)
        self.assertFalse(paths["graph_handoff"].is_file())
        graph = json.loads(paths["graph_json"].read_text(encoding="utf-8"))
        run_graph.ensure_graph_handoff(paths["graph_handoff"], graph)
        self.assertTrue(paths["graph_handoff"].is_file())
        text = paths["graph_handoff"].read_text(encoding="utf-8")
        self.assertIn("# Execution Progress", text)

    def test_append_execution_progress_replaces_placeholder(self) -> None:
        self.init_task_packages()
        self.import_graph(self.cli_graph())
        paths = graph_lib.graph_relative_paths(self.graph_id, self.repo)
        graph = json.loads(paths["graph_json"].read_text(encoding="utf-8"))
        run_graph.ensure_graph_handoff(paths["graph_handoff"], graph)
        first = run_graph.progress_line(
            "task-a", self.task_ids["task-a"], 1, 0, "success"
        )
        run_graph.append_execution_progress(paths["graph_handoff"], first)
        text = paths["graph_handoff"].read_text(encoding="utf-8")
        self.assertIn("task-a finished", text)
        self.assertNotIn("- Not started.", text)
        second = run_graph.progress_line(
            "task-b", self.task_ids["task-b"], 1, 0, "success"
        )
        run_graph.append_execution_progress(paths["graph_handoff"], second)
        text = paths["graph_handoff"].read_text(encoding="utf-8")
        self.assertIn("task-a finished", text)
        self.assertIn("task-b finished", text)
        self.assertLess(text.find("task-a"), text.find("task-b"))

    def test_default_validate_node_accepts_terminal_success(self) -> None:
        self.init_task_packages()
        task_id = self.task_ids["task-a"]
        node = {
            "id": "task-a",
            "kind": "task",
            "task_id": task_id,
            "goal": "Do A.",
        }
        task_paths = task_relative_paths(task_id, self.repo)
        result = {
            "task_id": task_id,
            "status": "success",
            "summary": "Task A done.",
            "handoff_path": handoff_display_path(self.repo, task_id),
            "changed_files": ["docs/a.md"],
            "validation": [{"command": "echo ok", "exit_code": 0, "result": "passed"}],
            "blocker": None,
            "recommended_next_action": "orchestrator_verify",
        }
        task_paths["result"].write_text(json.dumps(result), encoding="utf-8")
        update_handoff_frontmatter(task_paths["handoff"], {"status": "success"})
        status, errors = run_graph.default_validate_node(self.repo, node, 0)
        self.assertEqual(status, "success")
        self.assertEqual(errors, [])

    def test_default_validate_node_marks_mismatch_failed(self) -> None:
        self.init_task_packages()
        task_id = self.task_ids["task-a"]
        node = {
            "id": "task-a",
            "kind": "task",
            "task_id": task_id,
            "goal": "Do A.",
        }
        task_paths = task_relative_paths(task_id, self.repo)
        result = {
            "task_id": task_id,
            "status": "success",
            "summary": "Task A done.",
            "handoff_path": handoff_display_path(self.repo, task_id),
            "changed_files": ["docs/a.md"],
            "validation": [{"command": "echo ok", "exit_code": 0, "result": "passed"}],
            "blocker": None,
            "recommended_next_action": "orchestrator_verify",
        }
        # HANDOFF still says "planned", so terminal validation must fail.
        task_paths["result"].write_text(json.dumps(result), encoding="utf-8")
        status, errors = run_graph.default_validate_node(self.repo, node, 0)
        self.assertEqual(status, "failed")
        self.assertTrue(errors)

    def test_default_validate_node_missing_result_failed(self) -> None:
        self.init_task_packages()
        node = {
            "id": "task-a",
            "kind": "task",
            "task_id": self.task_ids["task-a"],
            "goal": "Do A.",
        }
        status, errors = run_graph.default_validate_node(self.repo, node, 0)
        self.assertEqual(status, "failed")
        self.assertTrue(any("result.json" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

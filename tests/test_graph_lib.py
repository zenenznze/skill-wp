from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from graph_lib import (  # noqa: E402
    critical_path,
    effective_dependencies,
    graph_relative_paths,
    graph_status,
    plan_batch,
    ready_tasks,
    required_context,
    validate_graph,
    write_conflict,
)


def base_graph(**overrides: object) -> dict:
    graph = {
        "graph_id": "20260819-test-graph",
        "repo_id": "repo-abc123",
        "goal": "A minimal graph used to exercise validation.",
        "status": "pending",
        "created_at": "2026-08-19T00:00:00+08:00",
        "updated_at": "2026-08-19T00:00:00+08:00",
        "nodes": [
            {
                "id": "task-a",
                "kind": "task",
                "task_id": "20260819-task-a",
                "goal": "Do task A.",
                "status": "pending",
                "writes": ["src/**"],
                "attempts": 0,
            },
            {
                "id": "task-b",
                "kind": "task",
                "task_id": "20260819-task-b",
                "goal": "Do task B.",
                "status": "pending",
            },
            {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md", "status": "pending"},
        ],
        "edges": [
            {
                "from": "task-a",
                "to": "task-b",
                "relation": "depends_on",
                "reason": "task b builds on task a",
            },
            {
                "from": "task-a",
                "to": "artifact-x",
                "relation": "produces",
                "reason": "task a writes the artifact",
            },
        ],
    }
    graph.update(overrides)
    return graph


class ValidateGraphTests(unittest.TestCase):
    def test_valid_minimal_graph_passes(self) -> None:
        self.assertEqual(validate_graph(base_graph()), [])

    def test_top_level_field_rules(self) -> None:
        graph = base_graph()
        graph["graph_id"] = "Bad-ID"
        graph["repo_id"] = ""
        graph.pop("goal")
        graph["status"] = "weird"
        graph["nodes"] = "not-a-list"
        graph["edges"] = None
        errors = validate_graph(graph)
        self.assertTrue(any("graph_id" in error for error in errors))
        self.assertTrue(any("repo_id" in error for error in errors))
        self.assertTrue(any("goal" in error for error in errors))
        self.assertTrue(any("status" in error for error in errors))
        self.assertTrue(any("nodes" in error for error in errors))
        self.assertTrue(any("edges" in error for error in errors))

    def test_non_object_graph_rejected(self) -> None:
        self.assertEqual(validate_graph(["not", "a", "dict"]), ["graph must be a JSON object"])

    def test_task_node_field_rules(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {
                "id": "bad_id",
                "kind": "task",
                "task_id": "not-a-task-id",
                "goal": "",
                "status": "weird",
                "writes": ["/abs/path"],
                "attempts": -1,
            }
        ]
        graph["edges"] = []
        errors = validate_graph(graph)
        self.assertTrue(any("id" in error for error in errors))
        self.assertTrue(any("task_id" in error for error in errors))
        self.assertTrue(any("goal" in error for error in errors))
        self.assertTrue(any("status" in error for error in errors))
        self.assertTrue(any("writes" in error for error in errors))
        self.assertTrue(any("attempts" in error for error in errors))

    def test_duplicate_node_ids_rejected(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
        ]
        graph["edges"] = []
        errors = validate_graph(graph)
        self.assertTrue(any("duplicate" in error for error in errors))

    def test_artifact_node_field_rules(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "artifact-x", "kind": "artifact", "path": "../outside.md", "status": "weird"}
        ]
        graph["edges"] = []
        errors = validate_graph(graph)
        self.assertTrue(any("path" in error for error in errors))
        self.assertTrue(any("status" in error for error in errors))

    def test_unknown_kind_rejected(self) -> None:
        graph = base_graph()
        graph["nodes"] = [{"id": "task-a", "kind": "mystery"}]
        graph["edges"] = []
        errors = validate_graph(graph)
        self.assertTrue(any("kind" in error for error in errors))

    def test_edge_missing_node_rejected(self) -> None:
        graph = base_graph()
        graph["edges"] = [
            {
                "from": "task-a",
                "to": "ghost",
                "relation": "depends_on",
                "reason": "task depends on a ghost node",
            }
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("reference an existing node" in error for error in errors))

    def test_self_edge_rejected(self) -> None:
        graph = base_graph()
        graph["edges"] = [
            {
                "from": "task-a",
                "to": "task-a",
                "relation": "depends_on",
                "reason": "task a depends on itself",
            }
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("itself" in error for error in errors))

    def test_edge_reason_mandatory(self) -> None:
        graph = base_graph()
        graph["edges"][0].pop("reason")
        errors = validate_graph(graph)
        self.assertTrue(any("reason" in error for error in errors))

    def test_placeholder_and_short_reasons_rejected(self) -> None:
        for reason in ("TODO", "tbd", "unknown", "n/a", "xxx", ".", "a"):
            graph = base_graph()
            graph["edges"][0]["reason"] = reason
            errors = validate_graph(graph)
            self.assertTrue(any("reason" in error for error in errors), reason)

    def test_valid_reason_accepted(self) -> None:
        graph = base_graph()
        graph["edges"] = [
            {
                "from": "task-a",
                "to": "task-b",
                "relation": "depends_on",
                "reason": "a genuinely descriptive reason here",
            }
        ]
        self.assertEqual(validate_graph(graph), [])

    def test_depends_on_cycle_reported(self) -> None:
        graph = base_graph()
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "b depends on a"},
            {"from": "task-b", "to": "task-a", "relation": "depends_on", "reason": "a depends on b"},
        ]
        errors = validate_graph(graph)
        cycle_error = next((error for error in errors if "cycle" in error), None)
        self.assertIsNotNone(cycle_error)
        self.assertIn("task-a", cycle_error)
        self.assertIn("task-b", cycle_error)

    def test_consumes_through_produces_cycle_reported(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
            {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md"},
        ]
        graph["edges"] = [
            {
                "from": "task-a",
                "to": "artifact-x",
                "relation": "produces",
                "reason": "task a writes the artifact",
            },
            {
                "from": "artifact-x",
                "to": "task-a",
                "relation": "consumes",
                "reason": "task a consumes its own artifact",
            },
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("cycle" in error for error in errors))

    def test_relation_shape_rules(self) -> None:
        graph = base_graph()
        graph["edges"] = [
            {
                "from": "artifact-x",
                "to": "task-b",
                "relation": "depends_on",
                "reason": "artifacts cannot gate tasks",
            }
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("depends_on must be task -> task" in error for error in errors))

        graph["edges"] = [
            {
                "from": "artifact-x",
                "to": "task-b",
                "relation": "produces",
                "reason": "artifacts cannot produce tasks",
            }
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("produces must be task -> artifact" in error for error in errors))

        graph["edges"] = [
            {
                "from": "task-a",
                "to": "task-b",
                "relation": "consumes",
                "reason": "tasks cannot consume other tasks",
            }
        ]
        errors = validate_graph(graph)
        self.assertTrue(any("consumes must be artifact -> task" in error for error in errors))


class ReadyTasksTests(unittest.TestCase):
    def test_ready_respects_success_gating_and_consumes_through_produces(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "status": "success"},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "status": "pending"},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C.", "status": "pending"},
            {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md", "status": "produced"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "artifact-x", "relation": "produces", "reason": "task a writes the artifact"},
            {"from": "artifact-x", "to": "task-b", "relation": "consumes", "reason": "task b reads the artifact"},
            {"from": "task-a", "to": "task-c", "relation": "depends_on", "reason": "task c depends on task a"},
        ]
        self.assertEqual(ready_tasks(graph), ["task-b", "task-c"])

    def test_ready_excludes_pending_dependencies(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "status": "pending"},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "status": "pending"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "task b depends on task a"},
        ]
        self.assertEqual(ready_tasks(graph), ["task-a"])

    def test_effective_dependencies_resolves_consumes(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
            {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "artifact-x", "relation": "produces", "reason": "task a writes the artifact"},
            {"from": "artifact-x", "to": "task-b", "relation": "consumes", "reason": "task b reads the artifact"},
        ]
        self.assertEqual(effective_dependencies(graph, "task-b"), {"task-a"})
        self.assertEqual(effective_dependencies(graph, "task-a"), set())


class WriteConflictTests(unittest.TestCase):
    def test_empty_write_set_conflicts_with_everything(self) -> None:
        self.assertTrue(write_conflict([], ["src/**"]))
        self.assertTrue(write_conflict(["src/**"], []))
        self.assertTrue(write_conflict([], []))

    def test_equal_globs_conflict(self) -> None:
        self.assertTrue(write_conflict(["src/**"], ["src/**"]))

    def test_static_prefix_conflict(self) -> None:
        self.assertTrue(write_conflict(["src/**"], ["src/auth/**"]))
        self.assertFalse(write_conflict(["src/a/**"], ["src/b/**"]))

    def test_root_glob_conflicts_by_static_prefix(self) -> None:
        self.assertTrue(write_conflict(["*.py"], ["docs/*.md"]))


class PlanBatchTests(unittest.TestCase):
    def test_respects_max_parallel_and_conflict_exclusion(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "writes": ["src/**"]},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "writes": ["src/auth/**"]},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C.", "writes": ["docs/**"]},
        ]
        self.assertEqual(plan_batch(["task-a", "task-b", "task-c"], graph, 3), ["task-a", "task-c"])
        self.assertEqual(plan_batch(["task-a", "task-b", "task-c"], graph, 1), ["task-a"])

    def test_deterministic_ordering(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "writes": ["src/**"]},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C.", "writes": ["docs/**"]},
        ]
        self.assertEqual(plan_batch(["task-c", "task-a"], graph, 3), ["task-a", "task-c"])
        self.assertEqual(plan_batch(["task-a", "task-c"], graph, 3), ["task-a", "task-c"])


class GraphStatusTests(unittest.TestCase):
    def test_success_when_all_tasks_success(self) -> None:
        graph = base_graph()
        for node in graph["nodes"]:
            if node["kind"] == "task":
                node["status"] = "success"
        self.assertEqual(graph_status(graph), "success")

    def test_failed_when_failed_and_no_ready_remain(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "status": "failed"},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "status": "pending"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "task b depends on task a"},
        ]
        self.assertEqual(graph_status(graph), "failed")

    def test_blocked_when_blocked_and_nothing_ready(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "status": "blocked"},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "status": "pending"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "task b depends on task a"},
        ]
        self.assertEqual(graph_status(graph), "blocked")

    def test_running_when_any_node_running(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A.", "status": "running"},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B.", "status": "success"},
        ]
        self.assertEqual(graph_status(graph), "running")

    def test_pending_otherwise(self) -> None:
        self.assertEqual(graph_status(base_graph()), "pending")


class CriticalPathTests(unittest.TestCase):
    def test_longest_chain_selected(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C."},
            {"id": "task-d", "kind": "task", "task_id": "20260819-task-d", "goal": "Do D."},
            {"id": "task-e", "kind": "task", "task_id": "20260819-task-e", "goal": "Do E."},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "b depends on a"},
            {"from": "task-b", "to": "task-c", "relation": "depends_on", "reason": "c depends on b"},
            {"from": "task-d", "to": "task-e", "relation": "depends_on", "reason": "e depends on d"},
        ]
        self.assertEqual(critical_path(graph), ["task-a", "task-b", "task-c"])

    def test_deterministic_tie_break(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C."},
            {"id": "task-d", "kind": "task", "task_id": "20260819-task-d", "goal": "Do D."},
            {"id": "task-e", "kind": "task", "task_id": "20260819-task-e", "goal": "Do E."},
            {"id": "task-f", "kind": "task", "task_id": "20260819-task-f", "goal": "Do F."},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "task-b", "relation": "depends_on", "reason": "b depends on a"},
            {"from": "task-b", "to": "task-c", "relation": "depends_on", "reason": "c depends on b"},
            {"from": "task-d", "to": "task-e", "relation": "depends_on", "reason": "e depends on d"},
            {"from": "task-e", "to": "task-f", "relation": "depends_on", "reason": "f depends on e"},
        ]
        self.assertEqual(critical_path(graph), ["task-a", "task-b", "task-c"])

    def test_empty_graph_has_no_critical_path(self) -> None:
        graph = base_graph()
        graph["nodes"] = []
        graph["edges"] = []
        self.assertEqual(critical_path(graph), [])


class RequiredContextTests(unittest.TestCase):
    def test_transitive_upstream_tasks_and_artifacts(self) -> None:
        graph = base_graph()
        graph["nodes"] = [
            {"id": "task-a", "kind": "task", "task_id": "20260819-task-a", "goal": "Do A."},
            {"id": "task-b", "kind": "task", "task_id": "20260819-task-b", "goal": "Do B."},
            {"id": "task-c", "kind": "task", "task_id": "20260819-task-c", "goal": "Do C."},
            {"id": "task-d", "kind": "task", "task_id": "20260819-task-d", "goal": "Do D."},
            {"id": "artifact-x", "kind": "artifact", "path": "docs/x.md"},
            {"id": "artifact-y", "kind": "artifact", "path": "docs/y.md"},
        ]
        graph["edges"] = [
            {"from": "task-a", "to": "artifact-x", "relation": "produces", "reason": "task a writes x"},
            {"from": "task-b", "to": "artifact-y", "relation": "produces", "reason": "task b writes y"},
            {"from": "artifact-x", "to": "task-c", "relation": "consumes", "reason": "task c reads x"},
            {"from": "task-b", "to": "task-c", "relation": "depends_on", "reason": "task c depends on task b"},
            {"from": "task-c", "to": "task-d", "relation": "depends_on", "reason": "task d depends on task c"},
        ]
        context = required_context(graph, "task-d")
        self.assertEqual(context["upstream_tasks"], ["task-a", "task-b", "task-c"])
        artifacts = {(item["path"], item["produced_by"]) for item in context["artifacts"]}
        self.assertEqual(artifacts, {("docs/x.md", "task-a"), ("docs/y.md", "task-b")})
        self.assertNotIn("task-d", context["upstream_tasks"])


class GraphRelativePathsTests(unittest.TestCase):
    def test_layout(self) -> None:
        repo = Path("/tmp/wp-graph-test-repo")
        paths = graph_relative_paths("20260819-test-graph", repo)
        self.assertEqual(paths["graph_dir"].name, "20260819-test-graph")
        self.assertEqual(paths["graph_dir"].parent.name, "graphs")
        self.assertEqual(paths["graph_json"].name, "graph.json")
        self.assertEqual(paths["graph_handoff"].name, "GRAPH_HANDOFF.md")

    def test_invalid_graph_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            graph_relative_paths("invalid graph id", Path("/tmp/repo"))


if __name__ == "__main__":
    unittest.main()

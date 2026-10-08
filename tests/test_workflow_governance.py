import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from workflow_governance import (  # noqa: E402
    GovernanceError,
    approval_allows,
    classify_risk,
    deployment_gate,
    monitor_policy,
    pending_signoff,
    select_herdr_space,
    validate_governance,
    validate_herdr_plan,
    validate_slots,
)


class RiskTests(unittest.TestCase):
    def test_read_only_requires_no_side_effect_signal(self):
        result = classify_risk({
            "writes": False, "executes": False, "signals": [],
            "file_count": 0, "bounded_scope": True, "reversible": True,
        })
        self.assertEqual("read-only", result["risk"])
        self.assertFalse(result["human_gate_required"])

    def test_tiny_is_narrow_and_reversible(self):
        result = classify_risk({
            "writes": True, "executes": False, "signals": [],
            "file_count": 2, "bounded_scope": True, "reversible": True,
        })
        self.assertEqual("tiny", result["risk"])

    def test_unknown_or_executing_work_defaults_medium(self):
        result = classify_risk({
            "writes": True, "executes": True, "signals": [],
            "file_count": 1, "bounded_scope": True, "reversible": True,
        })
        self.assertEqual("medium", result["risk"])
        self.assertTrue(result["human_gate_required"])

    def test_unknown_and_mixed_signals_are_at_least_medium(self):
        base = {
            "writes": True, "executes": False, "file_count": 1,
            "bounded_scope": True, "reversible": True,
        }
        unknown = classify_risk({**base, "signals": ["deplyoment"]})
        mixed = classify_risk({**base, "signals": ["generated_files", "deplyoment"]})
        self.assertEqual("medium", unknown["risk"])
        self.assertEqual("medium", mixed["risk"])
        self.assertTrue(any("unknown risk signal" in reason for reason in mixed["reasons"]))

    def test_non_boolean_side_effect_flags_are_rejected(self):
        with self.assertRaises(GovernanceError):
            classify_risk({
                "writes": "false", "executes": False, "signals": [],
                "file_count": 0, "bounded_scope": True, "reversible": True,
            })

    def test_minimum_risk_requires_a_string(self):
        for value in (None, [], {}, 1, ""):
            with self.subTest(value=value):
                task = {
                    "writes": False, "executes": False, "signals": [],
                    "file_count": 0, "bounded_scope": True, "reversible": True,
                    "minimum_risk": value,
                }
                if value is None:
                    self.assertEqual("read-only", classify_risk(task)["risk"])
                else:
                    with self.assertRaises(GovernanceError):
                        classify_risk(task)

    def test_high_signal_and_minimum_risk_only_raise(self):
        high = classify_risk({
            "writes": False, "executes": False, "signals": ["deployment"],
            "file_count": 0, "bounded_scope": True, "reversible": True,
        })
        self.assertEqual("high-risk", high["risk"])
        raised = classify_risk({
            "writes": False, "executes": False, "signals": [],
            "file_count": 0, "bounded_scope": True, "reversible": True,
            "minimum_risk": "medium",
        })
        self.assertEqual("medium", raised["risk"])


class SlotAndApprovalTests(unittest.TestCase):
    def test_exactly_one_writer_and_read_only_support_roles(self):
        slots = [
            {"role": "orchestrator", "actor_type": "agent", "can_write": False, "can_deploy": False},
            {"role": "sole-writer", "actor_type": "agent", "can_write": True, "can_deploy": False},
            {"role": "reviewer", "actor_type": "agent", "can_write": False, "can_deploy": False},
            {"role": "scout", "actor_type": "agent", "can_write": False, "can_deploy": False},
            {"role": "monitor", "actor_type": "agent", "can_write": False, "can_deploy": False},
            {"role": "human-approver", "actor_type": "human", "can_write": False, "can_deploy": False},
        ]
        self.assertEqual([], validate_slots(slots, writes=True))
        self.assertIn(
            "writing work requires exactly one sole-writer",
            validate_slots([slots[0]], writes=True),
        )

    def test_role_and_actor_type_require_strict_strings(self):
        invalid = (None, [], {}, 1, "")
        for value in invalid:
            with self.subTest(role=value):
                errors = validate_slots([{
                    "role": value, "actor_type": "agent",
                    "can_write": False, "can_deploy": False,
                }], writes=False)
                self.assertTrue(any("role is invalid" in error for error in errors))
            with self.subTest(actor_type=value):
                errors = validate_slots([{
                    "role": "orchestrator", "actor_type": value,
                    "can_write": False, "can_deploy": False,
                }], writes=False)
                self.assertTrue(any("actor_type is invalid" in error for error in errors))

    def test_human_approver_cannot_be_agent(self):
        errors = validate_slots([{
            "role": "human-approver", "actor_type": "agent",
            "can_write": False, "can_deploy": False,
        }], writes=False)
        self.assertIn("human-approver cannot be impersonated by an agent", errors)
        self.assertIn("every workflow requires one orchestrator", errors)

    def test_duplicate_role_is_rejected(self):
        slot = {"role": "orchestrator", "actor_type": "agent", "can_write": False, "can_deploy": False}
        self.assertIn("role orchestrator may be assigned only once", validate_slots([slot, slot], writes=False))

    def test_agents_initialize_pending_signoff_only(self):
        self.assertEqual("pending", pending_signoff(["implementation"])["status"])
        allowed, reasons = approval_allows(
            {"status": "pending", "actor_type": "human", "actions": ["implement"],
             "scope": ["implementation"], "statement": "Proceed."},
            "implement", ["implementation"],
        )
        self.assertFalse(allowed)
        self.assertIn("approval status is not approved", reasons)

    def test_scope_does_not_infer_deployment(self):
        approval = {
            "status": "approved", "actor_type": "human",
            "actions": ["implement", "verify", "commit", "push"],
            "scope": ["repository"], "statement": "全部执行",
        }
        allowed, reasons = approval_allows(approval, "deploy", ["production"])
        self.assertFalse(allowed)
        self.assertTrue(reasons)
        allowed, reasons = approval_allows(approval, "implement", [])
        self.assertFalse(allowed)
        self.assertIn("required_scope must not be empty", reasons)

    def test_malformed_approval_collections_and_action_fail_without_exception(self):
        base = {
            "status": "approved", "actor_type": "human",
            "actions": ["implement"], "scope": ["repository"],
            "statement": "Proceed.",
        }
        malformed = [None, {}, 1, "repository", [""], [1]]
        for value in malformed:
            with self.subTest(field="actions", value=value):
                self.assertFalse(approval_allows(dict(base, actions=value), "implement", ["repository"])[0])
            with self.subTest(field="scope", value=value):
                self.assertFalse(approval_allows(dict(base, scope=value), "implement", ["repository"])[0])
            with self.subTest(field="required_scope", value=value):
                self.assertFalse(approval_allows(base, "implement", value)[0])
        for action in (None, 1, "", "Implement", "unknown"):
            with self.subTest(action=action):
                self.assertFalse(approval_allows(base, action, ["repository"])[0])
        mixed = dict(base, actions=["implement", "unknown"])
        self.assertFalse(approval_allows(mixed, "implement", ["repository"])[0])


class MonitorAndDeploymentTests(unittest.TestCase):
    def test_monitor_interval_and_repeated_root_cause_stop(self):
        self.assertTrue(monitor_policy(60, 1)["automatic_retry_allowed"])
        stopped = monitor_policy(120, 2)
        self.assertFalse(stopped["automatic_retry_allowed"])
        self.assertEqual("blocked", stopped["status"])
        for value in (True, -1, 60.0, "60"):
            with self.subTest(interval=value), self.assertRaises(GovernanceError):
                monitor_policy(value, 0)
        for value in (True, -1, 1.0, "1"):
            with self.subTest(count=value), self.assertRaises(GovernanceError):
                monitor_policy(60, value)

    def test_deployment_requires_separate_human_gate_and_safety_steps(self):
        record = {
            "requester": "controller",
            "approval": {
                "status": "approved", "actor_type": "human",
                "actions": ["deploy"], "scope": ["production"],
                "statement": "Deploy production now.",
            },
            "preflight_passed": True, "backup_verified": True,
            "health_check_planned": True, "log_check_planned": True,
            "rollback_ready": True,
        }
        for requester in ("controller", "orchestrator"):
            with self.subTest(requester=requester):
                self.assertTrue(deployment_gate(dict(record, requester=requester), ["production"])["allowed"])
        for requester in (None, "", "Controller", "runner", "runner-v2", "worker", 1, True):
            with self.subTest(requester=requester):
                self.assertFalse(deployment_gate(dict(record, requester=requester), ["production"])["allowed"])


class HerdrTests(unittest.TestCase):
    def _plan(self):
        return {
            "controller_tabs": 1,
            "split_panes": False,
            "no_focus": True,
            "preserve_controller_tab": True,
            "timeout_action": "get-read-before-retry",
            "tabs": [{
                "kind": "pi", "label": "执行-协议复核", "pane_count": 1,
                "workspace_id": "w1", "tab_id": "w1:t2", "pane_id": "w1:p2",
                "agent_id": "protocol-review", "close_requested": True,
                "accepted": True, "completed_label": "完成-协议复核",
            }],
        }

    def test_pi_only_semantic_tab_plan(self):
        self.assertEqual([], validate_herdr_plan(self._plan()))
        plan = self._plan()
        plan["tabs"][0]["kind"] = "codex"
        plan["tabs"][0]["accepted"] = False
        plan["tabs"][0]["label"] = "reviewer"
        errors = validate_herdr_plan(plan)
        self.assertTrue(any("--kind pi" in error for error in errors))
        self.assertTrue(any("before independent acceptance" in error for error in errors))
        self.assertTrue(any("semantic label" in error for error in errors))

    def test_integer_topology_fields_reject_bool_float_string_and_negative(self):
        for value in (True, -1, 1.0, "1"):
            with self.subTest(controller_tabs=value):
                plan = self._plan()
                plan["controller_tabs"] = value
                self.assertTrue(validate_herdr_plan(plan))
            with self.subTest(pane_count=value):
                plan = self._plan()
                plan["tabs"][0]["pane_count"] = value
                self.assertTrue(validate_herdr_plan(plan))
        for value in (None, 0, 1, "false"):
            with self.subTest(split_panes=value):
                plan = self._plan()
                plan["split_panes"] = value
                self.assertTrue(validate_herdr_plan(plan))
        for value in (None, 0, 1, "false"):
            with self.subTest(close_requested=value):
                plan = self._plan()
                plan["tabs"][0]["close_requested"] = value
                self.assertTrue(validate_herdr_plan(plan))

    def test_space_selection_reuses_unique_cwd_or_creates_dedicated_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected = select_herdr_space(
                tmp, [{"workspace_id": "w7", "cwd": tmp}], False
            )
            self.assertEqual("reuse", selected["action"])
            dedicated = select_herdr_space(tmp, [], True, "WP融合SOP")
            self.assertEqual("create", dedicated["action"])
            self.assertEqual("WP融合SOP", dedicated["label"])
            for bad in (None, 1, 0, "true"):
                with self.subTest(topic_requires_dedicated_space=bad), self.assertRaises(GovernanceError):
                    select_herdr_space(tmp, [], bad, "WP融合SOP")


class GovernanceRecordTests(unittest.TestCase):
    def _read_only_record(self):
        return {
            "workflow_governance_version": 1,
            "task": {
                "writes": False, "executes": False, "signals": [], "file_count": 0,
                "bounded_scope": True, "reversible": True,
            },
            "slots": [{
                "role": "orchestrator", "actor_type": "agent",
                "can_write": False, "can_deploy": False,
            }],
            "approval_scope": ["repository"],
            "signoff": {
                "status": "pending", "scope": ["repository"],
                "decision_by": None, "statement": None,
            },
        }

    def test_governance_version_rejects_bool_and_coercions(self):
        for version in (True, 1.0, "1", None):
            with self.subTest(version=version):
                record = self._read_only_record()
                record["workflow_governance_version"] = version
                self.assertFalse(validate_governance(record)["valid"])

    def test_task_is_required_object_with_every_typed_field(self):
        for task in (None, [], "task", 1, True):
            with self.subTest(task=task):
                record = self._read_only_record()
                record["task"] = task
                self.assertFalse(validate_governance(record)["valid"])
        fields = ("writes", "executes", "signals", "file_count", "bounded_scope", "reversible")
        for field in fields:
            with self.subTest(missing=field):
                record = self._read_only_record()
                del record["task"][field]
                self.assertFalse(validate_governance(record)["valid"])
        invalid_values = {
            "writes": (None, 0, 1, "false", []),
            "executes": (None, 0, 1, "false", []),
            "signals": (None, {}, "signal", [""], [1]),
            "file_count": (None, True, -1, 0.0, "0"),
            "bounded_scope": (None, 0, 1, "true", []),
            "reversible": (None, 0, 1, "true", []),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    record = self._read_only_record()
                    record["task"][field] = value
                    self.assertFalse(validate_governance(record)["valid"])

    def test_medium_record_requires_scoped_human_implementation_approval(self):
        record = {
            "workflow_governance_version": 1,
            "task": {
                "writes": True, "executes": True, "signals": [], "file_count": 3,
                "bounded_scope": True, "reversible": True,
            },
            "slots": [
                {"role": "orchestrator", "actor_type": "agent", "can_write": False, "can_deploy": False},
                {"role": "sole-writer", "actor_type": "agent", "can_write": True, "can_deploy": False},
                {"role": "human-approver", "actor_type": "human", "can_write": False, "can_deploy": False},
            ],
            "approval_scope": ["repository"],
            "approval": {
                "status": "approved", "actor_type": "human", "actions": ["implement"],
                "scope": ["repository"], "statement": "Implement and verify.",
            },
            "signoff": {
                "status": "pending", "scope": ["repository"],
                "decision_by": None, "statement": None,
            },
        }
        self.assertTrue(validate_governance(record)["valid"])
        record["approval"]["actions"] = ["deploy"]
        self.assertFalse(validate_governance(record)["valid"])

    def test_pending_signoff_scope_is_strict_non_empty_string_list(self):
        for scope in (None, {}, "repository", [], [""], [1]):
            with self.subTest(scope=scope):
                record = self._read_only_record()
                record["signoff"]["scope"] = scope
                self.assertFalse(validate_governance(record)["valid"])

    def test_cli_malformed_records_return_structured_invalid_exit_two(self):
        script = SCRIPTS / "workflow_governance.py"
        records = [
            {"workflow_governance_version": 1},
            {**self._read_only_record(), "task": {"writes": False}},
            {
                **self._read_only_record(),
                "task": {**self._read_only_record()["task"], "file_count": True},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, record in enumerate(records):
                path = Path(tmp) / f"record-{index}.json"
                path.write_text(json.dumps(record), encoding="utf-8")
                process = subprocess.run(
                    [sys.executable, str(script), str(path)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(2, process.returncode)
                result = json.loads(process.stdout)
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"])

    def test_pending_signoff_cannot_contain_decision(self):
        record = {
            "workflow_governance_version": 1,
            "task": {
                "writes": False, "executes": False, "signals": [], "file_count": 0,
                "bounded_scope": True, "reversible": True,
            },
            "slots": [{
                "role": "orchestrator", "actor_type": "agent",
                "can_write": False, "can_deploy": False,
            }],
            "approval_scope": ["repository"],
            "signoff": {
                "status": "pending", "scope": ["repository"],
                "decision_by": "agent", "statement": "approved",
            },
        }
        result = validate_governance(record)
        self.assertFalse(result["valid"])
        self.assertIn("pending signoff cannot contain a human decision", result["errors"])


if __name__ == "__main__":
    unittest.main()

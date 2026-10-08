import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.herdr_pi_control as h


class FakeClient:
    def __init__(self, *, cwd, wait_status="done", start="nested", workspaces=None):
        self.cwd = cwd
        self.calls = []
        self.wait_status = wait_status
        self.start = start
        self.workspaces = workspaces or []

    def call(self, *args):
        self.calls.append(args)
        if args[:2] == ("tab", "rename") or args[:2] == ("tab", "close"):
            return {"result": {}}
        if args[:2] == ("workspace", "list"):
            return {"result": {"workspaces": self.workspaces}}
        if args[:2] == ("tab", "list"):
            return {"result": {"tabs": [{"tab_id": "w9:t1", "label": "执行-支付修复"}]}}
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": "w9:p1", "tab_id": "w9:t1", "cwd": self.cwd}]}}
        if args[:2] == ("workspace", "create"):
            return {"result": {"workspace": {"workspace_id": "w9"}, "tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}}
        if args[:2] == ("tab", "create"):
            return {"result": {"tab": {"tab_id": "w9:t1"}, "root_pane": {"pane_id": "w9:p1"}}}
        if args[:2] == ("agent", "start"):
            if self.start == "nested": return {"result": {"agent": {"name": args[2], "pane_id": "w9:p1"}}}
            if self.start == "missing": return {"result": {"agent": {"pane_id": "w9:p1"}}}
            return {"result": {"agent_name": args[2]}}
        if args[:2] == ("agent", "prompt") or args[:2] == ("agent", "read"):
            return {"result": {}}
        if args[:2] == ("agent", "wait"):
            return {"result": {"status": self.wait_status}}
        if args[:2] == ("agent", "get"):
            return {"result": {"agent": {"name": "payment-implementer", "pane_id": "w9:p1"}}}
        raise AssertionError(args)

    def result(self, *args):
        return h.herdr_result(self.call(*args))


def make_plan(cwd, **changes):
    value = {
        "cwd": cwd, "space_mode": "cwd", "agent_name": "payment-implementer",
        "tab_label": "执行-支付修复", "controller_tab_id": "w16:t7",
        "controller_tab_label": "主控-支付修复", "kind": "pi", "no_focus": True,
        "split": False, "prompt": "perform bounded work", "timeout_seconds": 1,
    }
    value.update(changes)
    return value


def receipt_for(path, closed=False):
    value = {
        "schema": h.SCHEMA, "receipt_id": "pending", "receipt_mac": "pending", "lifecycle_only": True,
        "partial": False, "closed": closed, "command": "run", "status": "done", "inconclusive": False,
        "timeout": False, "error_class": None, "error_message": None, "workspace_id": "w9", "workspace_created": True,
        "controller_tab_id": "w16:t7", "controller_tab_label": "主控-支付修复",
        "tab_id": "w9:t1", "tab_label": "执行-支付修复", "pane_id": "w9:p1",
        "agent_id": "payment-implementer",
        "created": [{"kind": "workspace", "id": "w9"}, {"kind": "tab", "id": "w9:t1"}, {"kind": "pane", "id": "w9:p1"}],
    }
    value = h.sign_receipt(value)
    value = h.validate_receipt(value)
    h.write_new_receipt(path, value)
    return value


class HerdrBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.tmp.name).resolve())
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.state_patch = patch.object(h, "STATE", self.state)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tmp.cleanup()

    def test_plan_requires_existing_absolute_cwd_and_pi_semantics(self):
        with self.assertRaises(h.BridgeError): h.validate_plan(make_plan("relative"))
        with self.assertRaises(h.BridgeError): h.validate_plan(make_plan(self.cwd, agent_name="worker"))
        with self.assertRaises(h.BridgeError): h.validate_plan(make_plan(self.cwd, kind="codex"))
        with self.assertRaises(h.BridgeError): h.validate_plan(make_plan(self.cwd, timeout_seconds=True))

    def test_env_gate_does_not_call_client(self):
        fake = FakeClient(cwd=self.cwd)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(h.BridgeError): h.run_plan(h.validate_plan(make_plan(self.cwd)), fake)
        self.assertEqual(fake.calls, [])

    def test_topic_create_and_cwd_reuse_uses_topology_not_workspace_cwd(self):
        fake = FakeClient(cwd=self.cwd)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            out = h.run_plan(h.validate_plan(make_plan(self.cwd, space_mode="topic", topic="支付修复")), fake, "topic.json")
        self.assertEqual(out["workspace_id"], "w9")
        self.assertIn(("workspace", "create", "--cwd", self.cwd, "--label", "支付修复", "--no-focus"), fake.calls)
        fake = FakeClient(cwd=self.cwd, workspaces=[{"workspace_id": "w9"}])
        with patch.dict(os.environ, {"HERDR_ENV": "1"}): h.run_plan(h.validate_plan(make_plan(self.cwd)), fake, "reuse.json")
        self.assertIn(("tab", "create", "--workspace", "w9", "--cwd", self.cwd, "--label", "执行-支付修复", "--no-focus"), fake.calls)

    def test_missing_cwd_and_multiple_matches_fail_closed(self):
        class Missing(FakeClient):
            def call(self, *args):
                if args[:2] == ("pane", "list"): return {"result": {"panes": [{"pane_id": "p"}]}}
                return super().call(*args)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with self.assertRaises((h.ProtocolError, h.BridgeError)): h.run_plan(h.validate_plan(make_plan(self.cwd)), Missing(cwd=self.cwd, workspaces=[{"workspace_id": "w1"}]))
            with self.assertRaises(h.BridgeError): h.discover_workspace(h.validate_plan(make_plan(self.cwd)), FakeClient(cwd=self.cwd, workspaces=[{"workspace_id": "w1"}, {"workspace_id": "w2"}]))

    def test_start_requires_real_nested_agent_id_and_pi_command(self):
        fake = FakeClient(cwd=self.cwd)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}): out = h.run_plan(h.validate_plan(make_plan(self.cwd)), fake, "run.json")
        self.assertEqual(out["agent_id"], "payment-implementer")
        self.assertIn(("agent", "start", "payment-implementer", "--kind", "pi", "--pane", "w9:p1"), fake.calls)
        with self.assertRaises(h.ProtocolError):
            h.run_plan(h.validate_plan(make_plan(self.cwd)), FakeClient(cwd=self.cwd, start="missing"), "missing.json")

    def test_status_classification_timeout_command_and_protocol(self):
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            out = h.run_plan(h.validate_plan(make_plan(self.cwd)), FakeClient(cwd=self.cwd, wait_status="unknown"), "unknown.json")
        self.assertEqual(out["status"], "unknown"); self.assertTrue(out["inconclusive"])
        class Timeout(FakeClient):
            def call(self, *args):
                if args[:2] == ("agent", "wait"): raise h.HerdrTimeout("timed out")
                if args[:2] in (("agent", "get"), ("agent", "read")): raise h.CommandError("readback failed")
                return super().call(*args)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            timed = h.run_plan(h.validate_plan(make_plan(self.cwd)), Timeout(cwd=self.cwd), "timeout.json")
        self.assertEqual(timed["status"], "timeout"); self.assertTrue(timed["timeout"])
        class Failing(FakeClient):
            def call(self, *args):
                if args[:2] == ("agent", "wait"): raise h.CommandError("failed")
                return super().call(*args)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with self.assertRaises(h.CommandError): h.run_plan(h.validate_plan(make_plan(self.cwd)), Failing(cwd=self.cwd), "failed.json")
        saved = h.read_receipt(h.receipt_path("failed.json")); self.assertEqual(saved["status"], "command_failed"); self.assertFalse(saved["timeout"])

    def test_atomic_no_overwrite_tamper_and_path_safety(self):
        path = h.receipt_path("safe.json")
        receipt_for(path)
        with self.assertRaises(h.BridgeError): receipt_for(path)
        with self.assertRaises(h.BridgeError): h.receipt_path("../escape.json")
        absolute = str(path)
        with self.assertRaises(h.BridgeError): h.receipt_path(absolute)
        path.write_text("{}")
        with self.assertRaises(h.BridgeError): h.read_receipt(path)
        key = self.state / h.KEY_NAME
        key.chmod(0o644)
        with self.assertRaises(h.BridgeError): h.bridge_key(False)
        key.chmod(0o600); key.unlink()
        with self.assertRaises(h.BridgeError): h.read_receipt(path)

    def test_close_requires_real_file_acceptance_topology_and_marks_once(self):
        path = h.receipt_path("close.json"); receipt_for(path)
        fake = FakeClient(cwd=self.cwd)
        with self.assertRaises(h.BridgeError): h.close_file("close.json", fake, False)
        out = h.close_file("close.json", fake, True)
        self.assertTrue(out["closed"])
        self.assertLess(fake.calls.index(("tab", "rename", "w9:t1", "完成-执行-支付修复")), fake.calls.index(("tab", "close", "w9:t1")))
        with self.assertRaises(h.BridgeError): h.close_file("close.json", fake, True)

    def test_close_interruption_leaves_receipt_open(self):
        path = h.receipt_path("interrupted.json"); receipt_for(path)
        class Interrupted(FakeClient):
            def call(self, *args):
                if args[:2] == ("tab", "close"): raise h.CommandError("close failed")
                return super().call(*args)
        with self.assertRaises(h.CommandError): h.close_file("interrupted.json", Interrupted(cwd=self.cwd), True)
        self.assertFalse(h.read_receipt(path)["closed"])

    def test_close_rejects_forged_controller_or_user_tab_and_tampered_ids(self):
        path = h.receipt_path("forged.json"); value = receipt_for(path)
        value["tab_id"] = value["controller_tab_id"]
        path.write_text(json.dumps(value))
        with self.assertRaises(h.BridgeError): h.close_file("forged.json", FakeClient(cwd=self.cwd), True)
        path.write_text(json.dumps(receipt_for(h.receipt_path("tmp.json"))))
        value = json.loads(path.read_text()); value["created"][1]["id"] = "user:t99"; path.write_text(json.dumps(value))
        with self.assertRaises(h.BridgeError): h.close_file("forged.json", FakeClient(cwd=self.cwd), True)

    def test_malformed_responses_and_cli_are_structured(self):
        with self.assertRaises(h.ProtocolError): h.herdr_result({"result": None})
        with self.assertRaises(h.ProtocolError): h.nested_id({"agent": {}}, "agent", "name")
        cp = subprocess.run([sys.executable, str(Path(h.__file__)), "plan", "--input", "[]"], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 2); self.assertNotIn("Traceback", cp.stderr); self.assertTrue(json.loads(cp.stderr)["error"])

    def test_fake_executable_and_command_timeout_types(self):
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "herdr"; marker = Path(td) / "called"
            stub.write_text("#!/usr/bin/env python3\nimport pathlib,os\npathlib.Path(os.environ['MARKER']).write_text('x')\nprint('{\\\"result\\\":{}}')\n")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"MARKER": str(marker)}): h.HerdrClient(str(stub)).call("workspace", "list")
            self.assertTrue(marker.exists())
            stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('not a timeout classification')\nsys.exit(4)\n")
            with self.assertRaises(h.CommandError) as caught: h.HerdrClient(str(stub)).call("agent", "wait")
            self.assertNotIsInstance(caught.exception, h.HerdrTimeout)
        self.assertTrue(issubclass(h.HerdrTimeout, h.CommandError))

    def test_receipt_symlink_is_rejected_by_fd_open(self):
        real = self.state / "real.json"; receipt_for(real)
        link = self.state / "link.json"; link.symlink_to(real)
        with self.assertRaises((h.BridgeError, OSError)): h.read_receipt(link)
        with self.assertRaises((h.BridgeError, OSError)): h.close_file("link.json", FakeClient(cwd=self.cwd), True)

    def test_public_hash_forgery_without_bridge_key_is_rejected(self):
        path = h.receipt_path("user.json")
        value = receipt_for(path)
        value["tab_id"] = "user:t1"
        value["created"][1]["id"] = "user:t1"
        value["receipt_mac"] = "00" * 32
        path.write_text(json.dumps(value))
        with self.assertRaises(h.BridgeError): h.read_receipt(path)

    def test_partial_journal_after_agent_start_and_prompt_failure(self):
        class PromptFailure(FakeClient):
            def call(self, *args):
                if args[:2] == ("agent", "prompt"): raise h.CommandError("prompt failed")
                return super().call(*args)
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with self.assertRaises(h.CommandError): h.run_plan(h.validate_plan(make_plan(self.cwd)), PromptFailure(cwd=self.cwd), "partial.json")
        partial = h.read_receipt(h.receipt_path("partial.json"))
        self.assertTrue(partial["partial"]); self.assertEqual(partial["status"], "command_failed"); self.assertEqual(partial["agent_id"], "payment-implementer")
        with self.assertRaises(h.BridgeError): h.close_file("partial.json", FakeClient(cwd=self.cwd), True)


if __name__ == "__main__": unittest.main()

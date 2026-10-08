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
RUNNER = ROOT / "scripts" / "run_claude_executor.py"
VERIFY = ROOT / "scripts" / "verify_result.py"
sys.path.insert(0, str(ROOT / "scripts"))
from protocol import (  # noqa: E402
    handoff_display_path,
    parse_task_package_text,
    task_relative_paths,
    validate_atomic_work_contract,
)


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.task_id = "20260803-test-task"
        process = subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--task-id", self.task_id,
             "--goal", "Implement and verify a bounded test change.", "--agent", "auto"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.paths = task_relative_paths(self.task_id, self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.paths["task_dir"].parents[1], ignore_errors=True)
        self.temporary.cleanup()

    def test_init_keeps_state_in_skill_and_uses_current_agent(self) -> None:
        self.assertTrue(self.paths["handoff"].is_file())
        self.assertFalse((self.repo / ".agent").exists())
        handoff = self.paths["handoff"].read_text(encoding="utf-8")
        self.assertIn("orchestrator: current-agent", handoff)
        self.assertIn("This is advisory context", handoff)
        self.assertIn("task_protocol_version: 2", handoff)
        self.assertIn("# Atomic Work Contract", handoff)

    def test_valid_atomic_work_contract(self) -> None:
        text = """---
task_protocol_version: 2
---

# Atomic Work Contract

```json
{
  "single_outcome": "Produce one verified parser change.",
  "deliverables": ["scripts/protocol.py"],
  "write_scope": ["scripts/protocol.py"],
  "read_only": false,
  "acceptance": ["The focused unit test passes."],
  "resume_boundary": "Resume at the failing focused unit test."
}
```

# Next section

# Acceptance Criteria

- [ ] The focused check passes.

# Validation Commands

```bash
true
```
"""
        parsed = parse_task_package_text(text)
        self.assertTrue(parsed.valid)
        self.assertFalse(parsed.legacy)
        self.assertEqual(parsed.protocol_version, 2)

    def test_unversioned_package_is_explicit_legacy(self) -> None:
        parsed = parse_task_package_text("---\nstatus: planned\n---\n# Goal\nLegacy")
        self.assertTrue(parsed.valid)
        self.assertTrue(parsed.legacy)

    def test_contract_rejects_each_invalid_field_class(self) -> None:
        contract = {
            "single_outcome": "TODO",
            "deliverables": [],
            "write_scope": ["../outside"],
            "read_only": "false",
            "acceptance": ["TBD"],
            "resume_boundary": "",
        }
        errors = validate_atomic_work_contract(contract)
        self.assertTrue(any("single_outcome" in error for error in errors))
        self.assertTrue(any("deliverables" in error for error in errors))
        self.assertTrue(any("write_scope" in error for error in errors))
        self.assertTrue(any("read_only" in error for error in errors))
        self.assertTrue(any("acceptance" in error for error in errors))
        self.assertTrue(any("resume_boundary" in error for error in errors))

    def test_placeholder_words_inside_legitimate_prose_are_allowed(self) -> None:
        contract = {
            "single_outcome": "Remove TODO markers from generated docs.",
            "deliverables": ["A report covering unknown and none cases."],
            "write_scope": ["docs/generated.md"],
            "read_only": False,
            "acceptance": ["Generated docs contain no TODO markers."],
            "resume_boundary": "Resume after documenting unknown inputs; none are silently ignored.",
        }
        self.assertEqual(validate_atomic_work_contract(contract), [])
        text = f"""---
task_protocol_version: 2
---

# Atomic Work Contract

```json
{json.dumps(contract, indent=2)}
```

# Acceptance Criteria

- [ ] Remove TODO markers from generated docs.

# Validation Commands

```bash
python3 -c 'print("unknown and none are documented")'
```

# Blockers

- None.
"""
        parsed = parse_task_package_text(text)
        self.assertTrue(parsed.valid, parsed.errors)

    def test_template_field_placeholder_is_rejected(self) -> None:
        text = """---
task_protocol_version: 2
---

# Goal

Produce one verified parser change.

# Atomic Work Contract

```json
{
  "single_outcome": "Produce one verified parser change.",
  "deliverables": ["scripts/protocol.py"],
  "write_scope": ["scripts/protocol.py"],
  "read_only": false,
  "acceptance": ["The focused unit test passes."],
  "resume_boundary": "Resume at the failing focused unit test."
}
```

# Current Repository State

- Repository type: TODO

# Acceptance Criteria

- [ ] The focused check passes.

# Validation Commands

```bash
true
```
"""
        parsed = parse_task_package_text(text)
        self.assertFalse(parsed.valid)
        self.assertTrue(any("placeholder outside" in error for error in parsed.errors))

    def test_contract_rejects_malformed_json_and_read_only_write_scope(self) -> None:
        malformed = parse_task_package_text(
            "---\ntask_protocol_version: 2\n---\n"
            "# Atomic Work Contract\n\n```json\n{bad}\n```\n"
        )
        self.assertFalse(malformed.valid)
        self.assertIn("malformed", malformed.errors[0])
        errors = validate_atomic_work_contract(
            {
                "single_outcome": "Inspect a repository.",
                "deliverables": ["An inspection report"],
                "write_scope": ["docs/**"],
                "read_only": True,
                "acceptance": ["The report is independently checked."],
                "resume_boundary": "Resume from the last inspection command.",
            }
        )
        self.assertTrue(any("empty write_scope" in error for error in errors))
        self.assertEqual(
            validate_atomic_work_contract(
                {
                    "single_outcome": "Inspect without changing files.",
                    "deliverables": ["Inspection findings"],
                    "write_scope": [],
                    "read_only": True,
                    "acceptance": ["The findings are independently checked."],
                    "resume_boundary": "Resume at the next inspection command.",
                }
            ),
            [],
        )

    def test_v2_requires_completed_acceptance_and_validation_sections(self) -> None:
        text = """---
task_protocol_version: 2
---

# Atomic Work Contract

```json
{
  "single_outcome": "Produce one verified parser change.",
  "deliverables": ["scripts/protocol.py"],
  "write_scope": ["scripts/protocol.py"],
  "read_only": false,
  "acceptance": ["An independent check passes."],
  "resume_boundary": "Resume at the failing focused test."
}
```

# Acceptance Criteria

- [ ] TODO

# Validation Commands

```bash
TODO
```
"""
        parsed = parse_task_package_text(text)
        self.assertFalse(parsed.valid)
        self.assertTrue(any("placeholder outside" in error for error in parsed.errors))

    def fake_claude(self, body: str) -> Path:
        executable = self.repo / "fake-claude"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, re, sys, json\n"
            "if '--help' in sys.argv:\n"
            "    print('--max-turns --model')\n"
            "    raise SystemExit(0)\n"
            + textwrap.dedent(body), encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def run_runner(self, binary: Path, timeout: float = 5) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "--repo", str(self.repo), "--task-id", self.task_id,
             "--claude-bin", str(binary), "--timeout-seconds", str(timeout)],
            check=False, capture_output=True, text=True,
        )

    def test_success_result_and_verify(self) -> None:
        fake = self.fake_claude(
            """
            handoff_path = pathlib.Path(os.environ['AGENT_HANDOFF_PATH'])
            handoff = handoff_path.read_text(encoding='utf-8')
            handoff = re.sub(r'^status:.*$', 'status: success', handoff, count=1, flags=re.M)
            handoff = re.sub(r'^runner_sentinel:.*\\n?', '', handoff, count=1, flags=re.M)
            handoff_path.write_text(handoff, encoding='utf-8')
            result = {
                'task_id': os.environ['AGENT_TASK_ID'], 'status': 'success',
                'summary': 'Fake executor completed.',
                'handoff_path': os.environ['AGENT_HANDOFF_RELATIVE'],
                'changed_files': [],
                'validation': [{'command': 'true', 'exit_code': 0, 'result': 'passed'}],
                'blocker': None, 'recommended_next_action': 'orchestrator_verify',
                'producer': 'fake-executor'
            }
            pathlib.Path(os.environ['AGENT_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')
            """
        )
        process = self.run_runner(fake)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(self.paths["result"].read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")
        verify = subprocess.run(
            [sys.executable, str(VERIFY), "--repo", str(self.repo), "--task-id", self.task_id,
             "--require-success"], check=False, capture_output=True, text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_missing_result_is_failed_without_revision(self) -> None:
        fake = self.fake_claude("raise SystemExit(7)\n")
        process = self.run_runner(fake)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(self.paths["result"].read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "task_failure")
        self.assertFalse((self.paths["task_dir"] / "revision-01.md").exists())

    def test_multiline_model_is_rejected_before_state_update(self) -> None:
        original = self.paths["handoff"].read_text(encoding="utf-8")
        process = subprocess.run(
            [sys.executable, str(INIT), "--repo", str(self.repo), "--task-id", "20260803-bad-model",
             "--goal", "Reject unsafe metadata.", "--agent", "codex", "--codex-model", "bad\nstatus: success"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("single-line", process.stderr)
        self.assertEqual(self.paths["handoff"].read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scan_task_record import main, scan_path  # noqa: E402


class ScanTaskRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.record = Path(self.tmp.name) / "tasks" / "20260101-demo"
        self.record.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, content: str) -> None:
        (self.record / name).write_text(content, encoding="utf-8")

    def test_clean_record(self) -> None:
        self.write("HANDOFF.md", "# Goal\n\nImplement the bounded change.\n")
        self.write("result.json", '{"status": "success"}\n')
        hard, warnings = scan_path(self.record)
        self.assertEqual(hard, [])
        self.assertEqual(warnings, [])

    def test_private_key_block_fails(self) -> None:
        self.write(
            "HANDOFF.md",
            "-----BEGIN RSA "
            "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA "
            "PRIVATE KEY-----\n",
        )
        hard, warnings = scan_path(self.record)
        self.assertEqual(len(hard), 1)
        self.assertIn("private key block", hard[0])
        self.assertEqual(main([str(self.record)]), 1)

    def test_credential_assignment_fails(self) -> None:
        self.write("note.md", 'export API_KEY="sk-1234567890abcdef"\n')
        hard, _ = scan_path(self.record)
        self.assertTrue(any("credential assignment" in item for item in hard))
        self.assertEqual(main([str(self.record)]), 1)

    def test_home_path_warns(self) -> None:
        self.write("roster.json", '{"path": "/home/alice/.grok/bin/grok"}\n')
        hard, warnings = scan_path(self.record)
        self.assertEqual(hard, [])
        self.assertTrue(any("home path" in item for item in warnings))
        self.assertEqual(main([str(self.record)]), 2)

    def test_deny_context_is_not_fatal(self) -> None:
        self.write(
            "claude-settings.json",
            '{"permissions": {"deny": ["Read(./secrets/**)", "Read(./.env)"]}}\n',
        )
        hard, warnings = scan_path(self.record)
        self.assertEqual(hard, [])
        self.assertTrue(any("deny rule context" in item for item in warnings))

    def test_missing_directory(self) -> None:
        self.assertEqual(main([str(self.record / "nope")]), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "discover_executors.py"


def fake_cli(
    path: Path,
    output: str = "ok",
    exit_code: int = 0,
    models_output: str | None = None,
) -> Path:
    model_branch = ""
    if models_output is not None:
        model_branch = (
            "if len(sys.argv) > 1 and sys.argv[1] == 'models':\n"
            f"    print({models_output!r})\n"
            "    raise SystemExit(0)\n"
        )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9-test')\n"
        "    raise SystemExit(0)\n"
        + model_branch
        + f"print({output!r})\n"
        + f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class DiscoverExecutorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.bin = Path(self.temporary.name) / "bin"
        self.bin.mkdir()
        self.old_path = os.environ.get("PATH")
        os.environ["PATH"] = str(self.bin) + os.pathsep + (self.old_path or "")
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + str(Path(sys.executable).parent)

    def tearDown(self) -> None:
        if self.old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.old_path
        self.temporary.cleanup()

    def run_script(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *extra],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def roster(self, process: subprocess.CompletedProcess) -> dict:
        return json.loads(process.stdout)

    def test_discovery_lists_installed_executors_with_version(self) -> None:
        fake_cli(self.bin / "claude")
        fake_cli(self.bin / "codex")
        process = self.run_script()
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        names = [agent["name"] for agent in roster["agents"]]
        self.assertIn("claude", names)
        self.assertIn("codex", names)
        claude = next(agent for agent in roster["agents"] if agent["name"] == "claude")
        self.assertEqual(claude["health"], "available")
        self.assertEqual(claude["version"], "9.9.9-test")
        self.assertEqual(claude["vendor"], "anthropic")
        self.assertIsNone(claude["probe_ms"])
        self.assertEqual(claude["tier"], "balanced")
        self.assertEqual(claude["models"]["fast"], ["haiku"])
        self.assertTrue(claude["transport"]["headless"])
        self.assertFalse(claude["transport"]["native_goal"])
        self.assertEqual(claude["models_discovered"], [])

    def test_probe_marks_healthy_executor_available_with_probe_ms(self) -> None:
        fake_cli(self.bin / "claude")
        process = self.run_script("--probe")
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        self.assertTrue(roster["probed"])
        by_name = {agent["name"]: agent for agent in roster["agents"]}
        self.assertEqual(by_name["claude"]["health"], "available")
        self.assertIsNotNone(by_name["claude"]["probe_ms"])

    def test_kimi_registered_with_moonshot_metadata(self) -> None:
        fake_cli(self.bin / "kimi")
        process = self.run_script()
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        names = [agent["name"] for agent in roster["agents"]]
        self.assertIn("kimi", names)
        kimi = next(agent for agent in roster["agents"] if agent["name"] == "kimi")
        self.assertEqual(kimi["vendor"], "moonshot")
        self.assertEqual(kimi["tier"], "balanced")
        self.assertIn("hard", kimi["tiers_available"])
        self.assertEqual(kimi["version"], "9.9.9-test")
        self.assertEqual(kimi["health"], "available")

    def test_grok_registered_with_headless_background_recipe(self) -> None:
        fake_cli(
            self.bin / "grok",
            models_output="grok-4.5\ngrok-4.6\ngrok-4.5",
        )
        process = self.run_script("--probe")
        self.assertEqual(process.returncode, 0, process.stderr)
        grok = next(
            agent for agent in self.roster(process)["agents"] if agent["name"] == "grok"
        )
        self.assertEqual(grok["vendor"], "xai")
        self.assertEqual(grok["health"], "available")
        self.assertIn("--prompt-file", grok["background_recipe"])
        self.assertIn("--no-memory", grok["background_recipe"])
        self.assertIn("--no-subagents", grok["background_recipe"])
        self.assertTrue(grok["transport"]["model_discovery"])
        self.assertTrue(grok["transport"]["single_shot"])
        self.assertEqual(grok["models_discovered"], ["grok-4.5", "grok-4.6"])

    def test_probe_accepts_list_prefixed_ok_answer(self) -> None:
        # Kimi renders plain answers with a bullet prefix ("• ok").
        fake_cli(self.bin / "kimi", output="• ok")
        process = self.run_script("--probe")
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        kimi = next(agent for agent in roster["agents"] if agent["name"] == "kimi")
        self.assertEqual(kimi["health"], "available")
        self.assertIsNotNone(kimi["probe_ms"])

    def test_probe_still_rejects_non_ok_answer(self) -> None:
        fake_cli(self.bin / "kimi", output="provider 503", exit_code=1)
        process = self.run_script("--probe")
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        kimi = next(agent for agent in roster["agents"] if agent["name"] == "kimi")
        self.assertEqual(kimi["health"], "degraded")
        self.assertTrue(any("probe failed" in note for note in kimi["notes"]))

    def test_probe_marks_unresponsive_executor_degraded(self) -> None:
        fake_cli(self.bin / "claude", output="provider 503", exit_code=1)
        process = self.run_script("--probe")
        self.assertEqual(process.returncode, 0, process.stderr)
        roster = self.roster(process)
        claude = next(agent for agent in roster["agents"] if agent["name"] == "claude")
        self.assertEqual(claude["health"], "degraded")
        self.assertTrue(any("probe failed" in note for note in claude["notes"]))

    def test_out_writes_roster_json(self) -> None:
        fake_cli(self.bin / "claude")
        out = ROOT / "wp-state" / "test-discovery-roster.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        process = self.run_script("--out", str(out))
        self.assertEqual(process.returncode, 0, process.stderr)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(saved["generated_by"], "wp/scripts/discover_executors.py")
        self.assertEqual(saved["agents"][0]["name"], "claude")
        out.unlink(missing_ok=True)

    def test_no_agents_warns_and_exits_nonzero(self) -> None:
        isolated_env = self.env.copy()
        isolated_env["PATH"] = str(self.bin)  # hermetic: no system agent CLIs
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=isolated_env,
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("no agent CLIs found", process.stderr)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from model_resolver import parse_model_catalog, resolve_effort, resolve_model  # noqa: E402


class ModelResolverTests(unittest.TestCase):
    def test_claude_uses_latest_model_aliases(self) -> None:
        for capability, expected in (
            ("fast", "haiku"),
            ("balanced", "sonnet"),
            ("frontier", "opus"),
        ):
            with self.subTest(capability=capability):
                resolved = resolve_model("claude", capability)
                self.assertEqual(resolved["model"], expected)
                self.assertEqual(resolved["source"], "alias")

    def test_executor_defaults_and_override_precedence(self) -> None:
        self.assertEqual(resolve_model("codex", "fast")["model"], "gpt-5.6-luna")
        self.assertEqual(resolve_model("codex", "hard")["model"], "gpt-5.6-sol")
        self.assertEqual(resolve_model("kimi", "frontier")["model"], "kimi-code/k3")
        self.assertEqual(resolve_model("agy", "fast")["model"], "gemini-3.7-flash-low")
        self.assertEqual(resolve_model("agy", "balanced")["model"], "gemini-3.7-flash-high")
        self.assertEqual(resolve_model("agy", "hard")["model"], "gemini-3.7-flash-high")
        resolved = resolve_model(
            "grok", "frontier", override="grok-custom", catalog=["grok-4.6"]
        )
        self.assertEqual(resolved["model"], "grok-custom")
        self.assertEqual(resolved["source"], "override")
        self.assertEqual(resolved["catalog"], ["grok-4.6"])

    def test_grok_catalog_prefers_current_then_older_compatible(self) -> None:
        current = resolve_model(
            "grok", "balanced", catalog=["grok-4.5", "grok-4.6", "grok-4.4"]
        )
        self.assertEqual(current["model"], "grok-4.6")
        self.assertEqual(current["source"], "discovered")

        older = resolve_model(
            "grok", "balanced", catalog=["grok-4.2", "grok-4.4", "other"]
        )
        self.assertEqual(older["model"], "grok-4.4")
        self.assertEqual(older["source"], "discovered")

    def test_catalog_miss_is_honest_inheritance(self) -> None:
        resolved = resolve_model("grok", "balanced", catalog=["other-model"])
        self.assertIsNone(resolved["model"])
        self.assertEqual(resolved["source"], "inherited")

    def test_catalog_parser_is_ordered_and_deduplicated(self) -> None:
        output = "available: grok-4.5\n* grok-4.6 (default)\ngrok-4.5\n"
        self.assertEqual(parse_model_catalog(output), ["grok-4.5", "grok-4.6"])

    def test_effort_defaults_match_real_runner_controls(self) -> None:
        self.assertEqual(resolve_effort("codex", "frontier"), "xhigh")
        self.assertEqual(resolve_effort("claude", "balanced"), "high")
        self.assertEqual(resolve_effort("grok", "frontier"), "inherited")
        self.assertEqual(resolve_effort("kimi", "balanced", "custom"), "custom")
        self.assertEqual(resolve_effort("agy", "fast"), "low")
        self.assertEqual(resolve_effort("agy", "balanced"), "high")
        self.assertEqual(resolve_effort("agy", "hard"), "high")


if __name__ == "__main__":
    unittest.main()

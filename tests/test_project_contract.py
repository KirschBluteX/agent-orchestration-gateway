from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-orchestration-gateway"
SKILL = PLUGIN / "skills" / "orchestrate"


class ProjectContractTests(unittest.TestCase):
    def test_plugin_is_one_thin_explicit_skill(self) -> None:
        expected = {
            ".codex-plugin/plugin.json",
            "skills/orchestrate/SKILL.md",
            "skills/orchestrate/agents/openai.yaml",
            "skills/orchestrate/references/module.md",
            "skills/orchestrate/references/supervisor.md",
            "skills/orchestrate/scripts/validate_plan.py",
        }
        actual = {
            path.relative_to(PLUGIN).as_posix()
            for path in PLUGIN.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(actual, expected)

        metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", metadata)

    def test_manifest_and_marketplace_match_release(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(manifest),
            {
                "name",
                "version",
                "description",
                "author",
                "homepage",
                "repository",
                "license",
                "keywords",
                "skills",
                "interface",
            },
        )
        self.assertEqual(manifest["name"], "agent-orchestration-gateway")
        self.assertEqual(manifest["version"], "0.11.0")
        self.assertIsNotNone(re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]))
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("hooks", manifest)
        self.assertNotIn("control plane", manifest["description"].lower())
        interface = manifest["interface"]
        self.assertEqual(
            set(interface),
            {
                "displayName",
                "shortDescription",
                "longDescription",
                "developerName",
                "category",
                "capabilities",
                "websiteURL",
                "defaultPrompt",
            },
        )
        self.assertLessEqual(len(interface["defaultPrompt"]), 3)
        self.assertTrue(
            all(len(prompt) <= 128 for prompt in interface["defaultPrompt"])
        )

        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(
            entry["source"]["path"], "./plugins/agent-orchestration-gateway"
        )
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )

    def test_repository_has_one_bilingual_readme_and_no_legacy_runtime(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Agent Orchestration Gateway", readme)
        self.assertIn("English", readme)
        self.assertIn("简体中文", readme)
        self.assertFalse((ROOT / "README.zh-CN.md").exists())
        self.assertFalse((ROOT / "AGENTS.md").exists())
        for path in ("hooks", "agents", "scripts", "skills/manage-aog"):
            self.assertFalse((PLUGIN / path).exists(), path)


if __name__ == "__main__":
    unittest.main()

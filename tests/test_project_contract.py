from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tomllib
import unittest


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "codex-cost-orchestrator"
SKILL = PLUGIN / "skills" / "orchestrate" / "SKILL.md"
REFERENCES = SKILL.parent / "references"
HOOKS = PLUGIN / "hooks" / "hooks.json"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def shipped_text() -> str:
    suffixes = {".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}
    return "\n".join(
        text(path)
        for path in PLUGIN.rglob("*")
        if path.is_file() and path.suffix.lower() in suffixes
    )


class ProjectContractTests(unittest.TestCase):
    def test_public_identity_version_and_bilingual_entrypoints(self) -> None:
        english = text(REPO / "README.md")
        chinese = text(REPO / "README.zh-CN.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "codex-cost-orchestrator")
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(manifest["author"]["name"], "KirschQAQ")
        self.assertEqual(
            manifest["repository"],
            "https://github.com/KirschBluteX/codex-cost-orchestrator",
        )
        for unrelated in ("OpenSquilla", "sol-advisor", "DannyMac180"):
            self.assertNotIn(unrelated, english + chinese + text(SKILL))

    def test_installation_uses_the_supported_plugin_add_command(self) -> None:
        for readme in (REPO / "README.md", REPO / "README.zh-CN.md"):
            contents = text(readme)
            with self.subTest(readme=readme.name):
                self.assertIn(
                    "codex plugin add codex-cost-orchestrator@codex-cost-orchestrator",
                    contents,
                )
                self.assertNotIn("codex plugin install ", contents)

    def test_cco_is_implicit_and_uses_only_two_physical_leaf_profiles(self) -> None:
        openai_yaml = text(SKILL.parent / "agents" / "openai.yaml")
        profiles = {
            path.name: tomllib.loads(text(path))
            for path in (PLUGIN / "agents").glob("*.toml")
        }

        self.assertIn("allow_implicit_invocation: true", openai_yaml)
        self.assertEqual(
            set(profiles),
            {
                "codex-cost-orchestrator-read-leaf.toml",
                "codex-cost-orchestrator-write-leaf.toml",
            },
        )
        for profile in profiles.values():
            self.assertNotIn("model", profile)
            self.assertNotIn("model_reasoning_effort", profile)
            self.assertFalse(profile["features"]["multi_agent"])
            self.assertEqual(profile["features"]["multi_agent_v2"], {"enabled": False})
        self.assertEqual(
            profiles["codex-cost-orchestrator-read-leaf.toml"]["sandbox_mode"],
            "read-only",
        )

    def test_hooks_cover_the_v6_native_lifecycle_without_legacy_roles(self) -> None:
        hooks = json.loads(text(HOOKS))["hooks"]
        serialized = json.dumps(hooks)

        self.assertIn("spawn_agent", serialized)
        self.assertIn("send_message", serialized)
        self.assertIn("followup_task", serialized)
        self.assertIn("interrupt_agent", serialized)
        self.assertIn("cost_orchestrator_read_leaf", serialized)
        self.assertIn("cost_orchestrator_write_leaf", serialized)
        for legacy in (
            "cost_orchestrator_analysis_worker",
            "cost_orchestrator_routine_worker",
            "cost_orchestrator_complex_worker",
            "cost_orchestrator_reviewer",
        ):
            self.assertNotIn(legacy, serialized)
        for event, groups in hooks.items():
            for group in groups:
                for hook in group.get("hooks", []):
                    expected_timeout = 120 if event == "SubagentStop" else 5
                    self.assertLessEqual(hook["timeout"], expected_timeout)

    def test_v6_is_the_only_shipped_protocol_and_has_no_fixed_retry_ritual(self) -> None:
        combined = shipped_text()
        lowered = combined.lower()

        self.assertIn("cco_dispatch cco.v6", lowered)
        self.assertIn("cco_result cco.v6", lowered)
        for forbidden in (
            "cco.v5",
            "contracts-v5",
            "lease_generation",
            "stop_generation",
            "at most three worker runs",
            "at most two live follow-ups",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_no_accounting_encryption_or_second_runtime_is_shipped(self) -> None:
        lowered = shipped_text().lower()
        for forbidden in (
            "sqlite",
            "providercallplan",
            "token ledger",
            "billing dashboard",
            "fernet",
            "encrypt(",
            "pi sdk session",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("only runtime", lowered)
        self.assertIn("not encryption", lowered)

    def test_marketplace_points_at_the_single_local_plugin(self) -> None:
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        marketplace = json.loads(text(REPO / ".agents" / "plugins" / "marketplace.json"))
        entries = marketplace["plugins"]

        self.assertEqual(marketplace["name"], manifest["name"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], manifest["name"])
        self.assertEqual(
            entries[0]["source"],
            {"source": "local", "path": "./plugins/codex-cost-orchestrator"},
        )

    def test_skill_reference_links_resolve(self) -> None:
        contents = text(SKILL)
        self.assertNotIn("worker-core.md", contents)
        for filename in ("runtime-gates.md", "contracts-v6.md"):
            self.assertTrue((REFERENCES / filename).is_file())
            self.assertIn(f"references/{filename}", contents)

    def test_repository_reachable_history_has_one_root(self) -> None:
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=REPO, text=True
        ).splitlines()
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()

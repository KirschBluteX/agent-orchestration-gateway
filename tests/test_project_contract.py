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


def current_runtime_text() -> str:
    paths = [
        *PLUGIN.glob("agents/*.toml"),
        *PLUGIN.glob("hooks/*.py"),
        *PLUGIN.glob("scripts/*.py"),
        SKILL,
        REFERENCES / "runtime-gates.md",
        REFERENCES / "contracts-v7.md",
    ]
    return "\n".join(text(path) for path in paths)


class ProjectContractTests(unittest.TestCase):
    def test_public_identity_version_and_bilingual_entrypoints(self) -> None:
        english = text(REPO / "README.md")
        chinese = text(REPO / "README.zh-CN.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "codex-cost-orchestrator")
        self.assertEqual(manifest["version"], "1.1.0")
        self.assertEqual(manifest["author"]["name"], "KirschQAQ")
        self.assertEqual(
            manifest["repository"],
            "https://github.com/KirschBluteX/codex-cost-orchestrator",
        )
        for unrelated in ("OpenSquilla", "sol-advisor", "DannyMac180"):
            self.assertNotIn(unrelated, english + chinese + text(SKILL))

    def test_installation_uses_current_codex_plugin_commands_and_explicit_trust(self) -> None:
        combined = text(REPO / "README.md") + text(REPO / "README.zh-CN.md")
        self.assertIn(
            "codex plugin add codex-cost-orchestrator@codex-cost-orchestrator",
            combined,
        )
        self.assertIn("codex plugin marketplace add", combined)
        self.assertIn("/hooks", combined)
        self.assertIn("--bootstrap", combined)
        self.assertIn("--doctor", combined)
        self.assertIn("--uninstall", combined)
        self.assertNotIn("codex plugin install ", combined)

    def test_implicit_skill_and_two_model_neutral_non_delegating_profiles(self) -> None:
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
            self.assertIn("CCO_DISPATCH cco.v7", profile["developer_instructions"])
        self.assertEqual(
            profiles["codex-cost-orchestrator-read-leaf.toml"]["sandbox_mode"],
            "read-only",
        )

    def test_hooks_use_supported_v7_transaction_and_wait_events(self) -> None:
        hooks = json.loads(text(HOOKS))["hooks"]
        self.assertEqual(
            set(hooks),
            {
                "SessionStart",
                "SessionEnd",
                "PreToolUse",
                "PostToolUse",
                "Stop",
                "UserPromptSubmit",
                "SubagentStop",
            },
        )
        serialized = json.dumps(hooks)
        for required in (
            "spawn_agent",
            "followup_task",
            "interrupt_agent",
            "cost_orchestrator_read_leaf",
            "cost_orchestrator_write_leaf",
        ):
            self.assertIn(required, serialized)
        count = 0
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    count += 1
                    self.assertLessEqual(hook["timeout"], 120 if event == "SubagentStop" else 5)
        self.assertEqual(count, 8)

    def test_current_protocol_is_v7_and_runtime_route_is_static_and_network_free(self) -> None:
        packet = text(PLUGIN / "scripts" / "packet_compiler.py")
        routing = text(PLUGIN / "scripts" / "routing_catalog.py")
        graph = text(PLUGIN / "scripts" / "graph_compiler.py")
        self.assertIn('PROTOCOL = "cco.v7"', packet)
        self.assertIn('ROUTE_PLAN_PROTOCOL = "cco.route-plan.v5"', routing)
        self.assertIn('GRAPH_PROTOCOL = "cco.graph.v4"', graph)
        self.assertIn('DISPATCH_BATCH_PROTOCOL = "cco.dispatch-batch.v2"', graph)
        for forbidden in (
            "urllib.request",
            "needs_refresh",
            "average_price_usd",
            "minimum_iq_exclusive",
            "refresh_radar",
        ):
            self.assertNotIn(forbidden, routing.casefold())
        self.assertIn("network-free", routing.casefold())

    def test_fast_dispatch_is_one_transaction_and_one_quiescent_wait(self) -> None:
        compiler = text(PLUGIN / "scripts" / "graph_compiler.py")
        transaction = text(PLUGIN / "scripts" / "dispatch_transaction.py")
        skill = text(SKILL)

        self.assertIn("prepare_transaction_batch", compiler)
        self.assertIn("CCO_DISPATCH_REF cco.dispatch-batch.v2", transaction)
        self.assertIn("same model turn", skill)
        self.assertIn("one long event wait", skill)
        self.assertIn("do not read the repository again", skill.casefold())

    def test_no_accounting_encryption_or_second_runtime_is_shipped(self) -> None:
        lowered = current_runtime_text().casefold()
        for forbidden in (
            "sqlite",
            "providercallplan",
            "token ledger",
            "billing dashboard",
            "fernet",
            "pi sdk session",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("second agent runtime", lowered)
        self.assertIn("not encryption", lowered)

    def test_marketplace_and_release_materials_are_complete(self) -> None:
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        marketplace = json.loads(text(REPO / ".agents" / "plugins" / "marketplace.json"))
        self.assertEqual(marketplace["name"], manifest["name"])
        self.assertEqual(len(marketplace["plugins"]), 1)
        self.assertEqual(
            marketplace["plugins"][0]["source"],
            {"source": "local", "path": "./plugins/codex-cost-orchestrator"},
        )
        for relative in (
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "ROADMAP.md",
            "SECURITY.md",
            "docs/BENCHMARK.md",
            "docs/OPERATIONS.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
        ):
            self.assertTrue((REPO / relative).is_file(), relative)

    def test_skill_reference_links_resolve(self) -> None:
        contents = text(SKILL)
        for filename in ("runtime-gates.md", "contracts-v7.md"):
            self.assertTrue((REFERENCES / filename).is_file())
            self.assertIn(f"references/{filename}", contents)
        self.assertFalse((REFERENCES / "contracts-v6.md").exists())

    def test_repository_reachable_history_has_one_root_without_author_restrictions(self) -> None:
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=REPO, text=True
        ).splitlines()
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()

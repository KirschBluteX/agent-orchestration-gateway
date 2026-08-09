from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-cost-orchestrator"
ORCHESTRATE = PLUGIN / "skills" / "orchestrate"
MANAGE = PLUGIN / "skills" / "manage-cco"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ProjectContractTests(unittest.TestCase):
    def test_public_identity_and_bilingual_entrypoints(self) -> None:
        english = text(ROOT / "README.md")
        chinese = text(ROOT / "README.zh-CN.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "codex-cost-orchestrator")
        self.assertRegex(manifest["version"], r"^4\.0\.0\+codex\.[a-z0-9.-]+$")
        self.assertEqual(manifest["author"]["name"], "KirschQAQ")
        self.assertEqual(
            manifest["repository"],
            "https://github.com/KirschBluteX/codex-cost-orchestrator",
        )
        for unrelated in ("OpenSquilla", "sol-advisor", "DannyMac180"):
            self.assertNotIn(unrelated, english + chinese + text(ORCHESTRATE / "SKILL.md"))

    def test_release_identity_is_consistent(self) -> None:
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        installer = text(PLUGIN / "scripts" / "install_agents.py")
        changelog = text(ROOT / "CHANGELOG.md")
        self.assertIn('PLUGIN_VERSION = "4.0.0"', installer)
        self.assertRegex(manifest["version"], r"^4\.0\.0\+")
        self.assertRegex(changelog, r"(?m)^## 4\.0\.0 - ")

    def test_two_thin_skills_use_progressive_disclosure(self) -> None:
        hot = text(ORCHESTRATE / "SKILL.md")
        cold = text(MANAGE / "SKILL.md")
        hot_words = len(re.findall(r"\S+", hot))
        self.assertLessEqual(hot_words, 450)
        self.assertIn("control_plane.py prepare", hot)
        self.assertNotIn("control_plane.py plan", hot)
        self.assertIn("one long `wait_agent`", hot)
        self.assertIn("$codex-cost-orchestrator:manage-cco", hot)
        self.assertNotIn("repair_host_edges", hot)
        self.assertNotIn("migration", cold.casefold())
        self.assertIn("allow_implicit_invocation: true", text(ORCHESTRATE / "agents" / "openai.yaml"))
        self.assertIn("allow_implicit_invocation: false", text(MANAGE / "agents" / "openai.yaml"))

    def test_profiles_are_model_neutral_non_delegating_v9_leaves(self) -> None:
        profiles = {
            path.name: tomllib.loads(text(path))
            for path in (PLUGIN / "agents").glob("*.toml")
        }
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
            instructions = profile["developer_instructions"]
            self.assertIn("CCO_TASK cco.v9", instructions)
            self.assertIn("CCO_RESULT cco.v9", instructions)
            self.assertNotIn("RESULT_SHA256", instructions)

    def test_runtime_is_clean_v9_without_compatibility_adapters(self) -> None:
        runtime_paths = [
            *(PLUGIN / "agents").glob("*.toml"),
            *(PLUGIN / "hooks").glob("*.py"),
            *(PLUGIN / "scripts").glob("*.py"),
            ORCHESTRATE / "SKILL.md",
            MANAGE / "SKILL.md",
        ]
        runtime = "\n".join(text(path) for path in runtime_paths)
        self.assertIn('PROTOCOL = "cco.v9"', runtime)
        for old in (
            "cco.v8",
            "graph_compiler",
            "dispatch_transaction",
            "packet_compiler",
            "prepared_graph",
            "TaskLedger",
            "CCO_DISPATCH_REF",
            "CCO_NATIVE_BYPASS",
        ):
            self.assertNotIn(old, runtime)
        for forbidden in ("sqlite", "radar", "token ledger", "billing dashboard", "mcp server"):
            self.assertNotIn(forbidden, runtime.casefold())

    def test_hooks_are_exact_and_have_no_all_tool_tax(self) -> None:
        hooks = json.loads(text(PLUGIN / "hooks" / "hooks.json"))["hooks"]
        self.assertEqual(
            set(hooks),
            {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop"},
        )
        serialized = json.dumps(hooks)
        self.assertNotIn('"matcher": ".*"', serialized)
        self.assertIn("spawn_agent", serialized)
        self.assertIn("followup_task", serialized)
        self.assertIn("interrupt_agent", serialized)
        count = sum(len(group["hooks"]) for groups in hooks.values() for group in groups)
        self.assertEqual(count, 5)

    def test_installation_docs_use_current_plugin_and_explicit_trust(self) -> None:
        combined = text(ROOT / "README.md") + text(ROOT / "README.zh-CN.md")
        self.assertIn("codex plugin marketplace add", combined)
        self.assertIn("codex plugin add codex-cost-orchestrator@codex-cost-orchestrator", combined)
        self.assertIn("--bootstrap", combined)
        self.assertIn("--doctor", combined)
        self.assertIn("/hooks", combined)
        self.assertNotIn("codex plugin install ", combined)

    def test_marketplace_and_release_files_exist(self) -> None:
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        marketplace = json.loads(text(ROOT / ".agents" / "plugins" / "marketplace.json"))
        self.assertEqual(marketplace["name"], manifest["name"])
        for relative in (
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "ROADMAP.md",
            "SECURITY.md",
            "docs/BENCHMARK.md",
            "docs/OPERATIONS.md",
            "benchmarks/cco_benchmark.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_development_and_ci_commands_cover_the_published_tooling(self) -> None:
        english = text(ROOT / "README.md")
        workflow = text(ROOT / ".github" / "workflows" / "ci.yml")
        requirements = text(ROOT / "requirements.txt")
        self.assertIn(
            "python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator",
            english,
        )
        self.assertIn("ruff check plugins tests benchmarks .github/scripts", workflow)
        self.assertIn("zstandard", requirements.casefold())
        self.assertIn("-r requirements.txt", workflow)

    def test_repository_history_has_one_root_without_author_restrictions(self) -> None:
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT, text=True
        ).splitlines()
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()

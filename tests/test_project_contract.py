from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-orchestration-gateway"
ORCHESTRATE = PLUGIN / "skills" / "orchestrate"
MANAGE = PLUGIN / "skills" / "manage-aog"
RELEASE = "0.10.0"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ProjectContractTests(unittest.TestCase):
    def test_previous_identity_is_absent_from_published_text_and_paths(self) -> None:
        previous_short = "".join(("c", "co"))
        previous_slug = "-".join(("codex", "cost", "orchestrator"))
        previous_display = " ".join(("Codex", "Cost", "Orchestrator"))
        previous_role = "_".join(("cost", "orchestrator"))
        short_identity = re.compile(
            rf"(^|[^A-Za-z0-9]){re.escape(previous_short)}([^A-Za-z0-9]|$)",
            re.IGNORECASE,
        )
        roots = (
            ROOT / ".agents",
            ROOT / ".github",
            ROOT / "benchmarks",
            ROOT / "docs",
            ROOT / "plugins",
            ROOT / "tests",
        )
        paths = [
            ROOT / "AGENTS.md",
            ROOT / "CHANGELOG.md",
            ROOT / "CONTRIBUTING.md",
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "SECURITY.md",
            *(path for root in roots for path in root.rglob("*") if path.is_file()),
        ]
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            if (ROOT / "benchmarks" / "runs") in path.parents:
                continue
            if path.suffix not in {".json", ".md", ".py", ".toml", ".yaml", ".yml"}:
                continue
            value = text(path)
            with self.subTest(path=relative):
                self.assertNotIn(previous_slug, relative)
                self.assertNotIn(previous_slug, value)
                self.assertNotIn(previous_display, value)
                self.assertNotIn(previous_role, value)
                self.assertIsNone(short_identity.search(value))

    def test_public_identity_and_bilingual_entrypoints_are_consistent(self) -> None:
        english = text(ROOT / "README.md")
        chinese = text(ROOT / "README.zh-CN.md")
        changelog = text(ROOT / "CHANGELOG.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        installer = text(PLUGIN / "scripts" / "install_agents.py")

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "agent-orchestration-gateway")
        self.assertEqual(manifest["version"], RELEASE)
        self.assertIn(f'PLUGIN_VERSION = "{RELEASE}"', installer)
        self.assertIn("PLUGIN_RELEASE = PLUGIN_VERSION", installer)
        self.assertNotIn("PLUGIN_BUILD_METADATA", installer)
        self.assertRegex(changelog, rf"(?m)^## {re.escape(RELEASE)} - \d{{4}}-\d{{2}}-\d{{2}}$")
        self.assertEqual(
            text(ROOT / "requirements.txt").strip(),
            'zstandard>=0.23,<1; python_version < "3.14"',
        )

    def test_hot_path_has_one_prepare_command_and_bounded_policy(self) -> None:
        agents = text(ROOT / "AGENTS.md")
        hot = text(ORCHESTRATE / "SKILL.md")

        self.assertLessEqual(len(re.findall(r"\S+", hot)), 600)
        self.assertEqual(hot.count("control_plane.py prepare"), 1)
        for required in (
            "timed_out",
            "do not spawn an unprepared",
            "same live dispatch",
            "one owner per code revision",
            "user explicitly requests direct execution",
        ):
            self.assertIn(required, hot)
        self.assertIn("agent-orchestration-gateway:orchestrate", agents)
        self.assertIn("explicit delegation instructions", agents)
        self.assertNotIn("control_plane.py plan", hot)
        self.assertIn(
            "allow_implicit_invocation: true",
            text(ORCHESTRATE / "agents" / "openai.yaml"),
        )
        self.assertIn(
            "allow_implicit_invocation: false",
            text(MANAGE / "agents" / "openai.yaml"),
        )

    def test_public_docs_cover_current_runtime_boundaries(self) -> None:
        public = "\n".join(
            text(path)
            for path in (
                ROOT / "README.md",
                ROOT / "README.zh-CN.md",
                ROOT / "SECURITY.md",
                ROOT / "docs" / "BENCHMARK.md",
                MANAGE / "references" / "operations.md",
            )
        )
        for value in (
            "aog.delegation.v1",
            "aog.wave.v1",
            "aog.lifecycle.v1",
            "aog.receipt.v1",
            "exact",
            "prefix",
            "zstandard",
            "四个 writer 的安全上限",
        ):
            self.assertIn(value, public)
        self.assertRegex(public, r"four-writer\s+safety ceiling")
        for stale in (
            "active V2 backend exposes it",
            "future host exposes Luna through V2",
            "docs/OPERATIONS.md",
            "repair_host_edges.py",
        ):
            self.assertNotIn(stale, public)

    def test_runtime_has_one_route_policy_and_no_dead_maintenance_path(self) -> None:
        runtime_paths = [
            *(PLUGIN / "agents").glob("*.toml"),
            *(PLUGIN / "hooks").glob("*.py"),
            *(PLUGIN / "scripts").glob("*.py"),
            ORCHESTRATE / "SKILL.md",
            MANAGE / "SKILL.md",
        ]
        runtime = "\n".join(text(path) for path in runtime_paths)
        for forbidden in (
            "PLANNER_DEFAULT_ROUTE",
            "resolve_planner_route",
            "load_route_policy",
            "trusted_project_roots",
            "aog.toml",
        ):
            self.assertNotIn(forbidden, runtime)
        self.assertFalse((PLUGIN / "maintenance" / "repair_host_edges.py").exists())
        self.assertIn('PROTOCOL = "aog.v1"', runtime)
        self.assertIn('LIFECYCLE_PROTOCOL = "aog.lifecycle.v1"', runtime)
        self.assertIn('PENDING_EVENT_PROTOCOL = "aog.receipt.v1"', runtime)

    def test_profiles_are_model_neutral_non_delegating_leaves(self) -> None:
        profiles = {
            path.name: tomllib.loads(text(path))
            for path in (PLUGIN / "agents").glob("*.toml")
        }
        self.assertEqual(
            set(profiles),
            {
                "aog-read-leaf.toml",
                "aog-write-leaf.toml",
            },
        )
        for profile in profiles.values():
            self.assertNotIn("model", profile)
            self.assertNotIn("model_reasoning_effort", profile)
            self.assertFalse(profile["features"]["multi_agent"])
            self.assertEqual(profile["features"]["multi_agent_v2"], {"enabled": False})
            self.assertIn("AOG_TASK aog.v1", profile["developer_instructions"])
            self.assertIn("AOG_RESULT aog.v1", profile["developer_instructions"])

    def test_hooks_ci_and_published_files_cover_release_validation(self) -> None:
        hooks = json.loads(text(PLUGIN / "hooks" / "hooks.json"))["hooks"]
        installer = text(PLUGIN / "scripts" / "install_agents.py")
        workflow = text(ROOT / ".github" / "workflows" / "ci.yml")

        self.assertEqual(
            set(hooks),
            {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop"},
        )
        self.assertEqual(
            hooks["SubagentStop"][0]["matcher"],
            "^(aog_read_leaf|aog_write_leaf)$",
        )
        self.assertIn("HOST_HOOK_CONTRACT", installer)
        self.assertIn("_validate_local_hook_contract", installer)
        self.assertNotIn('"matcher": ".*"', json.dumps(hooks))
        self.assertIn("ruff check plugins tests benchmarks .github/scripts", workflow)
        self.assertIn("validate_plugin.py plugins/agent-orchestration-gateway", workflow)
        self.assertIn("quick_validate.py", workflow)
        for relative in (
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/BENCHMARK.md",
            "benchmarks/aog_benchmark.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()

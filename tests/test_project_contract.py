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
RELEASE = "0.9.1"
RELEASE_DOCUMENTS = (
    "CHANGELOG.md",
    "README.md",
    "README.zh-CN.md",
    "ROADMAP.md",
    "docs/OPERATIONS.md",
    "plugins/codex-cost-orchestrator/skills/manage-cco/SKILL.md",
    "plugins/codex-cost-orchestrator/skills/manage-cco/references/operations.md",
)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ProjectContractTests(unittest.TestCase):
    def test_public_identity_bilingual_entrypoints_and_release_are_consistent(self) -> None:
        english = text(ROOT / "README.md")
        chinese = text(ROOT / "README.zh-CN.md")
        changelog = text(ROOT / "CHANGELOG.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        installer = text(PLUGIN / "scripts" / "install_agents.py")
        release_documents = [text(ROOT / path) for path in RELEASE_DOCUMENTS]
        release_surface = "\n".join([*release_documents, json.dumps(manifest), installer])

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "codex-cost-orchestrator")
        self.assertEqual(manifest["version"], RELEASE)
        self.assertIn('PLUGIN_VERSION = "0.9.1"', installer)
        self.assertIn("PLUGIN_RELEASE = PLUGIN_VERSION", installer)
        self.assertNotIn("PLUGIN_BUILD_METADATA", installer)
        for document in release_documents:
            self.assertIn(RELEASE, document)
        self.assertNotRegex(release_surface, r"\b0\.9\.0\+[A-Za-z0-9.-]+\b")
        self.assertNotRegex(release_surface, r"\bcodex\.\d{14}\b")
        self.assertNotRegex(release_surface, r"\b[1-9]\d*\.\d+\.\d+(?:\+[A-Za-z0-9.-]+)?\b")
        self.assertRegex(changelog, r"(?m)^## 0\.9\.1 - 2026-08-10$")
        self.assertIn("pre-1.0", changelog)
        self.assertIn("2.x through 5.x", changelog)
        self.assertIn("pre-0.9 development history", changelog)
        self.assertIn("Git history remains unchanged", changelog)
        self.assertEqual(
            text(ROOT / "requirements.txt").strip(),
            'zstandard>=0.23,<1; python_version < "3.14"',
        )

    def test_hot_path_uses_one_prepare_command_and_a_quiet_wait(self) -> None:
        agents = text(ROOT / "AGENTS.md")
        hot = text(ORCHESTRATE / "SKILL.md")
        self.assertLessEqual(len(re.findall(r"\S+", hot)), 360)
        self.assertIn("control_plane.py prepare --repo <WORKSPACE> --capacity <N>", hot)
        self.assertEqual(hot.count("control_plane.py prepare"), 1)
        self.assertIn("timed_out", hot)
        self.assertIn("without progress narration", hot)
        self.assertIn("below 30 seconds", agents)
        self.assertNotIn("control_plane.py plan", hot)
        self.assertNotIn("migrate-recoveries", hot)
        self.assertIn("allow_implicit_invocation: true", text(ORCHESTRATE / "agents" / "openai.yaml"))
        self.assertIn("allow_implicit_invocation: false", text(MANAGE / "agents" / "openai.yaml"))

    def test_release_docs_describe_current_boundaries(self) -> None:
        public = "\n".join(
            text(path)
            for path in (
                ROOT / "README.md",
                ROOT / "README.zh-CN.md",
                ROOT / "CHANGELOG.md",
                ROOT / "CONTRIBUTING.md",
                ROOT / "ROADMAP.md",
                ROOT / "SECURITY.md",
                ROOT / "docs" / "BENCHMARK.md",
                ROOT / "docs" / "OPERATIONS.md",
            )
        )
        for value in (
            "cco.delegation.v1",
            "cco.wave.v3",
            "cco.lifecycle.v2",
            "cco.receipt.v2",
            "exact",
            "prefix",
            "zstandard",
            "offline",
        ):
            self.assertIn(value, public)
        self.assertNotIn("migrate-recoveries", public)
        self.assertNotIn("Terra, then Luna", public)
        self.assertNotIn("billing", public.casefold())
        operations = text(ROOT / "docs" / "OPERATIONS.md")
        self.assertIn("duplicate", operations)
        self.assertIn("unknown", operations)
        self.assertIn("clock anomalies", operations)

    def test_runtime_has_no_dead_planner_or_predecessor_lifecycle_path(self) -> None:
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
            "migrate-recoveries",
            "cco.lifecycle.v1",
            "cco.pending-event.v1",
            "cco.wave.v1",
            "aggregation",
        ):
            self.assertNotIn(forbidden, runtime)
        self.assertIn('PROTOCOL = "cco.v9"', runtime)
        self.assertIn('LIFECYCLE_PROTOCOL = "cco.lifecycle.v2"', runtime)
        self.assertIn('PENDING_EVENT_PROTOCOL = "cco.receipt.v2"', runtime)

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
            self.assertIn("CCO_TASK cco.v9", profile["developer_instructions"])
            self.assertIn("CCO_RESULT cco.v9", profile["developer_instructions"])

    def test_hooks_ci_and_published_files_cover_release_validation(self) -> None:
        hooks = json.loads(text(PLUGIN / "hooks" / "hooks.json"))["hooks"]
        workflow = text(ROOT / ".github" / "workflows" / "ci.yml")
        self.assertEqual(
            set(hooks),
            {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop"},
        )
        self.assertNotIn('"matcher": ".*"', json.dumps(hooks))
        self.assertIn("ruff check plugins tests benchmarks .github/scripts", workflow)
        self.assertIn("validate_plugin.py plugins/codex-cost-orchestrator", workflow)
        self.assertIn("quick_validate.py", workflow)
        self.assertIn("-r requirements.txt", workflow)
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

    def test_repository_history_has_one_root_without_author_restrictions(self) -> None:
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT, text=True
        ).splitlines()
        self.assertEqual(len(roots), 1)


if __name__ == "__main__":
    unittest.main()

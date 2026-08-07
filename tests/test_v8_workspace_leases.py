from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from graph_compiler import prepare_dispatch_graph  # noqa: E402
from prepared_graph import (  # noqa: E402
    graph_scopes,
    verify_artifact_workspace,
    verify_pre_spawn_workspace,
)


def native_catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-luna",
                "supported_reasoning_levels": [{"effort": "max"}],
            },
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-terra",
                "supported_reasoning_levels": [{"effort": "max"}],
            },
        ]
    }


def no_risks() -> dict[str, str]:
    return {
        name: "no"
        for name in (
            "authentication_authorization",
            "build_release",
            "concurrency",
            "dependency_boundary",
            "destructive_data",
            "external_side_effect",
            "migration",
            "nondeterministic_verification",
            "public_interface",
            "schema",
            "security",
        )
    }


def node(name: str, path: str) -> dict[str, object]:
    return {
        "acceptance_facts": {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "events": [],
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": no_risks(),
        },
        "closure": {
            "acceptance_closed": True,
            "criteria_closed": True,
            "decision_space": "acceptance_equivalent",
            "interfaces_closed": True,
            "objective_closed": True,
            "ownership_closed": True,
        },
        "contract": {
            "contract_rev": 1,
            "node": name,
            "objective": f"change {path}",
        },
        "generation": 1,
        "node": name,
        "placement": {
            "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
            "direct_action_count": 2,
            "direct_verification_count": 1,
        },
        "role": "worker",
        "scopes": [{"kind": "exact", "path": path}],
        "selection": {
            "depends_on": [],
            "responsibility": name,
        },
    }


class V8WorkspaceLeaseTests(unittest.TestCase):
    def test_scoped_graph_detects_git_hidden_and_ignored_changes_outside_its_scope(self) -> None:
        for marker in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                repo = root / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                (repo / "active.txt").write_text("active baseline\n", encoding="utf-8")
                (repo / "external.txt").write_text("external baseline\n", encoding="utf-8")
                (repo / ".gitignore").write_text("secret.tmp\n", encoding="utf-8")
                (repo / "secret.tmp").write_text("secret baseline\n", encoding="utf-8")
                subprocess.run(["git", "add", "active.txt", "external.txt", ".gitignore"], cwd=repo, check=True)
                subprocess.run(["git", "update-index", marker, "external.txt"], cwd=repo, check=True)
                with mock.patch.dict(
                    os.environ,
                    {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": f"v8-hidden-{marker[2:]}"},
                ):
                    prepared = prepare_dispatch_graph(
                        [node("n01_active", "active.txt")],
                        native_capacity=1,
                        native_catalog=native_catalog(),
                        repo=repo,
                    )

                (repo / "external.txt").write_text("hidden overwrite\n", encoding="utf-8")
                (repo / "secret.tmp").write_text("ignored overwrite\n", encoding="utf-8")
                manifest = prepared["manifest"]
                self.assertIsInstance(manifest, dict)
                verdict = verify_artifact_workspace(
                    Path(str(prepared["baseline_path"])),
                    repo=repo,
                    baseline=str(prepared["baseline"]),
                    graph_sha256_value=str(prepared["graph_sha256"]),
                    graph_scopes_value=graph_scopes(manifest),
                    workspace_mode=str(manifest["workspace_mode"]),
                )
                self.assertEqual(verdict["verdict"], "violation")
                self.assertEqual(verdict["changed_paths"], ["external.txt", "secret.tmp"])
                self.assertIn("outside_lease:external.txt", verdict["violations"])
                self.assertIn("outside_lease:secret.tmp", verdict["violations"])

    def prepare(self, root: Path, *, thread_id: str) -> tuple[Path, dict[str, object]]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for name in ("active.txt", "pending.txt", "external.txt"):
            (repo / name).write_text(f"{name} baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        with mock.patch.dict(
            os.environ,
            {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": thread_id},
        ):
            prepared = prepare_dispatch_graph(
                [node("n01_active", "active.txt"), node("n02_pending", "pending.txt")],
                native_capacity=2,
                native_catalog=native_catalog(),
                repo=repo,
            )
        return repo, prepared

    def verify(
        self,
        repo: Path,
        prepared: dict[str, object],
        *,
        active: list[dict[str, str]],
        pending: list[dict[str, str]],
    ) -> dict[str, object]:
        manifest = prepared["manifest"]
        self.assertIsInstance(manifest, dict)
        return verify_pre_spawn_workspace(
            Path(str(prepared["baseline_path"])),
            repo=repo,
            baseline=str(prepared["baseline"]),
            graph_sha256_value=str(prepared["graph_sha256"]),
            graph_scopes_value=graph_scopes(manifest),
            workspace_mode=str(manifest["workspace_mode"]),
            active_sibling_scopes=active,
            pending_candidate_scopes=pending,
        )

    def test_first_spawn_requires_the_exact_prepared_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, prepared = self.prepare(Path(temp_dir), thread_id="v7-lease-first")
            pending = [{"kind": "exact", "path": "pending.txt"}]

            initial = self.verify(repo, prepared, active=[], pending=pending)
            self.assertEqual(initial["verdict"], "pass")
            self.assertEqual(initial["changed_paths"], [])
            self.assertEqual(initial["allowed_active_scopes"], [])
            self.assertEqual(initial["pending_scopes"], pending)
            self.assertEqual(initial["violations"], [])

            (repo / "external.txt").write_text("external edit\n", encoding="utf-8")
            blocked = self.verify(repo, prepared, active=[], pending=pending)

        self.assertEqual(blocked["verdict"], "violation")
        self.assertEqual(blocked["changed_paths"], ["external.txt"])
        self.assertIn("outside_lease:external.txt", blocked["violations"])

    def test_later_spawn_allows_active_sibling_delta_but_not_pending_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, prepared = self.prepare(Path(temp_dir), thread_id="v7-lease-later")
            active = [{"kind": "exact", "path": "active.txt"}]
            pending = [{"kind": "exact", "path": "pending.txt"}]
            (repo / "active.txt").write_text("active sibling delta\n", encoding="utf-8")

            allowed = self.verify(repo, prepared, active=active, pending=pending)
            manifest = prepared["manifest"]
            self.assertIsInstance(manifest, dict)
            result_time = verify_artifact_workspace(
                Path(str(prepared["baseline_path"])),
                repo=repo,
                baseline=str(prepared["baseline"]),
                graph_sha256_value=str(prepared["graph_sha256"]),
                graph_scopes_value=graph_scopes(manifest),
                workspace_mode=str(manifest["workspace_mode"]),
            )
            (repo / "pending.txt").write_text("stale candidate delta\n", encoding="utf-8")
            blocked = self.verify(repo, prepared, active=active, pending=pending)

        self.assertEqual(allowed["verdict"], "pass")
        self.assertEqual(allowed["changed_paths"], ["active.txt"])
        self.assertEqual(allowed["allowed_active_scopes"], active)
        self.assertEqual(result_time["verdict"], "pass")
        self.assertEqual(result_time["changed_paths"], ["active.txt"])
        self.assertEqual(blocked["verdict"], "violation")
        self.assertEqual(blocked["changed_paths"], ["active.txt", "pending.txt"])
        self.assertIn("outside_lease:pending.txt", blocked["violations"])

    def test_git_control_change_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, prepared = self.prepare(Path(temp_dir), thread_id="v7-lease-control")
            subprocess.run(
                ["git", "config", "user.name", "workspace lease mutation"],
                cwd=repo,
                check=True,
            )
            blocked = self.verify(
                repo,
                prepared,
                active=[],
                pending=[{"kind": "exact", "path": "pending.txt"}],
            )

        self.assertEqual(blocked["verdict"], "violation")
        self.assertIn("git_config_changed", blocked["violations"])


if __name__ == "__main__":
    unittest.main()

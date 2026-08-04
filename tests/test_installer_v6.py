from __future__ import annotations

from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-cost-orchestrator"
INSTALLER = PLUGIN / "scripts" / "install_agents.py"
sys.path.insert(0, str(INSTALLER.parent))
import install_agents  # noqa: E402
PROFILES = {
    "read": "codex-cost-orchestrator-read-leaf.toml",
    "write": "codex-cost-orchestrator-write-leaf.toml",
}


class InstallerV6Tests(unittest.TestCase):
    def run_installer(
        self,
        target: Path,
        *args: str,
        workspace: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(INSTALLER), "--target-dir", str(target)]
        if workspace is not None:
            command.extend(("--workspace", str(workspace)))
        command.extend(args)
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_known_v5_inventory_covers_the_locally_shipped_profiles(self) -> None:
        expected = {
            "codex-cost-orchestrator-routine-worker.toml":
                "5004b32698625ab551ae24e6206235c53a30d726eb77ec8981d6132ed2b971a3",
            "codex-cost-orchestrator-complex-worker.toml":
                "58bdfe423732a4896e7331461b6c789115e3491af72643d67756c7fd6654e904",
            "codex-cost-orchestrator-reviewer.toml":
                "0d8c3f65266444cebf48595e5a9257f54dcaea03775a8a66eb8d496e200bcddd",
            "codex-cost-orchestrator-analysis-worker.toml":
                "7978912a51a343e2efefc82ec56d82e036910138d66146f574a32f0546c131d9",
        }
        for filename, digest in expected.items():
            with self.subTest(filename=filename):
                self.assertIn(digest, install_agents.LEGACY_PROFILE_SHA256[filename])

    def test_upgrade_removes_only_hash_known_legacy_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            known = target / "codex-cost-orchestrator-old-known.toml"
            unknown = target / "codex-cost-orchestrator-old-unknown.toml"
            known.write_bytes(b"known legacy\n")
            unknown.write_bytes(b"user owned\n")
            inventory = {
                known.name: frozenset({hashlib.sha256(known.read_bytes()).hexdigest()}),
                unknown.name: frozenset({"0" * 64}),
            }

            with mock.patch.object(install_agents, "LEGACY_PROFILE_SHA256", inventory):
                result = install_agents.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=Path(temp_dir),
                )

            self.assertEqual(result, 0)
            self.assertTrue(set(PROFILES.values()) <= {path.name for path in target.iterdir()})
            self.assertFalse(known.exists())
            self.assertEqual(unknown.read_bytes(), b"user owned\n")

    def test_upgrade_rollback_restores_legacy_and_removes_new_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            first = target / "legacy-a.toml"
            second = target / "legacy-b.toml"
            first.write_bytes(b"legacy a\n")
            second.write_bytes(b"legacy b\n")
            inventory = {
                path.name: frozenset({hashlib.sha256(path.read_bytes()).hexdigest()})
                for path in (first, second)
            }
            original_unlink = Path.unlink
            injected = False

            def fail_second_once(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal injected
                if path == second and not injected:
                    injected = True
                    raise OSError("injected legacy removal failure")
                original_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(install_agents, "LEGACY_PROFILE_SHA256", inventory),
                mock.patch.object(Path, "unlink", fail_second_once),
            ):
                result = install_agents.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=Path(temp_dir),
                )

            self.assertEqual(result, 1)
            self.assertEqual(first.read_bytes(), b"legacy a\n")
            self.assertEqual(second.read_bytes(), b"legacy b\n")
            self.assertTrue(all(not (target / name).exists() for name in PROFILES.values()))
            self.assertEqual(
                {path.name for path in target.iterdir()},
                {first.name, second.name},
            )

    def test_upgrade_preserves_unknown_legacy_named_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            unknown = target / "codex-cost-orchestrator-reviewer.toml"
            unknown.write_text("user-owned\n", encoding="utf-8")

            result = self.run_installer(target, "--upgrade")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(unknown.read_text(encoding="utf-8"), "user-owned\n")

    def test_clean_install_manages_only_two_physical_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            result = self.run_installer(target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({path.name for path in target.iterdir()}, set(PROFILES.values()))

            second = self.run_installer(target)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual({path.name for path in target.iterdir()}, set(PROFILES.values()))

    def test_conflicting_current_profile_refuses_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            conflict = target / PROFILES["write"]
            conflict.write_text("user-owned\n", encoding="utf-8")

            result = self.run_installer(target)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(conflict.read_text(encoding="utf-8"), "user-owned\n")
            self.assertFalse((target / PROFILES["read"]).exists())

    def test_check_rejects_an_active_workspace_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "home" / "agents"
            workspace = root / "repo"
            (workspace / ".git").mkdir(parents=True)
            installed = self.run_installer(target, workspace=workspace)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            shadow_dir = workspace / ".codex" / "agents"
            shadow_dir.mkdir(parents=True)
            (shadow_dir / "shadow.toml").write_text(
                'name = "cost_orchestrator_read_leaf"\n', encoding="utf-8"
            )
            checked = self.run_installer(target, "--check", workspace=workspace)

            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("shadows", checked.stderr)

    def test_read_profile_is_read_only_and_both_are_model_neutral_leaves(self) -> None:
        for profile, filename in PROFILES.items():
            value = tomllib.loads((PLUGIN / "agents" / filename).read_text(encoding="utf-8"))
            with self.subTest(profile=profile):
                self.assertNotIn("model", value)
                self.assertNotIn("model_reasoning_effort", value)
                self.assertFalse(value["features"]["multi_agent"])
                self.assertEqual(value["features"]["multi_agent_v2"], {"enabled": False})
                if profile == "read":
                    self.assertEqual(value["sandbox_mode"], "read-only")

    def test_profile_selection_uses_permission_roles_not_task_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            for profile in PROFILES:
                result = self.run_installer(target, "--profile", profile)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

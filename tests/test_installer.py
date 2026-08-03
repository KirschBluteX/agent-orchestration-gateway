from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "codex-cost-orchestrator"
INSTALLER = PLUGIN / "scripts" / "install_agents.py"
sys.path.insert(0, str(INSTALLER.parent))
import install_agents as installer_module  # noqa: E402
AGENT_FILENAMES = (
    "codex-cost-orchestrator-routine-worker.toml",
    "codex-cost-orchestrator-complex-worker.toml",
    "codex-cost-orchestrator-reviewer.toml",
)


class InstallerBehaviorTests(unittest.TestCase):
    def test_published_0_3_1_profiles_are_recognized_for_upgrade(self) -> None:
        published_hashes = {
            "codex-cost-orchestrator-routine-worker.toml": (
                "2c5b1716312ad7be52eaec26676b52c1a5168cb1d3c602d39a82f907b4afa93d"
            ),
            "codex-cost-orchestrator-complex-worker.toml": (
                "881c3b606c1e9092a96e79ad85bd5b57fef97156f4838a26b18191d18bfee681"
            ),
            "codex-cost-orchestrator-reviewer.toml": (
                "015c8fe9da8b92a24f021120e71f6b0e3e0bfb10244ba529f7ff8981ce179e00"
            ),
        }

        for filename, digest in published_hashes.items():
            with self.subTest(filename=filename):
                self.assertIn(digest, installer_module.LEGACY_PROFILE_SHA256[filename])

    def test_clean_install_is_exact_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            command = [
                sys.executable,
                str(INSTALLER),
                "--target-dir",
                str(target),
            ]

            first = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)

            templates = PLUGIN / "agents"
            before = {}
            for filename in AGENT_FILENAMES:
                installed = target / filename
                self.assertEqual(installed.read_bytes(), (templates / filename).read_bytes())
                before[filename] = installed.read_bytes()

            second = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                {filename: (target / filename).read_bytes() for filename in AGENT_FILENAMES},
                before,
            )

            checked = subprocess.run(
                [*command, "--check"], text=True, capture_output=True, check=False
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(
                {filename: (target / filename).read_bytes() for filename in AGENT_FILENAMES},
                before,
            )

    def test_conflict_refusal_is_all_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            conflict = target / AGENT_FILENAMES[0]
            conflict.write_text("user-owned profile\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(INSTALLER), "--target-dir", str(target)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                conflict.read_text(encoding="utf-8"), "user-owned profile\n"
            )
            for filename in AGENT_FILENAMES[1:]:
                self.assertFalse((target / filename).exists())

    def test_explicit_upgrade_replaces_only_known_legacy_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            workspace = Path(temp_dir) / "repo"
            (workspace / ".git").mkdir(parents=True)
            legacy: dict[str, bytes] = {}
            for index, filename in enumerate(AGENT_FILENAMES, start=1):
                contents = f"known legacy profile {index}\n".encode()
                legacy[filename] = contents
                (target / filename).write_bytes(contents)
            known_hashes = {
                filename: frozenset({hashlib.sha256(contents).hexdigest()})
                for filename, contents in legacy.items()
            }

            with mock.patch.object(
                installer_module, "LEGACY_PROFILE_SHA256", known_hashes
            ):
                result = installer_module.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=workspace,
                )

            self.assertEqual(result, 0)
            for filename in AGENT_FILENAMES:
                self.assertEqual(
                    (target / filename).read_bytes(),
                    (PLUGIN / "agents" / filename).read_bytes(),
                )

    def test_upgrade_rolls_back_the_entire_batch_after_a_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            workspace = Path(temp_dir) / "repo"
            (workspace / ".git").mkdir(parents=True)
            legacy: dict[str, bytes] = {}
            for index, filename in enumerate(AGENT_FILENAMES, start=1):
                contents = f"known legacy profile {index}\n".encode()
                legacy[filename] = contents
                destination = target / filename
                destination.write_bytes(contents)
                timestamp = 1_700_000_000_000_000_000 + index
                os.utime(destination, ns=(timestamp, timestamp))
            metadata = {
                filename: (
                    (target / filename).stat().st_dev,
                    (target / filename).stat().st_ino,
                    (target / filename).stat().st_mtime_ns,
                    (target / filename).stat().st_mode,
                )
                for filename in AGENT_FILENAMES
            }
            known_hashes = {
                filename: frozenset({hashlib.sha256(contents).hexdigest()})
                for filename, contents in legacy.items()
            }
            real_replace = os.replace
            replacements = 0

            def fail_second_upgrade(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                nonlocal replacements
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    source_path.name.startswith(".cost-orchestrator-agent-")
                    and destination_path.name in AGENT_FILENAMES
                ):
                    replacements += 1
                    if replacements == 2:
                        raise PermissionError("injected second replacement failure")
                real_replace(source, destination)

            with (
                mock.patch.object(
                    installer_module, "LEGACY_PROFILE_SHA256", known_hashes
                ),
                mock.patch.object(installer_module.os, "replace", fail_second_upgrade),
            ):
                result = installer_module.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=workspace,
                )

            self.assertEqual(result, 1)
            self.assertEqual(
                {
                    filename: (target / filename).read_bytes()
                    for filename in AGENT_FILENAMES
                },
                legacy,
            )
            self.assertEqual(
                {
                    filename: (
                        (target / filename).stat().st_dev,
                        (target / filename).stat().st_ino,
                        (target / filename).stat().st_mtime_ns,
                        (target / filename).stat().st_mode,
                    )
                    for filename in AGENT_FILENAMES
                },
                metadata,
            )

    def test_upgrade_preparation_failure_leaves_no_staged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            workspace = Path(temp_dir) / "repo"
            (workspace / ".git").mkdir(parents=True)
            legacy: dict[str, bytes] = {}
            for index, filename in enumerate(AGENT_FILENAMES, start=1):
                contents = f"known legacy profile {index}\n".encode()
                legacy[filename] = contents
                (target / filename).write_bytes(contents)
            known_hashes = {
                filename: frozenset({hashlib.sha256(contents).hexdigest()})
                for filename, contents in legacy.items()
            }
            real_stage = installer_module.stage_bytes
            calls = 0

            def fail_second_stage(
                directory: Path, contents: bytes, prefix: str
            ) -> Path:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PermissionError("injected backup staging failure")
                return real_stage(directory, contents, prefix)

            with (
                mock.patch.object(
                    installer_module, "LEGACY_PROFILE_SHA256", known_hashes
                ),
                mock.patch.object(
                    installer_module, "stage_bytes", fail_second_stage
                ),
            ):
                result = installer_module.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=workspace,
                )

            self.assertEqual(result, 1)
            self.assertEqual(
                {path.name for path in target.iterdir()}, set(AGENT_FILENAMES)
            )
            self.assertEqual(
                {
                    filename: (target / filename).read_bytes()
                    for filename in AGENT_FILENAMES
                },
                legacy,
            )

    def test_upgrade_refuses_a_concurrent_known_legacy_inode_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "agents"
            target.mkdir()
            workspace = root / "repo"
            (workspace / ".git").mkdir(parents=True)
            filename = AGENT_FILENAMES[0]
            destination = target / filename
            original = b"known legacy A\n"
            replacement = b"known legacy B\n"
            destination.write_bytes(original)
            known_hashes = {
                filename: frozenset(
                    {
                        hashlib.sha256(original).hexdigest(),
                        hashlib.sha256(replacement).hexdigest(),
                    }
                )
            }
            real_stage_hardlink = installer_module.stage_hardlink

            def swap_after_backup(
                directory: Path, source: Path, prefix: str
            ) -> Path:
                backup = real_stage_hardlink(directory, source, prefix)
                alternate = directory / ".concurrent-known-legacy"
                alternate.write_bytes(replacement)
                os.replace(alternate, source)
                return backup

            with (
                mock.patch.object(
                    installer_module, "LEGACY_PROFILE_SHA256", known_hashes
                ),
                mock.patch.object(
                    installer_module, "stage_hardlink", swap_after_backup
                ),
            ):
                result = installer_module.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    profiles=["routine"],
                    workspace=workspace,
                )

            self.assertEqual(result, 1)
            self.assertEqual(destination.read_bytes(), replacement)

    def test_upgrade_refuses_an_unknown_profile_without_partial_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            workspace = Path(temp_dir) / "repo"
            (workspace / ".git").mkdir(parents=True)
            known_filename = AGENT_FILENAMES[0]
            unknown_filename = AGENT_FILENAMES[1]
            missing_filename = AGENT_FILENAMES[2]
            known_contents = b"known legacy profile\n"
            unknown_contents = b"user-owned profile\n"
            (target / known_filename).write_bytes(known_contents)
            (target / unknown_filename).write_bytes(unknown_contents)
            known_hashes = {
                known_filename: frozenset(
                    {hashlib.sha256(known_contents).hexdigest()}
                )
            }

            with mock.patch.object(
                installer_module, "LEGACY_PROFILE_SHA256", known_hashes
            ):
                result = installer_module.install(
                    target,
                    check_only=False,
                    upgrade=True,
                    workspace=workspace,
                )

            self.assertEqual(result, 1)
            self.assertEqual((target / known_filename).read_bytes(), known_contents)
            self.assertEqual((target / unknown_filename).read_bytes(), unknown_contents)
            self.assertFalse((target / missing_filename).exists())

    def test_profile_selection_checks_only_the_roles_used_by_a_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            install = subprocess.run(
                [sys.executable, str(INSTALLER), "--target-dir", str(target)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            (target / "codex-cost-orchestrator-complex-worker.toml").unlink()

            selected = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--check",
                    "--profile",
                    "routine",
                    "--profile",
                    "reviewer",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            all_profiles = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--check",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertIn("routine, reviewer", selected.stdout)
            self.assertNotEqual(all_profiles.returncode, 0)

    def test_check_rejects_a_shadowing_role_in_the_active_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "codex-home" / "agents"
            workspace = root / "repo"
            (workspace / ".git").mkdir(parents=True)
            install = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--workspace",
                    str(workspace),
                    "--profile",
                    "routine",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            installed_before = (target / AGENT_FILENAMES[0]).read_bytes()

            project_agents = workspace / ".codex" / "agents"
            project_agents.mkdir(parents=True)
            (project_agents / "shadow.toml").write_text(
                'name = "cost_orchestrator_routine_worker"\n'
                'description = "Shadowing role"\n'
                'developer_instructions = "Different authority"\n'
                "[features]\n"
                "multi_agent = false\n"
                "multi_agent_v2 = false\n",
                encoding="utf-8",
            )

            checked = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--workspace",
                    str(workspace),
                    "--check",
                    "--profile",
                    "routine",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("shadow", checked.stderr.lower())
            self.assertEqual((target / AGENT_FILENAMES[0]).read_bytes(), installed_before)

    def test_check_rejects_a_shadowing_declared_role_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "codex-home" / "agents"
            workspace = root / "repo"
            project_config = workspace / ".codex"
            (workspace / ".git").mkdir(parents=True)
            install = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--workspace",
                    str(workspace),
                    "--profile",
                    "reviewer",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            project_config.mkdir()
            (project_config / "shadow-reviewer.toml").write_text(
                'name = "cost_orchestrator_reviewer"\n'
                'description = "Shadow reviewer"\n'
                'developer_instructions = "Different review authority"\n',
                encoding="utf-8",
            )
            (project_config / "config.toml").write_text(
                "[agents.cost_orchestrator_reviewer]\n"
                'config_file = "./shadow-reviewer.toml"\n',
                encoding="utf-8",
            )

            checked = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(target),
                    "--workspace",
                    str(workspace),
                    "--check",
                    "--profile",
                    "reviewer",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("project config shadows", checked.stderr.lower())

    def test_refuses_a_shipped_worker_template_with_runtime_pins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_plugin = Path(temp_dir) / "plugin"
            scripts = copied_plugin / "scripts"
            agents = copied_plugin / "agents"
            scripts.mkdir(parents=True)
            agents.mkdir()
            shutil.copy2(INSTALLER, scripts / INSTALLER.name)
            for filename in AGENT_FILENAMES:
                shutil.copy2(PLUGIN / "agents" / filename, agents / filename)

            routine = agents / AGENT_FILENAMES[0]
            contents = routine.read_text(encoding="utf-8")
            routine.write_text(
                contents.replace(
                    'description = "Non-delegating executor for deterministic CCO work nodes."\n',
                    'description = "Non-delegating executor for deterministic CCO work nodes."\n'
                    'model = "gpt-fixed-by-mistake"\n',
                ),
                encoding="utf-8",
            )
            target = Path(temp_dir) / "installed"
            result = subprocess.run(
                [
                    sys.executable,
                    str(scripts / INSTALLER.name),
                    "--target-dir",
                    str(target),
                    "--profile",
                    "routine",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("worker profile must not pin model", result.stderr)
            self.assertFalse(target.exists())

    def test_refuses_a_shipped_profile_with_a_nested_agents_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_plugin = Path(temp_dir) / "plugin"
            scripts = copied_plugin / "scripts"
            agents = copied_plugin / "agents"
            scripts.mkdir(parents=True)
            agents.mkdir()
            shutil.copy2(INSTALLER, scripts / INSTALLER.name)
            for filename in AGENT_FILENAMES:
                shutil.copy2(PLUGIN / "agents" / filename, agents / filename)

            routine = agents / AGENT_FILENAMES[0]
            routine.write_text(
                routine.read_text(encoding="utf-8")
                + "\n[agents]\nenabled = false\n",
                encoding="utf-8",
            )
            target = Path(temp_dir) / "installed"
            result = subprocess.run(
                [
                    sys.executable,
                    str(scripts / INSTALLER.name),
                    "--target-dir",
                    str(target),
                    "--profile",
                    "routine",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not declare an agents table", result.stderr)
            self.assertFalse(target.exists())

    def test_codex_home_target_does_not_edit_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex-home"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text("model = 'user-choice'\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)

            result = subprocess.run(
                [sys.executable, str(INSTALLER)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            for filename in AGENT_FILENAMES:
                self.assertTrue((codex_home / "agents" / filename).is_file())
            self.assertEqual(
                config.read_text(encoding="utf-8"), "model = 'user-choice'\n"
            )

    def test_refuses_a_symlink_target_without_mutating_its_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_target = root / "real-agents"
            real_target.mkdir()
            link_target = root / "linked-agents"
            try:
                os.symlink(real_target, link_target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--target-dir",
                    str(link_target),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(real_target.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_refuses_a_junction_target_without_mutating_its_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_target = root / "real-agents"
            real_target.mkdir()
            junction = root / "junction-agents"
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(real_target)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)

            result = subprocess.run(
                [sys.executable, str(INSTALLER), "--target-dir", str(junction)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(real_target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

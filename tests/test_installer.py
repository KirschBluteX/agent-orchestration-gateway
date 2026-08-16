from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from install_agents import (  # noqa: E402
    InstallError,
    PROFILES,
    check,
    doctor,
    install,
    uninstall,
)
import install_agents  # noqa: E402


def native_catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-terra",
                "supported_reasoning_levels": [{"effort": "max"}],
            }
        ]
    }


def hook_inventory(*, trusted: bool = True) -> dict[str, object]:
    events = ["sessionStart", "preToolUse", "postToolUse", "stop", "subagentStop"]
    return {
        "errors": [],
        "hooks": [
            {
                "enabled": True,
                "eventName": event,
                "pluginId": "codex-cost-orchestrator@codex-cost-orchestrator",
                "trustStatus": "trusted" if trusted else "untrusted",
            }
            for event in events
        ],
        "warnings": [],
    }


class InstallerTests(unittest.TestCase):
    def test_fresh_install_check_idempotence_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            self.assertEqual(install(target), 0)
            self.assertEqual(check(target), 0)
            self.assertEqual(install(target), 0)
            self.assertEqual(uninstall(target), 0)
            self.assertFalse(any(target.glob("*.toml")))

    def test_different_profile_requires_explicit_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            destination = target / PROFILES["read"][0]
            destination.write_text("name='user-owned'\n", encoding="utf-8")
            with self.assertRaisesRegex(InstallError, "--replace"):
                install(target, profiles=["read"])
            self.assertEqual(install(target, profiles=["read"], replace=True), 0)
            self.assertEqual(check(target, profiles=["read"]), 0)

    def test_uninstall_refuses_modified_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            install(target, profiles=["write"])
            destination = target / PROFILES["write"][0]
            destination.write_text(destination.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
            self.assertEqual(uninstall(target, profiles=["write"]), 1)
            self.assertTrue(destination.exists())

    def test_failed_multi_profile_replace_restores_every_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            target.mkdir()
            originals: dict[Path, bytes] = {}
            for name, (filename, _agent) in PROFILES.items():
                destination = target / filename
                destination.write_text(f"name='{name}-original'\n", encoding="utf-8")
                originals[destination] = destination.read_bytes()
            real_replace = install_agents.os.replace
            install_calls = 0

            def fail_second_install(source: object, destination: object) -> None:
                nonlocal install_calls
                if Path(source).name.startswith(".cco-profile-"):
                    install_calls += 1
                    if install_calls == 2:
                        raise OSError("injected replace failure")
                real_replace(source, destination)

            with patch.object(install_agents.os, "replace", side_effect=fail_second_install):
                with self.assertRaisesRegex(OSError, "injected"):
                    install(target, replace=True)
            for destination, contents in originals.items():
                self.assertEqual(destination.read_bytes(), contents)
            self.assertFalse(list(target.glob(".cco-profile-*")))

    def test_doctor_checks_profiles_hooks_and_static_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            install(target)
            self.assertEqual(
                doctor(
                    target,
                    workspace=ROOT,
                    native_loader=native_catalog,
                    hook_loader=lambda _workspace: hook_inventory(),
                ),
                0,
            )
            self.assertEqual(
                doctor(
                    target,
                    workspace=ROOT,
                    native_loader=native_catalog,
                    hook_loader=lambda _workspace: hook_inventory(trusted=False),
                ),
                1,
            )

    def test_doctor_rejects_duplicate_and_unknown_cco_hook_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            install(target)
            for event_name in ("stop", "sessionEnd"):
                with self.subTest(event_name=event_name):
                    inventory = hook_inventory()
                    hooks = inventory["hooks"]
                    assert isinstance(hooks, list)
                    hooks.append(
                        {
                            "enabled": True,
                            "eventName": event_name,
                            "pluginId": "codex-cost-orchestrator@codex-cost-orchestrator",
                            "trustStatus": "trusted",
                        }
                    )
                    self.assertEqual(
                        doctor(
                            target,
                            workspace=ROOT,
                            native_loader=native_catalog,
                            hook_loader=lambda _workspace, value=inventory: value,
                        ),
                        1,
                    )

    def test_doctor_uses_canonical_repository_root_for_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            child = repo / "nested" / "work"
            child.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            target = root / "agents"
            install(target)
            observed: list[Path] = []

            def hooks(workspace: Path) -> dict[str, object]:
                observed.append(workspace)
                return hook_inventory()

            self.assertEqual(
                doctor(
                    target,
                    workspace=child,
                    native_loader=native_catalog,
                    hook_loader=hooks,
                ),
                0,
            )
            self.assertEqual(observed, [repo.resolve()])


if __name__ == "__main__":
    unittest.main()

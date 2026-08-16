from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-orchestration-gateway"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

from install_agents import (  # noqa: E402
    HOST_HOOK_CONTRACT,
    _validate_local_hook_contract,
    doctor,
    install,
)


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


def canonical_hook_inventory() -> dict[str, object]:
    manifest = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command_key = "commandWindows" if os.name == "nt" else "command"
    hooks: list[dict[str, object]] = []
    for lifecycle, groups in manifest["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                hooks.append(
                    {
                        "command": handler[command_key].replace(
                            "${PLUGIN_ROOT}", str(PLUGIN.resolve())
                        ),
                        "enabled": True,
                        "eventName": lifecycle[:1].lower() + lifecycle[1:],
                        "handlerType": handler["type"],
                        "matcher": group.get("matcher"),
                        "pluginId": "agent-orchestration-gateway@agent-orchestration-gateway",
                        "statusMessage": handler["statusMessage"],
                        "timeoutSec": handler["timeout"],
                        "trustStatus": "trusted",
                    }
                )
    return {"errors": [], "hooks": hooks, "warnings": []}


class InstallAgentsHookContractTests(unittest.TestCase):
    def _doctor(self, inventory: dict[str, object]) -> int:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "agents"
            self.assertEqual(install(target), 0)
            return doctor(
                target,
                workspace=ROOT,
                native_loader=native_catalog,
                hook_loader=lambda _workspace: inventory,
            )

    def test_doctor_accepts_the_complete_canonical_local_hook_schema(self) -> None:
        self.assertEqual(self._doctor(canonical_hook_inventory()), 0)

    def test_doctor_rejects_each_canonical_hook_schema_field_drift(self) -> None:
        replacements = {
            "command": "python -B \"not-the-aog-hook.py\"",
            "handlerType": "prompt",
            "matcher": "aog_read_leaf",
            "statusMessage": "Unexpected Hook status",
            "timeoutSec": 1,
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                inventory = copy.deepcopy(canonical_hook_inventory())
                hooks = inventory["hooks"]
                assert isinstance(hooks, list)
                target = next(
                    item
                    for item in hooks
                    if item["eventName"] == "subagentStop"
                )
                target[field] = replacement
                self.assertEqual(self._doctor(inventory), 1)

    def test_fixed_contract_rejects_missing_or_unknown_lifecycle(self) -> None:
        manifest = {
            event[:1].upper() + event[1:]: []
            for event, _matcher in HOST_HOOK_CONTRACT
        }
        del manifest["Stop"]
        with self.assertRaises(ValueError):
            _validate_local_hook_contract(manifest)

        manifest["Stop"] = []
        manifest["SessionEnd"] = []
        with self.assertRaises(ValueError):
            _validate_local_hook_contract(manifest)

    def test_fixed_contract_rejects_any_noncanonical_matcher(self) -> None:
        manifest = {
            event[:1].upper() + event[1:]: [
                {"matcher": matcher} if matcher is not None else {}
            ]
            for event, matcher in HOST_HOOK_CONTRACT
        }
        for lifecycle in manifest:
            with self.subTest(lifecycle=lifecycle):
                damaged = copy.deepcopy(manifest)
                damaged[lifecycle][0]["matcher"] = ".*"
                with self.assertRaises(ValueError):
                    _validate_local_hook_contract(damaged)


if __name__ == "__main__":
    unittest.main()

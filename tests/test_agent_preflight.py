from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks" / "agent_preflight.py"
HOOKS_CONFIG = HOOK.parent / "hooks.json"
sys.path.insert(0, str(HOOK.parent))

import agent_preflight  # noqa: E402
sys.path.insert(0, str(HOOK.parent.parent / "scripts"))
from packet_compiler import compile_dispatch  # noqa: E402
from tests.v6_test_support import fixed_route_plan  # noqa: E402


def payload(tool_input: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "spawn_agent",
        "tool_input": tool_input,
    }
    value.update(overrides)
    return value


def worker_input() -> dict[str, object]:
    return compile_dispatch(
        {
            "baseline": "sha256:" + "b" * 64,
            "contract": {"node": "n01_protocol", "objective": "bounded result"},
            "judgment": "routine",
            "kind": "work",
            "node": "n01_protocol",
            "purpose": "implementation",
            "route_plan": fixed_route_plan(),
        }
    )


def run_hook(value: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )


class AgentPreflightBehaviorTests(unittest.TestCase):
    def test_canonical_v6_worker_is_allowed(self) -> None:
        result = run_hook(payload(worker_input()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_legacy_or_substituted_packet_is_blocked(self) -> None:
        canonical = worker_input()
        for message in (
            "CCO_WORK cco.v5\nNODE: n01_protocol",
            str(canonical["message"]).replace("bounded result", "substituted objective"),
        ):
            with self.subTest(message=message[:24]):
                outcome = json.loads(
                    run_hook(payload({**canonical, "message": message})).stdout
                )
                self.assertEqual(outcome["decision"], "block")

    def test_every_visible_agent_spawn_requires_a_declared_cco_role(self) -> None:
        for tool_input in (
            {"task_name": "ordinary", "message": "do work"},
            {**worker_input(), "agent_type": "default"},
        ):
            with self.subTest(tool_input=tool_input):
                outcome = agent_preflight.evaluate(payload(tool_input))
                self.assertEqual(outcome["decision"], "block")

    def test_spawn_override_and_native_policy_cannot_bypass_hashes(self) -> None:
        extra = {**worker_input(), "sandbox_mode": "danger-full-access"}
        substituted = {**worker_input(), "model": "gpt-5.6-sol"}
        for tool_input in (extra, substituted):
            outcome = agent_preflight.evaluate(payload(tool_input))
            self.assertEqual(outcome["decision"], "block")

    def test_hook_config_covers_full_ledger_lifecycle(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            set(config["hooks"]),
            {"PreToolUse", "PostToolUse", "SubagentStop", "SessionEnd"},
        )
        serialized = json.dumps(config, sort_keys=True)
        for script in ("agent_preflight.py", "ledger_runtime.py", "subagent_stop.py"):
            self.assertIn(script, serialized)
        for obsolete in ("agent_postflight.py", "interrupt_preflight.py", "ledger_cleanup.py"):
            self.assertNotIn(obsolete, serialized)


if __name__ == "__main__":
    unittest.main()

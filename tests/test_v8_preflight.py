from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path[:0] = [str(HOOKS), str(SCRIPTS)]

import agent_preflight  # noqa: E402


class V8PreflightTests(unittest.TestCase):
    def test_nested_and_twice_encoded_protected_payloads_are_blocked(self) -> None:
        token = "gAAAA" + "A" * 96
        messages: list[object] = [
            [{"type": "reasoning", "encrypted_content": token}],
            json.dumps(json.dumps({"type": "reasoning", "encrypted_content": token})),
        ]
        for index, message in enumerate(messages):
            with self.subTest(index=index):
                outcome = agent_preflight.evaluate(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_input": {"message": message, "target": "/root/unmanaged"},
                        "tool_name": "send_message",
                    }
                )
                self.assertEqual(outcome["decision"], "block")
                self.assertIn("CCO_PROTECTED_MESSAGE", outcome["reason"])

    def test_raw_spawn_is_blocked_and_explicit_bypass_is_stripped(self) -> None:
        raw = {
            "agent_type": "explorer",
            "fork_turns": "3",
            "message": "inspect the repository",
            "task_name": "inspect_repo",
        }
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_input": raw,
            "tool_name": "spawn_agent",
        }

        blocked = agent_preflight.evaluate(payload)
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("CCO_REQUIRED", blocked["reason"])

        bypassed = agent_preflight.evaluate(
            {
                **payload,
                "tool_input": {
                    **raw,
                    "message": "CCO_NATIVE_BYPASS v1\ninspect the repository",
                },
            }
        )
        hook = bypassed["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], "allow")
        self.assertEqual(hook["updatedInput"]["message"], "inspect the repository")
        self.assertNotIn("model", hook["updatedInput"])
        self.assertNotIn("reasoning_effort", hook["updatedInput"])

    def test_old_task_and_malformed_bypass_fail_closed(self) -> None:
        base = {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
        }
        for protocol in ("cco.v6", "cco.v7"):
            with self.subTest(protocol=protocol):
                old = agent_preflight.evaluate(
                    {
                        **base,
                        "tool_input": {
                            "agent_type": "cost_orchestrator_write_leaf",
                            "fork_turns": "none",
                            "message": f"CCO_DISPATCH {protocol}\nlegacy",
                            "model": "gpt-5.6-luna",
                            "reasoning_effort": "max",
                            "task_name": "worker_legacy_g01",
                        },
                    }
                )
                self.assertEqual(old["decision"], "block")
                self.assertIn("CCO_OLD_TASK_REQUIRES_NEW_TASK", old["reason"])

        malformed = agent_preflight.evaluate(
            {
                **base,
                "tool_input": {
                    "agent_type": "explorer",
                    "fork_turns": "none",
                    "message": "CCO_NATIVE_BYPASS v1",
                    "task_name": "native_task",
                },
            }
        )
        self.assertEqual(malformed["decision"], "block")
        self.assertIn("malformed", malformed["reason"])


if __name__ == "__main__":
    unittest.main()

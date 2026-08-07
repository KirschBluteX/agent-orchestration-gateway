from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
sys.path.insert(0, str(HOOKS))

import cco_hook  # noqa: E402


class V9HookTests(unittest.TestCase):
    def test_protected_payload_guard_handles_nested_host_reasoning(self) -> None:
        protected = {
            "type": "reasoning",
            "encrypted_content": "gAAAA" + ("a" * 100),
        }
        wrapped: object = protected
        for _ in range(8):
            wrapped = json.dumps(wrapped)
        outcome = cco_hook.evaluate(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "hook-session",
                "tool_input": {"message": wrapped, "target": "/root/example"},
                "tool_name": "send_message",
                "tool_use_id": "call",
            }
        )
        self.assertEqual(outcome["decision"], "block")
        self.assertIn("opaque collaboration", outcome["reason"])

    def test_native_bypass_cannot_forward_embedded_protected_content(self) -> None:
        outcome = cco_hook.evaluate(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "hook-session",
                "tool_input": {
                    "message": "CCO_NATIVE_BYPASS v1\ninspect "
                    + "gAAAA"
                    + ("b" * 100)
                },
                "tool_name": "spawn_agent",
                "tool_use_id": "call",
            }
        )
        self.assertEqual(outcome["decision"], "block")
        self.assertIn("opaque collaboration", outcome["reason"])

    def test_hook_manifest_has_no_global_all_tool_matcher(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest["hooks"]),
            {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop"},
        )
        serialized = json.dumps(manifest)
        self.assertNotIn('"matcher": ".*"', serialized)
        count = sum(
            len(group["hooks"])
            for groups in manifest["hooks"].values()
            for group in groups
        )
        self.assertEqual(count, 5)

    def test_stop_blocks_only_the_first_stop_event_while_work_is_active(self) -> None:
        class Control:
            @staticmethod
            def stop_reason() -> str:
                return "wait for the native terminal event"

        with patch.object(cco_hook, "_control", return_value=Control()):
            first = cco_hook.evaluate(
                {"hook_event_name": "Stop", "stop_hook_active": False}
            )
            repeated = cco_hook.evaluate(
                {"hook_event_name": "Stop", "stop_hook_active": True}
            )
        self.assertEqual(first["decision"], "block")
        self.assertEqual(repeated, {})

    def test_session_compaction_does_not_fence_live_children(self) -> None:
        class Control:
            @staticmethod
            def restart() -> int:
                raise AssertionError("compact must not run restart recovery")

        with patch.object(cco_hook, "_control", return_value=Control()):
            compact = cco_hook.evaluate(
                {"hook_event_name": "SessionStart", "source": "compact"}
            )
        self.assertEqual(compact, {})

    def test_session_resume_fences_native_work_lost_by_restart(self) -> None:
        class Control:
            @staticmethod
            def restart() -> int:
                return 2

        with patch.object(cco_hook, "_control", return_value=Control()):
            resumed = cco_hook.evaluate(
                {"hook_event_name": "SessionStart", "source": "resume"}
            )
        context = resumed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("fenced child work", context)

    def test_subagent_stop_records_first_result_and_prevents_other_continuations(self) -> None:
        calls: list[tuple[str, object]] = []

        class Control:
            @staticmethod
            def record_result(owner: str, result: object) -> None:
                calls.append((owner, result))

        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(cco_hook, "_owner", return_value="/root/worker_n01"),
        ):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "CCO_RESULT cco.v9\n{}",
                    "stop_hook_active": False,
                }
            )
        self.assertEqual(calls, [("/root/worker_n01", "CCO_RESULT cco.v9\n{}")])
        self.assertEqual(outcome, {"continue": False})

    def test_unmanaged_post_tool_and_interrupt_are_noops(self) -> None:
        class Control:
            @staticmethod
            def owner_is_managed(_owner: str) -> bool:
                return False

        with patch.object(cco_hook, "_control", return_value=Control()):
            post = cco_hook.evaluate(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_input": {"message": "ordinary task"},
                }
            )
            interrupt = cco_hook.evaluate(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_input": {"target": "/root/unmanaged"},
                    "tool_name": "interrupt_agent",
                }
            )
        self.assertEqual(post, {})
        self.assertEqual(interrupt, {})


if __name__ == "__main__":
    unittest.main()

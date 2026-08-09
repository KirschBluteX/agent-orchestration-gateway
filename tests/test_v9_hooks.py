from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(SCRIPTS))

import cco_hook  # noqa: E402
from state_lock import StateLockBusy  # noqa: E402


class V9HookTests(unittest.TestCase):
    def test_uuid_subagent_stop_maps_owner_from_trusted_rollout(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "agent_path": "/root/worker_n01",
                            "id": agent_id,
                            "parent_thread_id": "parent-task",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()):
                self.assertEqual(
                    cco_hook._owner(
                        {
                            "agent_id": agent_id,
                            "agent_transcript_path": str(rollout),
                            "session_id": "parent-task",
                        }
                    ),
                    "/root/worker_n01",
                )

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

    def test_native_bypass_marker_has_no_authority(self) -> None:
        outcome = cco_hook.evaluate(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "hook-session",
                "tool_input": {"message": "CCO_NATIVE_BYPASS v1\ninspect one image"},
                "tool_name": "spawn_agent",
                "tool_use_id": "call",
            }
        )
        self.assertEqual(outcome["decision"], "block")
        self.assertIn("every native child", outcome["reason"])

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

    def test_pretool_internal_budget_is_below_host_timeout(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        host_timeout = manifest["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
        self.assertLess(cco_hook.PRETOOL_INTERNAL_BUDGET_SECONDS, host_timeout)
        control = cco_hook._control(
            {"hook_event_name": "PreToolUse", "session_id": "budgeted-hook"}
        )
        self.assertLess(control.lock_timeout, 3)

    def test_subagent_stop_budget_leaves_host_settlement_reserve(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        host_timeout = manifest["hooks"]["SubagentStop"][0]["hooks"][0]["timeout"]
        self.assertLess(cco_hook.SUBAGENT_STOP_INTERNAL_BUDGET_SECONDS, host_timeout)
        control = cco_hook._control(
            {"hook_event_name": "SubagentStop", "session_id": "result-budget"}
        )
        self.assertLessEqual(control.lock_timeout, 10)

    def test_postflight_delegates_only_success_settlement(self) -> None:
        class Control:
            @staticmethod
            def postflight_tool(_payload: object) -> None:
                return None

        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "hook-session",
                    "tool_input": {"message": "CCO_TASK cco.v9\n{}"},
                    "tool_name": "spawn_agent",
                    "tool_response": {"success": True},
                    "tool_use_id": "call",
                }
            )
        self.assertEqual(outcome, {})

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

    def test_arbitrary_assistant_error_text_is_not_treated_as_typed_native_failure(self) -> None:
        calls: list[tuple[str, object]] = []
        class Control:
            @staticmethod
            def record_result(owner: str, result: object) -> None:
                calls.append((owner, result))
                raise ValueError("not a CCO result")

            @staticmethod
            def fence_invalid_result(_owner: str) -> None:
                return None

        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(cco_hook, "_owner", return_value="/root/worker_n01"),
        ):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "429 timeout while discussing a test fixture",
                }
            )

        self.assertEqual(
            calls,
            [("/root/worker_n01", "429 timeout while discussing a test fixture")],
        )
        self.assertEqual(outcome["continue"], False)
        self.assertIn("rejected and fenced", outcome["systemMessage"])

    def test_subagent_stop_lock_contention_retries_without_fencing_result(self) -> None:
        class Control:
            @staticmethod
            def record_result(_owner: str, _result: object) -> None:
                raise StateLockBusy("busy")

            @staticmethod
            def fence_invalid_result(_owner: str) -> None:
                raise AssertionError("infrastructure contention must not fence a child")

        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(cco_hook, "_owner", return_value="/root/worker_n01"),
        ):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "CCO_RESULT cco.v9\n{}",
                }
            )

        self.assertEqual(outcome["decision"], "block")
        self.assertIn("same result", outcome["reason"])

    def test_interrupt_is_validated_then_settled_by_post_tool_use(self) -> None:
        calls: list[tuple[str, object]] = []

        class Control:
            @staticmethod
            def owner_is_managed(_owner: str) -> bool:
                return True

            @staticmethod
            def preflight_interrupt(payload: object) -> None:
                calls.append(("pre", payload))

            @staticmethod
            def postflight_interrupt(payload: object) -> None:
                calls.append(("post", payload))

        pre = {
            "hook_event_name": "PreToolUse",
            "tool_input": {"target": "/root/worker_n01"},
            "tool_name": "interrupt_agent",
            "tool_use_id": "interrupt-call",
        }
        post = {
            **pre,
            "hook_event_name": "PostToolUse",
            "tool_response": {"ok": True},
        }
        with patch.object(cco_hook, "_control", return_value=Control()):
            self.assertEqual(cco_hook.evaluate(pre), {})
            self.assertEqual(cco_hook.evaluate(post), {})
        self.assertEqual(calls, [("pre", pre), ("post", post)])

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

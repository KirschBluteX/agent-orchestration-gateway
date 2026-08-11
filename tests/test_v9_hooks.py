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
from rollout_io import RolloutUnavailable  # noqa: E402
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

    def test_uuid_owner_mapping_replays_after_temporary_rollout_io_failure(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000002"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
            payload = {
                "agent_id": agent_id,
                "agent_transcript_path": str(rollout),
                "session_id": "parent-task",
            }

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(
                    cco_hook,
                    "first_record",
                    side_effect=RolloutUnavailable("rollout is temporarily unavailable"),
                ),
                self.assertRaises(cco_hook.ControlPlaneUnavailable),
            ):
                cco_hook._owner(payload)

    def test_protected_subagent_result_is_recovered_from_trusted_rollout(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000004"
        result = "CCO_RESULT cco.v9\n{\"status\":\"complete\"}"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "agent_path": "/root/worker_n01",
                        "id": agent_id,
                        "parent_thread_id": "parent-task",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": result,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": result}],
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            calls: list[tuple[str, object]] = []

            class Control:
                @staticmethod
                def process_result_event(owner: str, raw_result: object) -> None:
                    calls.append((owner, raw_result))

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(cco_hook, "_control", return_value=Control()),
            ):
                outcome = cco_hook.evaluate(
                    {
                        "agent_id": agent_id,
                        "agent_transcript_path": str(rollout),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": "gAAAA" + ("a" * 100),
                        "session_id": "parent-task",
                    }
                )

        self.assertEqual(calls, [("/root/worker_n01", result)])
        self.assertEqual(outcome, {"continue": False})

    def test_partial_protected_result_tail_requests_a_retry(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000010"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            metadata = {
                "type": "session_meta",
                "payload": {
                    "agent_path": "/root/worker_n01",
                    "id": agent_id,
                    "parent_thread_id": "parent-task",
                },
            }
            partial_final = {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "CCO_RESULT cco.v9\n{\"status\":\"complete\"}",
                },
            }
            rollout.write_text(
                json.dumps(metadata) + "\n" + json.dumps(partial_final),
                encoding="utf-8",
            )
            processed: list[tuple[str, object]] = []

            class Control:
                @staticmethod
                def process_result_event(owner: str, raw_result: object) -> None:
                    processed.append((owner, raw_result))

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(cco_hook, "_control", return_value=Control()),
            ):
                outcome = cco_hook.evaluate(
                    {
                        "agent_id": agent_id,
                        "agent_transcript_path": str(rollout),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": "gAAAA" + ("a" * 100),
                        "session_id": "parent-task",
                    }
                )

        self.assertEqual(processed, [])
        self.assertEqual(outcome["decision"], "block")
        self.assertIn("exact same result", outcome["reason"])

    def test_protected_subagent_stop_uses_latest_rollout_result_for_reused_owner(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000005"
        historical = "CCO_RESULT cco.v9\n{\"status\":\"historical\"}"
        current = "CCO_RESULT cco.v9\n{\"status\":\"current\"}"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "agent_path": "/root/worker_n01",
                        "id": agent_id,
                        "parent_thread_id": "parent-task",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": historical,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": current}],
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            calls: list[tuple[str, object]] = []

            class Control:
                @staticmethod
                def process_result_event(owner: str, raw_result: object) -> None:
                    calls.append((owner, raw_result))

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(cco_hook, "_control", return_value=Control()),
            ):
                outcome = cco_hook.evaluate(
                    {
                        "agent_id": agent_id,
                        "agent_transcript_path": str(rollout),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": "gAAAA" + ("a" * 100),
                        "session_id": "parent-task",
                    }
                )

        self.assertEqual(calls, [("/root/worker_n01", current)])
        self.assertEqual(outcome, {"continue": False})

    def test_protected_subagent_stop_fences_non_cco_latest_rollout_final(self) -> None:
        agent_id = "00000000-0000-4000-8000-000000000007"
        historical = "CCO_RESULT cco.v9\n{\"status\":\"historical\"}"
        current = "I completed the work, but omitted the required result protocol."
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{agent_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "agent_path": "/root/worker_n01",
                        "id": agent_id,
                        "parent_thread_id": "parent-task",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": historical,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": historical}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": current,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": current}],
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            processed: list[tuple[str, object]] = []
            fenced: list[str] = []

            class Control:
                @staticmethod
                def process_result_event(owner: str, raw_result: object) -> None:
                    processed.append((owner, raw_result))

                @staticmethod
                def fence_invalid_result(owner: str) -> None:
                    fenced.append(owner)

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(cco_hook, "_control", return_value=Control()),
            ):
                outcome = cco_hook.evaluate(
                    {
                        "agent_id": agent_id,
                        "agent_transcript_path": str(rollout),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": "gAAAA" + ("a" * 100),
                        "session_id": "parent-task",
                    }
                )

        self.assertEqual(processed, [])
        self.assertEqual(fenced, ["/root/worker_n01"])
        self.assertEqual(outcome["continue"], False)
        self.assertIn("rejected and fenced", outcome["systemMessage"])

    def test_missing_protected_transcript_result_fences_managed_owner(self) -> None:
        fenced: list[str] = []

        class Control:
            @staticmethod
            def process_result_event(_owner: str, _raw_result: object) -> None:
                raise AssertionError("missing transcript output must not be processed")

            @staticmethod
            def fence_invalid_result(owner: str) -> None:
                fenced.append(owner)

        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(cco_hook, "_owner", return_value="/root/worker_n01"),
            patch.object(
                cco_hook,
                "_result_from_transcript",
                side_effect=cco_hook.ControlPlaneError(
                    "protected child result has no final CCO_RESULT"
                ),
            ),
        ):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "gAAAA" + ("a" * 100),
                }
            )

        self.assertEqual(fenced, ["/root/worker_n01"])
        self.assertEqual(outcome["continue"], False)
        self.assertIn("rejected and fenced", outcome["systemMessage"])

    def test_protected_subagent_result_accepts_canonical_agent_path_identity(self) -> None:
        agent_id = "/root/worker_n01"
        rollout_id = "00000000-0000-4000-8000-000000000006"
        result = "CCO_RESULT cco.v9\n{\"status\":\"complete\"}"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{rollout_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "agent_path": agent_id,
                        "id": rollout_id,
                        "parent_thread_id": "parent-task",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": result,
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            calls: list[tuple[str, object]] = []

            class Control:
                @staticmethod
                def process_result_event(owner: str, raw_result: object) -> None:
                    calls.append((owner, raw_result))

            with (
                patch.object(cco_hook, "_sessions_root", return_value=sessions.resolve()),
                patch.object(cco_hook, "_control", return_value=Control()),
            ):
                outcome = cco_hook.evaluate(
                    {
                        "agent_id": agent_id,
                        "agent_transcript_path": str(rollout),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": "gAAAA" + ("a" * 100),
                        "session_id": "parent-task",
                    }
                )

        self.assertEqual(calls, [(agent_id, result)])
        self.assertEqual(outcome, {"continue": False})

    def test_opaque_followup_is_delegated_to_the_control_plane(self) -> None:
        calls: list[object] = []

        class Control:
            @staticmethod
            def owner_is_managed(_target: str) -> bool:
                return True

            @staticmethod
            def preflight_opaque_followup(payload: object) -> None:
                calls.append(payload)

        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "opaque-followup",
            "tool_input": {
                "message": "gAAAA" + ("a" * 100),
                "target": "/root/worker_n01",
            },
            "tool_name": "followup_task",
            "tool_use_id": "opaque-followup-call",
        }
        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(payload)
        self.assertEqual(outcome, {})
        self.assertEqual(calls, [payload])

    def test_opaque_followup_control_plane_rejection_blocks(self) -> None:
        class Control:
            @staticmethod
            def preflight_opaque_followup(_payload: object) -> None:
                raise cco_hook.ControlPlaneError("no prepared opaque follow-up")

        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "opaque-unmanaged-followup",
                    "tool_input": {
                        "message": "gAAAA" + ("a" * 100),
                        "target": "/root/not_prepared",
                    },
                    "tool_name": "followup_task",
                    "tool_use_id": "opaque-unmanaged-followup-call",
                }
            )

        self.assertEqual(outcome["decision"], "block")
        self.assertIn("no prepared", outcome["reason"])

    def test_unmappable_owner_closes_unresolved_leases(self) -> None:
        calls: list[str] = []

        class Control:
            @staticmethod
            def close_unmappable_owner_leases() -> int:
                calls.append("closed")
                return 1

        payload = {
            "agent_id": "00000000-0000-4000-8000-000000000003",
            "hook_event_name": "SubagentStop",
            "session_id": "unmappable-owner-hook",
        }
        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(
                cco_hook,
                "_owner",
                side_effect=cco_hook.ControlPlaneError("missing trusted owner"),
            ),
        ):
            outcome = cco_hook.evaluate(payload)

        self.assertEqual(calls, ["closed"])
        self.assertFalse(outcome["continue"])
        self.assertIn("closed", outcome["systemMessage"])

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

    def test_opaque_spawn_is_delegated_to_the_control_plane(self) -> None:
        calls: list[tuple[object, bool]] = []

        class Control:
            @staticmethod
            def preflight_spawn(payload: object, *, opaque_message: bool = False) -> None:
                calls.append((payload, opaque_message))

        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "hook-session",
            "tool_input": {
                "agent_type": "cost_orchestrator_write_leaf",
                "fork_turns": "none",
                "message": "gAAAA" + ("c" * 100),
                "model": "gpt-5.6-terra",
                "reasoning_effort": "max",
                "task_name": "worker_task_deadbeef_terra_max_g01",
            },
            "tool_name": "spawn_agent",
            "tool_use_id": "opaque-spawn",
        }

        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(payload)

        self.assertEqual(outcome, {})
        self.assertEqual(calls, [(payload, True)])

    def test_opaque_postflight_is_delegated_to_the_control_plane(self) -> None:
        calls: list[tuple[object, object]] = []

        class Control:
            @staticmethod
            def process_postflight_event(payload: object, **kwargs: object) -> None:
                calls.append((payload, kwargs))

        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "opaque-postflight",
            "tool_input": {"message": "gAAAA" + ("d" * 100)},
            "tool_name": "spawn_agent",
            "tool_use_id": "opaque-postflight-call",
        }

        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(payload)

        self.assertEqual(outcome, {})
        self.assertEqual(calls, [(payload, {"opaque_message": True})])

    def test_opaque_unmanaged_message_postflight_stays_unrelated(self) -> None:
        outcome = cco_hook.evaluate(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "opaque-unmanaged-postflight",
                "tool_input": {
                    "message": "gAAAA" + ("d" * 100),
                    "target": "/root/unmanaged",
                },
                "tool_name": "send_message",
                "tool_use_id": "opaque-unmanaged-postflight-call",
            }
        )

        self.assertEqual(outcome, {})

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

    def test_posttool_internal_budget_is_below_host_timeout(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        host_timeout = manifest["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"]
        self.assertLess(cco_hook.POSTTOOL_INTERNAL_BUDGET_SECONDS, host_timeout)
        control = cco_hook._control(
            {"hook_event_name": "PostToolUse", "session_id": "budgeted-post-hook"}
        )
        self.assertLess(control.lock_timeout, cco_hook.POSTTOOL_INTERNAL_BUDGET_SECONDS)

    def test_subagent_stop_budget_leaves_host_settlement_reserve(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        host_timeout = manifest["hooks"]["SubagentStop"][0]["hooks"][0]["timeout"]
        self.assertLess(cco_hook.SUBAGENT_STOP_INTERNAL_BUDGET_SECONDS, host_timeout)
        control = cco_hook._control(
            {"hook_event_name": "SubagentStop", "session_id": "result-budget"}
        )
        self.assertLessEqual(control.lock_timeout, 10)

    def test_session_and_stop_budgets_are_below_host_timeouts(self) -> None:
        manifest = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        session_timeout = manifest["hooks"]["SessionStart"][0]["hooks"][0]["timeout"]
        stop_timeout = manifest["hooks"]["Stop"][0]["hooks"][0]["timeout"]

        self.assertLess(cco_hook.SESSION_START_INTERNAL_BUDGET_SECONDS, session_timeout)
        self.assertLess(cco_hook.STOP_INTERNAL_BUDGET_SECONDS, stop_timeout)

    def test_postflight_delegates_only_success_settlement(self) -> None:
        class Control:
            @staticmethod
            def process_postflight_event(_payload: object) -> None:
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

    def test_followup_task_routes_a_new_cco_task_to_reuse_preflight(self) -> None:
        calls: list[object] = []

        class Control:
            @staticmethod
            def preflight_reuse(payload: object) -> None:
                calls.append(payload)

        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "hook-session",
            "tool_input": {
                "message": "CCO_TASK cco.v9\n{}",
                "target": "/root/worker_first_terra_max_g01_abcd1234",
            },
            "tool_name": "followup_task",
            "tool_use_id": "reuse-call",
        }
        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(payload)

        self.assertEqual(outcome, {})
        self.assertEqual(calls, [payload])

    def test_stop_blocks_only_the_first_stop_event_while_work_is_active(self) -> None:
        class Control:
            @staticmethod
            def pending_event_reason() -> None:
                return None

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
            def process_restart_event(_source: str) -> int:
                raise AssertionError("compact must not run restart recovery")

        with patch.object(cco_hook, "_control", return_value=Control()):
            compact = cco_hook.evaluate(
                {"hook_event_name": "SessionStart", "source": "compact"}
            )
        self.assertEqual(compact, {})

    def test_session_resume_fences_native_work_lost_by_restart(self) -> None:
        class Control:
            @staticmethod
            def process_restart_event(_source: str) -> int:
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
            def process_result_event(owner: str, result: object) -> None:
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
            def process_result_event(owner: str, result: object) -> None:
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
            def process_result_event(_owner: str, _result: object) -> None:
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
            def preflight_interrupt(payload: object) -> bool:
                calls.append(("pre", payload))
                return True

            @staticmethod
            def process_postflight_event(payload: object) -> bool:
                calls.append(("post", payload))
                return True

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

            @staticmethod
            def preflight_interrupt(_payload: object) -> bool:
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

    def test_ambiguous_session_state_blocks_raw_managed_message(self) -> None:
        class Control:
            @staticmethod
            def owner_is_managed(_owner: str) -> bool:
                raise cco_hook.ControlPlaneError(
                    "current task has lifecycle state in multiple workspaces"
                )

        with patch.object(cco_hook, "_control", return_value=Control()):
            outcome = cco_hook.evaluate(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "ambiguous-session",
                    "tool_input": {
                        "message": "raw override",
                        "target": "/root/worker_n01",
                    },
                    "tool_name": "send_message",
                    "tool_use_id": "raw-message",
                }
            )

        self.assertEqual(outcome["decision"], "block")
        self.assertIn("multiple workspaces", outcome["reason"])

    def test_predecessor_artifacts_require_cleanup_for_one_shot_hook_events(self) -> None:
        class Control:
            @staticmethod
            def process_restart_event(_source: str) -> int:
                raise cco_hook.ControlPlaneUnavailable(
                    "unsupported predecessor CCO artifact; clean up the old CCO state"
                )

            @staticmethod
            def process_postflight_event(_payload: object) -> bool:
                raise cco_hook.ControlPlaneUnavailable(
                    "unsupported predecessor CCO artifact; clean up the old CCO state"
                )

            @staticmethod
            def process_result_event(_owner: str, _result: object) -> None:
                raise cco_hook.ControlPlaneUnavailable(
                    "unsupported predecessor CCO artifact; clean up the old CCO state"
                )

        with (
            patch.object(cco_hook, "_control", return_value=Control()),
            patch.object(cco_hook, "_owner", return_value="/root/worker_n01"),
        ):
            session = cco_hook.evaluate(
                {"hook_event_name": "SessionStart", "source": "resume"}
            )
            post = cco_hook.evaluate(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_input": {"message": "CCO_TASK cco.v9\n{}"},
                    "tool_name": "spawn_agent",
                }
            )
            result = cco_hook.evaluate(
                {
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "CCO_RESULT cco.v9\n{}",
                }
            )

        self.assertIn("clean up", session["systemMessage"])
        self.assertIn("clean up", post["reason"])
        self.assertIn("exact same result", result["reason"])


if __name__ == "__main__":
    unittest.main()

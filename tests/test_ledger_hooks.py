from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(SCRIPTS))

import ledger_runtime  # noqa: E402
from packet_compiler import (  # noqa: E402
    compile_continuation,
    compile_dispatch,
    compile_result,
    parse_message,
    result_sha256,
)
from protocol_hash import canonical_bytes  # noqa: E402
from task_ledger import LedgerConflict  # noqa: E402
from tests.v6_test_support import fixed_route_plan  # noqa: E402


def forged_result(capsule: dict[str, object], *, status: str, disposition: str) -> str:
    result: dict[str, object] = {
        "dispatch_sha256": capsule["capsule_sha256"],
        "disposition": disposition,
        "payload": {},
        "protocol": "cco.v6",
        "status": status,
    }
    result["result_sha256"] = result_sha256(result)
    return (
        "CCO_RESULT cco.v6\n"
        f"RESULT_SHA256: {result['result_sha256']}\n"
        f"RESULT_JSON: {canonical_bytes(result).decode('utf-8')}"
    )


class LedgerHookBehaviorTests(unittest.TestCase):
    def test_v6_compiler_capsule_owns_and_retires_one_native_result(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_v6", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_v6",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        capsule = parse_message(native["message"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {
                "session_id": "session-v6",
                "cwd": str(ROOT),
                "tool_use_id": "spawn-v6",
            }
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn(
                {**payload, "tool_response": {"task_name": native["task_name"]}}
            )
            result_fields = ledger_runtime.result_claim_from_message(
                compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    changed=["src/policy.py"],
                )
            )
            ledger_runtime.accept_subagent_result(
                {
                    **payload,
                    "agent_type": native["agent_type"],
                    "agent_id": "/root/" + native["task_name"],
                },
                result_fields,
            )

            ledger = ledger_runtime.ledger_for(payload)
            self.assertTrue(ledger.path.exists())
            self.assertTrue(
                ledger.is_managed_owner("/root/" + str(native["task_name"]))
            )

    def test_v6_review_continue_keeps_exact_owner_continuable(self) -> None:
        native = compile_dispatch(
            {
                "acceptance": {"mode": "independent"},
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "review_e01", "objective": "judge evidence"},
                "current_state": "sha256:" + "c" * 64,
                "epoch": "e01",
                "evidence": {"records": []},
                "judgment": "complex",
                "kind": "review",
                "mode": "fresh",
                "node": "review_e01",
                "purpose": "acceptance",
                "route_plan": fixed_route_plan(
                    purpose="acceptance",
                    judgment="complex",
                    model="gpt-5.6-terra",
                ),
            }
        )
        capsule = parse_message(native["message"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {
                "session_id": "session-v6-review",
                "cwd": str(ROOT),
                "tool_use_id": "spawn-v6-review",
            }
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn(
                {**payload, "tool_response": {"task_name": native["task_name"]}}
            )
            result_fields = ledger_runtime.result_claim_from_message(
                compile_result(
                    capsule,
                    status="complete",
                    disposition="continue",
                    findings=["F01"],
                )
            )
            ledger_runtime.accept_subagent_result(
                {
                    **payload,
                    "agent_type": native["agent_type"],
                    "agent_id": "/root/" + native["task_name"],
                },
                result_fields,
            )

            row = ledger_runtime.ledger_for(payload).read_rows()[0]
            self.assertEqual(row["state"], "continuable")

    def test_retired_result_stays_fenced_until_session_end(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_fenced", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_fenced",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        capsule = parse_message(native["message"])
        owner = "/root/" + native["task_name"]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {
                "session_id": "session-fenced",
                "cwd": str(ROOT),
                "tool_use_id": "spawn-fenced",
            }
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn(
                {**payload, "tool_response": {"task_name": native["task_name"]}}
            )
            result = ledger_runtime.result_claim_from_message(
                compile_result(capsule, status="complete", disposition="retire")
            )
            ledger_runtime.accept_subagent_result(
                {
                    **payload,
                    "agent_type": native["agent_type"],
                    "agent_id": owner,
                },
                result,
            )

            ledger = ledger_runtime.ledger_for(payload)
            self.assertTrue(ledger.path.exists())
            self.assertTrue(ledger.is_managed_owner(owner))
            ledger_runtime.evaluate(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "session-fenced",
                    "cwd": str(ROOT),
                }
            )
            self.assertFalse(ledger.path.exists())

    def test_only_a_complete_review_read_leaf_can_return_accept(self) -> None:
        worker = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_accept_worker", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_accept_worker",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        review = compile_dispatch(
            {
                "acceptance": {"mode": "independent"},
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "review_e99", "objective": "judge evidence"},
                "current_state": "sha256:" + "c" * 64,
                "epoch": "e99",
                "evidence": {"records": []},
                "judgment": "complex",
                "kind": "review",
                "mode": "fresh",
                "node": "review_e99",
                "purpose": "acceptance",
                "route_plan": fixed_route_plan(
                    purpose="acceptance",
                    judgment="complex",
                    model="gpt-5.6-terra",
                ),
            }
        )
        cases = (
            (worker, "complete", "accept"),
            (review, "partial", "accept"),
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            for index, (native, status, disposition) in enumerate(cases):
                with self.subTest(kind=native["task_name"]):
                    capsule = parse_message(native["message"])
                    payload = {
                        "session_id": "session-accept",
                        "cwd": str(ROOT),
                        "tool_use_id": f"spawn-accept-{index}",
                    }
                    ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
                    ledger_runtime.postflight_spawn(
                        {**payload, "tool_response": {"task_name": native["task_name"]}}
                    )
                    claim = ledger_runtime.result_claim_from_message(
                        forged_result(capsule, status=status, disposition=disposition)
                    )
                    with self.assertRaises(LedgerConflict):
                        ledger_runtime.accept_subagent_result(
                            {
                                **payload,
                                "agent_type": native["agent_type"],
                                "agent_id": "/root/" + str(native["task_name"]),
                            },
                            claim,
                        )

    def test_v6_continuation_reserves_and_advances_one_cursor(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_v6", "objective": "bounded result"},
                "judgment": "complex",
                "kind": "work",
                "node": "n01_v6",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(
                    judgment="complex", model="gpt-5.6-terra"
                ),
            }
        )
        capsule = parse_message(native["message"])
        target = "/root/" + native["task_name"]
        continuation = compile_continuation(
            capsule,
            target=target,
            delta={"request": "use the observed failure evidence"},
        )
        continued = parse_message(continuation["message"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {
                "session_id": "session-v6-cursor",
                "cwd": str(ROOT),
                "tool_use_id": "spawn-v6-cursor",
            }
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn(
                {**payload, "tool_response": {"task_name": native["task_name"]}}
            )
            call = {
                **payload,
                "tool_use_id": "continue-v6-cursor",
                "tool_input": continuation,
            }
            ledger_runtime.preflight_continuation(call, continued)
            ledger_runtime.postflight_continuation(
                {**call, "tool_response": {"ok": True}}
            )

            row = ledger_runtime.ledger_for(payload).read_rows()[0]
            self.assertEqual(row["generation"], 1)
            self.assertEqual(row["cursor"], 1)
            self.assertEqual(row["input_sha256"], continued["capsule_sha256"])

    def test_codex_lifecycle_events_share_one_hook_adapter(self) -> None:
        config = json.loads(
            (HOOKS / "hooks.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(config)
        self.assertNotIn("agent_postflight.py", serialized)
        self.assertNotIn("interrupt_preflight.py", serialized)
        self.assertNotIn("ledger_cleanup.py", serialized)
        self.assertNotIn("Stop", config["hooks"])
        self.assertIn("SessionEnd", config["hooks"])
        self.assertGreaterEqual(serialized.count("ledger_runtime.py"), 4)

    def test_ledger_lookup_does_not_run_stale_directory_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ), mock.patch.object(
            ledger_runtime.TaskLedger,
            "cleanup_stale",
            side_effect=AssertionError("hot-path stale scan"),
        ):
            ledger = ledger_runtime.ledger_for(
                {"session_id": "session-hot", "cwd": str(ROOT)}
            )
            self.assertIsNotNone(ledger)

    def test_nested_cwd_cannot_hide_an_in_repository_ledger_root(self) -> None:
        configured = ROOT / "in-repository-ledger"
        with mock.patch.dict(os.environ, {"CCO_LEDGER_DIR": str(configured)}):
            with self.assertRaises(ValueError):
                ledger_runtime._ledger_root({"cwd": str(ROOT / "tests")})

    def test_rejected_v6_spawn_releases_the_reservation(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_reject", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_reject",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        capsule = parse_message(native["message"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {"session_id": "session-reject", "cwd": str(ROOT), "tool_use_id": "spawn-reject"}
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn({**payload, "tool_response": {"status": "rejected"}})
            self.assertEqual(ledger_runtime.ledger_for(payload).read_rows(), [])

    def test_postflight_ignores_ordinary_messages_without_a_pending_continuation(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_ordinary", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_ordinary",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        capsule = parse_message(native["message"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            spawn = {
                "session_id": "session-ordinary",
                "cwd": str(ROOT),
                "tool_use_id": "spawn-ordinary",
            }
            ledger_runtime.reserve_spawn(spawn, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn(
                {**spawn, "tool_response": {"task_name": native["task_name"]}}
            )
            for tool_name in ("send_message", "followup_task"):
                with self.subTest(tool_name=tool_name):
                    outcome = ledger_runtime.evaluate(
                        {
                            "hook_event_name": "PostToolUse",
                            "tool_name": tool_name,
                            "session_id": "session-ordinary",
                            "cwd": str(ROOT),
                            "tool_use_id": f"ordinary-{tool_name}",
                            "tool_input": {
                                "target": "/root/ordinary_worker",
                                "message": "ordinary message",
                            },
                            "tool_response": {"ok": True},
                        }
                    )
                    self.assertEqual(outcome, {})
            row = ledger_runtime.ledger_for(spawn).read_rows()[0]
            self.assertEqual(row["state"], "owned")

    def test_interrupt_and_late_v6_result_are_fenced(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_late", "objective": "bounded result"},
                "judgment": "routine",
                "kind": "work",
                "node": "n01_late",
                "purpose": "implementation",
                "route_plan": fixed_route_plan(),
            }
        )
        capsule = parse_message(native["message"])
        owner = "/root/" + native["task_name"]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            payload = {"session_id": "session-late", "cwd": str(ROOT), "tool_use_id": "spawn-late"}
            ledger_runtime.reserve_spawn(payload, capsule, native["agent_type"])
            ledger_runtime.postflight_spawn({**payload, "tool_response": {"task_name": native["task_name"]}})
            ledger_runtime.preflight_interrupt({**payload, "tool_input": {"target": owner}})
            claim = ledger_runtime.result_claim_from_message(
                compile_result(capsule, status="complete", disposition="retire")
            )
            with self.assertRaises(LedgerConflict):
                ledger_runtime.accept_subagent_result(
                    {**payload, "agent_type": native["agent_type"], "agent_id": owner},
                    claim,
                )


if __name__ == "__main__":
    unittest.main()

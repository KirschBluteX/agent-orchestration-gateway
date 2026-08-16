from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "agent-orchestration-gateway" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from control_plane import (  # noqa: E402
    RESULT_HEADER,
    TASK_HEADER,
    ControlPlane,
    ControlPlaneError,
    ControlPlaneUnavailable,
    _select_units,
    parse_result,
    parse_task_message,
)
import control_plane as control_plane_module  # noqa: E402
from operation_deadline import OperationDeadlineExceeded  # noqa: E402
from state_lock import StateLockBusy, acquire  # noqa: E402
from workspace_guard import (  # noqa: E402
    WorkspaceGuardError,
)
import workspace_guard as workspace_guard_module  # noqa: E402
from workspace_state import StateError  # noqa: E402


def catalog(
    *models: str, efforts: tuple[str, ...] = ("max",)
) -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": model,
                "supported_reasoning_levels": [
                    {"effort": effort} for effort in efforts
                ],
            }
            for model in models
        ]
    }


def result_text(
    dispatch_id: str,
    *,
    cursor: int = 0,
    changed_paths: list[str] | None = None,
    evidence: dict[str, str] | None = None,
    status: str = "complete",
    outcome: str = "retire",
    blockers: list[str] | None = None,
    deviations: list[str] | None = None,
    failure_signature: str | None = None,
) -> str:
    return RESULT_HEADER + "\n" + json.dumps(
        {
            "blockers": blockers or [],
            "changed_paths": changed_paths or [],
            "cursor": cursor,
            "deviations": deviations or [],
            "dispatch_id": dispatch_id,
            "evidence": evidence or {},
            "failure_signature": failure_signature,
            "outcome": outcome,
            "status": status,
            "summary": "bounded result",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "a.txt").write_text("a0\n", encoding="utf-8")
        (self.repo / "b.txt").write_text("b0\n", encoding="utf-8")
        (self.repo / "c.txt").write_text("c0\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def control(self, session: str = "session-aog") -> ControlPlane:
        return ControlPlane(session, root=self.state_root)

    def brief(self, nodes: list[dict[str, object]]) -> dict[str, object]:
        acceptance = {
            acceptance_id: f"criterion {acceptance_id}"
            for node in nodes
            for acceptance_id in node["acceptance"]
        }
        return {"goal": "exercise aog.v1", "acceptance": acceptance, "nodes": nodes}

    def canonical_dag(self, nodes: list[dict[str, object]]) -> dict[str, object]:
        return {
            "authority": "delegated",
            "clarification_required": False,
            "closed": True,
            "declared_tools": [],
            "direct": False,
            "protocol": "aog.delegation.v1",
            "upper_bound_seconds": 30,
            "work": {"kind": "dag", "plan": self.brief(nodes)},
        }

    @staticmethod
    def node(
        node_id: str,
        acceptance_id: str,
        path: str,
        *,
        role: str = "worker",
        decision: str = "bounded",
        depends_on: list[str] | None = None,
        review_of: str | None = None,
    ) -> dict[str, object]:
        node: dict[str, object] = {
            "acceptance": [acceptance_id],
            "decision": decision,
            "depends_on": depends_on or [],
            "id": node_id,
            "objective": f"complete {node_id}",
            "role": role,
            "scopes": [{"kind": "exact", "path": path}],
        }
        if review_of is not None:
            node["review_of"] = review_of
        return node

    def start_dispatch(self, control: ControlPlane, native: dict[str, object]) -> tuple[str, str]:
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        tool_use_id = "call-" + dispatch_id[7:19]
        control.preflight_spawn(
            {
                "tool_input": native,
                "tool_use_id": tool_use_id,
            }
        )
        owner = "/root/" + str(native["task_name"])
        control.postflight_tool(
            {
                "tool_input": native,
                "tool_response": {"task_name": owner},
                "tool_use_id": tool_use_id,
            }
        )
        return dispatch_id, owner

    def test_trusted_host_opaque_spawn_binds_the_observed_ciphertext(self) -> None:
        control = self.control("opaque-native")
        control.create_plan(
            self.repo,
            self.brief([self.node("opaque", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        opaque = {**native, "message": "gAAAA" + ("d" * 100)}

        control.preflight_spawn(
            {"tool_input": opaque, "tool_use_id": "opaque-call"},
            opaque_message=True,
        )

        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        receipt = control._native_attempt_for_dispatch(dispatch)
        self.assertIsNotNone(receipt)
        self.assertEqual(
            receipt["tool_input_sha256"],
            control_plane_module._digest(b"aog.native-input.v1\0", opaque),
        )
        self.assertNotEqual(
            receipt["tool_input_sha256"],
            control_plane_module._digest(b"aog.native-input.v1\0", native),
        )
        owner = "/root/" + str(native["task_name"])
        substituted = {**opaque, "message": "gAAAA" + ("x" * 100)}
        with self.assertRaisesRegex(ControlPlaneError, "input changed"):
            control.preflight_spawn(
                {"tool_input": substituted, "tool_use_id": "opaque-call"},
                opaque_message=True,
            )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][dispatch_id]["receipt_id"], receipt["event_id"])
        with self.assertRaisesRegex(ControlPlaneError, "no matching preflight"):
            control.process_postflight_event(
                {
                    "tool_input": substituted,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": "opaque-call",
                },
                opaque_message=True,
            )
        with patch.dict(os.environ, {"AOG_OPAQUE_MESSAGE_POLICY": "strict"}):
            control.process_postflight_event(
                {
                    "tool_input": opaque,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": "opaque-call",
                },
                opaque_message=True,
            )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][dispatch_id]["state"], "running")
        self.assertEqual(state["dispatches"][dispatch_id]["owner"], owner)

    def test_trusted_host_opaque_reuse_binds_one_prepared_owner(self) -> None:
        control, _first_id, owner = self.ready_reuse_chain("opaque-reuse")
        action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        self.assertEqual(action["action"], "reuse_owner")
        native = action["tool_input"]
        opaque = {**native, "message": "gAAAA" + ("r" * 100)}
        control.preflight_opaque_followup(
            {"tool_input": opaque, "tool_use_id": "opaque-reuse-call"}
        )
        changed = {**opaque, "message": "gAAAA" + ("s" * 100)}
        with self.assertRaisesRegex(ControlPlaneError, "input changed"):
            control.preflight_opaque_followup(
                {"tool_input": changed, "tool_use_id": "opaque-reuse-call"}
            )
        control.process_postflight_event(
            {
                "tool_input": opaque,
                "tool_response": {},
                "tool_use_id": "opaque-reuse-call",
            },
            opaque_message=True,
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        running = [
            item
            for item in state["dispatches"].values()
            if item.get("owner") == owner and item.get("state") == "running"
        ]
        self.assertEqual(len(running), 1)

    def test_consumed_opaque_attempt_cannot_bind_a_later_reuse(self) -> None:
        control = self.control("opaque-cross-attempt")
        nodes = [
            self.node("first", "A01", "a.txt"),
            self.node("second", "A02", "a.txt", depends_on=["first"]),
            self.node("third", "A03", "a.txt", depends_on=["second"]),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        first_action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        first_id, owner = self.start_dispatch(control, first_action["tool_input"])
        control.record_result(
            owner,
            result_text(first_id, evidence={"A01": "first complete"}),
        )

        second_action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        second_native = second_action["tool_input"]
        second_opaque = {**second_native, "message": "gAAAA" + ("r" * 100)}
        second_payload = {
            "tool_input": second_opaque,
            "tool_use_id": "opaque-replayed-call",
        }
        control.preflight_opaque_followup(second_payload)
        control.process_postflight_event(
            {**second_payload, "tool_response": {}},
            opaque_message=True,
        )
        second_id = parse_task_message(second_native["message"])["dispatch_id"]
        control.record_result(
            owner,
            result_text(second_id, evidence={"A02": "second complete"}),
        )

        third_action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        third_native = third_action["tool_input"]
        self.assertEqual(third_native["target"], owner)
        replayed = {**second_opaque, "target": owner}
        with self.assertRaisesRegex(ControlPlaneError, "reuses a completed"):
            control.preflight_opaque_followup(
                {
                    "tool_input": replayed,
                    "tool_use_id": "opaque-replayed-call",
                }
            )
        with self.assertRaisesRegex(ControlPlaneError, "reuses a completed"):
            control.preflight_opaque_followup(
                {
                    "tool_input": replayed,
                    "tool_use_id": "opaque-new-call",
                }
            )
        with self.assertRaisesRegex(ControlPlaneError, "no matching preflight"):
            control.process_postflight_event(
                {**second_payload, "tool_response": {}},
                opaque_message=True,
            )
        third_id = parse_task_message(third_native["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][third_id]["state"], "starting")
        self.assertIsNone(state["dispatches"][third_id]["receipt_id"])

    def test_trusted_host_opaque_continuation_advances_the_cursor(self) -> None:
        control = self.control("opaque-continuation")
        control.create_plan(
            self.repo,
            self.brief([self.node("paused", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need one fact"],
                failure_signature="missing_fact",
            ),
        )
        prepared = control.prepare_continuation(dispatch_id, {"fact": "known"})
        continuation = prepared["tool_input"]
        opaque = {**continuation, "message": "gAAAA" + ("c" * 100)}
        control.preflight_opaque_followup(
            {"tool_input": opaque, "tool_use_id": "opaque-continue-call"}
        )
        changed = {**opaque, "message": "gAAAA" + ("t" * 100)}
        with self.assertRaisesRegex(ControlPlaneError, "input changed"):
            control.preflight_opaque_followup(
                {"tool_input": changed, "tool_use_id": "opaque-continue-call"}
            )
        control.process_postflight_event(
            {
                "tool_input": opaque,
                "tool_response": {},
                "tool_use_id": "opaque-continue-call",
            },
            opaque_message=True,
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertEqual(dispatch["state"], "running")
        self.assertEqual(dispatch["cursor"], 1)

    def test_strict_policy_rejects_opaque_spawn_before_receipt(self) -> None:
        control = self.control("opaque-native-strict")
        control.create_plan(
            self.repo,
            self.brief([self.node("opaque", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        opaque = {**native, "message": "gAAAA" + ("s" * 100)}
        with (
            patch.dict(os.environ, {"AOG_OPAQUE_MESSAGE_POLICY": "strict"}),
            self.assertRaisesRegex(ControlPlaneError, "strict policy"),
        ):
            control.preflight_spawn(
                {"tool_input": opaque, "tool_use_id": "opaque-call"},
                opaque_message=True,
            )
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["dispatches"][dispatch_id]["receipt_id"])

    def ready_reuse_chain(
        self,
        session: str,
        *,
        first_path: str = "a.txt",
        second_path: str = "a.txt",
    ) -> tuple[ControlPlane, str, str]:
        control = self.control(session)
        nodes = [
            self.node("first", "A01", first_path),
            self.node("second", "A02", second_path, depends_on=["first"]),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        first_action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        self.assertEqual(first_action["action"], "spawn_new_owner")
        first_id, owner = self.start_dispatch(control, first_action["tool_input"])
        control.record_result(
            owner,
            result_text(first_id, evidence={"A01": "first complete"}),
        )
        return control, first_id, owner

    def replace_active_wave_with_unsupported_protocol(self, control: ControlPlane) -> None:
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        current_wave_id = state["active_wave_id"]
        current_wave_path = control._artifact_path("wave", current_wave_id)
        wave = json.loads(current_wave_path.read_text(encoding="utf-8"))
        wave["protocol"] = "aog.wave.v0"
        unsupported_wave_id = "sha256:" + "0" * 64
        wave["wave_id"] = unsupported_wave_id
        control._artifact_path("wave", unsupported_wave_id).write_text(
            json.dumps(wave, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        state["active_wave_id"] = unsupported_wave_id
        for dispatch in state["dispatches"].values():
            if dispatch["wave_id"] != current_wave_id:
                continue
            dispatch["wave_id"] = unsupported_wave_id
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def cooperative_actions(
        self, session: str
    ) -> tuple[ControlPlane, list[dict[str, object]]]:
        control = self.control(session)
        plan = self.brief(
            [
                self.node("left", "A01", "a.txt"),
                self.node("right", "A02", "b.txt"),
            ]
        )
        plan["writer_isolation"] = "cooperative"
        control.create_plan(self.repo, plan)
        actions = control.next_wave(
            capacity=2,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"]
        self.assertEqual(len(actions), 2)
        return control, actions

    def cooperative_actions_for_nodes(
        self,
        session: str,
        nodes: list[dict[str, object]],
        *,
        capacity: int,
    ) -> tuple[ControlPlane, list[dict[str, object]]]:
        control = self.control(session)
        plan = self.brief(nodes)
        plan["writer_isolation"] = "cooperative"
        control.create_plan(self.repo, plan)
        actions = control.next_wave(
            capacity=capacity,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"]
        return control, actions

    def assert_no_live_batch_receipts(self, control: ControlPlane) -> None:
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        batch = [
            dispatch
            for dispatch in state["dispatches"].values()
            if isinstance(dispatch.get("isolation"), dict)
            and dispatch["isolation"].get("mode") == "cooperative"
        ]
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(item["receipt_id"] is None for item in batch))
        self.assertTrue(all(item["interrupt_receipt_id"] is None for item in batch))
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_direct_dependency_reuses_same_owner_with_a_fresh_dispatch(self) -> None:
        control, first_id, owner = self.ready_reuse_chain("reuse-direct")

        action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]

        self.assertEqual(action["action"], "reuse_owner")
        self.assertEqual(action["tool_name"], "followup_task")
        self.assertEqual(action["tool_input"]["target"], owner)
        second_task = parse_task_message(action["tool_input"]["message"])
        self.assertNotEqual(second_task["dispatch_id"], first_id)
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][second_task["dispatch_id"]]
        self.assertEqual(dispatch["reused_from"], first_id)
        self.assertEqual(dispatch["tool_kind"], "reuse")

        control.preflight_reuse(
            {"tool_input": action["tool_input"], "tool_use_id": "reuse-call"}
        )
        control.postflight_tool(
            {
                "tool_input": action["tool_input"],
                "tool_response": {"task_name": owner},
                "tool_use_id": "reuse-call",
            }
        )
        completed = control.record_result(
            owner,
            result_text(second_task["dispatch_id"], evidence={"A02": "second complete"}),
        )
        self.assertEqual(completed["state"], "retired")

    def test_reuse_predicate_requires_one_exact_clean_predecessor(self) -> None:
        dispatch_id = "sha256:" + "a" * 64
        route = {"effort": "max", "model": "gpt-5.6-terra"}
        scopes = [{"kind": "exact", "path": "a.txt"}]
        source = {
            "assurance": "bounded",
            "claim_expires_at": None,
            "context_turns": 0,
            "dispatch_id": dispatch_id,
            "fallback_from_owner": None,
            "generation": 1,
            "interrupt_claim_expires_at": None,
            "interrupt_receipt_id": None,
            "interrupt_tool_use_id": None,
            "interrupt_unresolved": False,
            "isolation": None,
            "last_transient_failure": None,
            "members": ["first"],
            "owner": "/root/worker_first",
            "pending_cursor": None,
            "receipt_id": None,
            "result": {
                "blockers": [],
                "deviations": [],
                "failure_signature": None,
                "outcome": "retire",
                "status": "complete",
            },
            "role": "worker",
            "route_candidates": [route],
            "route_cursor": 0,
            "scopes": scopes,
            "state": "retired",
            "tool_use_id": None,
            "transient_retries": 0,
        }
        arguments = {
            "dependency_dispatches": {dispatch_id},
            "dependency_member": "first",
            "role": "worker",
            "assurance": "bounded",
            "route": route,
            "scopes": scopes,
        }
        self.assertTrue(ControlPlane._source_matches_reuse(source, **arguments))
        mutations = (
            {"transient_retries": 1},
            {"generation": 2},
            {"receipt_id": "sha256:" + "b" * 64},
            {"interrupt_unresolved": True},
            {"members": ["other"]},
            {"route_candidates": [{"effort": "max", "model": "gpt-5.6-luna"}]},
            {"route_cursor": 1},
            {"scopes": [{"kind": "exact", "path": "b.txt"}]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(
                    ControlPlane._source_matches_reuse(
                        {**source, **mutation}, **arguments
                    )
                )
        self.assertFalse(
            ControlPlane._source_matches_reuse(
                source,
                **{**arguments, "dependency_dispatches": {dispatch_id, "sha256:" + "c" * 64}},
            )
        )

    def test_interrupt_after_owner_reuse_settles_only_the_active_dispatch(self) -> None:
        control, _first_id, owner = self.ready_reuse_chain("reuse-interrupt")
        action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        control.preflight_reuse(
            {"tool_input": action["tool_input"], "tool_use_id": "reuse-call"}
        )
        control.postflight_tool(
            {
                "tool_input": action["tool_input"],
                "tool_response": {"task_name": owner},
                "tool_use_id": "reuse-call",
            }
        )

        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-reuse"}
        )
        control.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "running"},
                "tool_use_id": "interrupt-reuse",
            }
        )

        status = control.status()
        self.assertEqual(status["counts"]["retired"], 1)
        self.assertEqual(status["counts"]["fenced"], 1)

    def test_reviewer_and_scope_expansion_never_reuse_an_owner(self) -> None:
        reviewer = self.control("reuse-reviewer")
        final_review = self.node(
            "final_review",
            "A01",
            "a.txt",
            role="reviewer",
            depends_on=["first"],
        )
        reviewer_nodes = [
            self.node("first", "A01", "a.txt", role="explorer"),
            final_review,
        ]
        reviewer.create_plan(self.repo, self.brief(reviewer_nodes))
        first = reviewer.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        first_id, owner = self.start_dispatch(reviewer, first["tool_input"])
        reviewer.record_result(
            owner,
            result_text(first_id, evidence={"A01": "inspected"}),
        )
        review_action = reviewer.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        self.assertEqual(review_action["action"], "spawn_new_owner")

        (self.repo / "src").mkdir()
        (self.repo / "src" / "owned.txt").write_text("owned\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/owned.txt"], cwd=self.repo, check=True)
        expanded = self.control("reuse-expanded-scope")
        first_node = self.node("first", "A01", "src/owned.txt")
        second_node = self.node("second", "A02", "src", depends_on=["first"])
        second_node["scopes"] = [{"kind": "prefix", "path": "src"}]
        expanded.create_plan(self.repo, self.brief([first_node, second_node]))
        first_action = expanded.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        first_id, owner = self.start_dispatch(expanded, first_action["tool_input"])
        expanded.record_result(
            owner,
            result_text(first_id, evidence={"A01": "bounded"}),
        )
        expanded_action = expanded.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        self.assertEqual(expanded_action["action"], "spawn_new_owner")

    def test_unavailable_reused_owner_falls_back_once_to_a_fresh_spawn(self) -> None:
        control, _first_id, owner = self.ready_reuse_chain("reuse-owner-unavailable")
        reuse = control.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        dispatch_id = parse_task_message(reuse["tool_input"]["message"])["dispatch_id"]
        control.preflight_reuse(
            {"tool_input": reuse["tool_input"], "tool_use_id": "missing-owner-call"}
        )

        fallback = control.settle_native_failure(dispatch_id, "owner_unavailable")

        self.assertEqual(fallback["action"], "spawn_new_owner")
        self.assertEqual(fallback["tool_name"], "spawn_agent")
        self.assertEqual(fallback["tool_input"]["message"], reuse["tool_input"]["message"])
        self.assertNotIn("target", fallback["tool_input"])
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertEqual(dispatch["tool_kind"], "spawn")
        self.assertIsNone(dispatch["owner"])
        self.assertEqual(dispatch["fallback_from_owner"], owner)
        control.preflight_spawn(
            {"tool_input": fallback["tool_input"], "tool_use_id": "fallback-spawn"}
        )

    def test_one_owner_is_reused_by_at_most_one_dispatch_in_a_wave(self) -> None:
        control = self.control("reuse-owner-reservation")
        nodes = [
            self.node("source", "A01", "a.txt", role="explorer"),
            self.node(
                "left",
                "A02",
                "a.txt",
                role="explorer",
                depends_on=["source"],
            ),
            self.node(
                "right",
                "A03",
                "a.txt",
                role="explorer",
                depends_on=["source"],
            ),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        source_action = control.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        source_id, owner = self.start_dispatch(control, source_action["tool_input"])
        control.record_result(
            owner,
            result_text(source_id, evidence={"A01": "source complete"}),
        )

        actions = control.next_wave(
            capacity=2, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"]

        self.assertEqual(
            sorted(item["action"] for item in actions),
            ["reuse_owner", "spawn_new_owner"],
        )

    def test_assurance_change_and_explicit_context_disable_owner_reuse(self) -> None:
        for session, mutate in (
            ("reuse-assurance-change", lambda node: node.update({"verification": "semantic"})),
            ("reuse-explicit-context", lambda node: node.update({"context_turns": 1})),
        ):
            with self.subTest(session=session):
                control = self.control(session)
                first = self.node("first", "A01", "a.txt")
                second = self.node("second", "A02", "a.txt", depends_on=["first"])
                mutate(second)
                control.create_plan(self.repo, self.brief([first, second]))
                first_action = control.next_wave(
                    capacity=1, native_catalog=catalog("gpt-5.6-terra")
                )["dispatches"][0]
                first_id, owner = self.start_dispatch(control, first_action["tool_input"])
                control.record_result(
                    owner,
                    result_text(first_id, evidence={"A01": "first complete"}),
                )

                second_action = control.next_wave(
                    capacity=1, native_catalog=catalog("gpt-5.6-terra")
                )["dispatches"][0]

                self.assertEqual(second_action["action"], "spawn_new_owner")
                control.restart()
                control.cleanup()

    def test_stale_unexecuted_reuse_wave_is_recaptured(self) -> None:
        control, first_id, _owner = self.ready_reuse_chain("reuse-stale-baseline")
        stale = control.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        stale_id = parse_task_message(stale["tool_input"]["message"])["dispatch_id"]
        (self.repo / "a.txt").write_text("changed before reuse\n", encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "call next again"):
            control.preflight_reuse(
                {"tool_input": stale["tool_input"], "tool_use_id": "stale-reuse"}
            )

        refreshed = control.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        refreshed_id = parse_task_message(refreshed["tool_input"]["message"])["dispatch_id"]
        self.assertEqual(refreshed["action"], "reuse_owner")
        self.assertNotEqual(refreshed_id, stale_id)
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][refreshed_id]["reused_from"], first_id)

    def test_reuse_spawn_fallback_can_consume_a_rejected_route(self) -> None:
        control = self.control("reuse-route-fallback")
        nodes = [
            self.node("first", "A01", "a.txt", decision="mechanical"),
            self.node(
                "second",
                "A02",
                "a.txt",
                decision="mechanical",
                depends_on=["first"],
            ),
        ]
        routes = catalog("gpt-5.6-luna", "gpt-5.6-terra")
        control.create_plan(self.repo, self.brief(nodes))
        first = control.next_wave(capacity=1, native_catalog=routes)["dispatches"][0]
        first_id, owner = self.start_dispatch(control, first["tool_input"])
        control.record_result(
            owner,
            result_text(first_id, evidence={"A01": "first complete"}),
        )
        reuse = control.next_wave(capacity=1, native_catalog=routes)["dispatches"][0]
        dispatch_id = parse_task_message(reuse["tool_input"]["message"])["dispatch_id"]
        control.preflight_reuse(
            {"tool_input": reuse["tool_input"], "tool_use_id": "reuse-unavailable"}
        )
        spawned = control.settle_native_failure(dispatch_id, "owner_unavailable")
        control.preflight_spawn(
            {"tool_input": spawned["tool_input"], "tool_use_id": "spawn-rejected"}
        )

        fallback = control.settle_native_failure(dispatch_id, "route_rejected")

        self.assertEqual(fallback["action"], "fallback_route")
        self.assertNotEqual(
            fallback["tool_input"]["model"], spawned["tool_input"]["model"]
        )
        control.preflight_spawn(
            {"tool_input": fallback["tool_input"], "tool_use_id": "fallback-route"}
        )

    def test_transient_reuse_failure_retries_the_unchanged_task_on_same_owner(self) -> None:
        control, _first_id, owner = self.ready_reuse_chain("reuse-transient")
        reuse = control.next_wave(
            capacity=1, native_catalog=catalog("gpt-5.6-terra")
        )["dispatches"][0]
        dispatch_id = parse_task_message(reuse["tool_input"]["message"])["dispatch_id"]
        control.preflight_reuse(
            {"tool_input": reuse["tool_input"], "tool_use_id": "reuse-network"}
        )

        retry = control.settle_native_failure(dispatch_id, "network")

        self.assertEqual(retry["action"], "reuse_owner")
        self.assertEqual(retry["tool_name"], "followup_task")
        self.assertEqual(retry["tool_input"], reuse["tool_input"])
        self.assertEqual(retry["tool_input"]["target"], owner)
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][dispatch_id]["tool_kind"], "reuse")
        self.assertEqual(state["dispatches"][dispatch_id]["transient_retries"], 1)

    def test_plan_is_stable_and_does_not_accept_primary_owned_lifecycle_fields(self) -> None:
        control = self.control()
        node = self.node("n01", "A01", "a.txt")
        created = control.create_plan(self.repo, self.brief([node]))
        self.assertRegex(created["plan_id"], r"^sha256:[0-9a-f]{64}$")
        plan_path = next((self.state_root / "artifacts").glob("*-plan-*.json"))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        serialized = json.dumps(plan)
        for leaked in ("completed_nodes", "risk_assessment", "placement", "member_mapping"):
            self.assertNotIn(leaked, serialized)
        with self.assertRaisesRegex(ControlPlaneError, "unsupported fields"):
            control.create_plan(
                self.repo,
                {**self.brief([node]), "completed_nodes": []},
            )

    def test_prepare_cli_rejects_duplicate_json_keys(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "duplicate-input"
        environment["PATH"] = ""
        command = SCRIPTS / "control_plane.py"
        duplicate = (
            '{"goal":"first","goal":"second","acceptance":{"A01":"criterion"},'
            '"nodes":[{"id":"n01","role":"worker","objective":"work",'
            '"acceptance":["A01"],"scopes":[{"kind":"file","path":"a.txt"}]}]}'
        )
        completed = subprocess.run(
            [sys.executable, "-B", str(command), "prepare", "--repo", str(self.repo)],
            input=duplicate,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("duplicate JSON key", completed.stderr)

    def test_prepare_rejects_invalid_capacity_without_persisting_a_plan(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "invalid-capacity"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "prepare",
                "--repo",
                str(self.repo),
                "--capacity",
                "0",
            ],
            input=json.dumps(self.canonical_dag([self.node("n01", "A01", "a.txt")])),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(list(self.state_root.rglob("*.json")))

    def test_prepare_primary_direct_does_not_require_a_session_or_catalog(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment.pop("CODEX_THREAD_ID", None)
        request = {
            "authority": "delegated",
            "clarification_required": False,
            "closed": True,
            "declared_tools": [],
            "direct": True,
            "protocol": "aog.delegation.v1",
            "upper_bound_seconds": 0,
            "work": None,
        }

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "prepare",
                "--repo",
                str(self.repo),
            ],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "protocol": "aog.prepare.v1",
                "reason": "explicit_direct",
                "state": "primary_direct",
            },
        )
        self.assertFalse(self.state_root.exists())

    def test_prepare_rejects_the_removed_brief_dialects(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "removed-brief"

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "prepare",
                "--repo",
                str(self.repo),
            ],
            input=json.dumps(self.brief([self.node("n01", "A01", "a.txt")])),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("delegation request", completed.stderr)

    def test_identical_prepare_can_resume_a_plan_that_has_no_wave(self) -> None:
        control = self.control("idempotent-prepare")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first = control.create_plan(self.repo, brief, resume_identical=True)

        resumed = control.create_plan(self.repo, brief, resume_identical=True)

        self.assertEqual(resumed, first)
        self.assertEqual(control.status()["state"], "ready")

    def test_prepare_cli_compiles_one_child_and_wave_without_temp_contract_file(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "single-prepare"
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(
            json.dumps(catalog("gpt-5.6-terra")),
            encoding="utf-8",
        )
        contract = {
            "authority": "delegated",
            "clarification_required": False,
            "closed": True,
            "declared_tools": [],
            "direct": False,
            "protocol": "aog.delegation.v1",
            "upper_bound_seconds": 30,
            "work": {
                "goal": "perform one closed edit",
                "kind": "atomic",
                "node": {
                    "acceptance": {
                        "A01": "the requested file is updated",
                        "A02": "verification is reported",
                    },
                    "decision": "mechanical",
                    "id": "edit",
                    "objective": "perform one closed edit",
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": "a.txt"}],
                },
            },
        }

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "prepare",
                "--repo",
                str(self.repo),
                "--capacity",
                "1",
                "--catalog",
                str(catalog_path),
            ],
            input=json.dumps(contract),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        batch = json.loads(completed.stdout)
        self.assertEqual(batch["protocol"], "aog.wave-batch.v1")
        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertEqual(
            set(batch["dispatches"][0]),
            {"action", "tool_name", "tool_input"},
        )
        self.assertEqual(batch["dispatches"][0]["action"], "spawn_new_owner")
        self.assertEqual(batch["dispatches"][0]["tool_name"], "spawn_agent")
        self.assertEqual(batch["dispatches"][0]["tool_input"]["model"], "gpt-5.6-terra")
        self.assertFalse(list(self.repo.glob("aog-*.json")))

    def test_prepare_cli_compiles_compact_multi_node_graph_in_one_call(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "multi-prepare"
        catalog_path = self.root / "multi-catalog.json"
        catalog_path.write_text(
            json.dumps(catalog("gpt-5.6-terra")),
            encoding="utf-8",
        )
        inspect_a = self.node("inspect_a", "A01", "a.txt", role="explorer")
        inspect_a["verification"] = "semantic"
        contract = self.canonical_dag(
            [
                inspect_a,
                self.node("inspect_b", "A02", "b.txt", role="explorer"),
            ]
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "prepare",
                "--repo",
                str(self.repo),
                "--capacity",
                "2",
                "--catalog",
                str(catalog_path),
            ],
            input=json.dumps(contract),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        batch = json.loads(completed.stdout)
        self.assertEqual(len(batch["dispatches"]), 2)
        messages = [
            parse_task_message(item["tool_input"]["message"])
            for item in batch["dispatches"]
        ]
        self.assertEqual(len({item["dispatch_id"] for item in messages}), 2)
        state = json.loads(self.control("multi-prepare").state_path.read_text(encoding="utf-8"))
        plan = json.loads(Path(state["plan_path"]).read_text(encoding="utf-8"))
        reviewer = next(item for item in plan["nodes"] if item["id"] == "final_review")
        self.assertEqual(reviewer["depends_on"], ["inspect_a", "inspect_b"])
        self.assertIsNone(reviewer["review_of"])
        self.assertEqual(reviewer["acceptance"], ["A01", "A02"])

    def test_normalized_acceptance_and_evidence_id_collisions_are_rejected(self) -> None:
        node = self.node("n01", "A01", "a.txt")
        brief = self.brief([node])
        brief["acceptance"] = {"A01": "first", " A01 ": "second"}
        with self.assertRaisesRegex(ControlPlaneError, "collide after normalization"):
            self.control("acceptance-collision").create_plan(self.repo, brief)

        with self.assertRaisesRegex(ControlPlaneError, "collide after normalization"):
            parse_result(
                result_text(
                    "sha256:" + ("a" * 64),
                    evidence={"A01": "first", " A01 ": "second"},
                )
            )

    def test_plan_requires_explicit_cleanup_before_replacement(self) -> None:
        control = self.control("no-silent-replan")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        control.create_plan(self.repo, brief)
        with self.assertRaisesRegex(ControlPlaneError, "explicit cleanup"):
            control.create_plan(self.repo, brief)

    def test_plan_rejects_unknown_dependencies_and_cycles(self) -> None:
        with self.assertRaisesRegex(ControlPlaneError, "unknown dependencies"):
            self.control("unknown").create_plan(
                self.repo,
                self.brief([self.node("n01", "A01", "a.txt", depends_on=["missing"])]),
            )
        cyclic = [
            self.node("n01", "A01", "a.txt", depends_on=["n02"]),
            self.node("n02", "A02", "b.txt", depends_on=["n01"]),
        ]
        with self.assertRaisesRegex(ControlPlaneError, "cycle"):
            self.control("cycle").create_plan(self.repo, self.brief(cyclic))

    def test_broken_git_workspace_never_downgrades_to_directory_mode(self) -> None:
        broken = self.root / "broken-git"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: missing\n", encoding="utf-8")
        (broken / "a.txt").write_text("a\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceGuardError, "Git workspace is present"):
            self.control("broken-git").create_plan(
                broken,
                self.brief([self.node("n01", "A01", "a.txt")]),
            )

    def test_route_resolves_at_wave_time_and_emits_complete_native_input(self) -> None:
        control = self.control()
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        batch = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )
        native = batch["dispatches"][0]["tool_input"]
        self.assertEqual(native["model"], "gpt-5.6-luna")
        self.assertEqual(native["reasoning_effort"], "max")
        self.assertIn("luna_max", native["task_name"])
        self.assertTrue(native["message"].startswith(TASK_HEADER + "\n"))
        self.assertNotIn("DISPATCH_REF", native["message"])

    def test_task_names_include_dispatch_identity_after_a_shared_long_prefix(self) -> None:
        prefix = "n" + ("a" * 31)
        nodes = [
            self.node(prefix + "one", "A01", "a.txt", role="explorer"),
            self.node(prefix + "two", "A02", "b.txt", role="explorer"),
        ]
        control = self.control("task-name-collision")
        control.create_plan(self.repo, self.brief(nodes))
        batch = control.next_wave(
            capacity=2,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        names = [item["tool_input"]["task_name"] for item in batch["dispatches"]]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)

    def test_exact_ready_set_keeps_one_writer_and_nonconflicting_reader(self) -> None:
        nodes = [
            self.node("n01_writer", "A01", "a.txt"),
            self.node("n02_writer", "A02", "b.txt"),
            self.node("n03_reader", "A03", "c.txt", role="explorer"),
        ]
        control = self.control()
        control.create_plan(self.repo, self.brief(nodes))
        batch = control.next_wave(capacity=3, native_catalog=catalog("gpt-5.6-terra"))
        self.assertEqual(len(batch["dispatches"]), 2)
        roles = [item["tool_input"]["agent_type"] for item in batch["dispatches"]]
        self.assertEqual(roles.count("aog_write_leaf"), 1)
        self.assertEqual(roles.count("aog_read_leaf"), 1)

    def test_exact_selector_fills_capacity_before_downstream_tiebreak(self) -> None:
        units = [
            {
                "downstream_count": 100,
                "id": "broad_writer",
                "role": "worker",
                "scopes": [{"kind": "prefix", "path": "src"}],
            },
            {
                "downstream_count": 1,
                "id": "reader_a",
                "role": "explorer",
                "scopes": [{"kind": "exact", "path": "src/a.txt"}],
            },
            {
                "downstream_count": 1,
                "id": "reader_b",
                "role": "explorer",
                "scopes": [{"kind": "exact", "path": "src/b.txt"}],
            },
        ]
        selected = _select_units(units, 2)
        self.assertEqual([item["id"] for item in selected], ["reader_a", "reader_b"])

    def test_mechanical_overflow_keeps_one_dispatch_per_logical_unit(self) -> None:
        nodes = [
            self.node("n01", "A01", "a.txt", role="explorer", decision="mechanical"),
            self.node("n02", "A02", "b.txt", role="explorer", decision="mechanical"),
            self.node("n03", "A03", "c.txt", role="explorer", decision="mechanical"),
        ]
        control = self.control()
        control.create_plan(self.repo, self.brief(nodes))
        batch = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )
        self.assertEqual(len(batch["dispatches"]), 1)
        task = parse_task_message(batch["dispatches"][0]["tool_input"]["message"])
        self.assertEqual([item["id"] for item in task["members"]], ["n01"])
        self.assertEqual(sorted(task["acceptance"]), ["A01"])
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["logical"]["n01"]["state"], "starting")
        self.assertEqual(state["logical"]["n02"]["state"], "ready")
        self.assertEqual(state["logical"]["n03"]["state"], "ready")

    def test_different_tasks_cannot_claim_one_workspace_writer(self) -> None:
        first = self.control("writer-one")
        second = self.control("writer-two")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        first.next_wave(capacity=1, native_catalog=catalog("gpt-5.6-terra"))

        with self.assertRaisesRegex(ControlPlaneError, "workspace writer lease"):
            second.next_wave(capacity=1, native_catalog=catalog("gpt-5.6-terra"))

        self.assertEqual(first.restart(), 1)
        released = second.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        self.assertEqual(len(released["dispatches"]), 1)

    def test_cross_task_writer_conflicts_with_only_overlapping_live_reader(self) -> None:
        reader = self.control("reader-task")
        overlapping = self.control("overlapping-writer")
        disjoint = self.control("disjoint-writer")
        reader.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        overlapping.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        disjoint.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "b.txt")]),
        )
        native = reader.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(reader, native)

        with self.assertRaisesRegex(ControlPlaneError, "overlapping reader"):
            overlapping.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertEqual(
            len(
                disjoint.next_wave(
                    capacity=1,
                    native_catalog=catalog("gpt-5.6-terra"),
                )["dispatches"]
            ),
            1,
        )

    def test_cross_task_reader_conflicts_with_only_overlapping_live_writer(self) -> None:
        writer = self.control("writer-task")
        overlapping = self.control("overlapping-reader")
        disjoint = self.control("disjoint-reader")
        writer.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        overlapping.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="reviewer")]),
        )
        disjoint.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "b.txt", role="explorer")]),
        )
        native = writer.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(writer, native)

        with self.assertRaisesRegex(ControlPlaneError, "workspace writer"):
            overlapping.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertEqual(
            len(
                disjoint.next_wave(
                    capacity=1,
                    native_catalog=catalog("gpt-5.6-terra"),
                )["dispatches"]
            ),
            1,
        )

    def test_unsupported_interrupting_state_is_rejected(self) -> None:
        control = self.control("unsupported-interrupting")
        control.create_plan(
            self.repo,
            self.brief([self.node("node", "A01", "a.txt")]),
        )
        control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = next(iter(state["dispatches"].values()))
        dispatch["state"] = "interrupting"
        state["logical"]["node"]["state"] = "interrupting"
        control.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"):
            control.status()

    def test_unsupported_lifecycle_and_receipt_protocols_fail_closed(self) -> None:
        control = self.control("unsupported-lifecycle-receipt")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        state["protocol"] = "aog.lifecycle.v0"
        control.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"):
            control.status()

        receipt = control._pending_event(
            "session_restart",
            occurrence="0" * 32,
            source="resume",
        )
        receipt["protocol"] = "aog.receipt.v0"
        with self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"):
            control._validate_pending_event(receipt)

























    def test_unrelated_workspace_state_work_does_not_block_interrupt_settlement(self) -> None:
        other_repo = self.root / "interrupt-unrelated-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)

        interrupted = ControlPlane(
            "interrupt-independent-workspace",
            root=self.state_root,
            lock_timeout=0.2,
        )
        interrupted.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = interrupted.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(interrupted, native)
        interrupted.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "independent-interrupt"}
        )

        unrelated = ControlPlane(
            "unrelated-workspace-state-work",
            root=self.state_root,
            lock_timeout=0.2,
        )
        unrelated.create_plan(
            other_repo,
            self.brief(
                [self.node("reader", "A02", "other.txt", role="explorer")]
            ),
        )
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold_unrelated_state_work() -> None:
            try:
                with unrelated._coordinated_state():
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test unrelated-state barrier timed out")
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        thread = threading.Thread(target=hold_unrelated_state_work)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            self.assertTrue(
                interrupted.postflight_interrupt(
                    {
                        "tool_input": {"target": owner},
                        "tool_response": {"previous_status": "running"},
                        "tool_use_id": "independent-interrupt",
                    }
                )
            )
        finally:
            release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(interrupted.status()["counts"]["fenced"], 1)











    def test_invalid_indexed_same_workspace_state_blocks_admission(self) -> None:
        control = self.control("indexed-corruption")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        poisoned = control_plane_module._lifecycle_state_path(
            self.state_root,
            self.repo,
            "poisoned-peer",
        )
        poisoned.write_text('{"protocol":', encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "valid JSON"):
            control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )

    def test_paused_reader_claim_blocks_writer_during_continuation_verification(self) -> None:
        reader = self.control("continuation-reader")
        writer = self.control("continuation-racing-writer")
        reader.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        writer.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = reader.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(reader, native)
        reader.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need evidence"],
                failure_signature="missing_evidence",
            ),
        )
        prepared = reader.prepare_continuation(dispatch_id, {"fact": "known"})
        continuation = prepared.get("tool_input", prepared)
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_verify = control_plane_module.verify_workspace

        def delayed_verify(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(5):
                raise AssertionError("continuation verification barrier timed out")
            return original_verify(*args, **kwargs)

        def run_continuation() -> None:
            try:
                reader.preflight_continuation(
                    {"tool_input": continuation, "tool_use_id": "continuation-race"}
                )
            except BaseException as error:  # captured for the parent test thread
                errors.append(error)

        with patch.object(control_plane_module, "verify_workspace", side_effect=delayed_verify):
            thread = threading.Thread(target=run_continuation)
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ControlPlaneError, "overlapping reader"):
                    writer.next_wave(
                        capacity=1,
                        native_catalog=catalog("gpt-5.6-terra"),
                    )
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_spawn_claim_survives_reservation_expiry_during_verification(self) -> None:
        first = self.control("expiring-spawn")
        second = self.control("spawn-racing-writer")
        brief = self.brief([self.node("writer", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        now = [100.0]
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        original_verify = control_plane_module.verify_workspace

        def delayed_verify(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(5):
                raise AssertionError("spawn verification barrier timed out")
            return original_verify(*args, **kwargs)

        with patch.object(control_plane_module.time, "time", side_effect=lambda: now[0]):
            native = first.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )["dispatches"][0]["tool_input"]

            def run_spawn() -> None:
                try:
                    first.preflight_spawn(
                        {"tool_input": native, "tool_use_id": "spawn-race"}
                    )
                except BaseException as error:  # captured for the parent test thread
                    errors.append(error)

            with patch.object(
                control_plane_module,
                "verify_workspace",
                side_effect=delayed_verify,
            ):
                thread = threading.Thread(target=run_spawn)
                thread.start()
                self.assertTrue(entered.wait(5))
                now[0] = 1000.0
                try:
                    with self.assertRaisesRegex(ControlPlaneError, "workspace writer lease"):
                        second.next_wave(
                            capacity=1,
                            native_catalog=catalog("gpt-5.6-terra"),
                        )
                finally:
                    release.set()
                    thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_stale_unexecuted_spawn_wave_is_recaptured(self) -> None:
        control = self.control("stale-unexecuted-wave")
        control.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        original = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        original_id = parse_task_message(original["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        state["dispatches"][original_id]["claim_expires_at"] = 1
        control.state_path.write_text(json.dumps(state), encoding="utf-8")
        (self.repo / "a.txt").write_text("changed before spawn\n", encoding="utf-8")
        rearmed = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertEqual(parse_task_message(rearmed["message"])["dispatch_id"], original_id)

        with self.assertRaisesRegex(ControlPlaneError, "call next again"):
            control.preflight_spawn(
                {"tool_input": rearmed, "tool_use_id": "stale-baseline"}
            )

        refreshed = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertNotEqual(
            parse_task_message(refreshed["message"])["dispatch_id"],
            original_id,
        )
        control.preflight_spawn(
            {"tool_input": refreshed, "tool_use_id": "fresh-baseline"}
        )

    def test_stale_fallback_route_recaptures_without_retrying_rejected_model(self) -> None:
        control = self.control("stale-fallback-wave")
        control.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt", decision="mechanical")]),
        )
        first = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        first_id = parse_task_message(first["message"])["dispatch_id"]
        control.preflight_spawn({"tool_input": first, "tool_use_id": "reject-first"})
        control.settle_native_failure(first_id, "route_rejected")
        fallback = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertEqual(fallback["model"], "gpt-5.6-terra")
        (self.repo / "a.txt").write_text("changed before fallback\n", encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "call next again"):
            control.preflight_spawn(
                {"tool_input": fallback, "tool_use_id": "stale-fallback"}
            )

        refreshed = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertEqual(refreshed["model"], "gpt-5.6-terra")

    def test_git_result_inspection_failure_does_not_fence_the_owner(self) -> None:
        control = self.control("result-infrastructure-failure")
        control.create_plan(
            self.repo,
            self.brief([self.node("worker", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)

        with (
            patch.object(
                workspace_guard_module,
                "verify",
                side_effect=StateError("Git is temporarily unavailable"),
            ),
            self.assertRaises(ControlPlaneUnavailable),
        ):
            control.record_result(
                owner,
                result_text(dispatch_id, evidence={"A01": "done"}),
            )

        self.assertEqual(control.status()["counts"]["running"], 1)

    @unittest.skipUnless(os.name == "nt", "extended path aliases are Windows-specific")
    def test_workspace_lock_identity_collapses_windows_extended_aliases(self) -> None:
        ordinary = str(self.repo.resolve())
        extended = "\\\\?\\" + ordinary
        self.assertEqual(
            control_plane_module._workspace_key(ordinary),
            control_plane_module._workspace_key(extended),
        )

    def test_worker_result_advances_logical_plan_atomically(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        (self.repo / "a.txt").write_text("a1\n", encoding="utf-8")
        accepted = control.record_result(
            owner,
            result_text(dispatch_id, changed_paths=["a.txt"], evidence={"A01": "focused check passed"}),
        )
        self.assertEqual(accepted["state"], "retired")
        self.assertEqual(control.status()["state"], "complete")
        self.assertEqual(
            control.next_wave(capacity=1, native_catalog=catalog("gpt-5.6-terra"))["state"],
            "complete",
        )
        self.assertFalse(list((self.state_root / "artifacts").glob("*-wave-*.json")))
        self.assertTrue(list((self.state_root / "artifacts").glob("*-plan-*.json")))
        self.assertEqual(control.status()["state"], "complete")

    def test_worker_result_can_late_bind_owner_from_subagent_stop_evidence(self) -> None:
        control = self.control("late-owner")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        control.preflight_spawn(
            {"tool_input": native, "tool_use_id": "spawn-without-owner"}
        )
        control.postflight_tool(
            {
                "tool_input": native,
                "tool_response": {"agent_id": "00000000-0000-4000-8000-000000000001"},
                "tool_use_id": "spawn-without-owner",
            }
        )
        pending = control.status()["attention"]
        self.assertEqual(pending[0]["reason"], "awaiting_native_owner")

        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        owner = "/root/" + str(native["task_name"])
        result = control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "verified"}),
        )
        self.assertEqual(result["state"], "retired")
        self.assertEqual(control.status()["state"], "complete")

    def test_terminal_result_settles_spawn_when_native_postflight_is_absent(self) -> None:
        control = self.control("missing-native-postflight")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        control.preflight_spawn({"tool_input": native, "tool_use_id": "missing-postflight"})
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        owner = "/root/" + native["task_name"]

        result = control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "verified"}),
        )

        self.assertEqual(result["state"], "retired")
        self.assertEqual(control.status()["state"], "complete")

    def test_cleanup_removes_only_inactive_current_task_state(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        with self.assertRaisesRegex(ControlPlaneError, "active or paused"):
            native = control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )["dispatches"][0]["tool_input"]
            control.cleanup()
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "verified"}),
        )
        self.assertGreaterEqual(control.cleanup(), 2)
        self.assertFalse(control.state_path.exists())
        self.assertFalse(list((self.state_root / "artifacts").glob("session-aog-*.json")))

    def test_out_of_scope_delta_is_rejected(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        (self.repo / "b.txt").write_text("outside\n", encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "workspace verification failed"):
            control.record_result(owner, result_text(dispatch_id, evidence={"A01": "claimed"}))

    def test_paused_writer_holds_lease_and_continuation_cursor(self) -> None:
        nodes = [
            self.node("n01", "A01", "a.txt"),
            self.node("n02", "A02", "b.txt"),
        ]
        control = self.control()
        control.create_plan(self.repo, self.brief(nodes))
        native = control.next_wave(
            capacity=2,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need one fact"],
                failure_signature="missing_fact",
            ),
        )
        waiting = control.next_wave(capacity=2, native_catalog=catalog("gpt-5.6-terra"))
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["dispatches"], [])
        continuation = control.prepare_continuation(dispatch_id, {"fact": "now known"})
        self.assertEqual(
            set(continuation),
            {"action", "tool_input", "tool_name"},
        )
        self.assertEqual(continuation["action"], "continue_same_owner")
        self.assertEqual(continuation["tool_name"], "followup_task")
        native_input = continuation["tool_input"]
        body = json.loads(native_input["message"].split("\n", 1)[1])
        self.assertEqual(body["cursor"], 1)
        self.assertEqual(native_input["target"], owner)

    def test_interrupt_preserves_lease_until_native_active_status_confirms_success(self) -> None:
        first = self.control("interrupt-one")
        second = self.control("interrupt-two")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        native = first.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(first, native)

        first.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-1"}
        )
        self.assertEqual(first.status()["counts"]["running"], 1)
        with self.assertRaisesRegex(ControlPlaneError, "workspace writer lease"):
            second.next_wave(capacity=1, native_catalog=catalog("gpt-5.6-terra"))

        first.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": {"errored": "network"}},
                "tool_use_id": "interrupt-1",
            }
        )
        self.assertEqual(first.status()["counts"]["running"], 1)
        first.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-2"}
        )
        first.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "running"},
                "tool_use_id": "interrupt-2",
            }
        )
        self.assertEqual(first.status()["counts"]["fenced"], 1)
        self.assertEqual(
            len(
                second.next_wave(
                    capacity=1,
                    native_catalog=catalog("gpt-5.6-terra"),
                )["dispatches"]
            ),
            1,
        )

    def test_task_name_interrupt_settles_ownerless_running_spawn(self) -> None:
        control = self.control("interrupt-ownerless")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        spawn_call = "ownerless-spawn"
        control.preflight_spawn(
            {"tool_input": native, "tool_use_id": spawn_call}
        )
        control.process_postflight_event(
            {
                "tool_input": native,
                "tool_response": {},
                "tool_use_id": spawn_call,
            }
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][dispatch_id]["state"], "running")
        self.assertIsNone(state["dispatches"][dispatch_id]["owner"])

        target = str(native["task_name"])
        self.assertTrue(control.owner_is_managed(target))
        interrupt_call = "ownerless-interrupt"
        self.assertTrue(
            control.preflight_interrupt(
                {
                    "tool_input": {"target": target},
                    "tool_use_id": interrupt_call,
                }
            )
        )
        self.assertTrue(
            control.process_postflight_event(
                {
                    "tool_input": {"target": target},
                    "tool_response": {"previous_status": "running"},
                    "tool_use_id": interrupt_call,
                }
            )
        )
        self.assertEqual(control.status()["counts"]["fenced"], 1)

    def test_unmanaged_interrupt_postflight_is_inert(self) -> None:
        control = self.control("interrupt-unmanaged")
        self.assertFalse(
            control.postflight_interrupt(
                {
                    "tool_input": {"target": "unmanaged_task"},
                    "tool_response": {"previous_status": "running"},
                    "tool_use_id": "unmanaged-interrupt",
                }
            )
        )

    def test_natural_result_wins_interrupt_postflight_race(self) -> None:
        control = self.control("interrupt-race")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-race"}
        )
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "completed naturally"}),
        )
        control.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "running"},
                "tool_use_id": "interrupt-race",
            }
        )
        self.assertEqual(control.status()["state"], "complete")

    def test_concurrent_interrupt_is_rejected_but_same_attempt_is_idempotent(self) -> None:
        first = self.control("interrupt-recovery")
        second = self.control("interrupt-recovery-writer")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        native = first.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(first, native)

        first.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "lost-postflight"}
        )
        with self.assertRaisesRegex(ControlPlaneError, "unresolved attempt"):
            first.preflight_interrupt(
                {
                    "tool_input": {"target": owner},
                    "tool_use_id": "recovery-interrupt",
                }
            )
        self.assertTrue(
            first.preflight_interrupt(
                {"tool_input": {"target": owner}, "tool_use_id": "lost-postflight"}
            )
        )
        first.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "interrupted"},
                "tool_use_id": "lost-postflight",
            }
        )

        self.assertEqual(first.status()["counts"]["fenced"], 1)
        self.assertEqual(
            len(
                second.next_wave(
                    capacity=1,
                    native_catalog=catalog("gpt-5.6-terra"),
                )["dispatches"]
            ),
            1,
        )

    def test_expired_interrupt_becomes_unresolved_and_requires_restart(self) -> None:
        control = self.control("interrupt-expired")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "expired-interrupt"}
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        state["dispatches"][dispatch_id]["interrupt_claim_expires_at"] = 1
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        control.status()
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertTrue(dispatch["interrupt_unresolved"])
        self.assertIsNone(dispatch["interrupt_receipt_id"])
        self.assertIsNone(dispatch["interrupt_tool_use_id"])
        self.assertIsNone(dispatch["interrupt_claim_expires_at"])
        remaining = [
            control._read_pending_event(path)
            for path in self.state_root.glob(".aog-pending-*.event")
        ]
        self.assertTrue(
            all(event.get("kind") != "interrupt_attempt" for event in remaining)
        )
        with self.assertRaisesRegex(ControlPlaneError, "unresolved interrupt"):
            control.preflight_interrupt(
                {"tool_input": {"target": owner}, "tool_use_id": "new-interrupt"}
            )
        self.assertGreaterEqual(control.restart(), 1)
        self.assertEqual(control.status()["counts"]["fenced"], 1)

    def test_paused_continuation_rejects_out_of_scope_change(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need one fact"],
                failure_signature="missing_fact",
            ),
        )
        continuation = control.prepare_continuation(dispatch_id, {"fact": "known"})[
            "tool_input"
        ]
        (self.repo / "b.txt").write_text("outside\n", encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "workspace verification failed"):
            control.preflight_continuation(
                {"tool_input": continuation, "tool_use_id": "continue-call"}
            )
        self.assertEqual(control.status()["counts"]["paused"], 1)

    def test_continuation_message_is_exactly_bound_to_prepared_evidence(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need one fact"],
                failure_signature="missing_fact",
            ),
        )
        continuation = control.prepare_continuation(dispatch_id, {"fact": "approved"})[
            "tool_input"
        ]
        body = json.loads(continuation["message"].split("\n", 1)[1])
        body["evidence_delta"] = {"fact": "rewritten"}
        tampered = {
            **continuation,
            "message": "AOG_CONTINUE aog.v1\n"
            + json.dumps(body, separators=(",", ":"), sort_keys=True),
        }
        with self.assertRaisesRegex(ControlPlaneError, "prepared input"):
            control.preflight_continuation(
                {"tool_input": tampered, "tool_use_id": "continue-call"}
            )

    def test_workspace_verification_runs_outside_the_lifecycle_lock(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        original_verify = control_plane_module.verify_workspace
        observations: list[bool] = []

        def verify_without_lock(*args: object, **kwargs: object) -> dict[str, object]:
            def contender() -> None:
                try:
                    with acquire(control.root, control.session_id, timeout=0.5):
                        observations.append(True)
                except StateLockBusy:
                    observations.append(False)

            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=2)
            return original_verify(*args, **kwargs)

        with patch.object(
            control_plane_module,
            "verify_workspace",
            side_effect=verify_without_lock,
        ):
            control.record_result(
                owner,
                result_text(dispatch_id, evidence={"A01": "verified"}),
            )
        self.assertEqual(observations, [True])

    def test_baseline_capture_runs_outside_the_lifecycle_lock(self) -> None:
        control = self.control("baseline-outside-lock")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        original_capture = control_plane_module.capture_workspace
        observations: list[bool] = []

        def capture_without_lock(*args: object, **kwargs: object) -> dict[str, object]:
            def contender() -> None:
                try:
                    with acquire(control.root, control.session_id, timeout=0.5):
                        observations.append(True)
                except StateLockBusy:
                    observations.append(False)

            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=2)
            return original_capture(*args, **kwargs)

        with patch.object(
            control_plane_module,
            "capture_workspace",
            side_effect=capture_without_lock,
        ):
            control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertEqual(observations, [True])

    def test_restart_fences_active_and_late_results(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        self.assertEqual(control.restart(), 1)
        self.assertEqual(control.status()["state"], "blocked")
        with self.assertRaisesRegex(ControlPlaneError, "stale or fenced"):
            control.record_result(owner, result_text(dispatch_id, evidence={"A01": "late"}))

    def test_session_start_restart_fences_an_awaiting_native_receipt(self) -> None:
        control = self.control("restart-awaiting-result")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, _owner = self.start_dispatch(control, native)
        receipt = next(self.state_root.glob(".aog-pending-*.event"))
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["phase"], "awaiting_result")

        self.assertEqual(control.process_restart_event("resume"), 1)

        self.assertEqual(control.status()["counts"]["fenced"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_cli_restart_fences_an_awaiting_native_receipt(self) -> None:
        control = self.control("cli-restart-awaiting-result")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(control, native)
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = control.session_id

        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "control_plane.py"), "restart"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["interrupted"], 1)
        self.assertEqual(control.status()["counts"]["fenced"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_pre_thread_rejection_consumes_prepared_route_fallback(self) -> None:
        control = self.control()
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        first = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        control.preflight_spawn({"tool_input": first, "tool_use_id": "rejected-call"})
        action = control.settle_native_failure(
            parse_task_message(first["message"])["dispatch_id"],
            "route_rejected",
        )
        self.assertEqual(action["action"], "fallback_route")
        fallback = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertEqual(fallback["model"], "gpt-5.6-terra")
        self.assertNotEqual(
            parse_task_message(first["message"])["dispatch_id"],
            parse_task_message(fallback["message"])["dispatch_id"],
        )

    def test_non_route_native_failure_does_not_consume_model_fallback(self) -> None:
        control = self.control("non-route-native-failure")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        first = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        control.preflight_spawn({"tool_input": first, "tool_use_id": "capacity-call"})
        control.settle_native_failure(
            parse_task_message(first["message"])["dispatch_id"],
            "service",
        )

        retried = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.assertEqual(retried["model"], "gpt-5.6-luna")
        self.assertEqual(
            parse_task_message(retried["message"])["dispatch_id"],
            parse_task_message(first["message"])["dispatch_id"],
        )

    def test_explicit_transient_failure_is_bounded(self) -> None:
        control = self.control("bounded-explicit-failure")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        first_dispatch_id: str | None = None
        for attempt in range(1, 5):
            native = control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
            )["dispatches"][0]["tool_input"]
            self.assertEqual(native["model"], "gpt-5.6-luna")
            dispatch_id = parse_task_message(native["message"])["dispatch_id"]
            first_dispatch_id = first_dispatch_id or dispatch_id
            self.assertEqual(dispatch_id, first_dispatch_id)
            tool_use_id = f"capacity-{attempt}"
            control.preflight_spawn(
                {"tool_input": native, "tool_use_id": tool_use_id}
            )
            control.settle_native_failure(dispatch_id, "service")

        self.assertEqual(control.status()["counts"]["fenced"], 1)

    def test_explicit_continuation_failure_returns_exact_retry_action(self) -> None:
        control = self.control("continuation-explicit-failure")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need input"],
                failure_signature="need_input",
            ),
        )
        prepared = control.prepare_continuation(dispatch_id, {"answer": "known"})
        control.preflight_continuation(
            {"tool_input": prepared["tool_input"], "tool_use_id": "failed-followup"}
        )

        action = control.settle_native_failure(dispatch_id, "network")

        self.assertEqual(action["action"], "continue_same_owner")
        self.assertEqual(action["tool_name"], "followup_task")
        self.assertEqual(action["tool_input"]["target"], owner)

    def test_failure_side_postflight_does_not_guess_a_settlement(self) -> None:
        control = self.control("unexpected-failure-postflight")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        control.preflight_spawn({"tool_input": native, "tool_use_id": "failed-call"})

        with self.assertRaisesRegex(ControlPlaneError, "use native-failure"):
            control.postflight_tool(
                {
                    "tool_input": native,
                    "tool_response": {
                        "success": False,
                        "code": "unsupported_model",
                    },
                    "tool_use_id": "failed-call",
                }
            )

        settled = control.settle_native_failure(dispatch_id, "route_rejected")
        self.assertEqual(settled["action"], "fallback_route")

    def test_unsupported_wave_is_rejected_before_native_admission(self) -> None:
        control = self.control("unsupported-wave")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.replace_active_wave_with_unsupported_protocol(control)

        with self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"):
            control.preflight_spawn({"tool_input": native, "tool_use_id": "unsupported-wave"})

    def test_wave_unit_mutation_is_rejected_before_spawn(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        wave_path = next((self.state_root / "artifacts").glob("*-wave-*.json"))
        wave = json.loads(wave_path.read_text(encoding="utf-8"))
        wave["units"][0]["route_candidates"][0]["model"] = "gpt-5.6-luna"
        wave_path.write_text(json.dumps(wave), encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "wave artifact digest"):
            control.preflight_spawn({"tool_input": native, "tool_use_id": "call"})

    def test_logical_baseline_is_bound_to_its_immutable_wave(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        wave_path = next((self.state_root / "artifacts").glob("*-wave-*.json"))
        wave = json.loads(wave_path.read_text(encoding="utf-8"))
        wave["baselines"]["n01"]["snapshot"]["head"] = "corrupted"
        wave_path.write_text(json.dumps(wave), encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "identifier does not match"):
            control.preflight_spawn({"tool_input": native, "tool_use_id": "call"})

    def test_acceptance_id_has_one_logical_owner(self) -> None:
        nodes = [
            self.node("n01", "A01", "a.txt", role="explorer"),
            self.node("n02", "A01", "b.txt", role="explorer"),
        ]
        with self.assertRaisesRegex(ControlPlaneError, "more than one logical owner"):
            self.control().create_plan(self.repo, self.brief(nodes))

    def test_reviewer_must_accept_for_overall_completion(self) -> None:
        control = self.control()
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", role="reviewer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "reviewed"}, outcome="retire"),
        )
        self.assertEqual(control.status()["state"], "blocked")

    def test_reviewer_rejection_does_not_satisfy_final_gate(self) -> None:
        control = self.control("review-gate")
        final_review = self.node(
            "final_review",
            "A01",
            "a.txt",
            role="reviewer",
            depends_on=["source"],
        )
        nodes = [
            self.node("source", "A01", "a.txt", role="explorer"),
            final_review,
        ]
        control.create_plan(self.repo, self.brief(nodes))
        source_native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        source_dispatch_id, source_owner = self.start_dispatch(control, source_native)
        control.record_result(
            source_owner,
            result_text(source_dispatch_id, evidence={"A01": "inspected"}),
        )
        review_native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        review_dispatch_id, review_owner = self.start_dispatch(control, review_native)
        control.record_result(
            review_owner,
            result_text(
                review_dispatch_id,
                evidence={"A01": "rejected"},
                outcome="retire",
            ),
        )
        blocked = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        self.assertEqual(blocked["dispatches"], [])
        self.assertEqual(blocked["state"], "blocked")

    def test_typed_native_failures_retry_same_owner_three_times(self) -> None:
        control = self.control("transient-retry")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        retries = [control.settle_native_failure(dispatch_id, "rate_limit")]
        with self.assertRaisesRegex(ControlPlaneError, "no unsettled native call"):
            control.settle_native_failure(dispatch_id, "rate_limit")
        for attempt in range(2, 4):
            continuation = retries[-1]["tool_input"]
            control.preflight_continuation(
                {
                    "tool_input": continuation,
                    "tool_use_id": f"transient-continuation-{attempt}",
                }
            )
            retries.append(control.settle_native_failure(dispatch_id, "rate_limit"))
        self.assertTrue(all(item["action"] == "continue_same_owner" for item in retries))
        self.assertTrue(all(item["tool_name"] == "followup_task" for item in retries))
        self.assertTrue(all(item["tool_input"]["target"] == owner for item in retries))
        final_continuation = retries[-1]["tool_input"]
        control.preflight_continuation(
            {
                "tool_input": final_continuation,
                "tool_use_id": "transient-continuation-exhausted",
            }
        )
        exhausted = control.settle_native_failure(dispatch_id, "rate_limit")
        self.assertEqual(exhausted["action"], "fenced")
        status = control.status()
        self.assertEqual(status["counts"]["fenced"], 1)
        self.assertEqual(status["attention"][0]["nodes"], ["n01"])
        self.assertNotIn("node", status["attention"][0])
        self.assertEqual(status["attention"][0]["owner"], owner)

    def test_expired_unclaimed_reservation_is_rearmed_without_restart(self) -> None:
        control = self.control("expired-native-reservation")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        with patch.object(control_plane_module.time, "time", return_value=100.0):
            native = control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )["dispatches"][0]["tool_input"]
        with patch.object(control_plane_module.time, "time", return_value=1000.0):
            retried = control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertEqual(retried["dispatches"][0]["tool_input"], native)

    def test_failed_native_claim_keeps_lease_until_explicit_settlement(self) -> None:
        first = self.control("failed-native-claim")
        second = self.control("blocked-by-native-claim")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        with patch.object(control_plane_module.time, "time", return_value=100.0):
            native = first.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )["dispatches"][0]["tool_input"]
            first.preflight_spawn(
                {"tool_input": native, "tool_use_id": "failed-native-call"}
            )
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        with patch.object(control_plane_module.time, "time", return_value=1000.0):
            with self.assertRaisesRegex(ControlPlaneError, "workspace writer lease"):
                second.next_wave(
                    capacity=1,
                    native_catalog=catalog("gpt-5.6-terra"),
                )
            attention = first.status()["attention"]
        self.assertEqual(attention[0]["reason"], "native_settlement_required")

        settled = first.settle_native_failure(dispatch_id, "other")
        self.assertEqual(settled["action"], "fenced")
        released = second.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        self.assertEqual(len(released["dispatches"]), 1)

    def test_ready_plus_waiting_dag_reports_ready_not_blocked(self) -> None:
        control = self.control("ordinary-dag")
        nodes = [
            self.node("first", "A01", "a.txt"),
            self.node("second", "A02", "b.txt", depends_on=["first"]),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        self.assertEqual(control.status()["state"], "ready")

    def test_cli_has_no_cross_task_session_override(self) -> None:
        environment = os.environ.copy()
        environment["AOG_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "attacker-task"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "control_plane.py"),
                "--session",
                "victim-task",
                "restart",
            ],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("victim-task", completed.stderr)
        self.assertFalse((self.state_root / "victim-task.json").exists())

    def test_complete_result_cannot_hide_a_deviation(self) -> None:
        with self.assertRaisesRegex(ControlPlaneError, "cannot contain"):
            parse_result(
                result_text(
                    "sha256:" + ("a" * 64),
                    evidence={"A01": "claimed"},
                    deviations=["changed the contract"],
                )
            )









    def test_cleanup_does_not_match_a_longer_session_artifact_prefix(self) -> None:
        short = self.control("artifact")
        longer = self.control("artifact-plan")
        brief = self.brief([self.node("reader", "A01", "a.txt", role="explorer")])
        short.create_plan(self.repo, brief)
        longer.create_plan(self.repo, brief)
        longer_state = json.loads(longer.state_path.read_text(encoding="utf-8"))
        longer_plan = Path(longer_state["plan_path"])

        short.cleanup()

        self.assertTrue(longer_plan.exists())
        self.assertEqual(longer.status()["state"], "ready")


    def test_exact_paused_result_replay_is_idempotent(self) -> None:
        control = self.control("paused-result-replay")
        control.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        result = result_text(
            dispatch_id,
            status="blocked",
            outcome="pause",
            blockers=["need one fact"],
            failure_signature="missing_fact",
        )
        control.record_result(owner, result)

        replay = control.record_result(owner, result)

        self.assertEqual(replay["state"], "paused")
        self.assertEqual(control.status()["counts"]["paused"], 1)

    def test_pending_event_replay_is_idempotent_after_clear_failure(self) -> None:
        control = self.control("pending-clear-replay")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        epoch_before = control.status()["epoch"]
        with (
            patch.object(
                control,
                "_clear_pending_event",
                side_effect=ControlPlaneUnavailable("simulated clear failure"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "clear failure"),
        ):
            control.process_restart_event("resume")

        epoch_after_settlement = control.status()["epoch"]
        self.assertEqual(epoch_after_settlement, epoch_before + 1)
        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 1)
        with self.assertRaisesRegex(ControlPlaneError, "unsettled native lifecycle receipt"):
            control.cleanup()
        control.replay_pending_events()
        self.assertEqual(control.status()["epoch"], epoch_after_settlement)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_new_event_can_queue_behind_an_older_unfinalized_receipt(self) -> None:
        control = self.control("pending-event-order")
        first = control._pending_event(
            "session_restart",
            occurrence="a" * 32,
            source="resume",
        )
        second = control._pending_event(
            "session_restart",
            occurrence="b" * 32,
            source="clear",
        )
        control._stage_pending_event(first)
        control._stage_pending_event(second)

        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 2)

    def test_two_real_resume_events_have_distinct_settlement_identity(self) -> None:
        control = self.control("distinct-restart-events")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        epoch = control.status()["epoch"]

        control.process_restart_event("resume")
        control.process_restart_event("resume")

        self.assertEqual(control.status()["epoch"], epoch + 2)

    def test_pending_receipt_blocks_a_replacement_plan_without_state(self) -> None:
        control = self.control("pending-without-state")
        event = control._pending_event(
            "session_restart",
            occurrence="c" * 32,
            source="resume",
        )
        control._stage_pending_event(event)

        with self.assertRaisesRegex(ControlPlaneError, "unsettled native lifecycle receipt"):
            control.create_plan(
                self.repo,
                self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
            )

    def test_late_spawn_postflight_cannot_undo_a_paused_result(self) -> None:
        control = self.control("late-postflight-after-pause")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        control.preflight_spawn(
            {"tool_input": native, "tool_use_id": "late-spawn-call"}
        )
        dispatch_id = json.loads(native["message"].split("\n", 1)[1])["dispatch_id"]
        owner = "/root/" + native["task_name"]
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need one fact"],
                failure_signature="missing_fact",
            ),
        )

        control.process_postflight_event(
            {
                "tool_input": native,
                "tool_response": {"task_name": owner},
                "tool_use_id": "late-spawn-call",
            }
        )

        self.assertEqual(control.status()["counts"]["paused"], 1)

    def test_native_result_receipt_survives_transient_settlement_failure(self) -> None:
        control = self.control("durable-child-result")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        result = result_text(dispatch_id, evidence={"A01": "verified"})
        with (
            patch.object(
                control,
                "record_result",
                side_effect=ControlPlaneUnavailable("simulated state outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "state outage"),
        ):
            control.process_result_event(owner, result)

        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 1)
        self.assertIn("unsettled native lifecycle receipt", control.pending_event_reason())
        control.process_result_event(owner, result)
        self.assertEqual(control.status()["counts"]["retired"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_restart_replays_result_observation_cleanup_without_a_second_epoch(self) -> None:
        control = self.control("restart-result-observation")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        with (
            patch.object(
                control,
                "record_result",
                side_effect=ControlPlaneUnavailable("simulated result settlement outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "settlement outage"),
        ):
            control.process_result_event(
                owner,
                result_text(dispatch_id, evidence={"A01": "durable observation"}),
            )

        receipt = next(self.state_root.glob(".aog-pending-*.event"))
        self.assertEqual(
            json.loads(receipt.read_text(encoding="utf-8"))["phase"],
            "result_observed",
        )
        epoch = control.status()["epoch"]
        with (
            patch.object(
                control,
                "_finalize_native_attempt_receipt",
                side_effect=ControlPlaneUnavailable("simulated receipt finalization outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "finalization outage"),
        ):
            control.process_restart_event("resume")

        fenced = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(fenced["epoch"], epoch + 1)
        self.assertEqual(fenced["dispatches"][dispatch_id]["state"], "fenced")
        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 2)

        self.assertEqual(control.restart(), 0)

        self.assertEqual(control.status()["epoch"], epoch + 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_unsupported_generic_result_receipt_is_rejected(self) -> None:
        control = self.control("unsupported-receipt")

        with self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"):
            control._pending_event("child_result", owner="/root/worker_n01", result="old")

    def test_staging_file_blocks_without_parsing_its_payload(self) -> None:
        control = self.control("staging-fail-closed")
        self.state_root.mkdir(parents=True, exist_ok=True)
        staging = self.state_root / ".aog-staging-broken.pending"
        staging.write_bytes(b"x" * 1024)

        with (
            patch.object(
                control_plane_module,
                "_load_object",
                side_effect=AssertionError("staging payload must not be parsed by a Hook"),
            ),
            self.assertRaisesRegex(ControlPlaneError, "unsupported AOG state"),
        ):
            _ = control.state_path

    def test_preflight_reserves_receipt_capacity_before_native_call(self) -> None:
        first = self.control("receipt-capacity-first")
        second = self.control("receipt-capacity-second")
        brief = self.brief([self.node("reader", "A01", "a.txt", role="explorer")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        native_first = first.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        native_second = second.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]

        with patch.object(control_plane_module, "MAX_PENDING_EVENT_FILES", 1):
            first.preflight_spawn(
                {"tool_input": native_first, "tool_use_id": "capacity-first"}
            )
            with self.assertRaisesRegex(ControlPlaneUnavailable, "receipt capacity"):
                second.preflight_spawn(
                    {"tool_input": native_second, "tool_use_id": "capacity-second"}
                )
            owner = "/root/" + str(native_first["task_name"])
            first.process_postflight_event(
                {
                    "tool_input": native_first,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": "capacity-first",
                }
            )
            first.process_result_event(
                owner,
                result_text(
                    parse_task_message(native_first["message"])["dispatch_id"],
                    evidence={"A01": "bounded result slot"},
                ),
            )

        state = json.loads(second.state_path.read_text(encoding="utf-8"))
        dispatch = next(iter(state["dispatches"].values()))
        self.assertIsNone(dispatch["tool_use_id"])
        self.assertIsNone(dispatch["receipt_id"])
        self.assertEqual(first.status()["counts"]["retired"], 1)

    def test_post_commit_artifact_cleanup_cannot_rollback_a_reserved_receipt(self) -> None:
        control = self.control("post-commit-artifact-cleanup")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]

        with patch.object(
            control,
            "_owned_artifact_paths",
            side_effect=ControlPlaneUnavailable("simulated artifact cleanup outage"),
        ):
            control.preflight_spawn(
                {"tool_input": native, "tool_use_id": "post-commit-cleanup"}
            )

        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        receipt_id = state["dispatches"][dispatch_id]["receipt_id"]
        self.assertIsInstance(receipt_id, str)
        self.assertTrue(
            control_plane_module._pending_event_path(
                control.root,
                control.session_id,
                receipt_id,
            ).exists()
        )

    def test_invalid_result_is_acknowledged_without_a_replay_loop(self) -> None:
        control = self.control("invalid-result-receipt")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(control, native)
        malformed = "not a AOG result"

        with (
            patch.object(
                control,
                "_clear_pending_event",
                side_effect=ControlPlaneUnavailable("simulated receipt cleanup outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "cleanup outage"),
        ):
            control.process_result_event(owner, malformed)

        self.assertEqual(control.status()["counts"]["fenced"], 1)
        receipts = list(self.state_root.glob(".aog-pending-*.event"))
        self.assertEqual(len(receipts), 1)
        persisted = receipts[0].read_text(encoding="utf-8")
        self.assertNotIn(malformed, persisted)
        self.assertEqual(json.loads(persisted)["phase"], "acknowledged")

        replay = control.process_result_event(owner, malformed)
        self.assertEqual(replay["state"], "ignored")
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])
        self.assertEqual(control.status()["counts"]["fenced"], 1)

    def test_late_postflight_after_continuation_is_inert(self) -> None:
        control = self.control("late-postflight-continuation")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(
                dispatch_id,
                status="blocked",
                outcome="pause",
                blockers=["need a fact"],
                failure_signature="missing_fact",
            ),
        )
        continuation = control.prepare_continuation(dispatch_id, {"fact": "later"})[
            "tool_input"
        ]
        control.preflight_continuation(
            {"tool_input": continuation, "tool_use_id": "continuation-call"}
        )

        self.assertFalse(
            control.process_postflight_event(
                {
                    "tool_input": native,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": "call-1",
                }
            )
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertEqual(dispatch["tool_kind"], "continuation")
        self.assertEqual(dispatch["pending_cursor"], 1)
        self.assertEqual(dispatch["tool_use_id"], "continuation-call")

    def test_cross_plan_restart_and_result_replay_are_inert(self) -> None:
        control = self.control("cross-plan-replay")
        old_brief = self.brief([self.node("old", "A01", "a.txt", role="explorer")])
        control.create_plan(self.repo, old_brief)
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        old_state = json.loads(control.state_path.read_text(encoding="utf-8"))
        restart_receipt = control._pending_event(
            "session_restart",
            occurrence="d" * 32,
            source="resume",
            plan_id=old_state["plan_id"],
            epoch=old_state["epoch"],
        )
        control.restart()
        control.cleanup()

        control.create_plan(
            self.repo,
            self.brief([self.node("new", "A01", "b.txt", role="explorer")]),
        )
        epoch = control.status()["epoch"]
        self.assertEqual(control._settle_restart_event(restart_receipt), 0)
        self.assertEqual(control.status()["epoch"], epoch)
        result = control.process_result_event(
            owner,
            result_text(dispatch_id, evidence={"A01": "old evidence"}),
        )
        self.assertEqual(result["state"], "ignored")
        self.assertEqual(control.status()["counts"]["ready"], 1)

    def test_acknowledged_receipt_survives_more_than_the_old_ack_ring(self) -> None:
        control = self.control("receipt-ack-retention")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        with (
            patch.object(
                control,
                "_clear_pending_event",
                side_effect=ControlPlaneUnavailable("simulated acknowledgement cleanup outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "cleanup outage"),
        ):
            control.process_restart_event("resume")

        for index in range(128):
            control._process_pending_event(
                control._pending_event(
                    "session_restart",
                    occurrence=f"{index:032x}",
                    source="resume",
                )
            )
        epoch_before_replay = control.status()["epoch"]
        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 1)

        control.replay_pending_events()

        self.assertEqual(control.status()["epoch"], epoch_before_replay)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_unmappable_owner_closes_live_leases(self) -> None:
        control = self.control("unmappable-owner")
        control.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(control, native)

        self.assertEqual(control.close_unmappable_owner_leases(), 1)
        self.assertEqual(control.status()["counts"]["fenced"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])


    def test_plan_creation_checks_receipts_under_canonical_lock_order(self) -> None:
        control = self.control("creation-lock-order")
        original_acquire = control_plane_module.acquire
        calls: list[str] = []

        def traced_acquire(root: Path, identity: str, *, timeout: float = 5.0) -> object:
            calls.append(identity)
            return original_acquire(root, identity, timeout=timeout)

        with patch.object(control_plane_module, "acquire", side_effect=traced_acquire):
            control.create_plan(
                self.repo,
                self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
            )

        workspace_lock = control_plane_module._workspace_lock_identity(self.repo)
        workspace_index = calls.index(workspace_lock)
        self.assertEqual(
            calls[workspace_index : workspace_index + 3],
            [
                workspace_lock,
                control.session_id,
                control_plane_module.STATE_ROOT_LOCK,
            ],
        )


    def test_replay_releases_reservation_orphaned_before_state_link(self) -> None:
        control = self.control("orphaned-native-reservation")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]

        with control._coordinated_state() as state:
            dispatch = state["dispatches"][dispatch_id]
            receipt = control._reserve_native_attempt_receipt(
                state,
                dispatch,
                "crash-before-state-link",
            )

        self.assertTrue(
            control_plane_module._pending_event_path(
                control.root,
                control.session_id,
                receipt["event_id"],
            ).exists()
        )
        self.assertEqual(control.replay_pending_events(), 1)
        self.assertFalse(
            control_plane_module._pending_event_path(
                control.root,
                control.session_id,
                receipt["event_id"],
            ).exists()
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["dispatches"][dispatch_id]["receipt_id"])

    def test_replay_releases_interrupt_reservation_orphaned_before_state_link(self) -> None:
        control = self.control("orphaned-interrupt-reservation")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)

        with control._coordinated_state() as state:
            dispatch = state["dispatches"][dispatch_id]
            receipt = control._reserve_interrupt_attempt_receipt(
                state,
                dispatch,
                owner,
                "crash-before-interrupt-state-link",
            )

        self.assertTrue(
            control_plane_module._pending_event_path(
                control.root,
                control.session_id,
                receipt["event_id"],
            ).exists()
        )
        self.assertGreaterEqual(control.replay_pending_events(), 1)
        self.assertFalse(
            control_plane_module._pending_event_path(
                control.root,
                control.session_id,
                receipt["event_id"],
            ).exists()
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["dispatches"][dispatch_id]["interrupt_receipt_id"])

    def test_wrong_dispatch_result_fences_the_current_owner_attempt(self) -> None:
        control = self.control("wrong-dispatch-result")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(control, native)

        settled = control.process_result_event(
            owner,
            result_text("sha256:" + "0" * 64, evidence={"A01": "wrong dispatch"}),
        )

        self.assertEqual(settled["state"], "fenced")
        self.assertEqual(control.status()["counts"]["fenced"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_cooperative_restart_clears_every_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-restart-receipts")
        self.start_dispatch(control, actions[0]["tool_input"])
        self.start_dispatch(control, actions[1]["tool_input"])

        self.assertEqual(control.restart(), 1)

        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_cooperative_invalid_result_clears_every_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-invalid-receipts")
        _dispatch_id, owner = self.start_dispatch(control, actions[0]["tool_input"])
        self.start_dispatch(control, actions[1]["tool_input"])

        settled = control.process_result_event(owner, "invalid cooperative result")

        self.assertEqual(settled["state"], "fenced")
        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_cooperative_interrupt_clears_every_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-interrupt-receipts")
        _dispatch_id, owner = self.start_dispatch(control, actions[0]["tool_input"])
        self.start_dispatch(control, actions[1]["tool_input"])

        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-batch"}
        )
        control.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "running"},
                "tool_use_id": "interrupt-batch",
            }
        )

        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_cooperative_drift_clears_every_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-drift-receipts")
        dispatch_id, owner = self.start_dispatch(control, actions[0]["tool_input"])
        self.start_dispatch(control, actions[1]["tool_input"])

        with (
            patch.object(
                control_plane_module,
                "verify_isolation_canonical",
                side_effect=control_plane_module.WriterIsolationError(
                    "canonical workspace drift: test"
                ),
            ),
            self.assertRaisesRegex(ControlPlaneError, "canonical workspace drift"),
        ):
            control.record_result(
                owner,
                result_text(dispatch_id, evidence={"A01": "isolate complete"}),
            )

        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_cooperative_abandon_clears_every_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-abandon-receipts")
        _dispatch_id, _owner = self.start_dispatch(control, actions[0]["tool_input"])
        self.start_dispatch(control, actions[1]["tool_input"])
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        first = parse_task_message(actions[0]["tool_input"]["message"])["dispatch_id"]
        state["dispatches"][first]["state"] = "paused"
        state["logical"]["left"]["state"] = "paused"
        control.state_path.write_text(json.dumps(state), encoding="utf-8")

        control.abandon("left")

        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_ready_to_apply_clears_its_settled_receipt_pointer(self) -> None:
        control, actions = self.cooperative_actions("ready-pointer-clear")
        first_id, first_owner = self.start_dispatch(control, actions[0]["tool_input"])
        second_id, _second_owner = self.start_dispatch(control, actions[1]["tool_input"])

        held = control.record_result(
            first_owner,
            result_text(first_id, evidence={"A01": "first isolate complete"}),
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))

        self.assertEqual(held["state"], "ready_to_apply")
        self.assertIsNone(state["dispatches"][first_id]["receipt_id"])
        self.assertIsInstance(state["dispatches"][second_id]["receipt_id"], str)

    def test_cooperative_fence_clears_orphaned_ready_peer_receipt(self) -> None:
        control, actions = self.cooperative_actions("cooperative-orphaned-ready-receipt")
        first_id, first_owner = self.start_dispatch(control, actions[0]["tool_input"])
        _second_id, second_owner = self.start_dispatch(control, actions[1]["tool_input"])

        with (
            patch.object(
                control,
                "_clear_pending_event",
                side_effect=ControlPlaneUnavailable("simulated cleanup outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "cleanup outage"),
        ):
            control.record_result(
                first_owner,
                result_text(first_id, evidence={"A01": "first isolate complete"}),
            )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["dispatches"][first_id]["state"], "ready_to_apply")
        self.assertIsNone(state["dispatches"][first_id]["receipt_id"])
        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 2)

        control.fence_invalid_result(second_owner)

        self.assertEqual(control.status()["counts"]["fenced"], 2)
        self.assert_no_live_batch_receipts(control)

    def test_late_reused_owner_result_cannot_fence_newer_dispatch(self) -> None:
        control, first_id, owner = self.ready_reuse_chain("late-reused-result")
        action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        self.assertEqual(action["action"], "reuse_owner")
        control.preflight_reuse(
            {"tool_input": action["tool_input"], "tool_use_id": "reuse-call"}
        )
        control.postflight_tool(
            {
                "tool_input": action["tool_input"],
                "tool_response": {"task_name": owner},
                "tool_use_id": "reuse-call",
            }
        )

        late = control.process_result_event(
            owner,
            result_text(first_id, evidence={"A01": "late first result"}),
        )
        duplicate_late = control.process_result_event(
            owner,
            result_text(first_id, evidence={"A01": "late first result"}),
        )

        self.assertEqual(late["state"], "ignored")
        self.assertEqual(duplicate_late["state"], "ignored")
        self.assertEqual(control.status()["counts"]["retired"], 1)
        self.assertEqual(control.status()["counts"]["running"], 1)
        self.assertEqual(control.status()["counts"]["fenced"], 0)

    def test_late_reused_result_does_not_settle_newer_observed_attempt(self) -> None:
        control, first_id, owner = self.ready_reuse_chain("late-observed-reuse")
        action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        second_id = parse_task_message(action["tool_input"]["message"])["dispatch_id"]
        control.preflight_reuse(
            {"tool_input": action["tool_input"], "tool_use_id": "reuse-call"}
        )
        control.postflight_tool(
            {
                "tool_input": action["tool_input"],
                "tool_response": {"task_name": owner},
                "tool_use_id": "reuse-call",
            }
        )
        with (
            patch.object(
                control,
                "record_result",
                side_effect=ControlPlaneUnavailable("simulated settlement outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "settlement outage"),
        ):
            control.process_result_event(
                owner,
                result_text(second_id, evidence={"A02": "second observation"}),
            )

        late = control.process_result_event(
            owner,
            result_text(first_id, evidence={"A01": "late first result"}),
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))

        self.assertEqual(late["state"], "ignored")
        self.assertEqual(state["dispatches"][second_id]["state"], "running")
        self.assertNotIn("result", state["dispatches"][second_id])
        self.assertIsInstance(state["dispatches"][second_id]["receipt_id"], str)

    def test_opaque_spawn_cannot_authorize_substituted_fields(self) -> None:
        control = self.control("opaque-spawn-no-scope-expansion")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        opaque = {
            **native,
            "message": "gAAAA" + ("x" * 100),
            "unprepared_scope": "b.txt",
        }

        with self.assertRaisesRegex(ControlPlaneError, "does not match one"):
            control.preflight_spawn(
                {"tool_input": opaque, "tool_use_id": "opaque-extra-field"},
                opaque_message=True,
            )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        self.assertIsNone(state["dispatches"][dispatch_id]["receipt_id"])

    def test_strict_policy_rejects_opaque_reuse_and_continuation(self) -> None:
        control = self.control("opaque-followup-no-binding")

        for kind in ("reuse", "continuation"):
            with self.subTest(kind=kind):
                with (
                    patch.dict(
                        os.environ,
                        {"AOG_OPAQUE_MESSAGE_POLICY": "strict"},
                    ),
                    self.assertRaisesRegex(ControlPlaneError, "strict policy"),
                ):
                    control.preflight_opaque_followup(
                        {
                            "tool_input": {
                                "message": "gAAAA" + ("q" * 100),
                                "target": "/root/worker_n01",
                            },
                            "tool_use_id": f"opaque-{kind}",
                        }
                    )

    def test_plain_spawn_requires_the_exact_prepared_tool_input(self) -> None:
        control = self.control("exact-spawn-input")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]

        with self.assertRaisesRegex(ControlPlaneError, "fields beyond"):
            control.preflight_spawn(
                {
                    "tool_input": {**native, "unprepared": True},
                    "tool_use_id": "extra-field",
                }
            )

    def test_unlinked_durable_native_receipt_cannot_admit_postflight(self) -> None:
        control = self.control("unlinked-native-receipt")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        tool_use_id = "unlinked-postflight"

        # Simulate a receipt replacement that became visible immediately before
        # its directory-sync acknowledgement failed.  The claim state was not
        # published, so this receipt must not authorize a host result.
        with control._coordinated_state() as state:
            dispatch = control._find_dispatch(state, dispatch_id)
            control._reserve_native_attempt_receipt(state, dispatch, tool_use_id)

        owner = "/root/" + str(native["task_name"])
        self.assertFalse(
            control.process_postflight_event(
                {
                    "tool_input": native,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": tool_use_id,
                }
            )
        )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertEqual(dispatch["state"], "starting")
        self.assertIsNone(dispatch["tool_use_id"])
        self.assertIsNone(dispatch["receipt_id"])
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_uncertain_postflight_state_publication_replays_its_receipt(self) -> None:
        control = self.control("uncertain-postflight-publication")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        owner = "/root/" + str(native["task_name"])
        tool_use_id = "uncertain-postflight"
        control.preflight_spawn({"tool_input": native, "tool_use_id": tool_use_id})
        original_write = control._write_state
        raised = False

        def publish_then_report_uncertain(state: dict[str, object]) -> None:
            nonlocal raised
            original_write(state)
            if (
                not raised
                and any(
                    isinstance(dispatch, dict) and dispatch.get("state") == "running"
                    for dispatch in state.get("dispatches", {}).values()
                )
            ):
                raised = True
                raise control_plane_module._AtomicWriteUncertain(
                    "simulated postflight directory sync failure"
                )

        payload = {
            "tool_input": native,
            "tool_response": {"task_name": owner},
            "tool_use_id": tool_use_id,
        }
        with patch.object(control, "_write_state", side_effect=publish_then_report_uncertain):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control.process_postflight_event(payload)

        persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["dispatches"][dispatch_id]["state"], "running")
        receipt = next(self.state_root.glob(".aog-pending-*.event"))
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["phase"], "native_observed")

        self.assertTrue(control.process_postflight_event(payload))
        control.process_result_event(
            owner,
            result_text(dispatch_id, evidence={"A01": "replayed"}),
        )
        self.assertEqual(control.status()["counts"]["retired"], 1)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_uncertain_bound_restart_receipt_is_not_replaced_by_an_unbound_one(self) -> None:
        control = self.control("uncertain-restart-receipt")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        original_stage = control._stage_pending_event
        raised = False

        def stage_then_report_uncertain(event: dict[str, object]) -> Path:
            nonlocal raised
            path = original_stage(event)
            if not raised:
                raised = True
                raise control_plane_module._AtomicWriteUncertain(
                    "simulated restart receipt directory sync failure"
                )
            return path

        with patch.object(control, "_stage_pending_event", side_effect=stage_then_report_uncertain):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control.process_restart_event("clear")

        receipts = list(self.state_root.glob(".aog-pending-*.event"))
        self.assertEqual(len(receipts), 1)
        self.assertIn("plan_id", json.loads(receipts[0].read_text(encoding="utf-8")))
        self.assertGreaterEqual(control.process_restart_event("clear"), 0)
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_first_result_observation_is_immutable_across_duplicate_delivery(self) -> None:
        control = self.control("immutable-result-observation")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        first = result_text(dispatch_id, evidence={"A01": "first observation"})
        conflicting = result_text(
            dispatch_id,
            evidence={"A01": "conflicting duplicate observation"},
        )

        with (
            patch.object(
                control,
                "record_result",
                side_effect=ControlPlaneUnavailable("simulated settlement outage"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "settlement outage"),
        ):
            control.process_result_event(owner, first)

        settled = control.process_result_event(owner, conflicting)
        state = json.loads(control.state_path.read_text(encoding="utf-8"))

        self.assertEqual(settled["state"], "retired")
        self.assertEqual(state["dispatches"][dispatch_id]["result"], parse_result(first))
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_apply_deadline_leaves_a_durable_journal_for_restart_recovery(self) -> None:
        control, actions = self.cooperative_actions("apply-deadline-recovery")
        tasks = [parse_task_message(action["tool_input"]["message"]) for action in actions]
        owners = [self.start_dispatch(control, action["tool_input"])[1] for action in actions]

        for task in tasks:
            path = task["scopes"][0]["path"]
            (Path(task["workspace_root"]) / path).write_text(
                "changed\n", encoding="utf-8"
            )

        def result_for(index: int) -> str:
            task = tasks[index]
            acceptance_id = next(iter(task["acceptance"]))
            path = task["scopes"][0]["path"]
            return result_text(
                task["dispatch_id"],
                changed_paths=[path],
                evidence={acceptance_id: "completed"},
            )

        control.record_result(owners[0], result_for(0))
        with (
            patch.object(
                control_plane_module,
                "apply_isolation_journal",
                side_effect=OperationDeadlineExceeded("simulated apply deadline"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "durable progress"),
        ):
            control.record_result(owners[1], result_for(1))

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["cooperative_journal"]["phase"], "applying")
        self.assertTrue(
            all(
                item["state"] == "ready_to_apply"
                for item in state["dispatches"].values()
                if item.get("wave_id") == state["active_wave_id"]
            )
        )

        self.assertGreaterEqual(control.restart(), 1)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((self.repo / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(control.status()["counts"]["fenced"], 2)

    def test_atomic_write_syncs_the_parent_directory_after_replacement(self) -> None:
        target = self.root / "atomic" / "state.json"

        with patch.object(control_plane_module, "_sync_directory") as sync:
            control_plane_module._atomic_write(target, {"state": "durable"})

        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), {"state": "durable"}
        )
        sync.assert_called_once_with(target.parent)

        uncertain = self.root / "atomic" / "uncertain.json"
        with patch.object(
            control_plane_module,
            "_sync_directory",
            side_effect=ControlPlaneUnavailable("injected directory sync failure"),
        ):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control_plane_module._atomic_write(uncertain, {"state": "replay"})
        self.assertEqual(
            json.loads(uncertain.read_text(encoding="utf-8")), {"state": "replay"}
        )

    def test_uncertain_cooperative_wave_publication_retains_isolates_for_replay(self) -> None:
        control = self.control("uncertain-cooperative-wave")
        plan = self.brief(
            [
                self.node("left", "A01", "a.txt"),
                self.node("right", "A02", "b.txt"),
            ]
        )
        plan["writer_isolation"] = "cooperative"
        control.create_plan(self.repo, plan)
        original_write = control._write_state
        raised = False

        def publish_then_report_uncertain(state: dict[str, object]) -> None:
            nonlocal raised
            original_write(state)
            if state.get("active_wave_id") is not None and not raised:
                raised = True
                raise control_plane_module._AtomicWriteUncertain(
                    "simulated post-replace directory sync failure"
                )

        with patch.object(control, "_write_state", side_effect=publish_then_report_uncertain):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control.next_wave(
                    capacity=2,
                    native_catalog=catalog("gpt-5.6-terra"),
                )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        roots = [
            Path(item["isolation"]["record"]["isolate_root"])
            for item in state["dispatches"].values()
        ]
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(root.exists() for root in roots))

        replay = control.next_wave(
            capacity=2,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        self.assertEqual(len(replay["dispatches"]), 2)

    def test_uncertain_journal_publication_retains_the_replay_backup(self) -> None:
        control, actions = self.cooperative_actions("uncertain-journal-publication")
        tasks = [parse_task_message(action["tool_input"]["message"]) for action in actions]
        owners = [self.start_dispatch(control, action["tool_input"])[1] for action in actions]
        for task in tasks:
            path = task["scopes"][0]["path"]
            (Path(task["workspace_root"]) / path).write_text(
                "changed\n", encoding="utf-8"
            )

        def result_for(index: int) -> str:
            task = tasks[index]
            return result_text(
                task["dispatch_id"],
                changed_paths=[task["scopes"][0]["path"]],
                evidence={next(iter(task["acceptance"])): "completed"},
            )

        control.record_result(owners[0], result_for(0))
        original_write = control._write_state
        raised = False

        def publish_then_report_uncertain(state: dict[str, object]) -> None:
            nonlocal raised
            original_write(state)
            journal = state.get("cooperative_journal")
            if (
                isinstance(journal, dict)
                and journal.get("phase") == "staged"
                and not raised
            ):
                raised = True
                raise control_plane_module._AtomicWriteUncertain(
                    "simulated journal directory sync failure"
                )

        with patch.object(control, "_write_state", side_effect=publish_then_report_uncertain):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control.record_result(owners[1], result_for(1))

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        backup = Path(state["cooperative_journal"]["backup_root"])
        self.assertEqual(state["cooperative_journal"]["phase"], "staged")
        self.assertTrue(backup.exists())

        self.assertGreaterEqual(control.restart(), 1)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((self.repo / "b.txt").read_text(encoding="utf-8"), "b0\n")

    def test_unresolved_interrupt_blocks_owner_reuse(self) -> None:
        control = self.control("interrupt-blocks-reuse")
        control.create_plan(
            self.repo,
            self.brief(
                [
                    self.node("first", "A01", "a.txt", role="explorer"),
                    self.node(
                        "second",
                        "A02",
                        "a.txt",
                        role="explorer",
                        depends_on=["first"],
                    ),
                ]
            ),
        )
        first = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, first)
        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "interrupt-pending"}
        )
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "first completed"}),
        )

        next_action = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]

        self.assertEqual(next_action["action"], "spawn_new_owner")

    def test_interrupt_replay_uses_receipt_target_and_rejects_spoofed_alias(self) -> None:
        control = self.control("interrupt-replay-target")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "replay-target"}
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        receipt = control._interrupt_attempt_for_dispatch(dispatch)
        self.assertIsNotNone(receipt)
        assert receipt is not None

        observed = dict(receipt)
        observed["previous_status"] = "running"
        observed["phase"] = "observed"
        control._write_interrupt_attempt_receipt(observed)
        self.assertGreaterEqual(control.replay_pending_events(), 1)
        self.assertEqual(control.status()["counts"]["fenced"], 1)

        spoof = self.control("interrupt-spoofed-target")
        spoof.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        spoof_native = spoof.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _spoof_id, spoof_owner = self.start_dispatch(spoof, spoof_native)
        canonical_spoof = "/root/unrelated/" + str(spoof_native["task_name"])
        self.assertFalse(
            spoof.preflight_interrupt(
                {
                    "tool_input": {"target": canonical_spoof},
                    "tool_use_id": "spoofed-target",
                }
            )
        )
        self.assertTrue(
            spoof.preflight_interrupt(
                {"tool_input": {"target": spoof_owner}, "tool_use_id": "safe-target"}
            )
        )

    def test_interrupt_duplicate_preflight_requires_the_stored_target(self) -> None:
        control = self.control("interrupt-idempotent-target")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(control, native)
        call_id = "same-interrupt-call"
        self.assertTrue(
            control.preflight_interrupt(
                {"tool_input": {"target": owner}, "tool_use_id": call_id}
            )
        )
        with self.assertRaisesRegex(ControlPlaneError, "target changed"):
            control.preflight_interrupt(
                {
                    "tool_input": {"target": str(native["task_name"])},
                    "tool_use_id": call_id,
                }
            )

    def test_interrupt_settlement_unlinks_receipt_in_its_authoritative_write(self) -> None:
        control = self.control("interrupt-single-write-settlement")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id, owner = self.start_dispatch(control, native)
        tool_use_id = "single-write-interrupt"
        control.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": tool_use_id}
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        receipt = control._interrupt_attempt_for_dispatch(
            state["dispatches"][dispatch_id]
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        observed = dict(receipt)
        observed["phase"] = "observed"
        observed["previous_status"] = "completed"
        observed = control._write_interrupt_attempt_receipt(observed)

        self.assertTrue(
            control._settle_interrupt_event(
                {
                    "kind": "interrupt_attempt_observation",
                    "previous_status": "completed",
                    "target": owner,
                    "tool_use_id": tool_use_id,
                },
                receipt=observed,
            )
        )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertIsNone(dispatch["interrupt_receipt_id"])
        self.assertIsNone(dispatch["interrupt_tool_use_id"])
        self.assertIsNone(dispatch["interrupt_claim_expires_at"])
        self.assertFalse(dispatch["interrupt_unresolved"])
        self.assertEqual(dispatch["state"], "running")
        self.assertGreaterEqual(control.replay_pending_events(), 1)
        remaining = [
            control._read_pending_event(path)
            for path in self.state_root.glob(".aog-pending-*.event")
        ]
        self.assertTrue(
            all(event.get("kind") != "interrupt_attempt" for event in remaining)
        )

    def test_native_failure_consumes_one_call_id_but_allows_a_new_retry(self) -> None:
        control = self.control("native-failure-consumed-call")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        control.preflight_spawn({"tool_input": native, "tool_use_id": "failed-call"})
        control.settle_native_failure(dispatch_id, "service")

        with self.assertRaisesRegex(ControlPlaneError, "reuses a completed"):
            control.preflight_spawn(
                {"tool_input": native, "tool_use_id": "failed-call"}
            )
        control.preflight_spawn({"tool_input": native, "tool_use_id": "fresh-call"})

    def test_native_failure_crash_keeps_the_consumed_call_anchor(self) -> None:
        control = self.control("native-failure-crash-anchor")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        control.preflight_spawn({"tool_input": native, "tool_use_id": "crashed-call"})
        write_state = control._write_state

        def publish_then_report_uncertain(state: dict[str, object]) -> None:
            write_state(state)
            raise control_plane_module._AtomicWriteUncertain(
                "simulated native-failure state sync failure"
            )

        with patch.object(
            control,
            "_write_state",
            side_effect=publish_then_report_uncertain,
        ):
            with self.assertRaises(control_plane_module._AtomicWriteUncertain):
                control.settle_native_failure(dispatch_id, "service")

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIn(
            "crashed-call",
            [item.get("tool_use_id") for item in state["tombstones"]],
        )
        with self.assertRaisesRegex(ControlPlaneError, "reuses a completed"):
            control.preflight_spawn(
                {"tool_input": native, "tool_use_id": "crashed-call"}
            )
        control.preflight_spawn({"tool_input": native, "tool_use_id": "fresh-call"})

    def test_late_postflight_after_native_failure_is_inert_against_a_fresh_retry(self) -> None:
        control = self.control("late-postflight-native-failure")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        owner = "/root/" + str(native["task_name"])
        control.preflight_spawn({"tool_input": native, "tool_use_id": "failed-call"})
        control.settle_native_failure(dispatch_id, "service")
        control.preflight_spawn({"tool_input": native, "tool_use_id": "fresh-call"})

        self.assertFalse(
            control.process_postflight_event(
                {
                    "tool_input": native,
                    "tool_response": {"task_name": owner},
                    "tool_use_id": "failed-call",
                }
            )
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = state["dispatches"][dispatch_id]
        self.assertEqual(dispatch["state"], "starting")
        self.assertEqual(dispatch["tool_use_id"], "fresh-call")

    def test_opaque_anchor_capacity_retains_the_original_ciphertext_proof(self) -> None:
        control = self.control("opaque-anchor-capacity")
        nodes = [
            self.node("first", "A01", "a.txt"),
            self.node("second", "A02", "a.txt", depends_on=["first"]),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        first_native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        first_opaque = {**first_native, "message": "gAAAA" + ("o" * 100)}
        first_id = parse_task_message(first_native["message"])["dispatch_id"]
        owner = "/root/" + str(first_native["task_name"])
        control.preflight_spawn(
            {"tool_input": first_opaque, "tool_use_id": "opaque-first-call"},
            opaque_message=True,
        )
        control.process_postflight_event(
            {
                "tool_input": first_opaque,
                "tool_response": {"task_name": owner},
                "tool_use_id": "opaque-first-call",
            },
            opaque_message=True,
        )
        control.record_result(
            owner,
            result_text(first_id, evidence={"A01": "first complete"}),
        )
        opaque_digest = control_plane_module._digest(
            b"aog.native-input.v1\0", first_opaque
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        opaque_anchor = next(
            item
            for item in state["tombstones"]
            if item.get("tool_input_sha256") == opaque_digest
        )
        state["tombstones"] = [
            opaque_anchor,
            *[
                {
                    "cursor": 0,
                    "dispatch_id": first_id,
                    "owner": owner,
                    "reason": "native_attempt_consumed",
                    "tool_input_sha256": "sha256:" + ("b" * 64),
                    "tool_use_id": f"retained-anchor-{index}",
                }
                for index in range(control_plane_module.MAX_TOMBSTONES - 1)
            ],
        ]
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        second_native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        second_opaque = {**second_native, "message": "gAAAA" + ("p" * 100)}
        with self.assertRaisesRegex(ControlPlaneUnavailable, "replay-anchor capacity"):
            control.preflight_opaque_followup(
                {"tool_input": second_opaque, "tool_use_id": "opaque-second-call"}
            )
        persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIn(opaque_anchor, persisted["tombstones"])

    def test_tombstone_anchor_capacity_fails_closed(self) -> None:
        control = self.control("tombstone-anchor-capacity")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        control.preflight_spawn(
            {"tool_input": native, "tool_use_id": "capacity-failure-call"}
        )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        state["tombstones"] = [
            {
                "cursor": 0,
                "dispatch_id": dispatch_id,
                "owner": None,
                "reason": "native_attempt_consumed",
                "tool_input_sha256": "sha256:" + ("a" * 64),
                "tool_use_id": "anchor-call",
            }
            for _ in range(control_plane_module.MAX_TOMBSTONES)
        ]
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ControlPlaneUnavailable, "replay-anchor capacity"):
            control.settle_native_failure(dispatch_id, "service")

    def test_tombstone_anchor_capacity_blocks_before_native_admission(self) -> None:
        control = self.control("tombstone-anchor-preflight-capacity")
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        state["tombstones"] = [
            {
                "cursor": 0,
                "dispatch_id": dispatch_id,
                "owner": None,
                "reason": "native_attempt_consumed",
                "tool_use_id": f"retained-call-{index}",
            }
            for index in range(control_plane_module.MAX_TOMBSTONES)
        ]
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ControlPlaneUnavailable,
            "replay-anchor capacity is exhausted before native admission",
        ):
            control.preflight_spawn(
                {"tool_input": native, "tool_use_id": "must-not-run"}
            )
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["dispatches"][dispatch_id]["receipt_id"])
        self.assertIsNone(state["dispatches"][dispatch_id]["tool_use_id"])
        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

    def test_three_writer_cooperative_wave_is_bounded_and_settles_all_members(self) -> None:
        nodes = [
            self.node("left", "A01", "a.txt"),
            self.node("middle", "A02", "b.txt"),
            self.node("right", "A03", "c.txt"),
        ]
        control, actions = self.cooperative_actions_for_nodes(
            "cooperative-three-writers", nodes, capacity=3
        )
        self.assertEqual(len(actions), 3)
        tasks = [parse_task_message(action["tool_input"]["message"]) for action in actions]
        owners = [self.start_dispatch(control, action["tool_input"])[1] for action in actions]
        for task in tasks:
            path = task["scopes"][0]["path"]
            (Path(task["workspace_root"]) / path).write_text("changed\n", encoding="utf-8")
        for index, task in enumerate(tasks):
            settled = control.record_result(
                owners[index],
                result_text(
                    task["dispatch_id"],
                    changed_paths=[task["scopes"][0]["path"]],
                    evidence={next(iter(task["acceptance"])): "complete"},
                ),
            )
        self.assertEqual(settled["state"], "retired")
        self.assertEqual(control.status()["counts"]["retired"], 3)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual((self.repo / "b.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual((self.repo / "c.txt").read_text(encoding="utf-8"), "changed\n")

    def test_cooperative_wave_fills_capacity_up_to_its_explicit_safety_bound(self) -> None:
        (self.repo / "d.txt").write_text("d0\n", encoding="utf-8")
        (self.repo / "e.txt").write_text("e0\n", encoding="utf-8")
        nodes = [
            self.node(f"writer_{index}", f"A0{index}", f"{name}.txt")
            for index, name in enumerate(("a", "b", "c", "d", "e"), start=1)
        ]
        control, actions = self.cooperative_actions_for_nodes(
            "cooperative-safety-bound",
            nodes,
            capacity=5,
        )

        self.assertEqual(len(actions), control_plane_module.MAX_COOPERATIVE_WRITERS)
        selected = {
            parse_task_message(action["tool_input"]["message"])["members"][0]["id"]
            for action in actions
        }
        self.assertEqual(len(selected), control_plane_module.MAX_COOPERATIVE_WRITERS)
        status = control.status()
        self.assertEqual(status["counts"]["starting"], 4)
        self.assertEqual(status["counts"]["ready"], 1)

    def test_cooperative_capacity_reclaims_a_settled_wave_for_the_next_pair(self) -> None:
        nodes = [
            self.node("left", "A01", "a.txt"),
            self.node("middle", "A02", "b.txt"),
            self.node("right", "A03", "c.txt"),
            self.node("tail", "A04", "d.txt"),
        ]
        control, actions = self.cooperative_actions_for_nodes(
            "cooperative-two-waves", nodes, capacity=2
        )
        self.assertEqual(len(actions), 2)
        for action in actions:
            task = parse_task_message(action["tool_input"]["message"])
            owner = self.start_dispatch(control, action["tool_input"])[1]
            path = task["scopes"][0]["path"]
            (Path(task["workspace_root"]) / path).write_text("changed\n", encoding="utf-8")
            control.record_result(
                owner,
                result_text(
                    task["dispatch_id"],
                    changed_paths=[path],
                    evidence={next(iter(task["acceptance"])): "complete"},
                ),
            )

        next_actions = control.next_wave(
            capacity=2,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"]
        self.assertEqual(len(next_actions), 2)
        next_tasks = [
            parse_task_message(action["tool_input"]["message"])
            for action in next_actions
        ]
        self.assertTrue(
            all(task["workspace_root"] != str(self.repo) for task in next_tasks)
        )

    def test_cooperative_capacity_reason_is_observable(self) -> None:
        plan = self.brief(
            [
                self.node("left", "A01", "a.txt"),
                self.node("right", "A02", "b.txt"),
            ]
        )
        plan["writer_isolation"] = "cooperative"
        control = self.control("cooperative-capacity-reason")
        control.create_plan(self.repo, plan)
        wave = control.next_wave(capacity=1, native_catalog=catalog("gpt-5.6-terra"))
        self.assertEqual(wave["cooperative_reason"], "cooperative_capacity_below_two")
        self.assertEqual(len(wave["dispatches"]), 1)

    def test_receipt_rejects_fields_that_exceed_its_durable_bound(self) -> None:
        control = self.control("oversized-receipt-field")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]

        with self.assertRaisesRegex(ControlPlaneError, "receipt is too large"):
            control.preflight_spawn(
                {
                    "tool_input": native,
                    "tool_use_id": "x" * control_plane_module.MAX_PENDING_EVENT_BYTES,
                }
            )

        self.assertEqual(list(self.state_root.glob(".aog-pending-*.event")), [])

        interrupt = self.control("oversized-interrupt-receipt-field")
        interrupt.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "b.txt")]),
        )
        interrupt_native = interrupt.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(interrupt, interrupt_native)

        with self.assertRaisesRegex(ControlPlaneError, "receipt is too large"):
            interrupt.preflight_interrupt(
                {
                    "tool_input": {"target": owner},
                    "tool_use_id": "x" * control_plane_module.MAX_PENDING_EVENT_BYTES,
                }
            )

        state = json.loads(interrupt.state_path.read_text(encoding="utf-8"))
        interrupt_dispatch_id = parse_task_message(interrupt_native["message"])[
            "dispatch_id"
        ]
        self.assertIsNone(
            state["dispatches"][interrupt_dispatch_id]["interrupt_receipt_id"]
        )
        self.assertEqual(len(list(self.state_root.glob(".aog-pending-*.event"))), 1)


if __name__ == "__main__":
    unittest.main()

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
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from control_plane import (  # noqa: E402
    RESULT_HEADER,
    TASK_HEADER,
    ControlPlane,
    ControlPlaneError,
    _select_units,
    parse_result,
    parse_task_message,
)
import control_plane as control_plane_module  # noqa: E402
from state_lock import StateLockBusy, acquire  # noqa: E402
from workspace_guard import WorkspaceGuardError  # noqa: E402


def catalog(*models: str) -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": model,
                "supported_reasoning_levels": [
                    {"effort": "max"},
                    {"effort": "xhigh"},
                    {"effort": "high"},
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


class V9ControlPlaneTests(unittest.TestCase):
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

    def control(self, session: str = "session-v9") -> ControlPlane:
        return ControlPlane(session, root=self.state_root)

    def brief(self, nodes: list[dict[str, object]]) -> dict[str, object]:
        acceptance = {
            acceptance_id: f"criterion {acceptance_id}"
            for node in nodes
            for acceptance_id in node["acceptance"]
        }
        return {"goal": "exercise cco.v9", "acceptance": acceptance, "nodes": nodes}

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
            "scopes": [{"kind": "file", "path": path}],
        }
        if review_of is not None:
            node["review_of"] = review_of
        return node

    def start_dispatch(self, control: ControlPlane, native: dict[str, object]) -> tuple[str, str]:
        control.preflight_spawn(
            {
                "tool_input": native,
                "tool_use_id": "call-1",
            }
        )
        owner = "/root/" + str(native["task_name"])
        control.postflight_tool(
            {
                "tool_input": native,
                "tool_response": {"task_name": owner},
                "tool_use_id": "call-1",
            }
        )
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        return dispatch_id, owner

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

    def test_plan_cli_rejects_duplicate_json_keys(self) -> None:
        environment = os.environ.copy()
        environment["CCO_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "duplicate-input"
        command = SCRIPTS / "control_plane.py"
        duplicate = (
            '{"goal":"first","goal":"second","acceptance":{"A01":"criterion"},'
            '"nodes":[{"id":"n01","role":"worker","objective":"work",'
            '"acceptance":["A01"],"scopes":[{"kind":"file","path":"a.txt"}]}]}'
        )
        completed = subprocess.run(
            [sys.executable, "-B", str(command), "plan", "--repo", str(self.repo)],
            input=duplicate,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("duplicate JSON key", completed.stderr)

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
        native = batch["dispatches"][0]
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
        names = [item["task_name"] for item in batch["dispatches"]]
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
        roles = [item["agent_type"] for item in batch["dispatches"]]
        self.assertEqual(roles.count("cost_orchestrator_write_leaf"), 1)
        self.assertEqual(roles.count("cost_orchestrator_read_leaf"), 1)

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

    def test_mechanical_overflow_aggregates_logical_acceptance(self) -> None:
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
        task = parse_task_message(batch["dispatches"][0]["message"])
        self.assertEqual([item["id"] for item in task["members"]], ["n01", "n02", "n03"])
        self.assertEqual(sorted(task["acceptance"]), ["A01", "A02", "A03"])
        for member in task["members"]:
            self.assertEqual(
                set(member),
                {"acceptance", "depends_on", "id", "objective", "review_of", "scopes"},
            )

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
        )["dispatches"][0]
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
        )["dispatches"][0]
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
        )["dispatches"][0]
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
        )["dispatches"][0]
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
        )["dispatches"][0]
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
            )["dispatches"][0]
            control.cleanup()
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "verified"}),
        )
        self.assertGreaterEqual(control.cleanup(), 2)
        self.assertFalse(control.state_path.exists())
        self.assertFalse(list((self.state_root / "artifacts").glob("session-v9-*.json")))

    def test_out_of_scope_delta_is_rejected(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
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
        )["dispatches"][0]
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
        body = json.loads(continuation["message"].split("\n", 1)[1])
        self.assertEqual(body["cursor"], 1)
        self.assertEqual(continuation["target"], owner)

    def test_interrupt_is_read_only_until_native_active_status_confirms_success(self) -> None:
        first = self.control("interrupt-one")
        second = self.control("interrupt-two")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first.create_plan(self.repo, brief)
        second.create_plan(self.repo, brief)
        native = first.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
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

    def test_natural_result_wins_interrupt_postflight_race(self) -> None:
        control = self.control("interrupt-race")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
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

    def test_paused_continuation_rejects_out_of_scope_change(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
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
        continuation = control.prepare_continuation(dispatch_id, {"fact": "known"})
        (self.repo / "b.txt").write_text("outside\n", encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "workspace verification failed"):
            control.preflight_continuation(
                {"tool_input": continuation, "tool_use_id": "continue-call"}
            )

    def test_continuation_message_is_exactly_bound_to_prepared_evidence(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
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
        continuation = control.prepare_continuation(dispatch_id, {"fact": "approved"})
        body = json.loads(continuation["message"].split("\n", 1)[1])
        body["evidence_delta"] = {"fact": "rewritten"}
        tampered = {
            **continuation,
            "message": "CCO_CONTINUE cco.v9\n"
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
        )["dispatches"][0]
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
        )["dispatches"][0]
        dispatch_id, owner = self.start_dispatch(control, native)
        self.assertEqual(control.restart(), 1)
        self.assertEqual(control.status()["state"], "blocked")
        with self.assertRaisesRegex(ControlPlaneError, "stale or fenced"):
            control.record_result(owner, result_text(dispatch_id, evidence={"A01": "late"}))

    def test_pre_thread_rejection_consumes_prepared_route_fallback(self) -> None:
        control = self.control()
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", decision="mechanical")]),
        )
        first = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]
        control.preflight_spawn({"tool_input": first, "tool_use_id": "rejected-call"})
        control.postflight_tool(
            {
                "tool_input": first,
                "tool_response": {
                    "isError": True,
                    "error": {"code": "unsupported_model", "message": "Unknown model"},
                },
                "tool_use_id": "rejected-call",
            }
        )
        fallback = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]
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
        )["dispatches"][0]
        control.preflight_spawn({"tool_input": first, "tool_use_id": "capacity-call"})
        control.postflight_tool(
            {
                "tool_input": first,
                "tool_response": {
                    "success": False,
                    "error": {
                        "code": "capacity_exhausted",
                        "message": "selected model is temporarily unavailable",
                    },
                },
                "tool_use_id": "capacity-call",
            }
        )

        retried = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-luna", "gpt-5.6-terra"),
        )["dispatches"][0]
        self.assertEqual(retried["model"], "gpt-5.6-luna")
        self.assertEqual(
            parse_task_message(retried["message"])["dispatch_id"],
            parse_task_message(first["message"])["dispatch_id"],
        )

    def test_wave_unit_mutation_is_rejected_before_spawn(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        wave_path = next((self.state_root / "artifacts").glob("*-wave-*.json"))
        wave = json.loads(wave_path.read_text(encoding="utf-8"))
        wave["units"][0]["route_candidates"][0]["model"] = "gpt-5.6-luna"
        wave_path.write_text(json.dumps(wave), encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "wave artifact digest"):
            control.preflight_spawn({"tool_input": native, "tool_use_id": "call"})

    def test_wave_snapshot_content_must_match_its_bound_state_id(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        wave_path = next((self.state_root / "artifacts").glob("*-wave-*.json"))
        wave = json.loads(wave_path.read_text(encoding="utf-8"))
        wave["baseline"]["snapshot"]["head"] = "corrupted"
        wave_path.write_text(json.dumps(wave), encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "identifier does not match"):
            control.preflight_spawn({"tool_input": native, "tool_use_id": "call"})

    def test_shared_acceptance_id_can_be_owned_by_multiple_nodes(self) -> None:
        nodes = [
            self.node("n01", "A01", "a.txt", role="explorer"),
            self.node("n02", "A01", "b.txt", role="explorer"),
        ]
        created = self.control().create_plan(self.repo, self.brief(nodes))
        self.assertEqual(created["ready"], ["n01", "n02"])

    def test_reviewer_must_accept_for_overall_completion(self) -> None:
        control = self.control()
        control.create_plan(
            self.repo,
            self.brief([self.node("n01", "A01", "a.txt", role="reviewer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "reviewed"}, outcome="retire"),
        )
        self.assertEqual(control.status()["state"], "blocked")

    def test_reviewer_rejection_does_not_satisfy_downstream_dependency(self) -> None:
        control = self.control("review-gate")
        nodes = [
            self.node("review_gate", "A01", "a.txt", role="reviewer"),
            self.node("downstream", "A02", "b.txt", depends_on=["review_gate"]),
        ]
        control.create_plan(self.repo, self.brief(nodes))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]
        dispatch_id, owner = self.start_dispatch(control, native)
        control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "rejected"}, outcome="retire"),
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
        )["dispatches"][0]
        dispatch_id, owner = self.start_dispatch(control, native)
        retries = [control.settle_native_failure(dispatch_id, "rate_limit")]
        with self.assertRaisesRegex(ControlPlaneError, "no unsettled native call"):
            control.settle_native_failure(dispatch_id, "rate_limit")
        for attempt in range(2, 4):
            continuation = {
                "message": retries[-1]["message"],
                "target": retries[-1]["target"],
            }
            control.preflight_continuation(
                {
                    "tool_input": continuation,
                    "tool_use_id": f"transient-continuation-{attempt}",
                }
            )
            retries.append(control.settle_native_failure(dispatch_id, "rate_limit"))
        self.assertTrue(all(item["action"] == "continue_same_owner" for item in retries))
        self.assertTrue(all(item["target"] == owner for item in retries))
        final_continuation = {
            "message": retries[-1]["message"],
            "target": retries[-1]["target"],
        }
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
            )["dispatches"][0]
        with patch.object(control_plane_module.time, "time", return_value=1000.0):
            retried = control.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertEqual(retried["dispatches"], [native])

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
            )["dispatches"][0]
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
        environment["CCO_STATE_DIR"] = str(self.state_root)
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


if __name__ == "__main__":
    unittest.main()

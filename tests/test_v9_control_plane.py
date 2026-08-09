from __future__ import annotations

import hashlib
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
    ControlPlaneUnavailable,
    _select_units,
    parse_result,
    parse_task_message,
)
import control_plane as control_plane_module  # noqa: E402
from state_lock import StateLockBusy, acquire  # noqa: E402
from workspace_guard import (  # noqa: E402
    WorkspaceGuardError,
)
import workspace_guard as workspace_guard_module  # noqa: E402
from workspace_state import StateError  # noqa: E402


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

    def downgrade_active_wave_to_v1(self, control: ControlPlane) -> None:
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        current_wave_id = state["active_wave_id"]
        current_wave_path = control._artifact_path("wave", current_wave_id)
        wave = json.loads(current_wave_path.read_text(encoding="utf-8"))
        wave["protocol"] = "cco.wave.v1"
        identity = {
            key: wave[key]
            for key in ("baseline_id", "plan_id", "protocol", "sequence", "units")
        }
        legacy_wave_id = control_plane_module._digest(b"cco.wave.v1\0", identity)
        wave["wave_id"] = legacy_wave_id
        control._artifact_path("wave", legacy_wave_id).write_text(
            json.dumps(wave, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        state["active_wave_id"] = legacy_wave_id
        state.pop("lineage_id", None)
        state.pop("parent_state_sha256", None)
        for dispatch in state["dispatches"].values():
            if dispatch["wave_id"] != current_wave_id:
                continue
            dispatch["wave_id"] = legacy_wave_id
            dispatch.pop("context_turns", None)
            dispatch.pop("fallback_from_owner", None)
            dispatch.pop("reused_from", None)
        control.state_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

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
        reviewer_nodes = [
            self.node("first", "A01", "a.txt", role="explorer"),
            self.node(
                "review",
                "A02",
                "a.txt",
                role="reviewer",
                depends_on=["first"],
                review_of="first",
            ),
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
        second_node["scopes"] = [{"kind": "tree", "path": "src"}]
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
            self.node("first", "A01", "a.txt"),
            self.node("second", "A02", "a.txt", depends_on=["first"]),
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
        environment["CCO_STATE_DIR"] = str(self.state_root)
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
        environment["CCO_STATE_DIR"] = str(self.state_root)
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
            input=json.dumps(self.brief([self.node("n01", "A01", "a.txt")])),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(list(self.state_root.rglob("*.json")))

    def test_identical_prepare_can_resume_a_plan_that_has_no_wave(self) -> None:
        control = self.control("idempotent-prepare")
        brief = self.brief([self.node("n01", "A01", "a.txt")])
        first = control.create_plan(self.repo, brief, resume_identical=True)

        resumed = control.create_plan(self.repo, brief, resume_identical=True)

        self.assertEqual(resumed, first)
        self.assertEqual(control.status()["state"], "ready")

    def test_prepare_cli_compiles_one_child_and_wave_without_temp_contract_file(self) -> None:
        environment = os.environ.copy()
        environment["CCO_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "single-prepare"
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(
            json.dumps(catalog("gpt-5.6-terra")),
            encoding="utf-8",
        )
        contract = {
            "acceptance": ["the requested file is updated", "verification is reported"],
            "decision": "mechanical",
            "objective": "perform one closed edit",
            "role": "worker",
            "scopes": [{"kind": "file", "path": "a.txt"}],
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
        self.assertEqual(batch["protocol"], "cco.wave-batch.v2")
        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertEqual(
            set(batch["dispatches"][0]),
            {"action", "tool_name", "tool_input"},
        )
        self.assertEqual(batch["dispatches"][0]["action"], "spawn_new_owner")
        self.assertEqual(batch["dispatches"][0]["tool_name"], "spawn_agent")
        self.assertEqual(batch["dispatches"][0]["tool_input"]["model"], "gpt-5.6-terra")
        self.assertFalse(list(self.repo.glob("cco-*.json")))

    def test_prepare_cli_compiles_compact_multi_node_graph_in_one_call(self) -> None:
        environment = os.environ.copy()
        environment["CCO_STATE_DIR"] = str(self.state_root)
        environment["CODEX_THREAD_ID"] = "multi-prepare"
        catalog_path = self.root / "multi-catalog.json"
        catalog_path.write_text(
            json.dumps(catalog("gpt-5.6-terra")),
            encoding="utf-8",
        )
        contract = {
            "goal": "inspect two independent files",
            "nodes": [
                {
                    "acceptance": ["a.txt is inspected"],
                    "id": "inspect_a",
                    "objective": "inspect a.txt",
                    "role": "explorer",
                    "scopes": [{"kind": "file", "path": "a.txt"}],
                },
                {
                    "acceptance": ["b.txt is inspected"],
                    "id": "inspect_b",
                    "objective": "inspect b.txt",
                    "role": "explorer",
                    "scopes": [{"kind": "file", "path": "b.txt"}],
                },
                {
                    "acceptance": ["the a.txt inspection is independently accepted"],
                    "depends_on": ["inspect_a"],
                    "id": "review_a",
                    "objective": "review the a.txt inspection",
                    "review_of": "inspect_a",
                    "role": "reviewer",
                    "scopes": [{"kind": "file", "path": "a.txt"}],
                },
            ],
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
        reviewer = next(item for item in plan["nodes"] if item["id"] == "review_a")
        self.assertEqual(reviewer["depends_on"], ["inspect_a"])
        self.assertEqual(reviewer["review_of"], "inspect_a")

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
        task = parse_task_message(batch["dispatches"][0]["tool_input"]["message"])
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

    def test_legacy_interrupting_preserves_lease_until_restart_or_interrupt(self) -> None:
        legacy = self.control("legacy-interrupting")
        legacy.create_plan(
            self.repo,
            self.brief([self.node("legacy", "A01", "a.txt")]),
        )
        native = legacy.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(legacy, native)
        state = json.loads(legacy.state_path.read_text(encoding="utf-8"))
        dispatch = next(iter(state["dispatches"].values()))
        dispatch["state"] = "interrupting"
        dispatch["interrupt_previous"] = {"state": "running", "tool_kind": "spawn"}
        dispatch["tool_kind"] = "interrupt"
        dispatch["tool_use_id"] = "legacy-interrupt-call"
        state["logical"]["legacy"]["state"] = "interrupting"
        legacy.state_path.write_text(json.dumps(state), encoding="utf-8")
        legacy.state_path.replace(self.state_root / "legacy-interrupting.json")
        legacy = self.control("legacy-interrupting")

        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "a.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        unrelated = self.control("unrelated-workspace")
        unrelated.create_plan(
            other_repo,
            self.brief([self.node("other", "A01", "a.txt")]),
        )

        batch = unrelated.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )
        self.assertEqual(len(batch["dispatches"]), 1)
        contender = self.control("same-workspace-contender")
        contender.create_plan(
            self.repo,
            self.brief([self.node("contender", "A01", "a.txt")]),
        )
        with self.assertRaisesRegex(ControlPlaneError, "writer lease"):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertTrue(legacy.owner_is_managed(owner))
        legacy.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "retry-interrupt"}
        )
        self.assertEqual(legacy.restart(), 1)
        migrated = json.loads(legacy.state_path.read_text(encoding="utf-8"))
        self.assertEqual(next(iter(migrated["dispatches"].values()))["state"], "fenced")
        self.assertEqual(migrated["logical"]["legacy"]["state"], "fenced")

    def test_workspace_scan_cannot_miss_a_concurrent_legacy_migration(self) -> None:
        legacy = self.control("legacy-race")
        brief = self.brief([self.node("writer", "A01", "a.txt")])
        legacy.create_plan(self.repo, brief)
        native = legacy.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(legacy, native)
        legacy.state_path.replace(self.state_root / "legacy-race.json")
        legacy = self.control("legacy-race")

        contender = self.control("legacy-race-contender")
        contender.create_plan(self.repo, brief)
        original_snapshot = control_plane_module._state_json_paths
        migrated = False
        inside_migration = False

        def racing_snapshot(path: Path) -> list[Path]:
            nonlocal inside_migration, migrated
            snapshot = original_snapshot(path)
            if (
                path == self.state_root
                and not migrated
                and not inside_migration
            ):
                inside_migration = True
                migrated = True
                try:
                    self.assertTrue(legacy.owner_is_managed(owner))
                finally:
                    inside_migration = False
            return snapshot

        with (
            patch.object(
                control_plane_module,
                "_state_json_paths",
                side_effect=racing_snapshot,
            ),
            self.assertRaises(ControlPlaneUnavailable),
        ):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertTrue(migrated)
        with self.assertRaisesRegex(ControlPlaneError, "writer lease"):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )

    def test_completed_legacy_migration_recovers_an_identical_duplicate(self) -> None:
        session = "legacy-crash-window"
        control = self.control(session)
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        canonical = control.state_path
        legacy_state = json.loads(canonical.read_text(encoding="utf-8"))
        legacy_state["revision"] -= 1
        legacy_path = self.state_root / f"{session}.json"
        legacy_path.write_text(json.dumps(legacy_state), encoding="utf-8")

        recovered = self.control(session)
        self.assertEqual(recovered.status()["state"], "ready")
        self.assertFalse(legacy_path.exists())
        self.assertEqual(recovered.state_path, canonical)

    def test_unindexed_invalid_legacy_state_is_quarantined(self) -> None:
        malformed = self.state_root / "old-unindexable.json"
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        malformed.write_text('{"protocol":', encoding="utf-8")
        control = self.control("quarantine-legacy")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )

        batch = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )

        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertFalse(malformed.exists())
        self.assertEqual(len(list((self.state_root / "quarantine").glob("*.json"))), 1)

    def test_unmarked_shared_state_does_not_move_unrelated_json(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        unrelated = self.state_root / "notes.json"
        unrelated.write_text('{"not":"cco"}', encoding="utf-8")
        control = self.control("shared-state-root")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )

        batch = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )

        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertTrue(unrelated.exists())
        self.assertFalse((self.state_root / "quarantine").exists())

    def test_unmarked_shared_state_still_honors_a_valid_legacy_writer(self) -> None:
        legacy = self.control("legacy-writer-in-shared-root")
        brief = self.brief([self.node("writer", "A01", "a.txt")])
        legacy.create_plan(self.repo, brief)
        native = legacy.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(legacy, native)
        legacy.state_path.replace(self.state_root / "legacy-writer.json")
        (self.state_root / ".cco-state-root-v1").unlink()
        unrelated = self.state_root / "notes.json"
        unrelated.write_text('{"not":"cco"}', encoding="utf-8")

        contender = self.control("indexed-contender-in-shared-root")
        contender.create_plan(self.repo, brief)

        with self.assertRaisesRegex(ControlPlaneError, "writer lease"):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertTrue(unrelated.exists())
        self.assertFalse((self.state_root / ".cco-state-root-v1").exists())

    def test_marker_probe_does_not_hide_temporary_legacy_io_failure(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / "legacy.json").write_text("{}", encoding="utf-8")
        control = self.control("unavailable-marker-probe")

        with (
            patch.object(
                control_plane_module,
                "_load_object",
                side_effect=ControlPlaneUnavailable("temporarily unavailable"),
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "temporarily unavailable"),
        ):
            control._mark_state_root_if_safe()

    def test_state_root_budget_blocks_preflight_before_a_native_claim(self) -> None:
        control = self.control("bounded-state-root")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        (self.state_root / "unrelated.json").write_text("{}", encoding="utf-8")

        with (
            patch.object(
                control_plane_module,
                "MAX_STATE_FILES",
                1,
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "file limit"),
        ):
            control.preflight_spawn(
                {
                    "tool_input": native,
                    "tool_use_id": "bounded-state-call",
                }
            )

        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        dispatch = next(iter(state["dispatches"].values()))
        self.assertIsNone(dispatch["tool_use_id"])

    def test_state_root_at_capacity_rejects_plan_before_writing_artifacts(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        (self.state_root / "occupied.json").write_text("{}", encoding="utf-8")
        control = self.control("capacity-before-plan")

        with (
            patch.object(control_plane_module, "MAX_STATE_FILES", 1),
            self.assertRaisesRegex(ControlPlaneUnavailable, "file limit"),
        ):
            control.create_plan(
                self.repo,
                self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
            )

        self.assertEqual(
            list((self.state_root / "artifacts").glob("capacity-before-plan-*.json"))
            if (self.state_root / "artifacts").exists()
            else [],
            [],
        )
        self.assertEqual(len(list(self.state_root.glob("*.json"))), 1)

    def test_state_root_capacity_is_atomic_across_sessions(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        controls = [self.control("capacity-one"), self.control("capacity-two")]
        barrier = threading.Barrier(2)
        original_atomic_write = control_plane_module._atomic_write
        outcomes: list[str] = []

        def synchronized_state_write(path: Path, value: object) -> None:
            if path.parent == self.state_root and path.suffix == ".json":
                try:
                    barrier.wait(timeout=0.25)
                except threading.BrokenBarrierError:
                    pass
            original_atomic_write(path, value)

        def create(control: ControlPlane) -> None:
            try:
                control.create_plan(
                    self.repo,
                    self.brief(
                        [self.node("reader", "A01", "a.txt", role="explorer")]
                    ),
                )
            except ControlPlaneUnavailable:
                outcomes.append("full")
            else:
                outcomes.append("created")

        with (
            patch.object(control_plane_module, "MAX_STATE_FILES", 1),
            patch.object(
                control_plane_module,
                "_atomic_write",
                side_effect=synchronized_state_write,
            ),
        ):
            threads = [threading.Thread(target=create, args=(control,)) for control in controls]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["created", "full"])
        self.assertEqual(len(list(self.state_root.glob("*.json"))), 1)

    def test_over_capacity_root_still_allows_current_status_and_cleanup(self) -> None:
        control = self.control("over-capacity-recovery")
        control.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        state_path = control.state_path
        (self.state_root / "overflow.json").write_text("{}", encoding="utf-8")
        recovered = self.control("over-capacity-recovery")

        with patch.object(control_plane_module, "MAX_STATE_FILES", 1):
            self.assertEqual(recovered.status()["state"], "ready")
            self.assertGreaterEqual(recovered.cleanup(), 2)

        self.assertFalse(state_path.exists())
        self.assertTrue((self.state_root / "overflow.json").exists())

    def test_quarantine_never_overwrites_an_earlier_payload(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        path = self.state_root / "old.json"
        control = self.control("quarantine-no-replace")
        path.write_text('{"first":', encoding="utf-8")
        control._workspace_state_candidates(self.repo)
        path.write_text('{"second":', encoding="utf-8")
        control._workspace_state_candidates(self.repo)

        quarantined = list((self.state_root / "quarantine").glob("*.json"))
        self.assertEqual(len(quarantined), 2)

    def test_quarantine_does_not_delete_a_repaired_replacement(self) -> None:
        source = self.control("quarantine-replacement-source")
        source.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        repaired = source.state_path.read_bytes()
        source.state_path.unlink()
        legacy = self.state_root / "replace-during-quarantine.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        original_replace = os.replace

        def replace_then_repair(source_path: object, destination: object) -> None:
            original_replace(source_path, destination)
            if Path(source_path) == legacy:
                legacy.write_bytes(repaired)

        with patch.object(
            control_plane_module.os,
            "replace",
            side_effect=replace_then_repair,
        ):
            candidates = source._workspace_state_candidates(self.repo)

        self.assertTrue(legacy.exists())
        self.assertEqual(legacy.read_bytes(), repaired)
        self.assertIn(source.session_id, {state["session_id"] for _, state in candidates})

    def test_quarantine_restores_a_valid_state_moved_by_the_race(self) -> None:
        source = self.control("quarantine-valid-move-source")
        source.create_plan(
            self.repo,
            self.brief([self.node("reader", "A01", "a.txt", role="explorer")]),
        )
        repaired = source.state_path.read_bytes()
        source.state_path.unlink()
        legacy = self.state_root / "repair-before-quarantine-move.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        original_replace = os.replace

        def repair_then_replace(source_path: object, destination: object) -> None:
            if Path(source_path) == legacy:
                legacy.write_bytes(repaired)
            original_replace(source_path, destination)

        with patch.object(
            control_plane_module.os,
            "replace",
            side_effect=repair_then_replace,
        ):
            candidates = source._workspace_state_candidates(self.repo)

        self.assertFalse(legacy.exists())
        self.assertTrue(source.state_path.exists())
        self.assertEqual(source.state_path.read_bytes(), repaired)
        self.assertIn(source.session_id, {state["session_id"] for _, state in candidates})

    def test_quarantine_failure_keeps_a_recovered_writer_visible(self) -> None:
        source = self.control("quarantine-crash-safe-writer")
        brief = self.brief([self.node("writer", "A01", "a.txt")])
        source.create_plan(self.repo, brief)
        native = source.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(source, native)
        repaired = source.state_path.read_bytes()
        source.state_path.unlink()
        legacy = self.state_root / "repair-before-link.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        original_replace = os.replace

        def repair_then_replace(source_path: object, destination: object) -> None:
            if Path(source_path) == legacy:
                legacy.write_bytes(repaired)
            original_replace(source_path, destination)

        with (
            patch.object(
                control_plane_module.os,
                "replace",
                side_effect=repair_then_replace,
            ),
            patch.object(
                control_plane_module.os,
                "link",
                side_effect=OSError("simulated publication failure"),
                create=True,
            ),
            self.assertRaisesRegex(ControlPlaneUnavailable, "could not be restored"),
        ):
            source._workspace_state_candidates(self.repo)

        recovery_files = list(self.state_root.glob(".cco-recovery-*.json"))
        self.assertEqual(len(recovery_files), 1)
        contender = self.control("quarantine-crash-safe-contender")
        contender.create_plan(self.repo, brief)
        with self.assertRaisesRegex(ControlPlaneError, "writer lease"):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )

    def test_recovery_publication_replays_after_finalization_failure(self) -> None:
        source = self.control("quarantine-replay-writer")
        source.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = source.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(source, native)
        repaired = source.state_path.read_bytes()
        source.state_path.unlink()
        legacy = self.state_root / "repair-before-finalize.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        original_replace = os.replace
        original_unlink = Path.unlink
        failed = False

        def repair_then_replace(source_path: object, destination: object) -> None:
            if Path(source_path) == legacy:
                legacy.write_bytes(repaired)
            original_replace(source_path, destination)

        def fail_recovery_unlink(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal failed
            if path.name.startswith(".cco-recovery-") and path.suffix == ".json" and not failed:
                failed = True
                raise OSError("simulated finalization failure")
            original_unlink(path, *args, **kwargs)

        with (
            patch.object(
                control_plane_module.os,
                "replace",
                side_effect=repair_then_replace,
            ),
            patch.object(Path, "unlink", new=fail_recovery_unlink),
            self.assertRaisesRegex(ControlPlaneUnavailable, "finalization failed"),
        ):
            source._workspace_state_candidates(self.repo)

        self.assertEqual(len(list(self.state_root.glob(".cco-recovery-*.json"))), 1)
        recovered = self.control("recovery-reader")
        candidates = recovered._workspace_state_candidates(self.repo)
        self.assertIn(source.session_id, {state["session_id"] for _, state in candidates})
        self.assertTrue(source.state_path.exists())
        self.assertEqual(list(self.state_root.glob(".cco-recovery-*.json")), [])

    def test_current_session_discovers_an_active_recovery_state(self) -> None:
        resumed = self.control("recovery-owned-writer")
        self.assertFalse(resumed.state_path.exists())
        source = self.control("recovery-owned-writer")
        source.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        native = source.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(source, native)
        recovery = self.state_root / ".cco-recovery-owned.json"
        os.replace(source.state_path, recovery)

        self.assertEqual(resumed.status()["counts"]["running"], 1)
        self.assertTrue(resumed.owner_is_managed(owner))
        with self.assertRaisesRegex(ControlPlaneError, "already has CCO lifecycle proof"):
            resumed.create_plan(
                self.repo,
                self.brief([self.node("replacement", "A02", "b.txt")]),
            )

    def test_unrelated_high_revision_state_cannot_delete_active_recovery(self) -> None:
        source = self.control("recovery-lineage-writer")
        brief = self.brief([self.node("writer", "A01", "a.txt")])
        source.create_plan(self.repo, brief)
        native = source.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(source, native)
        recovery = self.state_root / ".cco-recovery-lineage.json"
        os.replace(source.state_path, recovery)
        unrelated = json.loads(recovery.read_text(encoding="utf-8"))
        unrelated["plan_id"] = "sha256:" + "f" * 64
        unrelated.pop("lineage_id", None)
        unrelated["revision"] += 100
        unrelated["dispatches"] = {}
        for logical in unrelated["logical"].values():
            logical["dispatch_id"] = None
            logical["result"] = None
            logical["state"] = "ready"
        canonical = control_plane_module._lifecycle_state_path(
            self.state_root,
            self.repo,
            source.session_id,
        )
        canonical.write_text(
            json.dumps(unrelated, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        contender = self.control("recovery-lineage-contender")
        contender.create_plan(self.repo, brief)
        with self.assertRaisesRegex(ControlPlaneError, "conflicting lifecycle recovery"):
            contender.next_wave(
                capacity=1,
                native_catalog=catalog("gpt-5.6-terra"),
            )
        self.assertTrue(recovery.exists())

    def test_recovery_capacity_is_reserved_before_quarantine_move(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        (self.state_root / ".cco-recovery-existing.json").write_text(
            "{}", encoding="utf-8"
        )
        legacy = self.state_root / "legacy-invalid.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        control = self.control("recovery-capacity")
        original_read = control_plane_module._read_bounded_bytes

        def fail_staged_read(path: Path, label: str, **kwargs: object) -> bytes:
            if path.name.startswith(".cco-recovery-") and path.suffix == ".json":
                raise ControlPlaneUnavailable("simulated staged read failure")
            return original_read(path, label, **kwargs)

        with (
            patch.object(control_plane_module, "MAX_RECOVERY_FILES", 1),
            patch.object(
                control_plane_module,
                "_read_bounded_bytes",
                side_effect=fail_staged_read,
            ),
            self.assertRaises(ControlPlaneUnavailable),
        ):
            control._quarantine_legacy_state(legacy)

        self.assertTrue(legacy.exists())
        self.assertEqual(len(list(self.state_root.glob(".cco-recovery-*.json"))), 1)

    def test_quarantining_a_recovery_at_the_exact_limit_needs_no_new_slot(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        source = self.state_root / ".cco-recovery-source.json"
        source.write_text('{"broken":', encoding="utf-8")
        (self.state_root / ".cco-recovery-peer.json").write_text(
            '{"also-broken":', encoding="utf-8"
        )

        with patch.object(control_plane_module, "MAX_RECOVERY_FILES", 2):
            self.control("recovery-at-limit")._quarantine_legacy_state(source)

        self.assertFalse(source.exists())
        self.assertEqual(len(list(self.state_root.glob(".cco-recovery-*.json"))), 1)
        self.assertEqual(len(list((self.state_root / "quarantine").glob("*.json"))), 1)

    def test_foreign_workspace_scan_does_not_replay_recovery(self) -> None:
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        foreign = self.control("foreign-recovery")
        foreign.create_plan(
            other_repo,
            self.brief([self.node("writer", "A01", "other.txt")]),
        )
        native = foreign.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(foreign, native)
        canonical = foreign.state_path
        recovery = self.state_root / ".cco-recovery-foreign-workspace.json"
        os.replace(canonical, recovery)

        self.control("local-scan")._workspace_state_candidates(self.repo)

        self.assertTrue(recovery.exists())
        self.assertFalse(canonical.exists())

    def test_same_session_foreign_recovery_cannot_replace_cached_workspace(self) -> None:
        session = "same-session-cross-workspace"
        active = self.control(session)
        active.create_plan(
            self.repo,
            self.brief([self.node("writer_a", "A01", "a.txt")]),
        )
        native = active.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, owner = self.start_dispatch(active, native)
        canonical_a = active.state_path
        self.assertTrue(active.owner_is_managed(owner))

        other_repo = self.root / "same-session-other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        foreign_root = self.root / "foreign-state-root"
        foreign = ControlPlane(session, root=foreign_root)
        foreign.create_plan(
            other_repo,
            self.brief([self.node("writer_b", "A02", "other.txt")]),
        )
        foreign_native = foreign.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.start_dispatch(foreign, foreign_native)
        recovery = self.state_root / ".cco-recovery-other-workspace-same-session.json"
        recovery.write_bytes(foreign.state_path.read_bytes())
        canonical_b = control_plane_module._lifecycle_state_path(
            self.state_root,
            other_repo,
            session,
        )

        with self.assertRaisesRegex(ControlPlaneError, "multiple workspaces"):
            active.owner_is_managed(owner)

        self.assertTrue(canonical_a.exists())
        self.assertTrue(recovery.exists())
        self.assertFalse(canonical_b.exists())

        legacy_a = self.state_root / f"{session}.json"
        os.replace(canonical_a, legacy_a)
        with self.assertRaisesRegex(ControlPlaneError, "multiple workspaces"):
            self.control(session).stop_reason()
        self.assertTrue(legacy_a.exists())
        self.assertTrue(recovery.exists())
        self.assertFalse(canonical_b.exists())

    def test_repaired_foreign_legacy_state_is_not_replayed_under_current_lock(self) -> None:
        other_repo = self.root / "quarantine-other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        foreign = ControlPlane("foreign-quarantine", root=self.root / "foreign-quarantine")
        foreign.create_plan(
            other_repo,
            self.brief([self.node("writer", "A01", "other.txt")]),
        )
        repaired = foreign.state_path.read_bytes()
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / ".cco-state-root-v1").write_bytes(b"cco.state-root.v1\n")
        legacy = self.state_root / "repair-to-foreign-workspace.json"
        legacy.write_text('{"broken":', encoding="utf-8")
        control = self.control("quarantine-current-workspace")
        original_replace = os.replace

        def repair_then_replace(source: object, destination: object) -> None:
            if Path(source) == legacy:
                legacy.write_bytes(repaired)
            original_replace(source, destination)

        with (
            patch.object(
                control_plane_module.os,
                "replace",
                side_effect=repair_then_replace,
            ),
            patch.object(
                control,
                "_replay_recovery_state",
                side_effect=AssertionError("foreign recovery replayed under current lock"),
            ),
        ):
            candidates = control._workspace_state_candidates(self.repo)

        self.assertEqual(candidates, [])
        self.assertEqual(len(list(self.state_root.glob(".cco-recovery-*.json"))), 1)

    def test_recovery_publication_waits_for_authoritative_hook_decision(self) -> None:
        session = "recovery-publication-linearized"
        local = self.control(session)
        local.create_plan(
            self.repo,
            self.brief([self.node("local", "A01", "a.txt", role="explorer")]),
        )

        other_repo = self.root / "publication-other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        foreign = ControlPlane(session, root=self.root / "publication-foreign-root")
        foreign.create_plan(
            other_repo,
            self.brief([self.node("foreign", "A02", "other.txt")]),
        )
        foreign_native = foreign.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        _dispatch_id, foreign_owner = self.start_dispatch(foreign, foreign_native)
        repaired = foreign.state_path.read_bytes()

        hook_control = self.control(session)
        publisher = self.control("recovery-publication-scanner")
        legacy = self.state_root / f"{session}.json"
        read_entered = threading.Event()
        release_read = threading.Event()
        published = threading.Event()
        errors: list[BaseException] = []
        managed: list[bool] = []
        original_read = hook_control._read_state

        def block_authoritative_read(
            *, expected_workspace: object | None = None
        ) -> dict[str, object]:
            read_entered.set()
            if not release_read.wait(timeout=5):
                raise TimeoutError("test authoritative-read barrier timed out")
            return original_read(expected_workspace=expected_workspace)

        def run_hook_decision() -> None:
            try:
                managed.append(hook_control.owner_is_managed(foreign_owner))
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def publish_recovery() -> None:
            try:
                legacy.write_bytes(repaired)
                publisher._quarantine_legacy_state(legacy)
                published.set()
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch.object(hook_control, "_read_state", side_effect=block_authoritative_read):
            hook_thread = threading.Thread(target=run_hook_decision)
            hook_thread.start()
            self.assertTrue(read_entered.wait(timeout=5))
            publication_thread = threading.Thread(target=publish_recovery)
            publication_thread.start()
            try:
                self.assertFalse(
                    published.wait(timeout=0.2),
                    "recovery publication overtook the authoritative Hook decision",
                )
            finally:
                release_read.set()
            hook_thread.join(timeout=5)
            publication_thread.join(timeout=5)

        self.assertFalse(hook_thread.is_alive())
        self.assertFalse(publication_thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ControlPlaneError)
        self.assertIn("conflicting lifecycle state", str(errors[0]))
        self.assertEqual(managed, [])
        self.assertTrue(published.is_set())

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

    def test_workspace_switch_is_rejected_before_foreign_recovery_replay(self) -> None:
        session = "workspace-switch-before-replay"
        local = self.control(session)
        local.create_plan(
            self.repo,
            self.brief([self.node("local", "A01", "a.txt", role="explorer")]),
        )
        canonical = local.state_path

        other_repo = self.root / "workspace-switch-other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        foreign = ControlPlane(session, root=self.root / "workspace-switch-foreign-root")
        foreign.create_plan(
            other_repo,
            self.brief([self.node("foreign", "A02", "other.txt", role="explorer")]),
        )
        foreign_state = foreign.state_path.read_bytes()
        recovery = self.state_root / ".cco-recovery-workspace-switch.json"
        original_hint = local._workspace_hint

        def switch_after_hint() -> str:
            workspace = original_hint()
            canonical.unlink()
            recovery.write_bytes(foreign_state)
            return workspace

        with (
            patch.object(local, "_workspace_hint", side_effect=switch_after_hint),
            patch.object(
                local,
                "_replay_recovery_state",
                side_effect=AssertionError("foreign workspace replay attempted"),
            ),
            self.assertRaisesRegex(
                ControlPlaneError,
                "lifecycle workspace changed during coordination",
            ),
        ):
            local.status()

    def test_unmarked_full_root_keeps_valid_recovery_in_its_existing_slot(self) -> None:
        other_repo = self.root / "unmarked-recovery-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
        (other_repo / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        session = "unmarked-recovery"
        source_root = self.root / "unmarked-source"
        source = ControlPlane(session, root=source_root)
        source.create_plan(
            other_repo,
            self.brief([self.node("reader", "A01", "other.txt", role="explorer")]),
        )
        unmarked_root = self.root / "unmarked-full-root"
        unmarked_root.mkdir()
        (unmarked_root / "occupied.json").write_text("{}", encoding="utf-8")
        recovery = unmarked_root / ".cco-recovery-valid.json"
        recovery.write_bytes(source.state_path.read_bytes())
        canonical = control_plane_module._lifecycle_state_path(
            unmarked_root,
            other_repo,
            session,
        )
        recovered = ControlPlane(session, root=unmarked_root)

        with patch.object(control_plane_module, "MAX_STATE_FILES", 1):
            self.assertEqual(recovered.status()["state"], "ready")

        self.assertTrue(recovery.exists())
        self.assertFalse(canonical.exists())
        self.assertFalse((unmarked_root / ".cco-state-root-v1").exists())

    def test_recovery_replay_serializes_with_its_own_workspace_writer(self) -> None:
        owner = self.control("recovery-race-owner")
        owner.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        canonical = owner.state_path
        writer = self.control("recovery-race-owner")
        self.assertEqual(writer.status()["epoch"], 1)
        canonical_raw = canonical.read_bytes()
        recovery_state = json.loads(canonical_raw.decode("utf-8"))
        recovery_state["revision"] += 1
        recovery_state["epoch"] = 11
        recovery_state["parent_state_sha256"] = (
            "sha256:" + hashlib.sha256(canonical_raw).hexdigest()
        )
        recovery = self.state_root / ".cco-recovery-workspace-race.json"
        recovery.write_text(
            json.dumps(recovery_state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        replay = self.control("recovery-race-reader")
        entered = threading.Event()
        release = threading.Event()
        writer_done = threading.Event()
        errors: list[BaseException] = []
        original_replace = os.replace

        def blocking_replace(source: object, destination: object) -> None:
            if Path(source) == recovery and Path(destination) == canonical:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test recovery barrier timed out")
            original_replace(source, destination)

        def replay_recovery() -> None:
            try:
                replay._replay_recovery_state(recovery, recovery_state)
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        def update_owner() -> None:
            try:
                with writer._coordinated_state() as state:
                    state["epoch"] = 99
                    writer._write_state(state)
                writer_done.set()
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch.object(control_plane_module.os, "replace", side_effect=blocking_replace):
            replay_thread = threading.Thread(target=replay_recovery)
            writer_thread = threading.Thread(target=update_owner)
            replay_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            writer_thread.start()
            try:
                self.assertFalse(writer_done.wait(timeout=0.2))
            finally:
                release.set()
            replay_thread.join(timeout=5)
            writer_thread.join(timeout=5)

        self.assertFalse(replay_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(canonical.read_text(encoding="utf-8"))["epoch"], 99)

    def test_same_lineage_recovery_requires_direct_parent_proof(self) -> None:
        control = self.control("recovery-parent-proof")
        control.create_plan(
            self.repo,
            self.brief([self.node("writer", "A01", "a.txt")]),
        )
        canonical = control.state_path
        canonical_raw = canonical.read_bytes()
        recovery_state = json.loads(canonical_raw.decode("utf-8"))
        recovery_state["revision"] += 1
        recovery_state["parent_state_sha256"] = "sha256:" + ("0" * 64)
        recovery = self.state_root / ".cco-recovery-wrong-parent.json"
        recovery.write_text(
            json.dumps(recovery_state, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ControlPlaneError, "conflicting lifecycle recovery"):
            control._replay_recovery_state(recovery, recovery_state)

        self.assertEqual(canonical.read_bytes(), canonical_raw)
        self.assertTrue(recovery.exists())

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
        self.assertFalse(list((self.state_root / "artifacts").glob("session-v9-*.json")))

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

    def test_retry_interrupt_settles_already_interrupted_native_owner(self) -> None:
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
        first.preflight_interrupt(
            {"tool_input": {"target": owner}, "tool_use_id": "recovery-interrupt"}
        )
        first.postflight_interrupt(
            {
                "tool_input": {"target": owner},
                "tool_response": {"previous_status": "interrupted"},
                "tool_use_id": "recovery-interrupt",
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

    def test_active_v1_wave_can_finish_after_upgrade(self) -> None:
        control = self.control("legacy-wave-settlement")
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
        self.downgrade_active_wave_to_v1(control)

        dispatch_id, owner = self.start_dispatch(control, native)
        result = control.record_result(
            owner,
            result_text(dispatch_id, evidence={"A01": "legacy wave settled"}),
        )

        self.assertEqual(result["state"], "retired")
        self.assertEqual(control.status()["state"], "complete")

    def test_real_v1_prepared_continuation_restores_context_from_wave(self) -> None:
        control = self.control("legacy-v1-continuation")
        node = self.node("n01", "A01", "a.txt")
        node["context_turns"] = 3
        control.create_plan(self.repo, self.brief([node]))
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
        self.assertEqual(set(prepared["tool_input"]), {"message", "target"})
        self.downgrade_active_wave_to_v1(control)

        upgraded = self.control("legacy-v1-continuation")
        self.assertEqual(upgraded.status()["counts"]["paused"], 1)
        migrated = json.loads(upgraded.state_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["dispatches"][dispatch_id]["context_turns"], 3)
        upgraded.preflight_continuation(
            {
                "tool_input": prepared["tool_input"],
                "tool_use_id": "legacy-followup",
            }
        )
        upgraded.postflight_tool(
            {
                "tool_input": prepared["tool_input"],
                "tool_response": {"ok": True},
                "tool_use_id": "legacy-followup",
            }
        )
        result = upgraded.record_result(
            owner,
            result_text(
                dispatch_id,
                cursor=1,
                evidence={"A01": "legacy continuation settled"},
            ),
        )

        self.assertEqual(result["state"], "retired")
        self.assertEqual(upgraded.status()["state"], "complete")

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

    def test_wave_snapshot_content_must_match_its_bound_state_id(self) -> None:
        control = self.control()
        control.create_plan(self.repo, self.brief([self.node("n01", "A01", "a.txt")]))
        native = control.next_wave(
            capacity=1,
            native_catalog=catalog("gpt-5.6-terra"),
        )["dispatches"][0]["tool_input"]
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
        )["dispatches"][0]["tool_input"]
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
        )["dispatches"][0]["tool_input"]
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

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
sys.path[:0] = [str(HOOKS), str(SCRIPTS)]

import agent_preflight  # noqa: E402
import ledger_runtime  # noqa: E402
import subagent_stop  # noqa: E402
from graph_compiler import prepare_dispatch_graph  # noqa: E402
from packet_compiler import (  # noqa: E402
    CapsuleError,
    capsule_sha256,
    compile_continuation,
    compile_result,
    parse_message,
)
from protocol_hash import canonical_bytes  # noqa: E402


def native_catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-luna",
                "supported_reasoning_levels": [{"effort": "max"}],
            },
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-terra",
                "supported_reasoning_levels": [{"effort": "max"}],
            },
        ]
    }


def no_risks() -> dict[str, str]:
    return {
        name: "no"
        for name in (
            "authentication_authorization",
            "build_release",
            "concurrency",
            "dependency_boundary",
            "destructive_data",
            "external_side_effect",
            "migration",
            "nondeterministic_verification",
            "public_interface",
            "schema",
            "security",
        )
    }


def render_capsule(capsule: dict[str, object]) -> str:
    value = deepcopy(capsule)
    value["capsule_sha256"] = capsule_sha256(value)
    return (
        "CCO_DISPATCH cco.v7\n"
        f"CAPSULE_SHA256: {value['capsule_sha256']}\n"
        f"CAPSULE_JSON: {canonical_bytes(value).decode('utf-8')}"
    )


def node(
    name: str,
    path: str,
    *,
    role: str = "worker",
    epoch: str | None = None,
    decision_space: str = "acceptance_equivalent",
    events: list[str] | None = None,
    generation: int = 1,
) -> dict[str, object]:
    value: dict[str, object] = {
        "acceptance_facts": {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "events": list(events or []),
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": no_risks(),
        },
        "closure": {
            "acceptance_closed": True,
            "criteria_closed": True,
            "decision_space": decision_space,
            "interfaces_closed": True,
            "objective_closed": True,
            "ownership_closed": True,
        },
        "contract": {"contract_rev": 1, "node": name, "objective": f"handle {path}"},
        "generation": generation,
        "node": name,
        "placement": {
            "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
            "direct_action_count": 2,
            "direct_verification_count": 1,
        },
        "role": role,
        "scopes": [{"kind": "exact", "path": path}],
        "selection": {
            "depends_on": [],
            "responsibility": name,
        },
    }
    if epoch is not None:
        value["epoch"] = epoch
    return value


class V7LifecycleTests(unittest.TestCase):
    def test_global_lifecycle_hooks_are_noops_outside_a_git_repository_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            ledger_root = root / "ledger"
            payload = {
                "cwd": str(workspace),
                "session_id": "outside-repository",
            }

            with mock.patch.dict(os.environ, {"CCO_LEDGER_DIR": str(ledger_root)}):
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {**payload, "hook_event_name": "UserPromptSubmit"}
                    ),
                    {},
                )
                self.assertEqual(
                    ledger_runtime.evaluate({**payload, "hook_event_name": "SessionEnd"}),
                    {},
                )

    def test_session_end_removes_terminal_session_artifacts_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            ledger_root = root / "ledger"
            workspace = root / "workspace"
            workspace.mkdir()
            artifact = workspace / ("ended-task-" + "a" * 64 + ".json")
            artifact.write_text("{}", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CCO_LEDGER_DIR": str(ledger_root)}):
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            "cwd": str(repo),
                            "hook_event_name": "SessionEnd",
                            "session_id": "ended-task",
                        }
                    ),
                    {},
                )

            self.assertFalse(artifact.exists())

    def test_corrected_second_subagent_stop_is_validated_and_retires_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("ready\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-stop-corrected"}
            with mock.patch.dict(os.environ, env):
                dispatch = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                capsule = parse_message(dispatch["message"])
                owner = "/root/" + dispatch["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-stop-corrected",
                    "tool_input": dispatch,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-stop-corrected",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )

                first_stop = {
                    "agent_id": owner,
                    "agent_type": dispatch["agent_type"],
                    "cwd": str(repo),
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "not a CCO result",
                    "session_id": "v7-stop-corrected",
                    "stop_hook_active": False,
                }
                first_outcome = subagent_stop.evaluate(first_stop)
                self.assertEqual(first_outcome["decision"], "block")
                self.assertEqual(ledger_runtime.ledger_for(first_stop).read_rows()[0]["state"], "owned")

                (repo / "owned.txt").write_text("corrected\n", encoding="utf-8")
                corrected = compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["owned.txt"],
                    deviations=[],
                    evidence={"A01": "corrected second stop has the exact owned-file delta"},
                    failure_signature=None,
                    summary="corrected result",
                )
                self.assertEqual(
                    subagent_stop.evaluate(
                        {**first_stop, "last_assistant_message": corrected, "stop_hook_active": True}
                    ),
                    {},
                )
                self.assertEqual(ledger_runtime.ledger_for(first_stop).read_rows()[0]["state"], "retired")

    def test_still_invalid_second_subagent_stop_retires_fences_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("ready\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-stop-invalid"}
            with mock.patch.dict(os.environ, env):
                prepared = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt", decision_space="bounded_effect")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                dispatch = prepared["dispatches"][0]
                artifact = Path(prepared["baseline_path"])
                owner = "/root/" + dispatch["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-stop-invalid",
                    "tool_input": dispatch,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-stop-invalid",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )

                invalid_stop = {
                    "agent_id": owner,
                    "agent_type": dispatch["agent_type"],
                    "cwd": str(repo),
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "still not a CCO result",
                    "session_id": "v7-stop-invalid",
                    "stop_hook_active": False,
                }
                self.assertEqual(subagent_stop.evaluate(invalid_stop)["decision"], "block")
                self.assertTrue(artifact.exists())

                warning = subagent_stop.evaluate({**invalid_stop, "stop_hook_active": True})
                self.assertNotIn("decision", warning)
                self.assertIn("WARNING", warning["systemMessage"])
                self.assertFalse(artifact.exists())

                ledger = ledger_runtime.ledger_for(invalid_stop)
                self.assertEqual(ledger.read_rows()[0]["state"], "retired")
                document = json.loads(ledger.path.read_text(encoding="utf-8"))
                self.assertIn(owner, document["fenced_owners"])
                self.assertIn({"node": "n01_worker", "role": "worker"}, document["guarded_floors"])

                unguarded = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt", decision_space="bounded_effect", generation=2)],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                blocked = agent_preflight.evaluate(
                    {**preflight, "tool_input": unguarded, "tool_use_id": "spawn-stop-invalid-retry"}
                )
                self.assertEqual(blocked["decision"], "block")
                self.assertIn("guarded assurance", blocked["reason"])

    def test_light_graph_tracks_ignored_files_inside_typed_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
            (repo / "secret.txt").write_text("baseline\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-ignored"}
            with mock.patch.dict(os.environ, env):
                dispatch = prepare_dispatch_graph(
                    [node("n01_worker", "secret.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                    workspace_mode="light",
                )["dispatches"][0]
                capsule = parse_message(dispatch["message"])
                owner = "/root/" + dispatch["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-ignored",
                    "tool_input": dispatch,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-ignored",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )
                (repo / "secret.txt").write_text("changed\n", encoding="utf-8")
                result = compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["secret.txt"],
                    deviations=[],
                    evidence={"A01": "ignored scoped file changed and verified"},
                    failure_signature=None,
                    summary="completed ignored-file contract",
                )
                self.assertEqual(
                    subagent_stop.evaluate(
                        {
                            "agent_id": owner,
                            "agent_type": dispatch["agent_type"],
                            "cwd": str(repo),
                            "hook_event_name": "SubagentStop",
                            "last_assistant_message": result,
                            "session_id": "v7-ignored",
                            "stop_hook_active": False,
                        }
                    ),
                    {},
                )

    def test_result_blocks_a_new_delta_outside_the_whole_graph_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("owned\n", encoding="utf-8")
            (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-outside"}
            with mock.patch.dict(os.environ, env):
                dispatch = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                capsule = parse_message(dispatch["message"])
                owner = "/root/" + dispatch["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-outside",
                    "tool_input": dispatch,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-outside",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )
                (repo / "outside.txt").write_text("unexpected\n", encoding="utf-8")
                result = compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=[],
                    deviations=[],
                    evidence={"A01": "claimed no owned delta"},
                    failure_signature=None,
                    summary="claimed completion",
                )
                blocked = subagent_stop.evaluate(
                    {
                        "agent_id": owner,
                        "agent_type": dispatch["agent_type"],
                        "cwd": str(repo),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": result,
                        "session_id": "v7-outside",
                        "stop_hook_active": False,
                    }
                )
                self.assertEqual(blocked["decision"], "block")
                self.assertIn("outside_lease:outside.txt", blocked["reason"])

    def test_terminal_graph_keeps_tombstones_and_deletes_artifact_only_after_last_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "one.txt").write_text("one\n", encoding="utf-8")
            (repo / "two.txt").write_text("two\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-lifecycle"}
            with mock.patch.dict(os.environ, env):
                prepared = prepare_dispatch_graph(
                    [node("n01_worker", "one.txt"), node("n02_worker", "two.txt")],
                    native_capacity=2,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                artifact = Path(prepared["baseline_path"])
                dispatches = prepared["dispatches"]
                owners: list[str] = []
                for index, dispatch in enumerate(dispatches, start=1):
                    preflight = {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "v7-lifecycle",
                        "tool_input": dispatch,
                        "tool_name": "spawn_agent",
                        "tool_use_id": f"spawn-{index}",
                    }
                    self.assertEqual(agent_preflight.evaluate(preflight), {})
                    owner = "/root/" + dispatch["task_name"]
                    owners.append(owner)
                    self.assertEqual(
                        ledger_runtime.evaluate(
                            {
                                **preflight,
                                "hook_event_name": "PostToolUse",
                                "tool_response": {"task_path": owner},
                            }
                        ),
                        {},
                    )

                ledger = ledger_runtime.ledger_for(preflight)
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            "cwd": str(repo),
                            "hook_event_name": "PreToolUse",
                            "session_id": "v7-lifecycle",
                            "tool_input": {"target": "/root/native_bypass_owner"},
                            "tool_name": "interrupt_agent",
                        }
                    ),
                    {},
                )
                self.assertEqual([row["state"] for row in ledger.read_rows()], ["owned", "owned"])

                (repo / "one.txt").write_text("one changed\n", encoding="utf-8")
                first_capsule = parse_message(dispatches[0]["message"])
                first_message = compile_result(
                    first_capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["one.txt"],
                    deviations=[],
                    evidence={"A01": "owned file changed and verified"},
                    failure_signature=None,
                    summary="completed first worker contract",
                )
                first_stop = {
                    "agent_id": owners[0],
                    "agent_type": dispatches[0]["agent_type"],
                    "cwd": str(repo),
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": first_message,
                    "session_id": "v7-lifecycle",
                    "stop_hook_active": False,
                }
                wrong_paths = compile_result(
                    first_capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["two.txt"],
                    deviations=[],
                    evidence={"A01": "claimed the wrong file"},
                    failure_signature=None,
                    summary="incorrect path claim",
                )
                mismatch = subagent_stop.evaluate(
                    {**first_stop, "last_assistant_message": wrong_paths}
                )
                self.assertEqual(mismatch["decision"], "block")
                self.assertIn("exact node workspace delta", mismatch["reason"])
                self.assertEqual(subagent_stop.evaluate(first_stop), {})
                self.assertTrue(artifact.exists())

                (repo / "two.txt").write_text("two changed\n", encoding="utf-8")
                second_capsule = parse_message(dispatches[1]["message"])
                second_message = compile_result(
                    second_capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["two.txt"],
                    deviations=[],
                    evidence={"A01": "owned file changed and verified"},
                    failure_signature=None,
                    summary="completed second worker contract",
                )
                self.assertEqual(
                    subagent_stop.evaluate({**first_stop, "agent_id": owners[1], "last_assistant_message": second_message}),
                    {},
                )
                self.assertFalse(artifact.exists())

                ledger = ledger_runtime.ledger_for(first_stop)
                self.assertEqual([row["state"] for row in ledger.read_rows()], ["retired", "retired"])
                late = subagent_stop.evaluate(first_stop)
                self.assertEqual(late["decision"], "block")
                self.assertIn("stale", late["reason"])

                self.assertEqual(
                    ledger_runtime.evaluate(
                        {"cwd": str(repo), "hook_event_name": "Stop", "session_id": "v7-lifecycle"}
                    ),
                    {},
                )
                self.assertTrue(ledger.path.exists())
                raw_followup = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "v7-lifecycle",
                        "tool_input": {"message": "override", "target": owners[0]},
                        "tool_name": "followup_task",
                        "tool_use_id": "late-followup",
                    }
                )
                self.assertEqual(raw_followup["decision"], "block")
                self.assertIn("continuation capsule", raw_followup["reason"])

    def test_session_start_injects_the_gate_once_and_cleans_only_stale_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            ledger_root = root / "ledger"
            ledger_root.mkdir()
            stale = ledger_root / "old-session.json"
            stale.write_text('{"fenced_owners":[],"guarded_floors":[],"rows":{}}', encoding="utf-8")
            old_time = stale.stat().st_mtime - (25 * 60 * 60)
            os.utime(stale, (old_time, old_time))
            with mock.patch.dict(os.environ, {"CCO_LEDGER_DIR": str(ledger_root)}):
                outcome = ledger_runtime.evaluate(
                    {"cwd": str(repo), "hook_event_name": "SessionStart", "session_id": "new-session"}
                )

            hook = outcome["hookSpecificOutput"]
            self.assertEqual(hook["hookEventName"], "SessionStart")
            self.assertIn("cco.v7", hook["additionalContext"])
            self.assertIn("CCO_NATIVE_BYPASS v1", hook["additionalContext"])
            self.assertFalse(stale.exists())

    def test_failure_result_forces_a_guarded_new_generation_even_after_terra(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("ready\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-floor"}
            with mock.patch.dict(os.environ, env):
                first = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt", decision_space="bounded_effect")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                capsule = parse_message(first["message"])
                owner = "/root/" + first["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-floor",
                    "tool_input": first,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-floor-1",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )
                failed = compile_result(
                    capsule,
                    status="partial",
                    disposition="retire",
                    blockers=[],
                    changed_paths=[],
                    deviations=["quality_mismatch"],
                    evidence={},
                    failure_signature="quality:mismatch-v1",
                    summary="bounded implementation was not acceptable",
                )
                self.assertEqual(
                    subagent_stop.evaluate(
                        {
                            "agent_id": owner,
                            "agent_type": first["agent_type"],
                            "cwd": str(repo),
                            "hook_event_name": "SubagentStop",
                            "last_assistant_message": failed,
                            "session_id": "v7-floor",
                            "stop_hook_active": False,
                        }
                    ),
                    {},
                )

                unguarded = prepare_dispatch_graph(
                    [node("n01_worker", "owned.txt", decision_space="bounded_effect", generation=2)],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                blocked = agent_preflight.evaluate(
                    {**preflight, "tool_input": unguarded, "tool_use_id": "spawn-floor-2"}
                )
                self.assertEqual(blocked["decision"], "block")
                self.assertIn("guarded assurance", blocked["reason"])

                guarded = prepare_dispatch_graph(
                    [
                        node(
                            "n01_worker",
                            "owned.txt",
                            decision_space="bounded_effect",
                            events=["deviation"],
                            generation=2,
                        )
                    ],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )["dispatches"][0]
                self.assertEqual(parse_message(guarded["message"])["assurance"], "guarded")
                self.assertEqual(
                    agent_preflight.evaluate(
                        {**preflight, "tool_input": guarded, "tool_use_id": "spawn-floor-3"}
                    ),
                    {},
                )

    def test_continuation_advances_cursor_and_only_reviewer_can_accept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "review.txt").write_text("ready\n", encoding="utf-8")
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-review"}
            with mock.patch.dict(os.environ, env):
                prepared = prepare_dispatch_graph(
                    [node("review_release", "review.txt", role="reviewer", epoch="e01")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                dispatch = prepared["dispatches"][0]
                capsule = parse_message(dispatch["message"])
                owner = "/root/" + dispatch["task_name"]
                preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-review",
                    "tool_input": dispatch,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-review",
                }
                self.assertEqual(agent_preflight.evaluate(preflight), {})
                ledger_runtime.evaluate(
                    {**preflight, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                )

                continuation = compile_continuation(capsule, target=owner, delta={"evidence": "added"})
                wrong_mode = parse_message(continuation["message"])
                wrong_mode["mode"] = "fresh"
                wrong_mode_outcome = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "v7-review",
                        "tool_input": {**continuation, "message": render_capsule(wrong_mode)},
                        "tool_name": "followup_task",
                        "tool_use_id": "wrong-mode-review",
                    }
                )
                self.assertEqual(wrong_mode_outcome["decision"], "block")
                self.assertIn("delta mode", wrong_mode_outcome["reason"])
                missing_delta = parse_message(continuation["message"])
                missing_delta.pop("delta")
                missing_delta_outcome = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "v7-review",
                        "tool_input": {**continuation, "message": render_capsule(missing_delta)},
                        "tool_name": "followup_task",
                        "tool_use_id": "missing-delta-review",
                    }
                )
                self.assertEqual(missing_delta_outcome["decision"], "block")
                self.assertIn("non-empty delta", missing_delta_outcome["reason"])
                forged = parse_message(continuation["message"])
                forged["generation"] += 1
                forged_preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-review",
                    "tool_input": {**continuation, "message": render_capsule(forged)},
                    "tool_name": "followup_task",
                    "tool_use_id": "forged-review",
                }
                forged_outcome = agent_preflight.evaluate(forged_preflight)
                self.assertEqual(forged_outcome["decision"], "block")
                self.assertIn("task_name does not match", forged_outcome["reason"])
                forged_epoch = parse_message(continuation["message"])
                forged_epoch["epoch"] = "e02"
                forged_epoch_outcome = agent_preflight.evaluate(
                    {
                        **forged_preflight,
                        "tool_input": {**continuation, "message": render_capsule(forged_epoch)},
                        "tool_use_id": "forged-epoch-review",
                    }
                )
                self.assertEqual(forged_epoch_outcome["decision"], "block")
                self.assertIn("task_name does not match", forged_epoch_outcome["reason"])
                continue_preflight = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-review",
                    "tool_input": continuation,
                    "tool_name": "followup_task",
                    "tool_use_id": "continue-review",
                }
                self.assertEqual(agent_preflight.evaluate(continue_preflight), {})
                ledger_runtime.evaluate(
                    {**continue_preflight, "hook_event_name": "PostToolUse", "tool_response": {"delivered": True}}
                )
                continued_capsule = parse_message(continuation["message"])
                with self.assertRaisesRegex(CapsuleError, "acceptance evidence"):
                    compile_result(
                        continued_capsule,
                        status="complete",
                        disposition="accept",
                        blockers=[],
                        changed_paths=[],
                        deviations=[],
                        evidence={},
                        failure_signature=None,
                        summary="missing evidence",
                    )
                accepted = compile_result(
                    continued_capsule,
                    status="complete",
                    disposition="accept",
                    blockers=[],
                    changed_paths=[],
                    deviations=[],
                    evidence={"A01": "reviewed exact state"},
                    failure_signature=None,
                    summary="accepted exact reviewed state",
                )
                self.assertEqual(
                    subagent_stop.evaluate(
                        {
                            "agent_id": owner,
                            "agent_type": dispatch["agent_type"],
                            "cwd": str(repo),
                            "hook_event_name": "SubagentStop",
                            "last_assistant_message": accepted,
                            "session_id": "v7-review",
                            "stop_hook_active": False,
                        }
                    ),
                    {},
                )


if __name__ == "__main__":
    unittest.main()

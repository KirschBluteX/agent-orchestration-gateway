from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
sys.path[:0] = [str(HOOKS), str(SCRIPTS)]

import agent_preflight  # noqa: E402
import dispatch_transaction  # noqa: E402
import ledger_runtime  # noqa: E402
import prepared_graph  # noqa: E402
import subagent_stop  # noqa: E402
from graph_compiler import compact_dispatch_batch, prepare_dispatch_graph  # noqa: E402
from packet_compiler import compile_result, parse_message  # noqa: E402


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


def node(name: str, path: str, *, generation: int = 1) -> dict[str, object]:
    return {
        "acceptance_facts": {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "events": [],
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": no_risks(),
        },
        "closure": {
            "acceptance_closed": True,
            "criteria_closed": True,
            "decision_space": "acceptance_equivalent",
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
        "role": "worker",
        "scopes": [{"kind": "exact", "path": path}],
        "selection": {"depends_on": [], "responsibility": name},
    }


class DispatchTransactionTests(unittest.TestCase):
    def _prepared(self, root: Path, session_id: str, *nodes: dict[str, object]) -> tuple[Path, dict[str, object]]:
        repo = root / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for item in nodes:
            relative = str(item["scopes"][0]["path"])
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("baseline\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": session_id},
        ):
            prepared = prepare_dispatch_graph(
                list(nodes),
                native_capacity=len(nodes),
                native_catalog=native_catalog(),
                repo=repo,
            )
        return repo, prepared

    def _transaction(self, root: Path, repo: Path, prepared: dict[str, object], session_id: str) -> dict[str, object]:
        environment = mock.patch.dict(os.environ, {"CCO_LEDGER_DIR": str(root / "ledger")})
        environment.start()
        self.addCleanup(environment.stop)
        return dispatch_transaction.prepare_dispatch_batch(
            compact_dispatch_batch(prepared),
            ledger_root=root / "ledger",
            repo=repo,
            session_id=session_id,
        )

    def test_ref_expands_before_reservation_and_activation_discards_only_its_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-activation"
            repo, prepared = self._prepared(root, session_id, node("n01_one", "one.txt"), node("n02_two", "two.txt"))
            batch = self._transaction(root, repo, prepared, session_id)
            first, second = batch["dispatches"]
            _transaction_id, first_ref = dispatch_transaction.parse_spawn_reference(first["message"])
            _transaction_id, second_ref = dispatch_transaction.parse_spawn_reference(second["message"])
            _transaction_id, first_fallback_ref = dispatch_transaction.parse_spawn_reference(
                batch["fallback_dispatches"]["n01_one"][0]["message"]
            )
            self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
            self.assertNotIn("CAPSULE_JSON", first["message"])
            initial = dispatch_transaction.read_transaction_state(root / "ledger", session_id, batch["transaction_id"])
            self.assertEqual(initial["state"], "prepared")
            self.assertTrue(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], first_ref).exists())

            payload = {
                "cwd": str(repo),
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "tool_input": first,
                "tool_name": "spawn_agent",
                "tool_use_id": "spawn-one",
            }
            outcome = agent_preflight.evaluate(payload)
            updated = outcome["hookSpecificOutput"]["updatedInput"]
            self.assertIn("CCO_DISPATCH cco.v7", updated["message"])
            self.assertEqual(updated["task_name"], first["task_name"])
            self.assertEqual(updated["agent_type"], "cost_orchestrator_write_leaf")
            self.assertEqual(updated["model"], "gpt-5.6-luna")
            self.assertEqual(updated["reasoning_effort"], "max")
            dispatching = dispatch_transaction.read_transaction_state(root / "ledger", session_id, batch["transaction_id"])
            self.assertEqual(dispatching["nodes"]["n01_one"]["state"], "dispatching")
            self.assertTrue(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], first_ref).exists())

            owner = "/root/" + first["task_name"]
            self.assertEqual(
                ledger_runtime.evaluate(
                    {**payload, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}}
                ),
                {},
            )
            active = dispatch_transaction.read_transaction_state(root / "ledger", session_id, batch["transaction_id"])
            self.assertEqual(active["nodes"]["n01_one"]["state"], "active")
            self.assertFalse(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], first_ref).exists())
            self.assertFalse(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], first_fallback_ref).exists())
            self.assertTrue(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], second_ref).exists())

    def test_desktop_collaboration_spawn_name_expands_and_activates_the_same_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-desktop-spawn"
            repo, prepared = self._prepared(root, session_id, node("n01_one", "one.txt"))
            batch = self._transaction(root, repo, prepared, session_id)
            ref = batch["dispatches"][0]
            payload = {
                "cwd": str(repo),
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "tool_input": ref,
                "tool_name": "collaborationspawn_agent",
                "tool_use_id": "desktop-spawn-one",
            }

            expanded = agent_preflight.evaluate(payload)

            self.assertIn("updatedInput", expanded["hookSpecificOutput"])
            owner = "/root/" + ref["task_name"]
            self.assertEqual(
                ledger_runtime.evaluate(
                    {
                        **payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"task_path": owner},
                    }
                ),
                {},
            )
            state = dispatch_transaction.read_transaction_state(
                root / "ledger", session_id, batch["transaction_id"]
            )
            self.assertEqual(state["nodes"]["n01_one"]["state"], "active")

    def test_session_transaction_context_does_not_require_host_cwd_to_be_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            host_workspace = root / "workspace"
            repo = host_workspace / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "one.txt").write_text("baseline\n", encoding="utf-8")
            session_id = "txn-host-workspace"
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(state_root / "ledger"),
                    "CODEX_THREAD_ID": session_id,
                },
            ):
                prepared = prepare_dispatch_graph(
                    [node("n01_one", "one.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
            batch = self._transaction(state_root, repo, prepared, session_id)

            context = ledger_runtime.evaluate(
                {
                    "cwd": str(host_workspace),
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                }
            )

            self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
            self.assertIn("additionalContext", context["hookSpecificOutput"])
            self.assertIn("pending=n01_one", context["hookSpecificOutput"]["additionalContext"])

            spawn = agent_preflight.evaluate(
                {
                    "cwd": str(host_workspace),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": batch["dispatches"][0],
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-from-host-workspace",
                }
            )
            self.assertIn("updatedInput", spawn["hookSpecificOutput"])
            self.assertTrue(
                spawn["hookSpecificOutput"]["updatedInput"]["message"].startswith(
                    "CCO_DISPATCH cco.v7"
                )
            )
            waiting = ledger_runtime.evaluate(
                {
                    "cwd": str(host_workspace),
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                }
            )
            self.assertEqual(waiting["decision"], "block")
            self.assertIn("CCO_EVENT_FIRST_WAIT", waiting["reason"])

    def test_non_git_worker_spawn_and_result_enforce_full_root_scope_fencing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "owned.txt").write_text("baseline\n", encoding="utf-8")
            (workspace / "outside.txt").write_text("outside\n", encoding="utf-8")
            session_id = "txn-directory-worker"
            environment = {
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": session_id,
            }
            with mock.patch.dict(os.environ, environment):
                prepared = prepare_dispatch_graph(
                    [node("n01_one", "owned.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=workspace,
                )
                batch = dispatch_transaction.prepare_dispatch_batch(
                    compact_dispatch_batch(prepared),
                    ledger_root=root / "ledger",
                    repo=workspace,
                    session_id=session_id,
                )
                ref = batch["dispatches"][0]
                payload = {
                    "cwd": str(workspace),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": ref,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": "directory-spawn",
                }
                expanded = agent_preflight.evaluate(payload)["hookSpecificOutput"][
                    "updatedInput"
                ]
                owner = "/root/" + ref["task_name"]
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            **payload,
                            "hook_event_name": "PostToolUse",
                            "tool_response": {"task_path": owner},
                        }
                    ),
                    {},
                )
                (workspace / "owned.txt").write_text("changed\n", encoding="utf-8")
                capsule = parse_message(expanded["message"])
                result = compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=["owned.txt"],
                    deviations=[],
                    evidence={"A01": "owned file changed"},
                    failure_signature=None,
                    summary="completed directory worker",
                )
                stop_payload = {
                    "agent_id": owner,
                    "agent_type": expanded["agent_type"],
                    "cwd": str(workspace),
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": result,
                    "session_id": session_id,
                    "stop_hook_active": False,
                }
                self.assertEqual(subagent_stop.evaluate(stop_payload), {})

            second_root = root / "second"
            second_workspace = second_root / "project"
            second_workspace.mkdir(parents=True)
            (second_workspace / "owned.txt").write_text("baseline\n", encoding="utf-8")
            (second_workspace / "outside.txt").write_text("outside\n", encoding="utf-8")
            second_session = "txn-directory-outside"
            second_env = {
                "CCO_LEDGER_DIR": str(second_root / "ledger"),
                "CODEX_THREAD_ID": second_session,
            }
            with mock.patch.dict(os.environ, second_env):
                prepared = prepare_dispatch_graph(
                    [node("n01_one", "owned.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=second_workspace,
                )
                batch = dispatch_transaction.prepare_dispatch_batch(
                    compact_dispatch_batch(prepared),
                    ledger_root=second_root / "ledger",
                    repo=second_workspace,
                    session_id=second_session,
                )
                ref = batch["dispatches"][0]
                payload = {
                    "cwd": str(second_workspace),
                    "hook_event_name": "PreToolUse",
                    "session_id": second_session,
                    "tool_input": ref,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": "directory-outside-spawn",
                }
                expanded = agent_preflight.evaluate(payload)["hookSpecificOutput"][
                    "updatedInput"
                ]
                owner = "/root/" + ref["task_name"]
                ledger_runtime.evaluate(
                    {
                        **payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"task_path": owner},
                    }
                )
                (second_workspace / "outside.txt").write_text(
                    "changed outside\n", encoding="utf-8"
                )
                capsule = parse_message(expanded["message"])
                result = compile_result(
                    capsule,
                    status="complete",
                    disposition="retire",
                    blockers=[],
                    changed_paths=[],
                    deviations=[],
                    evidence={"A01": "claimed no owned changes"},
                    failure_signature=None,
                    summary="attempted outside change",
                )
                rejected = subagent_stop.evaluate(
                    {
                        "agent_id": owner,
                        "agent_type": expanded["agent_type"],
                        "cwd": str(second_workspace),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": result,
                        "session_id": second_session,
                        "stop_hook_active": False,
                    }
                )
                self.assertEqual(rejected["decision"], "block")
                self.assertIn("outside_scope:outside.txt", rejected["reason"])

    def test_rejection_enables_only_its_precompiled_fallback_and_keeps_active_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-fallback"
            repo, prepared = self._prepared(root, session_id, node("n01_one", "one.txt"), node("n02_two", "two.txt"))
            batch = self._transaction(root, repo, prepared, session_id)
            first, sibling = batch["dispatches"]
            fallback = batch["fallback_dispatches"]["n01_one"][0]

            sibling_payload = {
                "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                "tool_input": sibling, "tool_name": "spawn_agent", "tool_use_id": "spawn-two",
            }
            self.assertIn("updatedInput", agent_preflight.evaluate(sibling_payload)["hookSpecificOutput"])
            sibling_owner = "/root/" + sibling["task_name"]
            self.assertEqual(
                ledger_runtime.evaluate({**sibling_payload, "hook_event_name": "PostToolUse", "tool_response": {"task_path": sibling_owner}}),
                {},
            )

            first_payload = {
                "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                "tool_input": first, "tool_name": "spawn_agent", "tool_use_id": "spawn-one",
            }
            self.assertIn("updatedInput", agent_preflight.evaluate(first_payload)["hookSpecificOutput"])
            self.assertEqual(
                ledger_runtime.evaluate(
                    {**first_payload, "hook_event_name": "PostToolUse", "tool_response": {"error": "unsupported before thread creation"}}
                ),
                {},
            )
            rejected = dispatch_transaction.read_transaction_state(root / "ledger", session_id, batch["transaction_id"])
            self.assertEqual(rejected["nodes"]["n01_one"]["state"], "rejected")
            self.assertEqual(rejected["nodes"]["n02_two"]["state"], "active")

            fallback_payload = {**first_payload, "tool_input": fallback, "tool_use_id": "spawn-one-fallback"}
            self.assertIn("updatedInput", agent_preflight.evaluate(fallback_payload)["hookSpecificOutput"])
            wrong = agent_preflight.evaluate({**first_payload, "tool_input": first, "tool_use_id": "repeat-rejected"})
            self.assertEqual(wrong["decision"], "block")
            self.assertIn("current pending candidate", wrong["reason"])

    def test_completed_sibling_keeps_artifact_and_lease_for_pending_and_fallback_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-completed-sibling"
            repo, prepared = self._prepared(
                root,
                session_id,
                node("n01_one", "one.txt"),
                node("n02_two", "two.txt"),
            )
            batch = self._transaction(root, repo, prepared, session_id)
            first, second = batch["dispatches"]

            first_payload = {
                "cwd": str(repo),
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "tool_input": first,
                "tool_name": "collaborationspawn_agent",
                "tool_use_id": "spawn-first",
            }
            expanded = agent_preflight.evaluate(first_payload)["hookSpecificOutput"][
                "updatedInput"
            ]
            owner = "/root/" + first["task_name"]
            self.assertEqual(
                ledger_runtime.evaluate(
                    {
                        **first_payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"task_path": owner},
                    }
                ),
                {},
            )
            (repo / "one.txt").write_text("completed sibling\n", encoding="utf-8")
            capsule = parse_message(expanded["message"])
            result = compile_result(
                capsule,
                status="complete",
                disposition="retire",
                blockers=[],
                changed_paths=["one.txt"],
                deviations=[],
                evidence={"A01": "completed and verified one.txt"},
                failure_signature=None,
                summary="completed first sibling",
            )
            self.assertEqual(
                subagent_stop.evaluate(
                    {
                        "agent_id": owner,
                        "agent_type": expanded["agent_type"],
                        "cwd": str(repo),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": result,
                        "session_id": session_id,
                        "stop_hook_active": False,
                    }
                ),
                {},
            )
            artifact = Path(str(batch["baseline_path"]))
            self.assertTrue(artifact.exists())

            second_payload = {
                **first_payload,
                "tool_input": second,
                "tool_use_id": "spawn-second",
            }
            self.assertIn(
                "updatedInput",
                agent_preflight.evaluate(second_payload)["hookSpecificOutput"],
            )
            self.assertEqual(
                ledger_runtime.evaluate(
                    {
                        **second_payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"error": "unsupported before thread creation"},
                    }
                ),
                {},
            )
            fallback = batch["fallback_dispatches"]["n02_two"][0]
            fallback_outcome = agent_preflight.evaluate(
                {
                    **second_payload,
                    "tool_input": fallback,
                    "tool_use_id": "spawn-second-fallback",
                }
            )
            self.assertIn("updatedInput", fallback_outcome["hookSpecificOutput"])

    def test_exhausted_sibling_never_leases_its_scope_to_pending_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-exhausted-sibling"
            repo, prepared = self._prepared(
                root,
                session_id,
                node("n01_one", "one.txt"),
                node("n02_two", "two.txt"),
            )
            batch = self._transaction(root, repo, prepared, session_id)
            first = batch["dispatches"][0]
            candidates = [first, *batch["fallback_dispatches"]["n01_one"]]
            for index, candidate in enumerate(candidates, start=1):
                payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": candidate,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": f"reject-first-{index}",
                }
                self.assertIn(
                    "updatedInput",
                    agent_preflight.evaluate(payload)["hookSpecificOutput"],
                )
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            **payload,
                            "hook_event_name": "PostToolUse",
                            "tool_response": {
                                "error": "unsupported before thread creation"
                            },
                        }
                    ),
                    {},
                )

            state = dispatch_transaction.read_transaction_state(
                root / "ledger", session_id, batch["transaction_id"]
            )
            self.assertEqual(state["nodes"]["n01_one"]["state"], "exhausted")
            (repo / "one.txt").write_text("unowned change\n", encoding="utf-8")
            second = batch["dispatches"][1]
            rejected = agent_preflight.evaluate(
                {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": second,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": "spawn-second-after-exhaustion",
                }
            )
            self.assertEqual(rejected["decision"], "block")
            self.assertIn("one.txt", rejected["reason"])

    def test_pending_gate_stop_recovery_and_active_wait_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-stop"
            repo, prepared = self._prepared(root, session_id, node("n01_one", "one.txt"))
            batch = self._transaction(root, repo, prepared, session_id)
            arbitrary = agent_preflight.evaluate(
                {
                    "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                    "tool_input": {"message": "unrelated", "target": "/root/unmanaged"},
                    "tool_name": "send_message", "tool_use_id": "unrelated-tool",
                }
            )
            self.assertEqual(arbitrary["decision"], "block")
            bypass = agent_preflight.evaluate(
                {
                    "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                    "tool_input": {
                        "agent_type": "explorer", "fork_turns": "none", "task_name": "native_task",
                        "message": "CCO_NATIVE_BYPASS v1\nunmanaged",
                    },
                    "tool_name": "spawn_agent", "tool_use_id": "bypass",
                }
            )
            self.assertEqual(bypass["decision"], "block")

            first_stop = ledger_runtime.evaluate({"cwd": str(repo), "hook_event_name": "Stop", "session_id": session_id})
            self.assertEqual(first_stop["decision"], "block")
            self.assertIn("recovery", first_stop["reason"].casefold())
            second_stop = ledger_runtime.evaluate({"cwd": str(repo), "hook_event_name": "Stop", "session_id": session_id})
            self.assertIn("fenced", (second_stop.get("reason", "") + second_stop.get("systemMessage", "")).casefold())
            fenced = dispatch_transaction.read_transaction_state(root / "ledger", session_id, batch["transaction_id"])
            self.assertEqual(fenced["nodes"]["n01_one"]["state"], "fenced")
            _transaction_id, fenced_ref = dispatch_transaction.parse_spawn_reference(batch["dispatches"][0]["message"])
            self.assertEqual(fenced["state"], "terminal")
            self.assertFalse(dispatch_transaction.bundle_path(root / "ledger", session_id, batch["transaction_id"], fenced_ref).exists())

            session_id = "txn-active-wait"
            active_root = root / "active"
            repo, prepared = self._prepared(active_root, session_id, node("n01_active", "active.txt"))
            active_batch = self._transaction(active_root, repo, prepared, session_id)
            ref = active_batch["dispatches"][0]
            payload = {
                "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                "tool_input": ref, "tool_name": "spawn_agent", "tool_use_id": "spawn-active",
            }
            agent_preflight.evaluate(payload)
            owner = "/root/" + ref["task_name"]
            ledger_runtime.evaluate({**payload, "hook_event_name": "PostToolUse", "tool_response": {"task_path": owner}})
            waiting = ledger_runtime.evaluate({"cwd": str(repo), "hook_event_name": "Stop", "session_id": session_id})
            self.assertEqual(waiting["decision"], "block")
            self.assertIn("1800000", waiting["reason"])
            prompt = ledger_runtime.evaluate({"cwd": str(repo), "hook_event_name": "UserPromptSubmit", "session_id": session_id})
            self.assertIn("additionalContext", prompt["hookSpecificOutput"])
            self.assertIn("active", prompt["hookSpecificOutput"]["additionalContext"])

    def test_exact_abort_keeps_a_late_postflight_from_activating_a_fenced_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-late-postflight"
            repo, prepared = self._prepared(root, session_id, node("n01_one", "one.txt"))
            batch = self._transaction(root, repo, prepared, session_id)
            ref = batch["dispatches"][0]
            spawn_payload = {
                "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                "tool_input": ref, "tool_name": "spawn_agent", "tool_use_id": "spawn-one",
            }
            self.assertIn("updatedInput", agent_preflight.evaluate(spawn_payload)["hookSpecificOutput"])
            aborted = agent_preflight.evaluate(
                {
                    "cwd": str(repo), "hook_event_name": "PreToolUse", "session_id": session_id,
                    "tool_input": {
                        "message": dispatch_transaction.render_abort_command(batch["transaction_id"]),
                        "target": "/root/cco_dispatch_abort",
                    },
                    "tool_name": "send_message", "tool_use_id": "abort-one",
                }
            )
            self.assertEqual(aborted["decision"], "block")
            self.assertIn("CCO_TRANSACTION_ABORTED", aborted["reason"])
            late = ledger_runtime.evaluate(
                {
                    **spawn_payload,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"task_path": "/root/" + ref["task_name"]},
                }
            )
            self.assertEqual(late["decision"], "block")
            self.assertIn("fenced", late["reason"])
            row = ledger_runtime.ledger_for(spawn_payload).read_rows()[0]
            self.assertEqual(row["state"], "exhausted")
            settled = dispatch_transaction.read_transaction_state(
                root / "ledger", session_id, batch["transaction_id"]
            )
            self.assertIsNone(settled["nodes"]["n01_one"]["call_id"])
            self.assertIsNone(settled["nodes"]["n01_one"]["dispatch_ref"])

    def test_capacity_never_prunes_a_fenced_late_postflight_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-capacity-fenced-call"
            repo, prepared = self._prepared(
                root, session_id, node("n01_one", "one.txt")
            )
            with mock.patch.object(dispatch_transaction, "_MAX_TRANSACTIONS", 1):
                batch = self._transaction(root, repo, prepared, session_id)
                ref = batch["dispatches"][0]
                spawn_payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": ref,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-one",
                }
                self.assertIn(
                    "updatedInput",
                    agent_preflight.evaluate(spawn_payload)["hookSpecificOutput"],
                )
                dispatch_transaction.abort_pending_transaction(
                    {"cwd": str(repo), "session_id": session_id},
                    batch["transaction_id"],
                )

                prepared_next = prepare_dispatch_graph(
                    [node("n02_two", "two.txt")],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                with self.assertRaisesRegex(
                    dispatch_transaction.DispatchTransactionError,
                    "capacity is exhausted",
                ):
                    self._transaction(root, repo, prepared_next, session_id)

                late = ledger_runtime.evaluate(
                    {
                        **spawn_payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {
                            "task_path": "/root/" + ref["task_name"]
                        },
                    }
                )
                self.assertEqual(late["decision"], "block")
                prepared_after = prepare_dispatch_graph(
                    [node("n02_two", "two.txt", generation=2)],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                replacement = self._transaction(
                    root, repo, prepared_after, session_id
                )
                self.assertIsNotNone(replacement["transaction_id"])

    def test_exhausted_candidate_chain_allows_a_new_generation_from_rank_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-exhausted-route"
            repo, prepared = self._prepared(
                root, session_id, node("n01_one", "one.txt")
            )
            batch = self._transaction(root, repo, prepared, session_id)
            candidates = [
                batch["dispatches"][0],
                *batch["fallback_dispatches"]["n01_one"],
            ]
            self.assertGreaterEqual(len(candidates), 2)
            for index, candidate in enumerate(candidates, start=1):
                payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": candidate,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": f"rejected-{index}",
                }
                self.assertIn(
                    "updatedInput",
                    agent_preflight.evaluate(payload)["hookSpecificOutput"],
                )
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            **payload,
                            "hook_event_name": "PostToolUse",
                            "tool_response": {
                                "error": "unsupported before thread creation"
                            },
                        }
                    ),
                    {},
                )

            ledger = ledger_runtime.ledger_for(payload)
            self.assertEqual(ledger.read_rows()[0]["state"], "exhausted")
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": session_id,
                },
            ):
                next_prepared = prepare_dispatch_graph(
                    [node("n01_one", "one.txt", generation=2)],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
            next_batch = self._transaction(root, repo, next_prepared, session_id)
            next_spawn = next_batch["dispatches"][0]
            next_outcome = agent_preflight.evaluate(
                {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": session_id,
                    "tool_input": next_spawn,
                    "tool_name": "collaborationspawn_agent",
                    "tool_use_id": "generation-two-rank-one",
                }
            )
            self.assertIn("updatedInput", next_outcome["hookSpecificOutput"])

    def test_exact_abort_accepts_shell_or_full_spawn_carriers_without_executing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index, carrier in enumerate(("shell", "spawn"), start=1):
                session_id = f"txn-abort-carrier-{index}"
                case_root = root / carrier
                repo, prepared = self._prepared(
                    case_root, session_id, node("n01_one", "one.txt")
                )
                batch = self._transaction(case_root, repo, prepared, session_id)
                command = dispatch_transaction.render_abort_command(batch["transaction_id"])
                tool_input = (
                    {"command": command}
                    if carrier == "shell"
                    else {
                        "agent_type": "cost_orchestrator_read_leaf",
                        "fork_turns": "none",
                        "message": command,
                        "model": "gpt-5.6-terra",
                        "reasoning_effort": "max",
                        "task_name": "explorer_abort_terra_max_g01",
                    }
                )
                outcome = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": session_id,
                        "tool_input": tool_input,
                        "tool_name": (
                            "shell_command"
                            if carrier == "shell"
                            else "collaborationspawn_agent"
                        ),
                        "tool_use_id": f"abort-{carrier}",
                    }
                )
                self.assertEqual(outcome["decision"], "block")
                self.assertIn("CCO_TRANSACTION_ABORTED", outcome["reason"])
                state = dispatch_transaction.read_transaction_state(
                    case_root / "ledger", session_id, batch["transaction_id"]
                )
                self.assertEqual(state["nodes"]["n01_one"]["state"], "fenced")

    def test_stale_cleanup_removes_orphan_and_expired_transaction_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_root = root / "ledger"
            bundles = root / "dispatch-bundles"
            suffix = "f" * 64
            keep = bundles / f"keep-session-{suffix}"
            stale = bundles / f"old-session-{suffix}"
            keep.mkdir(parents=True)
            stale.mkdir(parents=True)
            keep_file = keep / ("a" * 64 + ".json")
            stale_file = stale / ("b" * 64 + ".json")
            keep_file.write_text("{}", encoding="utf-8")
            stale_file.write_text("{}", encoding="utf-8")
            old_time = stale_file.stat().st_mtime - (8 * 24 * 60 * 60)
            os.utime(stale_file, (old_time, old_time))
            os.utime(stale, (old_time, old_time))

            removed = dispatch_transaction.cleanup_stale_dispatch_state(
                ledger_root,
                keep_session_id="keep-session",
                max_age_seconds=7 * 24 * 60 * 60,
            )

            self.assertTrue(keep_file.exists())
            self.assertFalse(stale.exists())
            self.assertIn(str(stale_file.resolve()).casefold(), {str(path).casefold() for path in removed})

    def test_stale_cleanup_preserves_fresh_or_locked_malformed_sessions_and_unknown_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_root = root / "ledger"
            ledger_root.mkdir()
            bundles = root / "dispatch-bundles"
            transaction_suffix = "a" * 64
            locked = bundles / f"locked-session-{transaction_suffix}"
            fresh = bundles / f"fresh-session-{transaction_suffix}"
            unknown = bundles / "not-a-cco-bundle"
            for directory in (locked, fresh, unknown):
                directory.mkdir(parents=True)
                candidate = directory / ("b" * 64 + ".json")
                candidate.write_text("{}", encoding="utf-8")
                old = candidate.stat().st_mtime - (8 * 24 * 60 * 60)
                os.utime(candidate, (old, old))
                os.utime(directory, (old, old))

            (ledger_root / "locked-session.dispatch-transactions.json").write_text(
                "not json", encoding="utf-8"
            )
            (ledger_root / ".locked-session.dispatch-transactions.lock").write_text(
                "123\n", encoding="ascii"
            )
            (ledger_root / "fresh-session.dispatch-transactions.json").write_text(
                "not json", encoding="utf-8"
            )

            dispatch_transaction.cleanup_stale_dispatch_state(
                ledger_root,
                keep_session_id="current-session",
                max_age_seconds=7 * 24 * 60 * 60,
            )

            self.assertTrue(locked.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unknown.exists())

    def test_stale_cleanup_never_enters_a_reparse_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_root = root / "ledger"
            ledger_root.mkdir()
            old = time.time() - (8 * 24 * 60 * 60)

            bundle_root = root / "dispatch-bundles"
            bundle = bundle_root / ("old-session-" + "a" * 64)
            bundle.mkdir(parents=True)
            bundle_file = bundle / ("b" * 64 + ".json")
            bundle_file.write_text("{}", encoding="utf-8")
            os.utime(bundle_file, (old, old))
            os.utime(bundle, (old, old))

            workspace_root = root / "workspace"
            workspace_root.mkdir()
            artifact = workspace_root / ("old-session-" + "c" * 64 + ".json")
            artifact.write_text("{}", encoding="utf-8")
            os.utime(artifact, (old, old))

            def transaction_reparse(path: Path) -> bool:
                return Path(path).name == "dispatch-bundles"

            def artifact_reparse(path: Path) -> bool:
                return Path(path).name == "workspace"

            with mock.patch.object(
                dispatch_transaction,
                "_has_reparse_ancestor",
                side_effect=transaction_reparse,
            ):
                removed_bundles = (
                    dispatch_transaction.cleanup_stale_dispatch_state(
                        ledger_root,
                        keep_session_id="current-session",
                        max_age_seconds=7 * 24 * 60 * 60,
                    )
                )
            with mock.patch.object(
                prepared_graph,
                "_has_reparse_ancestor",
                side_effect=artifact_reparse,
                create=True,
            ):
                removed_artifacts = prepared_graph.cleanup_stale_artifacts(
                    ledger_root,
                    keep_session_id="current-session",
                    max_age_seconds=7 * 24 * 60 * 60,
                )

            self.assertEqual(removed_bundles, [])
            self.assertEqual(removed_artifacts, [])
            self.assertTrue(bundle_file.exists())
            self.assertTrue(artifact.exists())

    def test_transaction_capacity_prunes_validated_terminal_records_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "txn-capacity"
            repo = root / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "one.txt").write_text("baseline\n", encoding="utf-8")
            environment = {
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": session_id,
            }
            with (
                mock.patch.object(dispatch_transaction, "_MAX_TRANSACTIONS", 2),
                mock.patch.dict(os.environ, environment),
            ):
                for generation in range(1, 4):
                    prepared = prepare_dispatch_graph(
                        [node("n01_one", "one.txt", generation=generation)],
                        native_capacity=1,
                        native_catalog=native_catalog(),
                        repo=repo,
                    )
                    transaction = dispatch_transaction.prepare_dispatch_batch(
                        compact_dispatch_batch(prepared),
                        ledger_root=root / "ledger",
                        repo=repo,
                        session_id=session_id,
                    )
                    dispatch_transaction.abort_pending_transaction(
                        {
                            "cwd": str(repo),
                            "session_id": session_id,
                        },
                        transaction["transaction_id"],
                    )

                document = dispatch_transaction._read_document(
                    root / "ledger", session_id
                )
                self.assertLessEqual(len(document["transactions"]), 2)


if __name__ == "__main__":
    unittest.main()

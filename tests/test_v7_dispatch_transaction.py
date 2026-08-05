from __future__ import annotations

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
import dispatch_transaction  # noqa: E402
import ledger_runtime  # noqa: E402
from graph_compiler import compact_dispatch_batch, prepare_dispatch_graph  # noqa: E402


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


def node(name: str, path: str) -> dict[str, object]:
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
        "generation": 1,
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
            self.assertEqual(row["state"], "rejected")


if __name__ == "__main__":
    unittest.main()

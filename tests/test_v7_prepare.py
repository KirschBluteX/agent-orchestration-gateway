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
import ledger_runtime  # noqa: E402
from graph_compiler import (  # noqa: E402
    GraphCompilerError,
    compact_dispatch_batch,
    prepare_dispatch_graph,
)
from packet_compiler import parse_message  # noqa: E402


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


class V7PrepareTests(unittest.TestCase):
    def test_toml_policy_routes_through_graph_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            repo = root / "repo"
            home.mkdir()
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "owned.txt"], cwd=repo, check=True)
            (home / "cco.toml").write_text(
                "[routes.worker.mechanical]\n"
                'candidates = [{ model = "gpt-5.6-terra", effort = "max" }]\n',
                encoding="utf-8",
            )
            node = {
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
                "contract": {"contract_rev": 1, "node": "n01_policy", "objective": "inspect policy"},
                "generation": 1,
                "node": "n01_policy",
                "placement": {
                    "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "owned.txt"}],
                "selection": {"dependencies_ready": True, "responsibility": "policy-route"},
            }
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "v7-toml-policy"}):
                prepared = prepare_dispatch_graph(
                    [node],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                    codex_home=home,
                )

        self.assertEqual(prepared["route_errors"], {})
        self.assertEqual(prepared["dispatches"][0]["model"], "gpt-5.6-terra")

    def test_prethread_fallbacks_must_follow_a_confirmed_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "owned.txt"], cwd=repo, check=True)
            node = {
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
                "contract": {"contract_rev": 1, "node": "n01_fallback", "objective": "change owned"},
                "generation": 1,
                "node": "n01_fallback",
                "placement": {
                    "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "owned.txt"}],
                "selection": {"dependencies_ready": True, "responsibility": "fallback-order"},
            }
            env = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-fallback-order"}
            with mock.patch.dict(os.environ, env):
                prepared = prepare_dispatch_graph(
                    [node],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                first = prepared["dispatches"][0]
                fallback = prepared["fallback_dispatches"]["n01_fallback"][0]
                base = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-fallback-order",
                    "tool_name": "spawn_agent",
                }
                skipped = agent_preflight.evaluate(
                    {**base, "tool_input": fallback, "tool_use_id": "skip-r1"}
                )
                self.assertEqual(skipped["decision"], "block")
                self.assertIn("rank 1", skipped["reason"])
                first_payload = {**base, "tool_input": first, "tool_use_id": "spawn-r1"}
                self.assertEqual(agent_preflight.evaluate(first_payload), {})
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            **first_payload,
                            "hook_event_name": "PostToolUse",
                            "tool_response": {"error": "unsupported before thread creation"},
                        }
                    ),
                    {},
                )
                fallback_payload = {**base, "tool_input": fallback, "tool_use_id": "spawn-r2"}
                self.assertEqual(agent_preflight.evaluate(fallback_payload), {})
                fallback_owner = "/root/" + fallback["task_name"]
                self.assertEqual(
                    ledger_runtime.evaluate(
                        {
                            **fallback_payload,
                            "hook_event_name": "PostToolUse",
                            "tool_response": {"task_path": fallback_owner},
                        }
                    ),
                    {},
                )
                row = ledger_runtime.ledger_for(fallback_payload).read_rows()[0]

        self.assertEqual(row["route"]["rank"], 2)
        self.assertEqual(row["owner"], fallback_owner)

    def test_single_entry_prepares_static_route_and_v7_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/owned.txt"], cwd=repo, check=True)
            node = {
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
                "contract": {
                    "contract_rev": 1,
                    "node": "n01_worker",
                    "objective": "change the owned file",
                },
                "generation": 1,
                "node": "n01_worker",
                "placement": {
                    "benefits": [
                        {"evidence": ["contract:A01"], "kind": "closed_chain"}
                    ],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "src/owned.txt"}],
                "selection": {
                    "dependencies_ready": True,
                    "responsibility": "owned-file",
                },
            }
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "v7-prepare-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [node],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                unsafe = {**node, "scopes": [{"kind": "exact", "path": "src/OWNED.txt"}]}
                with self.assertRaisesRegex(GraphCompilerError, "graph scope is unsafe"):
                    prepare_dispatch_graph(
                        [unsafe],
                        native_capacity=1,
                        native_catalog=native_catalog(),
                        repo=repo,
                    )
                native = prepared["dispatches"][0]
                payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "v7-prepare-session",
                    "tool_input": native,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-v7",
                }
                self.assertEqual(agent_preflight.evaluate(payload), {})
                wrong_owner = ledger_runtime.evaluate(
                    {
                        **payload,
                        "hook_event_name": "PostToolUse",
                        "tool_response": {"task_path": "/root/wrong_owner"},
                    }
                )
                self.assertEqual(wrong_owner["decision"], "block")
                self.assertIn("reserved task path", wrong_owner["reason"])
                ledger = ledger_runtime.ledger_for(payload)
                self.assertEqual(ledger.read_rows()[0]["state"], "reserved")
                owner = "/root/" + native["task_name"]
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
                row = ledger.read_rows()[0]

        self.assertEqual(prepared["protocol"], "cco.prepared-graph.v2")
        self.assertEqual(len(prepared["dispatches"]), 1)
        native = prepared["dispatches"][0]
        capsule = parse_message(native["message"])
        self.assertEqual(capsule["protocol"], "cco.v7")
        self.assertEqual(capsule["role"], "worker")
        self.assertEqual(capsule["assurance"], "mechanical")
        self.assertEqual(capsule["acceptance_ids"], ["A01"])
        self.assertEqual(native["model"], "gpt-5.6-luna")
        self.assertEqual(row["role"], "worker")
        self.assertEqual(row["assurance"], "mechanical")
        self.assertEqual(row["owner"], owner)
        self.assertNotIn("purpose", capsule)
        self.assertNotIn("judgment", capsule)
        compact = compact_dispatch_batch(prepared)
        self.assertEqual(compact["protocol"], "cco.dispatch-batch.v1")
        self.assertNotIn("manifest", compact)
        self.assertNotIn("route_plan", compact)

    def test_one_unavailable_user_pin_returns_only_that_node_to_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for name in ("good.txt", "pinned.txt"):
                (repo / name).write_text("baseline\n", encoding="utf-8")

            def graph_node(name: str, path: str, route: dict[str, object] | None = None) -> dict[str, object]:
                value = {
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
                    "contract": {"contract_rev": 1, "node": name, "objective": path},
                    "generation": 1,
                    "node": name,
                    "placement": {
                        "benefits": [{"evidence": ["contract:A01"], "kind": "parallel_ready"}],
                        "direct_action_count": 2,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": path}],
                    "selection": {
                        "dependencies_ready": True,
                        "downstream_count": 1,
                        "responsibility": name,
                    },
                }
                if route is not None:
                    value["route"] = route
                return value

            with mock.patch.dict(
                os.environ,
                {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-partial-route"},
            ):
                prepared = prepare_dispatch_graph(
                    [
                        graph_node("n01_good", "good.txt"),
                        graph_node(
                            "n02_pinned",
                            "pinned.txt",
                            {
                                "fixed_effort": "max",
                                "fixed_model": "not-installed",
                                "source": "user",
                            },
                        ),
                    ],
                    native_capacity=2,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                primary_only = prepare_dispatch_graph(
                    [
                        graph_node(
                            "n02_pinned",
                            "pinned.txt",
                            {
                                "fixed_effort": "max",
                                "fixed_model": "not-installed",
                                "source": "user",
                            },
                        )
                    ],
                    native_capacity=2,
                    native_catalog=native_catalog(),
                    repo=repo,
                )

        self.assertEqual([item["task_name"] for item in prepared["dispatches"]], ["worker_n01_good_g01"])
        self.assertEqual(prepared["primary_nodes"], ["n02_pinned"])
        self.assertIn("n02_pinned", prepared["route_errors"])
        self.assertEqual(primary_only["dispatches"], [])
        self.assertIsNone(primary_only["baseline"])
        self.assertIsNone(primary_only["baseline_path"])


if __name__ == "__main__":
    unittest.main()

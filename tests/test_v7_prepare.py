from __future__ import annotations

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
from graph_compiler import (  # noqa: E402
    GraphCompilerError,
    compact_dispatch_batch,
    prepare_dispatch_graph,
    verify_prepared_graph,
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
                "selection": {"depends_on": [], "responsibility": "policy-route"},
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
                "selection": {"depends_on": [], "responsibility": "fallback-order"},
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
                self.assertEqual(first["task_name"], "worker_n01_fallback_luna_max_g01")
                self.assertEqual(fallback["task_name"], "worker_n01_fallback_terra_max_g01")
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
                    "depends_on": [],
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

        self.assertEqual(prepared["protocol"], "cco.prepared-graph.v3")
        self.assertEqual(len(prepared["dispatches"]), 1)
        native = prepared["dispatches"][0]
        capsule = parse_message(native["message"])
        self.assertEqual(capsule["protocol"], "cco.v7")
        self.assertEqual(capsule["role"], "worker")
        self.assertEqual(capsule["assurance"], "mechanical")
        self.assertEqual(capsule["acceptance_ids"], ["A01"])
        self.assertEqual(native["model"], "gpt-5.6-luna")
        self.assertEqual(native["task_name"], "worker_n01_worker_luna_max_g01")
        self.assertEqual(row["role"], "worker")
        self.assertEqual(row["assurance"], "mechanical")
        self.assertEqual(row["owner"], owner)
        self.assertNotIn("purpose", capsule)
        self.assertNotIn("judgment", capsule)
        compact = compact_dispatch_batch(prepared)
        self.assertEqual(compact["protocol"], "cco.dispatch-batch.v2")
        self.assertNotIn("manifest", compact)
        self.assertNotIn("route_plan", compact)

    def test_no_dispatch_returns_no_baseline_artifact(self) -> None:
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
                "contract": {"contract_rev": 1, "node": "n01_waiting", "objective": "change owned"},
                "generation": 1,
                "node": "n01_waiting",
                "placement": {
                    "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "owned.txt"}],
                "selection": {"depends_on": [], "responsibility": "waiting"},
            }
            with mock.patch.dict(
                os.environ,
                {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-empty-batch"},
            ):
                prepared = prepare_dispatch_graph(
                    [node],
                    native_capacity=0,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                self.assertFalse((root / "workspace").exists())

        self.assertEqual(prepared["dispatches"], [])
        self.assertIsNone(prepared["baseline"])
        self.assertIsNone(prepared["baseline_path"])
        self.assertIsNone(prepared["graph_sha256"])
        self.assertIsNone(prepared["manifest"])

    def test_dispatch_graph_separates_primary_deferred_and_dependency_blocked_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for name in ("primary.txt", "selected.txt", "deferred.txt", "blocked.txt", "route-error.txt"):
                (repo / name).write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            def graph_node(
                name: str,
                path: str,
                *,
                placement_benefit: str | None = "parallel_ready",
                depends_on: list[str] | None = None,
                route: dict[str, object] | None = None,
            ) -> dict[str, object]:
                value: dict[str, object] = {
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
                        "benefits": (
                            []
                            if placement_benefit is None
                            else [{"evidence": ["contract:A01"], "kind": placement_benefit}]
                        ),
                        "direct_action_count": 2,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": path}],
                    "selection": {
                        "depends_on": [] if depends_on is None else depends_on,
                        "responsibility": name,
                    },
                }
                if route is not None:
                    value["route"] = route
                return value

            with mock.patch.dict(
                os.environ,
                {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-batch-barrier"},
            ):
                prepared = prepare_dispatch_graph(
                    [
                        graph_node("n01_primary", "primary.txt", placement_benefit=None),
                        graph_node("n02_selected", "selected.txt"),
                        graph_node("n03_deferred", "deferred.txt"),
                        graph_node("n04_blocked", "blocked.txt", depends_on=["n02_selected"]),
                        graph_node(
                            "n05_route_error",
                            "route-error.txt",
                            route={
                                "fixed_effort": "max",
                                "fixed_model": "not-installed",
                                "source": "user",
                            },
                        ),
                    ],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )

        self.assertEqual([item["task_name"] for item in prepared["dispatches"]], ["worker_n02_selected_luna_max_g01"])
        self.assertEqual(prepared["primary_nodes"], ["n01_primary", "n05_route_error"])
        self.assertEqual(prepared["deferred_nodes"], ["n03_deferred"])
        self.assertEqual(prepared["blocked_dependency_nodes"], ["n04_blocked"])
        self.assertIn("n05_route_error", prepared["route_errors"])

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
                        "depends_on": [],
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

        self.assertEqual([item["task_name"] for item in prepared["dispatches"]], ["worker_n01_good_luna_max_g01"])
        self.assertEqual(prepared["primary_nodes"], ["n02_pinned"])
        self.assertIn("n02_pinned", prepared["route_errors"])
        self.assertEqual(primary_only["dispatches"], [])
        self.assertIsNone(primary_only["baseline"])
        self.assertIsNone(primary_only["baseline_path"])

    def test_dependency_dag_derives_ready_priority_and_rejects_invalid_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for name in ("root.txt", "ready.txt", "blocked.txt", "completed.txt"):
                (repo / name).write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            def graph_node(name: str, path: str, depends_on: list[str] | None = None) -> dict[str, object]:
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
                        "depends_on": [] if depends_on is None else depends_on,
                        "responsibility": name,
                    },
                }

            nodes = [
                graph_node("n01_root", "root.txt"),
                graph_node("n02_ready", "ready.txt"),
                graph_node("n03_blocked", "blocked.txt", ["n01_root"]),
                graph_node("n04_completed", "completed.txt", ["done"]),
            ]
            environment = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-dag"}
            with mock.patch.dict(os.environ, environment):
                prepared = prepare_dispatch_graph(
                    nodes,
                    completed_nodes=["done"],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                for label, invalid_nodes, completed in (
                    (
                        "sorted",
                        [
                            graph_node("n01_first", "root.txt"),
                            graph_node("n02_second", "ready.txt"),
                            graph_node("n03_unsorted", "blocked.txt", ["n02_second", "n01_first"]),
                        ],
                        [],
                    ),
                    (
                        "duplicate",
                        [graph_node("n01_duplicate", "root.txt", ["n01_duplicate", "n01_duplicate"])],
                        [],
                    ),
                    (
                        "self",
                        [graph_node("n01_self", "root.txt", ["n01_self"])],
                        [],
                    ),
                    (
                        "unknown",
                        [graph_node("n01_unknown", "root.txt", ["n99_missing"])],
                        [],
                    ),
                    (
                        "cycle",
                        [
                            graph_node("n01_cycle", "root.txt", ["n02_cycle"]),
                            graph_node("n02_cycle", "ready.txt", ["n01_cycle"]),
                        ],
                        [],
                    ),
                ):
                    with self.subTest(label=label), self.assertRaisesRegex(GraphCompilerError, label if label in {"sorted", "duplicate"} else "dependency"):
                        prepare_dispatch_graph(
                            invalid_nodes,
                            completed_nodes=completed,
                            native_capacity=1,
                            native_catalog=native_catalog(),
                            repo=repo,
                        )

        self.assertEqual([item["task_name"] for item in prepared["dispatches"]], ["worker_n01_root_luna_max_g01"])
        self.assertEqual(prepared["completed_nodes"], ["done"])
        self.assertEqual(prepared["deferred_nodes"], ["n02_ready", "n04_completed"])
        self.assertEqual(prepared["blocked_dependency_nodes"], ["n03_blocked"])
        selections = {node["node"]: node["selection"] for node in prepared["manifest"]["nodes"]}
        self.assertEqual(selections["n01_root"]["downstream_count"], 1)
        self.assertTrue(selections["n04_completed"]["dependencies_ready"])

    def test_matching_microtasks_aggregate_with_member_contracts_and_an_isolated_one_stays_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for name in ("first.txt", "second.txt", "isolated.txt"):
                (repo / name).write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            def microtask(name: str, path: str, acceptance_id: str, responsibility: str) -> dict[str, object]:
                return {
                    "acceptance_facts": {
                        "acceptance_ids": [acceptance_id],
                        "deterministic_graph_coverage": [acceptance_id],
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
                        "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": path}],
                    "selection": {"depends_on": [], "responsibility": responsibility},
                }

            first = microtask("n01_first", "first.txt", "A01", "shared")
            second = microtask("n02_second", "second.txt", "A02", "shared")
            isolated = microtask("n03_isolated", "isolated.txt", "A03", "isolated")
            environment = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-aggregate"}
            with mock.patch.dict(os.environ, environment):
                prepared = prepare_dispatch_graph(
                    [first, second, isolated],
                    native_capacity=1,
                    native_catalog=native_catalog(),
                    repo=repo,
                )
                verified = verify_prepared_graph(prepared, repo=repo)
                with self.assertRaisesRegex(GraphCompilerError, "prepared graph is malformed"):
                    verify_prepared_graph(
                        {**prepared, "protocol": "cco.prepared-graph.v2"},
                        repo=repo,
                    )

        capsule = parse_message(prepared["dispatches"][0]["message"])
        aggregate = capsule["node"]
        self.assertTrue(aggregate.startswith("aggregate_"))
        self.assertEqual(
            prepared["member_mapping"],
            {"n01_first": aggregate, "n02_second": aggregate},
        )
        self.assertEqual(prepared["primary_nodes"], ["n03_isolated"])
        self.assertEqual(verified["verdict"], "pass")
        self.assertEqual(capsule["acceptance_ids"], ["A01", "A02"])
        self.assertEqual(capsule["contract"]["members"], [first["contract"], second["contract"]])
        self.assertEqual(capsule["scopes"], [
            {"kind": "exact", "path": "first.txt"},
            {"kind": "exact", "path": "second.txt"},
        ])
        self.assertEqual(prepared["protocol"], "cco.prepared-graph.v3")
        self.assertEqual(prepared["manifest"]["protocol"], "cco.graph.v4")
        self.assertEqual(compact_dispatch_batch(prepared)["protocol"], "cco.dispatch-batch.v2")
        with self.assertRaisesRegex(GraphCompilerError, "prepared graph is malformed"):
            compact_dispatch_batch({**prepared, "protocol": "cco.prepared-graph.v2"})
        with self.assertRaisesRegex(GraphCompilerError, "prepared graph is malformed"):
            compact_dispatch_batch(
                {
                    **prepared,
                    "manifest": {**prepared["manifest"], "protocol": "cco.graph.v3"},
                }
            )

    def test_prepare_cli_accepts_completed_nodes_and_emits_the_v2_batch(self) -> None:
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
                "contract": {"contract_rev": 1, "node": "n01_cli", "objective": "owned.txt"},
                "generation": 1,
                "node": "n01_cli",
                "placement": {
                    "benefits": [{"evidence": ["contract:A01"], "kind": "parallel_ready"}],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "owned.txt"}],
                "selection": {"depends_on": ["done"], "responsibility": "cli"},
            }
            environment = {
                **os.environ,
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": "v7-cli-dag",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "graph_compiler.py"),
                    "--repo",
                    str(repo),
                    "--native-capacity",
                    "0",
                ],
                input=json.dumps(
                    {
                        "completed_nodes": ["done"],
                        "native_catalog": native_catalog(),
                        "nodes": [node],
                    }
                ),
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        batch = json.loads(result.stdout)
        self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
        self.assertEqual(batch["completed_nodes"], ["done"])
        self.assertEqual(batch["deferred_nodes"], ["n01_cli"])
        self.assertNotIn("manifest", batch)

    def test_microtask_aggregation_requires_every_compatibility_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for name in ("first.txt", "second.txt"):
                (repo / name).write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)

            def microtask(name: str, path: str) -> dict[str, object]:
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
                    "contract": {"contract_rev": 1, "node": name, "objective": path},
                    "generation": 1,
                    "node": name,
                    "placement": {
                        "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": path}],
                    "selection": {"depends_on": [], "responsibility": "shared"},
                }

            def different(field: str, node: dict[str, object]) -> None:
                if field == "role":
                    node["role"] = "explorer"
                elif field == "assurance":
                    node["closure"] = {**node["closure"], "decision_space": "bounded_effect"}
                elif field == "acceptance mode":
                    node["acceptance_facts"] = {**node["acceptance_facts"], "events": ["deviation"]}
                elif field == "route":
                    node["route"] = {"fixed_effort": "max", "fixed_model": "gpt-5.6-luna", "source": "user"}
                elif field == "generation":
                    node["generation"] = 2
                elif field == "fork":
                    node["fork_turns"] = "1"
                elif field == "responsibility":
                    node["selection"] = {**node["selection"], "responsibility": "other"}
                elif field == "dependency frontier":
                    node["selection"] = {**node["selection"], "depends_on": ["done"]}
                else:  # pragma: no cover - the table above is exhaustive
                    raise AssertionError(field)

            environment = {"CCO_LEDGER_DIR": str(root / "ledger"), "CODEX_THREAD_ID": "v7-incompatible"}
            with mock.patch.dict(os.environ, environment):
                for field in (
                    "role",
                    "assurance",
                    "acceptance mode",
                    "route",
                    "generation",
                    "fork",
                    "responsibility",
                    "dependency frontier",
                ):
                    first = microtask("n01_first", "first.txt")
                    second = microtask("n02_second", "second.txt")
                    different(field, second)
                    with self.subTest(field=field):
                        prepared = prepare_dispatch_graph(
                            [first, second],
                            completed_nodes=["done"],
                            native_capacity=1,
                            native_catalog=native_catalog(),
                            repo=repo,
                        )
                        self.assertEqual(prepared["dispatches"], [])
                        self.assertEqual(prepared["member_mapping"], {})
                        self.assertEqual(prepared["primary_nodes"], ["n01_first", "n02_second"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
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
from graph_compiler import (  # noqa: E402
    GraphCompilerError,
    prepare_dispatch_graph,
    verify_prepared_graph,
)
from packet_compiler import compile_continuation, compile_result, parse_message  # noqa: E402
from routing_catalog import resolve_route_plan, route_plan_sha256  # noqa: E402
from tests.v6_test_support import closed_graph_node, fixed_route_plan  # noqa: E402


class GraphCompilerBehaviorTests(unittest.TestCase):
    def test_prepared_graph_precompiles_bound_native_fallback_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            plan = resolve_route_plan(
                [
                    {
                        "assurance": "deterministic",
                        "judgment": "complex",
                        "placement_benefits": [
                            {
                                "evidence": ["contract:fallback"],
                                "kind": "closed_execution",
                            }
                        ],
                        "purpose": "implementation",
                    }
                ],
                {
                    "fingerprint": "a" * 64,
                    "history": {},
                    "method": {"iq": "latest valid result per task; pass_rate * 150"},
                    "metrics_source": "https://api.codexradar.com/api/v1/model-metrics",
                    "models": 2,
                    "points": [
                        {
                            "average_minutes": 30.0,
                            "average_price_usd": 0.5,
                            "duration_samples": 100,
                            "effort": "max",
                            "incomplete_cost_samples": 0,
                            "iq": 108.0,
                            "latest_graded_at": "2026-08-03T09:00:00+00:00",
                            "model": "gpt-5.6-luna",
                            "passed": 72,
                            "price_samples": 100,
                            "total_runs": 300,
                            "valid_tasks": 100,
                        },
                        {
                            "average_minutes": 25.0,
                            "average_price_usd": 4.0,
                            "duration_samples": 100,
                            "effort": "max",
                            "incomplete_cost_samples": 0,
                            "iq": 109.5,
                            "latest_graded_at": "2026-08-03T09:00:00+00:00",
                            "model": "gpt-5.6-terra",
                            "passed": 73,
                            "price_samples": 100,
                            "total_runs": 300,
                            "valid_tasks": 100,
                        },
                    ],
                    "schema": 2,
                    "source": "https://api.codexradar.com/api/v1/table",
                    "source_updated_at": "2026-08-03T09:00:00+00:00",
                    "type": "distributed_intelligence_efficiency",
                },
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-luna",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        },
                        {
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        },
                    ]
                },
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "fallback-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [closed_graph_node()],
                    route_plan=plan,
                    native_capacity=1,
                    repo=repo,
                )
                initial = prepared["dispatches"][0]
                fallback = prepared["fallback_dispatches"]["n01_graph"][0]
                initial_capsule = parse_message(initial["message"])
                fallback_capsule = parse_message(fallback["message"])
                self.assertEqual(initial_capsule["graph_sha256"], fallback_capsule["graph_sha256"])
                self.assertEqual(initial_capsule["baseline"], fallback_capsule["baseline"])
                self.assertEqual(initial["model"], "gpt-5.6-luna")
                self.assertEqual(fallback["model"], "gpt-5.6-terra")

                initial_payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "fallback-session",
                    "tool_input": initial,
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-fallback-initial",
                }
                self.assertEqual(agent_preflight.evaluate(initial_payload), {})
                ledger_runtime.postflight_spawn(
                    {**initial_payload, "tool_response": {"status": "rejected"}}
                )
                fallback_outcome = agent_preflight.evaluate(
                    {
                        **initial_payload,
                        "tool_input": fallback,
                        "tool_use_id": "spawn-fallback-next",
                    }
                )

            self.assertEqual(fallback_outcome, {})

    def test_session_end_removes_prepared_workspace_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "cleanup-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [closed_graph_node()],
                    route_plan=fixed_route_plan(judgment="complex"),
                    native_capacity=1,
                    repo=repo,
                )
                artifact = Path(prepared["baseline_path"])
                self.assertTrue(artifact.is_file())
                abandoned = artifact.parent / ("abandoned-" + "a" * 64 + ".json")
                abandoned.write_text("{}\n", encoding="utf-8")
                os.utime(abandoned, (0, 0))
                ledger_runtime.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "SessionEnd",
                        "session_id": "cleanup-session",
                    }
                )

            self.assertFalse(artifact.exists())
            self.assertFalse(abandoned.exists())

    def test_subagent_stop_allows_graph_scopes_and_blocks_outside_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            for name in ("one.txt", "two.txt"):
                (repo / "src" / name).write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=CCO Tests",
                    "-c",
                    "user.email=cco-tests@example.invalid",
                    "commit",
                    "-qm",
                    "initial",
                ],
                cwd=repo,
                check=True,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "graph-scope-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [
                        closed_graph_node(
                            node="n01_one",
                            path="src/one.txt",
                            responsibility="one",
                        ),
                        closed_graph_node(
                            node="n02_two",
                            path="src/two.txt",
                            responsibility="two",
                        ),
                    ],
                    route_plan=fixed_route_plan(judgment="complex"),
                    native_capacity=2,
                    repo=repo,
                )
                capsules: list[dict[str, object]] = []
                owners: list[str] = []
                for index, native in enumerate(prepared["dispatches"]):
                    call_id = f"spawn-graph-scope-{index}"
                    preflight = {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "graph-scope-session",
                        "tool_input": native,
                        "tool_name": "spawn_agent",
                        "tool_use_id": call_id,
                    }
                    self.assertEqual(agent_preflight.evaluate(preflight), {})
                    ledger_runtime.postflight_spawn(
                        {
                            **preflight,
                            "tool_response": {"task_name": native["task_name"]},
                        }
                    )
                    capsules.append(parse_message(native["message"]))
                    owners.append("/root/" + str(native["task_name"]))

                (repo / "src" / "one.txt").write_text("one\n", encoding="utf-8")
                (repo / "src" / "two.txt").write_text("two\n", encoding="utf-8")
                first = subagent_stop.evaluate(
                    {
                        "agent_id": owners[0],
                        "agent_type": prepared["dispatches"][0]["agent_type"],
                        "cwd": str(repo),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": compile_result(
                            capsules[0], status="complete", disposition="retire"
                        ),
                        "session_id": "graph-scope-session",
                        "stop_hook_active": False,
                    }
                )
                self.assertEqual(first, {})

                (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
                second = subagent_stop.evaluate(
                    {
                        "agent_id": owners[1],
                        "agent_type": prepared["dispatches"][1]["agent_type"],
                        "cwd": str(repo),
                        "hook_event_name": "SubagentStop",
                        "last_assistant_message": compile_result(
                            capsules[1], status="complete", disposition="retire"
                        ),
                        "session_id": "graph-scope-session",
                        "stop_hook_active": False,
                    }
                )

            self.assertEqual(second["decision"], "block")
            self.assertIn("outside_lease:outside.txt", second["reason"])

    def test_spawn_is_blocked_when_its_prepared_baseline_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=CCO Tests",
                    "-c",
                    "user.email=cco-tests@example.invalid",
                    "commit",
                    "-qm",
                    "initial",
                ],
                cwd=repo,
                check=True,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "missing-baseline-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [closed_graph_node()],
                    route_plan=fixed_route_plan(judgment="complex"),
                    native_capacity=1,
                    repo=repo,
                )
                Path(prepared["baseline_path"]).unlink()
                outcome = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "missing-baseline-session",
                        "tool_input": prepared["dispatches"][0],
                        "tool_name": "spawn_agent",
                        "tool_use_id": "spawn-missing-baseline",
                    }
                )

            self.assertEqual(outcome["decision"], "block")
            self.assertIn("baseline", outcome["reason"])

    def test_safe_entry_derives_decisions_and_captures_one_real_strict_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
            (repo / "secret.txt").write_text("private\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=CCO Tests",
                    "-c",
                    "user.email=cco-tests@example.invalid",
                    "commit",
                    "-qm",
                    "initial",
                ],
                cwd=repo,
                check=True,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "graph-session",
                },
            ):
                plan = fixed_route_plan(judgment="complex")
                prepared = prepare_dispatch_graph(
                    [closed_graph_node()],
                    route_plan=plan,
                    native_capacity=1,
                    repo=repo,
                    workspace_mode="strict",
                )
                preflight_payload = {
                    "cwd": str(repo),
                    "hook_event_name": "PreToolUse",
                    "session_id": "graph-session",
                    "tool_input": prepared["dispatches"][0],
                    "tool_name": "spawn_agent",
                    "tool_use_id": "spawn-graph",
                }
                self.assertEqual(agent_preflight.evaluate(preflight_payload), {})
                ledger_row = ledger_runtime.ledger_for(preflight_payload).read_rows()[0]

            self.assertEqual(prepared["protocol"], "cco.prepared-graph.v1")
            self.assertEqual(len(prepared["dispatches"]), 1)
            capsule = parse_message(prepared["dispatches"][0]["message"])
            self.assertEqual(capsule["purpose"], "implementation")
            self.assertEqual(capsule["judgment"], "complex")
            self.assertEqual(capsule["decision"]["derived"]["placement"]["target"], "child")
            self.assertEqual(capsule["decision"]["derived"]["acceptance"]["mode"], "primary")
            self.assertEqual(capsule["baseline"], prepared["baseline"])
            self.assertRegex(capsule["graph_sha256"], r"^sha256:[0-9a-f]{64}$")
            baseline_path = Path(prepared["baseline_path"])
            self.assertTrue(baseline_path.is_file())
            snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))["snapshot"]
            self.assertEqual(snapshot["ignored_mode"], "strict")
            self.assertIn("secret.txt", snapshot["entries"])
            self.assertEqual(prepared["route_plan"], plan)
            self.assertEqual(ledger_row["baseline"], prepared["baseline"])
            self.assertEqual(ledger_row["baseline_path"], prepared["baseline_path"])
            self.assertEqual(ledger_row["graph_sha256"], prepared["graph_sha256"])
            self.assertEqual(ledger_row["scopes"], capsule["scopes"])
            self.assertEqual(ledger_row["workspace_mode"], "strict")

            (repo / "secret.txt").write_text("unexpected\n", encoding="utf-8")
            verification = verify_prepared_graph(prepared, repo=repo)
            self.assertEqual(verification["verdict"], "violation")
            self.assertEqual(verification["changed_paths"], ["secret.txt"])
            self.assertEqual(
                verification["violations"], ["outside_lease:secret.txt"]
            )

            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "graph-session",
                },
            ):
                ledger_runtime.postflight_spawn(
                    {
                        **preflight_payload,
                        "tool_response": {
                            "task_name": prepared["dispatches"][0]["task_name"]
                        },
                    }
                )
                continuation = compile_continuation(
                    capsule,
                    target="/root/" + str(prepared["dispatches"][0]["task_name"]),
                    delta={"request": "report the observed evidence"},
                )
                baseline_path.unlink()
                continuation_outcome = agent_preflight.evaluate(
                    {
                        "cwd": str(repo),
                        "hook_event_name": "PreToolUse",
                        "session_id": "graph-session",
                        "tool_input": continuation,
                        "tool_name": "send_message",
                        "tool_use_id": "continue-missing-artifact",
                    }
                )

            self.assertEqual(continuation_outcome["decision"], "block")
            self.assertIn("baseline", continuation_outcome["reason"])

    def test_graph_derives_assurance_and_binds_each_node_to_its_exact_route(self) -> None:
        deterministic = closed_graph_node(
            node="n01_deterministic",
            path="src/deterministic.txt",
            responsibility="deterministic-file",
        )
        guarded = closed_graph_node(
            node="n02_guarded",
            path="src/guarded.txt",
            responsibility="guarded-file",
        )
        guarded["acceptance_facts"]["risk_assessment"]["security"] = "yes"
        benefits = [{"evidence": ["contract:test"], "kind": "closed_execution"}]
        plan = resolve_route_plan(
            [
                {
                    "assurance": "deterministic",
                    "fixed_effort": "max",
                    "fixed_model": "gpt-5.6-luna",
                    "judgment": "complex",
                    "placement_benefits": benefits,
                    "purpose": "implementation",
                },
                {
                    "assurance": "guarded",
                    "fixed_effort": "max",
                    "fixed_model": "gpt-5.6-terra",
                    "judgment": "complex",
                    "placement_benefits": benefits,
                    "purpose": "implementation",
                },
            ],
            {},
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    },
                    {
                        "slug": "gpt-5.6-terra",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    },
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "deterministic.txt").write_text(
                "one\n", encoding="utf-8"
            )
            (repo / "src" / "guarded.txt").write_text("two\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "assurance-session",
                },
            ):
                mismatched = json.loads(json.dumps(plan))
                mismatched["routes"] = [
                    route
                    for route in mismatched["routes"]
                    if route["assurance"] == "deterministic"
                ]
                mismatched["plan_sha256"] = route_plan_sha256(mismatched)
                with self.assertRaisesRegex(
                    GraphCompilerError, "exact derived route key"
                ):
                    prepare_dispatch_graph(
                        [guarded],
                        route_plan=mismatched,
                        native_capacity=1,
                        repo=repo,
                    )
                prepared = prepare_dispatch_graph(
                    [deterministic, guarded],
                    route_plan=plan,
                    native_capacity=2,
                    repo=repo,
                )

        self.assertEqual(
            {
                parse_message(item["message"])["node"]: item["model"]
                for item in prepared["dispatches"]
            },
            {
                "n01_deterministic": "gpt-5.6-luna",
                "n02_guarded": "gpt-5.6-terra",
            },
        )


if __name__ == "__main__":
    unittest.main()

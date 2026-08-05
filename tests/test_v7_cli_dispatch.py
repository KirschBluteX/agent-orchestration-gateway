from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts" / "graph_compiler.py"


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


class V7CliDispatchTests(unittest.TestCase):
    def test_reviewer_fast_path_reuses_ledger_baseline_scopes_and_result_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("implemented\n", encoding="utf-8")
            session_id = "review-fast-path"
            ledger_root = root / "ledger"
            ledger_root.mkdir()
            old_baseline = "sha256:" + ("a" * 64)
            (ledger_root / f"{session_id}.json").write_text(
                json.dumps(
                    {
                        "fenced_owners": [],
                        "guarded_floors": [],
                        "rows": {
                            "n01_worker@1": {
                                "acceptance_ids": ["A01"],
                                "baseline": old_baseline,
                                "contract_rev": 1,
                                "input_sha256": "sha256:" + ("b" * 64),
                                "node": "n01_worker",
                                "review_seed": {
                                    "disposition": "retire",
                                    "payload": {
                                        "blockers": [],
                                        "changed_paths": ["owned.txt"],
                                        "deviations": [],
                                        "evidence": {"A01": "focused worker check passed"},
                                        "failure_signature": None,
                                        "summary": "implemented owned.txt",
                                    },
                                    "status": "complete",
                                },
                                "scopes": [{"kind": "exact", "path": "owned.txt"}],
                                "role": "worker",
                                "state": "retired",
                            }
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            document = {
                "native_catalog": {
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                },
                "nodes": [
                    {
                        "contract": {
                            "acceptance": [
                                {
                                    "criterion": "Review the exact worker delta and evidence.",
                                    "id": "A01",
                                }
                            ],
                            "contract_rev": 1,
                            "node": "review_release",
                            "objective": "review n01_worker",
                        },
                        "epoch": "e01",
                        "node": "review_release",
                        "review_source": {"contract_rev": 1, "node": "n01_worker"},
                    }
                ],
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--repo",
                    str(repo),
                    "--native-capacity",
                    "1",
                    "--full",
                ],
                input=json.dumps(document),
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "CCO_LEDGER_DIR": str(ledger_root),
                    "CODEX_THREAD_ID": session_id,
                },
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prepared = json.loads(completed.stdout)
            capsule = json.loads(
                prepared["dispatches"][0]["message"].split("\n", 2)[2][
                    len("CAPSULE_JSON: ") :
                ]
            )
            self.assertEqual(capsule["role"], "reviewer")
            self.assertEqual(capsule["baseline"], old_baseline)
            self.assertRegex(capsule["current_state"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(capsule["scopes"], [{"kind": "exact", "path": "owned.txt"}])
            self.assertEqual(capsule["acceptance_ids"], ["A01"])
            self.assertEqual(
                capsule["evidence"]["source_result"]["payload"]["changed_paths"],
                ["owned.txt"],
            )

    def test_normal_cli_commits_transaction_and_emits_only_short_spawn_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "owned.txt").write_text("baseline\n", encoding="utf-8")
            document = {
                "defaults": {
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
                    "generation": 1,
                    "placement": {
                        "benefits": [
                            {"evidence": ["contract:A01"], "kind": "parallel_ready"}
                        ],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                },
                "native_catalog": {
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                },
                "nodes": [
                    {
                        "contract": {
                            "contract_rev": 1,
                            "node": "n01_cli",
                            "objective": "change owned.txt",
                        },
                        "node": "n01_cli",
                        "scopes": [{"kind": "exact", "path": "owned.txt"}],
                        "selection": {"depends_on": [], "responsibility": "cli"},
                    }
                ],
            }
            environment = {
                **os.environ,
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": "cli-dispatch",
            }
            completed = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--repo", str(repo), "--native-capacity", "1"],
                input=json.dumps(document),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            batch = json.loads(completed.stdout)

        self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
        self.assertIsNotNone(batch["transaction_id"])
        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertTrue(batch["dispatches"][0]["message"].startswith("CCO_DISPATCH_REF "))
        self.assertNotIn("CAPSULE_JSON", completed.stdout)

    def test_non_git_worker_cli_uses_bounded_directory_backend_without_git_init(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "owned.txt").write_text("baseline\n", encoding="utf-8")
            document = {
                "defaults": {
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
                    "generation": 1,
                    "placement": {
                        "benefits": [
                            {"evidence": ["contract:A01"], "kind": "parallel_ready"}
                        ],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                },
                "native_catalog": {
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                },
                "nodes": [
                    {
                        "contract": {
                            "contract_rev": 1,
                            "node": "n01_directory",
                            "objective": "change owned.txt",
                        },
                        "node": "n01_directory",
                        "scopes": [{"kind": "exact", "path": "owned.txt"}],
                        "selection": {
                            "depends_on": [],
                            "responsibility": "directory worker",
                        },
                    }
                ],
            }
            environment = {
                **os.environ,
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": "directory-cli",
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--repo",
                    str(workspace),
                    "--native-capacity",
                    "1",
                ],
                input=json.dumps(document),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            batch = json.loads(completed.stdout)
            self.assertFalse((workspace / ".git").exists())
            self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
            self.assertEqual(len(batch["dispatches"]), 1)
            state_path = root / "ledger" / "directory-cli.dispatch-transactions.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            record = next(iter(state["transactions"].values()))
            self.assertEqual(record["workspace_mode"], "light")

    def test_non_git_explorer_uses_scope_capture_and_rejects_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "inspect.txt").write_text("baseline\n", encoding="utf-8")
            document = {
                "defaults": {
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
                    "generation": 1,
                    "placement": {
                        "benefits": [
                            {
                                "evidence": [
                                    "capsule:self-contained",
                                    "context:history-not-required",
                                ],
                                "kind": "context_partition",
                            }
                        ],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "explorer",
                },
                "native_catalog": {
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                },
                "nodes": [
                    {
                        "contract": {
                            "contract_rev": 1,
                            "node": "n01_explore",
                            "objective": "inspect inspect.txt",
                        },
                        "node": "n01_explore",
                        "scopes": [{"kind": "exact", "path": "inspect.txt"}],
                        "selection": {
                            "depends_on": [],
                            "responsibility": "inspect directory",
                        },
                    }
                ],
            }
            environment = {
                **os.environ,
                "CCO_LEDGER_DIR": str(root / "ledger"),
                "CODEX_THREAD_ID": "directory-explorer-cli",
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--repo",
                    str(workspace),
                    "--native-capacity",
                    "1",
                    "--full",
                ],
                input=json.dumps(document),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prepared = json.loads(completed.stdout)
            artifact = json.loads(
                Path(prepared["baseline_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["snapshot"]["capture_mode"], "scope")

    def test_non_git_worker_budget_excess_returns_to_primary_before_content_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "large.bin").write_bytes(b"x" * 32)
            document = {
                "defaults": {
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
                    "generation": 1,
                    "placement": {
                        "benefits": [
                            {"evidence": ["contract:A01"], "kind": "parallel_ready"}
                        ],
                        "direct_action_count": 1,
                        "direct_verification_count": 1,
                    },
                    "role": "worker",
                },
                "directory_max_bytes": 16,
                "native_catalog": {
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "slug": "gpt-5.6-terra",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                },
                "nodes": [
                    {
                        "contract": {
                            "contract_rev": 1,
                            "node": "n01_large",
                            "objective": "change large.bin",
                        },
                        "node": "n01_large",
                        "scopes": [{"kind": "exact", "path": "large.bin"}],
                        "selection": {"depends_on": [], "responsibility": "large"},
                    }
                ],
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "--repo",
                    str(workspace),
                    "--native-capacity",
                    "1",
                ],
                input=json.dumps(document),
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "directory-budget-cli",
                },
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["dispatches"], [])
            self.assertEqual(result["primary_nodes"], ["n01_large"])
            self.assertIn(
                "exceeds the configured file or byte budget",
                result["route_errors"]["n01_large"],
            )
            self.assertFalse((workspace / ".git").exists())


if __name__ == "__main__":
    unittest.main()

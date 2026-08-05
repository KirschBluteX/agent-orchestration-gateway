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
                check=True,
            )
            batch = json.loads(completed.stdout)

        self.assertEqual(batch["protocol"], "cco.dispatch-batch.v2")
        self.assertIsNotNone(batch["transaction_id"])
        self.assertEqual(len(batch["dispatches"]), 1)
        self.assertTrue(batch["dispatches"][0]["message"].startswith("CCO_DISPATCH_REF "))
        self.assertNotIn("CAPSULE_JSON", completed.stdout)


if __name__ == "__main__":
    unittest.main()

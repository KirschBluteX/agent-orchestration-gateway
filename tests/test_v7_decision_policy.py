from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from decision_policy import (  # noqa: E402
    DecisionPolicyError,
    derive_acceptance,
    derive_node_decision,
    select_ready_nodes,
)


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


class V7DecisionPolicyTests(unittest.TestCase):
    def test_mechanical_worker_is_derived_without_purpose_or_judgment(self) -> None:
        decision = derive_node_decision(
            {
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
                "placement": {
                    "benefits": [
                        {"evidence": ["contract:A01"], "kind": "closed_chain"}
                    ],
                    "direct_action_count": 2,
                    "direct_verification_count": 1,
                },
                "role": "worker",
            }
        )

        self.assertEqual(
            decision,
            {
                "acceptance": {"mode": "primary", "reasons": []},
                "acceptance_ids": ["A01"],
                "assurance": "mechanical",
                "placement": {"reason": "closed_chain", "target": "child"},
                "role": "worker",
            },
        )
        self.assertNotIn("purpose", decision)
        self.assertNotIn("judgment", decision)

    def test_microtask_and_unresolved_choice_stay_in_primary(self) -> None:
        base = {
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
            "placement": {
                "benefits": [{"evidence": ["contract:A01"], "kind": "closed_chain"}],
                "direct_action_count": 1,
                "direct_verification_count": 1,
            },
            "role": "worker",
        }
        decision = derive_node_decision(base)
        self.assertEqual(decision["placement"], {"reason": "microtask", "target": "primary"})

        unresolved = {**base, "closure": {**base["closure"], "interfaces_closed": False}}
        with self.assertRaisesRegex(DecisionPolicyError, "unresolved closure"):
            derive_node_decision(unresolved)

    def test_only_real_risk_events_require_independent_acceptance(self) -> None:
        facts = {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": no_risks(),
        }
        self.assertEqual(
            derive_acceptance(**facts, events=[]),
            {"mode": "primary", "reasons": []},
        )
        self.assertEqual(
            derive_acceptance(**facts, events=["deviation"]),
            {"mode": "independent", "reasons": ["deviation"]},
        )
        with self.assertRaisesRegex(DecisionPolicyError, "unsupported acceptance event"):
            derive_acceptance(**facts, events=["followup"])

    def test_selector_fills_native_capacity_with_a_maximum_nonconflicting_set(self) -> None:
        edges = {(0, 2), (0, 3), (0, 4), (1, 2), (1, 3)}
        nodes = []
        for index in range(5):
            nodes.append(
                {
                    "access": "write",
                    "dependencies_ready": True,
                    "downstream_count": 1,
                    "node": f"n{index}",
                    "responsibility": f"responsibility-{index}",
                    "scope": [
                        {"kind": "exact", "path": f"conflicts/e{left}_{right}"}
                        for left, right in sorted(edges)
                        if index in {left, right}
                    ],
                }
            )
        self.assertEqual(select_ready_nodes(nodes, native_capacity=3), ["n2", "n3", "n4"])

        shared = [{"kind": "prefix", "path": "src/shared"}]
        reads_and_write = [
            {
                "access": access,
                "dependencies_ready": True,
                "node": name,
                "responsibility": name,
                "scope": shared,
            }
            for access, name in (
                ("read", "n01_read"),
                ("read", "n02_read"),
                ("write", "n03_write"),
            )
        ]
        self.assertEqual(
            select_ready_nodes(reads_and_write, native_capacity=3),
            ["n01_read", "n02_read"],
        )


if __name__ == "__main__":
    unittest.main()

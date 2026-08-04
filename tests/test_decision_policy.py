from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "plugins" / "codex-cost-orchestrator" / "scripts" / "decision_policy.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))


def load_module():
    spec = importlib.util.spec_from_file_location("cco_decision_policy", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load decision_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlacementPolicyTests(unittest.TestCase):
    def test_same_model_closed_execution_returns_to_primary(self) -> None:
        policy = load_module()
        decision = policy.select_placement(
            purpose="implementation",
            primary_model="gpt-5.6-luna",
            selected_model="gpt-5.6-luna",
            benefits=[
                {
                    "evidence": ["contract:sha256:" + "a" * 64],
                    "kind": "closed_execution",
                }
            ],
        )

        self.assertEqual(
            decision,
            {
                "reason": "same_model_execution_only",
                "target": "primary",
            },
        )


class DecisionKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_module()

    def complete_risks(self, answer: str = "no") -> dict[str, str]:
        return {risk: answer for risk in self.policy.RISK_CATEGORIES}

    def test_purpose_is_derived_from_authorized_effects(self) -> None:
        self.assertEqual(
            self.policy.classify_purpose(
                acceptance_verdict=False,
                diagnostic_process=False,
                repository_mutation=False,
            ),
            "analysis_inspect",
        )
        self.assertEqual(
            self.policy.classify_purpose(
                acceptance_verdict=False,
                diagnostic_process=True,
                repository_mutation=False,
            ),
            "analysis_probe",
        )
        self.assertEqual(
            self.policy.classify_purpose(
                acceptance_verdict=False,
                diagnostic_process=True,
                repository_mutation=True,
            ),
            "implementation",
        )
        self.assertEqual(
            self.policy.classify_purpose(
                acceptance_verdict=True,
                diagnostic_process=False,
                repository_mutation=False,
            ),
            "acceptance",
        )
        with self.assertRaises(self.policy.DecisionPolicyError):
            self.policy.classify_purpose(
                acceptance_verdict=True,
                diagnostic_process=False,
                repository_mutation=True,
            )

    def test_closure_record_mechanically_derives_judgment(self) -> None:
        closure = {
            "acceptance_closed": True,
            "criteria_closed": True,
            "decision_space": "bounded_effect",
            "interfaces_closed": True,
            "objective_closed": True,
            "ownership_closed": True,
        }

        self.assertEqual(self.policy.derive_judgment(closure), "complex")
        closure["decision_space"] = "acceptance_equivalent"
        self.assertEqual(self.policy.derive_judgment(closure), "routine")
        closure["objective_closed"] = False
        self.assertEqual(self.policy.derive_judgment(closure), "unresolved")

    def test_risk_assessment_is_complete_and_uses_explicit_yes_no_answers(self) -> None:
        risks = self.complete_risks()
        risks["security"] = "yes"

        self.assertEqual(self.policy.active_risks(risks), ("security",))

        incomplete = dict(risks)
        incomplete.pop("schema")
        with self.assertRaises(self.policy.DecisionPolicyError):
            self.policy.normalize_risk_assessment(incomplete)

        invalid = dict(risks)
        invalid["security"] = False
        with self.assertRaises(self.policy.DecisionPolicyError):
            self.policy.normalize_risk_assessment(invalid)

    def test_acceptance_is_primary_for_complex_or_multi_contract_structure(
        self,
    ) -> None:
        decision = self.policy.derive_acceptance(
            acceptance_ids=["A01", "A02"],
            deterministic_graph_coverage=["A01", "A02"],
            events=[],
            required_verification_strengths=[
                "deterministic",
                "deterministic",
                "deterministic",
            ],
            risk_assessment=self.complete_risks(),
        )

        self.assertEqual(decision, {"mode": "primary", "reasons": []})

    def test_acceptance_reasons_are_derived_from_risk_strength_coverage_and_events(
        self,
    ) -> None:
        risks = self.complete_risks()
        risks["public_interface"] = "yes"
        decision = self.policy.derive_acceptance(
            acceptance_ids=["A01", "A02"],
            deterministic_graph_coverage=["A01"],
            events=["retry"],
            required_verification_strengths=["deterministic", "manual"],
            risk_assessment=risks,
        )

        self.assertEqual(
            decision,
            {
                "mode": "independent",
                "reasons": [
                    "declared_risk",
                    "graph_verification_incomplete",
                    "retry",
                    "verification_not_deterministic",
                ],
            },
        )

    def test_acceptance_inputs_are_bounded_canonical_enums(self) -> None:
        with self.assertRaises(self.policy.DecisionPolicyError):
            self.policy.derive_acceptance(
                acceptance_ids=["A01"],
                deterministic_graph_coverage=["A01"],
                events=["complex_lane"],
                required_verification_strengths=["deterministic"],
                risk_assessment=self.complete_risks(),
            )

        with self.assertRaises(self.policy.DecisionPolicyError):
            self.policy.require_verification_strength("strong")

    def test_concurrent_execution_alone_does_not_require_independent_acceptance(self) -> None:
        decision = self.policy.derive_acceptance(
            acceptance_ids=["A01"],
            deterministic_graph_coverage=["A01"],
            events=["concurrent_execution"],
            required_verification_strengths=["deterministic"],
            risk_assessment=self.complete_risks(),
        )

        self.assertEqual(decision, {"mode": "primary", "reasons": []})

    def test_primary_owned_change_uses_model_neutral_acceptance_language(self) -> None:
        decision = self.policy.derive_acceptance(
            acceptance_ids=["A01"],
            deterministic_graph_coverage=["A01"],
            events=["primary_owned_change"],
            required_verification_strengths=["deterministic"],
            risk_assessment=self.complete_risks(),
        )

        self.assertEqual(
            decision,
            {"mode": "independent", "reasons": ["primary_owned_change"]},
        )

    def test_ready_disjoint_responsibilities_fill_observed_native_capacity(self) -> None:
        nodes = [
            {
                "access": "write",
                "node": f"n{index:02d}",
                "responsibility": f"responsibility-{index}",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": f"src/part-{index}"}],
            }
            for index in range(1, 7)
        ]

        selected = self.policy.select_ready_nodes(nodes, native_capacity=6)

        self.assertEqual(selected, [f"n{index:02d}" for index in range(1, 7)])

    def test_capacity_selection_skips_dependency_scope_and_responsibility_conflicts(self) -> None:
        nodes = [
            {
                "access": "write",
                "node": "n01",
                "responsibility": "api",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": "src/api"}],
            },
            {
                "access": "write",
                "node": "n02",
                "responsibility": "api",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": "src/other"}],
            },
            {
                "access": "write",
                "node": "n03",
                "responsibility": "docs",
                "dependencies_ready": False,
                "scope": [{"kind": "prefix", "path": "docs"}],
            },
            {
                "access": "write",
                "node": "n04",
                "responsibility": "tests",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": "src/api"}],
            },
            {
                "access": "write",
                "node": "n05",
                "responsibility": "cli",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": "src/cli"}],
            },
            {
                "access": "write",
                "node": "n06",
                "responsibility": "nested",
                "dependencies_ready": True,
                "scope": [{"kind": "prefix", "path": "src/api/routes"}],
            },
        ]

        self.assertEqual(
            self.policy.select_ready_nodes(nodes, native_capacity=4),
            ["n02", "n04", "n05"],
        )

    def test_overlapping_reads_can_run_together_but_a_write_is_serialized(self) -> None:
        shared = [{"kind": "prefix", "path": "src/shared"}]
        nodes = [
            {
                "access": "read",
                "dependencies_ready": True,
                "node": "n01_read_api",
                "responsibility": "inspect-api",
                "scope": shared,
            },
            {
                "access": "read",
                "dependencies_ready": True,
                "node": "n02_read_tests",
                "responsibility": "inspect-tests",
                "scope": shared,
            },
            {
                "access": "write",
                "dependencies_ready": True,
                "node": "n03_write_shared",
                "responsibility": "change-shared",
                "scope": shared,
            },
        ]

        self.assertEqual(
            self.policy.select_ready_nodes(nodes, native_capacity=3),
            ["n01_read_api", "n02_read_tests"],
        )

    def test_selector_avoids_a_broad_node_that_would_underfill_capacity(self) -> None:
        nodes = [
            {
                "access": "write",
                "dependencies_ready": True,
                "node": "n01_broad",
                "responsibility": "broad",
                "scope": [{"kind": "prefix", "path": "src"}],
            },
            {
                "access": "write",
                "dependencies_ready": True,
                "node": "n02_left",
                "responsibility": "left",
                "scope": [{"kind": "exact", "path": "src/left.py"}],
            },
            {
                "access": "write",
                "dependencies_ready": True,
                "node": "n03_right",
                "responsibility": "right",
                "scope": [{"kind": "exact", "path": "src/right.py"}],
            },
        ]

        self.assertEqual(
            self.policy.select_ready_nodes(nodes, native_capacity=2),
            ["n02_left", "n03_right"],
        )

    def test_selector_finds_a_capacity_sized_set_when_greedy_degree_cannot(self) -> None:
        edges = {(0, 2), (0, 3), (0, 4), (1, 2), (1, 3)}
        nodes = []
        for index in range(5):
            scopes = [
                {"kind": "exact", "path": f"conflicts/e{left}_{right}"}
                for left, right in sorted(edges)
                if index in {left, right}
            ]
            nodes.append(
                {
                    "access": "write",
                    "dependencies_ready": True,
                    "node": f"n{index}",
                    "responsibility": f"responsibility-{index}",
                    "scope": scopes,
                }
            )

        selected = self.policy.select_ready_nodes(nodes, native_capacity=3)

        self.assertEqual(selected, ["n2", "n3", "n4"])

    def test_selector_rejects_unsafe_or_ambiguous_repository_scopes(self) -> None:
        base = {
            "access": "write",
            "dependencies_ready": True,
            "node": "n01",
            "responsibility": "unsafe",
        }
        for path in ("../outside", ".git/config", "src\\alias.py"):
            with self.subTest(path=path), self.assertRaises(
                self.policy.DecisionPolicyError
            ):
                self.policy.select_ready_nodes(
                    [{**base, "scope": [{"kind": "exact", "path": path}]}],
                    native_capacity=1,
                )

    def test_zero_observed_capacity_selects_no_nodes_without_a_cco_cap(self) -> None:
        self.assertEqual(self.policy.select_ready_nodes([], native_capacity=0), [])


if __name__ == "__main__":
    unittest.main()

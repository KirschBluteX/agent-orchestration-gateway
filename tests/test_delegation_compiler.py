from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from delegation_compiler import (  # noqa: E402
    DELEGATE,
    PRIMARY_DIRECT,
    DelegationCompilerError,
    compile_delegation_request,
    normalize_closed_plan,
)


def atomic_request(**changes: object) -> dict[str, object]:
    request: dict[str, object] = {
        "authority": "delegated",
        "clarification_required": False,
        "closed": True,
        "declared_tools": [],
        "direct": False,
        "protocol": "cco.delegation.v1",
        "upper_bound_seconds": 30,
        "work": {
            "goal": "update one bounded file",
            "kind": "atomic",
            "node": {
                "acceptance": {"A01": "the file is updated"},
                "id": "update_file",
                "objective": "update one bounded file",
                "role": "worker",
                "scopes": [{"kind": "exact", "path": "src/file.py"}],
            },
        },
    }
    request.update(changes)
    return request


def dag_plan() -> dict[str, object]:
    return {
        "acceptance": {"A01": "inspect a", "A02": "inspect b"},
        "goal": "inspect two files",
        "nodes": [
            {
                "acceptance": ["A02"],
                "id": "inspect_b",
                "objective": "inspect b",
                "role": "explorer",
                "scopes": [{"kind": "exact", "path": "b.txt"}],
            },
            {
                "acceptance": ["A01"],
                "id": "inspect_a",
                "objective": "inspect a",
                "role": "explorer",
                "scopes": [{"kind": "exact", "path": "a.txt"}],
            },
        ],
    }


class DelegationCompilerTests(unittest.TestCase):
    def test_atomic_compilation_is_pure_and_deterministic(self) -> None:
        first = compile_delegation_request(atomic_request())
        reordered = atomic_request()
        reordered["declared_tools"] = []
        second = compile_delegation_request(reordered)

        self.assertEqual(first, second)
        self.assertEqual(first["disposition"], DELEGATE)
        self.assertEqual(first["plan"]["nodes"][0]["acceptance"], ["A01"])
        self.assertEqual(
            first["plan"]["nodes"][0]["scopes"],
            [{"kind": "exact", "path": "src/file.py"}],
        )
        self.assertEqual(first["plan"], normalize_closed_plan(first["plan"]))

    def test_primary_exceptions_are_compiled_before_work(self) -> None:
        cases = {
            "authority": atomic_request(authority="primary", work=None),
            "clarification": atomic_request(
                clarification_required=True, closed=False, work=None
            ),
            "explicit_direct": atomic_request(direct=True, work=None),
            "fast_tool": atomic_request(
                declared_tools=["read_file"], upper_bound_seconds=29
            ),
        }
        for reason, request in cases.items():
            with self.subTest(reason=reason):
                compiled = compile_delegation_request(request)
                self.assertEqual(compiled["disposition"], PRIMARY_DIRECT)
                self.assertEqual(compiled["reason"], reason)

    def test_one_fast_declared_tool_stays_in_primary_for_a_closed_dag(self) -> None:
        request = atomic_request(
            declared_tools=["read_file"],
            upper_bound_seconds=1,
            work={"kind": "dag", "plan": dag_plan()},
        )

        compiled = compile_delegation_request(request)

        self.assertEqual(compiled["disposition"], PRIMARY_DIRECT)
        self.assertEqual(compiled["reason"], "fast_tool")

    def test_fast_declared_tool_stays_in_primary(self) -> None:
        request = atomic_request(
            declared_tools=["read_file"], upper_bound_seconds=29
        )
        compiled = compile_delegation_request(request)

        self.assertEqual(compiled["reason"], "fast_tool")

    def test_planner_proposal_is_stateless_canonical_dag_input(self) -> None:
        request = atomic_request(
            work={
                "kind": "planner_proposal",
                "proposal": {
                    "plan": dag_plan(),
                    "protocol": "cco.planner-proposal.v1",
                },
            }
        )

        compiled = compile_delegation_request(request)

        self.assertEqual(compiled["disposition"], DELEGATE)
        self.assertNotIn("planner", compiled)
        self.assertEqual(
            [node["id"] for node in compiled["plan"]["nodes"]],
            ["inspect_a", "inspect_b"],
        )

    def test_incomplete_or_route_capable_planner_proposals_fail_closed(self) -> None:
        incomplete = atomic_request(
            work={
                "kind": "planner_proposal",
                "proposal": {"protocol": "cco.planner-proposal.v1"},
            }
        )
        route_capable = atomic_request(
            work={
                "kind": "planner_proposal",
                "proposal": {
                    "plan": dag_plan(),
                    "protocol": "cco.planner-proposal.v1",
                    "pin": {"model": "gpt-5.6-terra"},
                },
            }
        )
        for request in (incomplete, route_capable):
            with self.assertRaises(DelegationCompilerError):
                compile_delegation_request(request)

    def test_assurance_ladder_and_one_final_reviewer_are_deterministic(self) -> None:
        plan = {
            "acceptance": {
                "A01": "mechanical change",
                "A02": "bounded inspection",
                "A03": "public interface change",
            },
            "goal": "exercise assurance",
            "nodes": [
                {
                    "acceptance": ["A01"],
                    "decision": "mechanical",
                    "id": "mechanical",
                    "objective": "mechanical edit",
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": "a.txt"}],
                },
                {
                    "acceptance": ["A02"],
                    "id": "bounded",
                    "objective": "bounded inspection",
                    "role": "explorer",
                    "scopes": [{"kind": "exact", "path": "b.txt"}],
                },
                {
                    "acceptance": ["A03"],
                    "id": "public_change",
                    "objective": "change a public interface",
                    "risks": ["public interface"],
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": "c.txt"}],
                },
            ],
        }
        compiled = compile_delegation_request(
            atomic_request(work={"kind": "dag", "plan": plan})
        )["plan"]
        nodes = {node["id"]: node for node in compiled["nodes"]}

        self.assertEqual(nodes["mechanical"]["assurance"], "mechanical")
        self.assertEqual(nodes["bounded"]["assurance"], "bounded")
        self.assertEqual(nodes["public_change"]["assurance"], "guarded")
        self.assertEqual(nodes["final_review"]["role"], "reviewer")
        self.assertEqual(
            nodes["final_review"]["depends_on"],
            ["bounded", "mechanical", "public_change"],
        )
        self.assertEqual(nodes["final_review"]["acceptance"], ["A01", "A02", "A03"])
        self.assertEqual(
            nodes["final_review"]["scopes"],
            [
                {"kind": "exact", "path": "a.txt"},
                {"kind": "exact", "path": "b.txt"},
                {"kind": "exact", "path": "c.txt"},
            ],
        )

    def test_planner_proposal_cannot_claim_primary_authority(self) -> None:
        cases = {
            "risk": {**dag_plan(), "accept_risk": True},
            "isolation": {**dag_plan(), "writer_isolation": "cooperative"},
            "route": {
                **dag_plan(),
                "nodes": [{**dag_plan()["nodes"][0], "pin": {"model": "gpt-5.6-terra"}}, dag_plan()["nodes"][1]],
            },
            "context": {
                **dag_plan(),
                "nodes": [{**dag_plan()["nodes"][0], "context_turns": 1}, dag_plan()["nodes"][1]],
            },
        }
        for label, plan in cases.items():
            with self.subTest(label=label), self.assertRaises(DelegationCompilerError):
                compile_delegation_request(
                    atomic_request(
                        work={
                            "kind": "planner_proposal",
                            "proposal": {
                                "plan": plan,
                                "protocol": "cco.planner-proposal.v1",
                            },
                        }
                    )
                )

    def test_primary_may_opt_in_cooperative_isolation_after_planner_review(self) -> None:
        plan = dag_plan()
        plan["nodes"] = [{**node, "role": "worker"} for node in plan["nodes"]]
        request = atomic_request(
            work={
                "kind": "planner_proposal",
                "proposal": {
                    "plan": plan,
                    "protocol": "cco.planner-proposal.v1",
                },
            },
            writer_isolation="cooperative",
        )

        compiled = compile_delegation_request(request)

        self.assertEqual(compiled["plan"]["writer_isolation"], "cooperative")

    def test_explicit_current_accept_risk_is_the_only_reviewer_omission(self) -> None:
        plan = {
            "accept_risk": True,
            "acceptance": {"A01": "security-sensitive change"},
            "goal": "accept the current risk",
            "nodes": [
                {
                    "acceptance": ["A01"],
                    "id": "secure_change",
                    "objective": "change authentication",
                    "risks": ["auth"],
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": "auth.py"}],
                }
            ],
        }
        compiled = compile_delegation_request(
            atomic_request(work={"kind": "dag", "plan": plan})
        )["plan"]

        self.assertTrue(compiled["accept_risk"])
        self.assertEqual([node["id"] for node in compiled["nodes"]], ["secure_change"])
        with self.assertRaisesRegex(DelegationCompilerError, "unsupported risk"):
            compile_delegation_request(
                atomic_request(
                    work={
                        "kind": "dag",
                        "plan": {
                            **plan,
                            "nodes": [{**plan["nodes"][0], "risks": ["guess"]}],
                        },
                    }
                )
            )

    def test_guarded_explorer_only_plan_receives_one_final_reviewer(self) -> None:
        plan = {
            "acceptance": {"A01": "authentication analysis is complete"},
            "goal": "inspect authentication semantics",
            "nodes": [
                {
                    "acceptance": ["A01"],
                    "id": "inspect_auth",
                    "objective": "inspect authentication semantics",
                    "risks": ["auth"],
                    "role": "explorer",
                    "scopes": [{"kind": "prefix", "path": "src/auth"}],
                }
            ],
        }

        compiled = compile_delegation_request(
            atomic_request(work={"kind": "dag", "plan": plan})
        )["plan"]
        nodes = {node["id"]: node for node in compiled["nodes"]}

        self.assertEqual(nodes["inspect_auth"]["assurance"], "guarded")
        self.assertEqual(nodes["final_review"]["depends_on"], ["inspect_auth"])
        self.assertEqual(nodes["final_review"]["acceptance"], ["A01"])
        self.assertEqual(
            nodes["final_review"]["scopes"],
            [{"kind": "prefix", "path": "src/auth"}],
        )


if __name__ == "__main__":
    unittest.main()

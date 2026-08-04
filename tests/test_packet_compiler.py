from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from packet_compiler import (  # noqa: E402
    CapsuleError,
    READ_ROLE,
    WRITE_ROLE,
    capsule_sha256,
    compile_dispatch_batch,
    compile_dispatch,
    compile_result,
    compile_continuation,
    parse_message,
    parse_result_message,
)
from routing_catalog import route_plan_sha256  # noqa: E402


def contract(*, purpose: str = "implementation") -> dict[str, object]:
    return {
        "constraints": ["keep scope closed"],
        "interfaces": ["one bounded interface"],
        "node": "n01_capsule",
        "objective": "produce one bounded result",
        "purpose": purpose,
    }


def route(model: str = "gpt-5.6-luna", effort: str = "max") -> dict[str, object]:
    """Return a complete compact route plan for both common test purposes."""

    candidate = {"effort": effort, "model": model}
    routes = []
    for purpose, judgment, placement in (
        (
            "acceptance",
            "complex",
            {"reason": "independent_acceptance", "target": "child"},
        ),
        ("implementation", "complex", {"reason": "closed_execution", "target": "child"}),
        ("implementation", "routine", {"reason": "closed_execution", "target": "child"}),
    ):
        routes.append(
            {
                "candidates": [dict(candidate)],
                "decision_sha256": "sha256:" + "d" * 64,
                "dispatch": {"rank": 1, "rejection_tickets": []},
                "judgment": judgment,
                "placement": placement,
                "purpose": purpose,
                "selected": dict(candidate),
            }
        )
    plan: dict[str, object] = {
        "native_catalog_sha256": "sha256:" + "e" * 64,
        "needs_refresh": False,
        "protocol": "cco.route-plan.v1",
        "routes": routes,
    }
    plan["plan_sha256"] = route_plan_sha256(plan)
    return plan


class PacketCompilerTests(unittest.TestCase):
    def test_execution_has_one_generation_and_one_continuation_cursor(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "generation": 7,
                "judgment": "routine",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
            }
        )

        execution = parse_message(tool_input["message"])["execution"]
        self.assertEqual(
            execution,
            {
                "cursor": 0,
                "fork_turns": "none",
                "generation": 7,
                "task_name": "work_n01_capsule_routine_g07",
            },
        )
        self.assertNotIn("attempt", execution)
        self.assertNotIn("followup", execution)

    def test_deprecated_execution_counters_are_rejected(self) -> None:
        for field in ("attempt", "followup"):
            with self.subTest(field=field), self.assertRaises(CapsuleError):
                compile_dispatch(
                    {
                        "baseline": "sha256:" + "b" * 64,
                        "contract": contract(),
                        field: 1,
                        "judgment": "routine",
                        "kind": "work",
                        "node": "n01_capsule",
                        "purpose": "implementation",
                        "route_plan": route(),
                    }
                )

    def test_continuation_advances_only_the_cursor_and_keeps_generation(self) -> None:
        initial = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "generation": 3,
                "judgment": "complex",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route("gpt-5.6-terra", "max"),
            }
        )
        capsule = parse_message(initial["message"])

        continuation = compile_continuation(
            capsule,
            target="/root/" + initial["task_name"],
            delta={"request": "rerun V01 with the observed fixture"},
        )
        continued = parse_message(continuation["message"])

        self.assertEqual(set(continuation), {"message", "target"})
        self.assertEqual(continued["execution"]["generation"], 3)
        self.assertEqual(continued["execution"]["cursor"], 1)
        self.assertEqual(continued["previous_capsule_sha256"], capsule["capsule_sha256"])
        self.assertEqual(continued["delta"], {"request": "rerun V01 with the observed fixture"})

    def test_work_compile_is_one_small_canonical_capsule(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "generation": 1,
                "judgment": "routine",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
                "scopes": [{"kind": "exact", "path": "src/capsule.py"}],
            }
        )
        self.assertEqual(tool_input["agent_type"], WRITE_ROLE)
        self.assertEqual(tool_input["task_name"], "work_n01_capsule_routine_g01")
        self.assertLess(len(tool_input["message"].encode()), 1800)
        capsule = parse_message(tool_input["message"])
        self.assertEqual(capsule["role"], WRITE_ROLE)
        self.assertEqual(capsule["execution"]["generation"], 1)
        self.assertEqual(capsule_sha256(capsule), capsule["capsule_sha256"])

    def test_logical_complexity_does_not_create_a_second_physical_profile(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "judgment": "complex",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route("gpt-5.6-terra", "xhigh"),
            }
        )
        capsule = parse_message(tool_input["message"])
        self.assertEqual(tool_input["agent_type"], WRITE_ROLE)
        self.assertEqual(capsule["judgment"], "complex")
        self.assertEqual(tool_input["model"], "gpt-5.6-terra")
        self.assertEqual(tool_input["reasoning_effort"], "xhigh")

    def test_review_uses_read_only_leaf_and_one_acceptance_bundle(self) -> None:
        acceptance = {"graph": {"contracts": [contract()]}}
        evidence = {"current_state": "sha256:" + "c" * 64, "records": []}
        tool_input = compile_dispatch(
            {
                "acceptance": acceptance,
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "review_e01", "objective": "judge evidence"},
                "current_state": "sha256:" + "c" * 64,
                "epoch": "e01",
                "evidence": evidence,
                "generation": 2,
                "judgment": "complex",
                "kind": "review",
                "mode": "fresh",
                "node": "review_e01",
                "purpose": "acceptance",
                "route_plan": route("gpt-5.6-terra", "max"),
            }
        )
        self.assertEqual(tool_input["agent_type"], READ_ROLE)
        capsule = parse_message(tool_input["message"])
        self.assertEqual(capsule["mode"], "fresh")
        self.assertEqual(capsule["acceptance"], acceptance)
        self.assertEqual(capsule["evidence"], evidence)

    def test_native_input_route_and_capsule_tampering_are_rejected(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "kind": "work",
                "judgment": "routine",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
            }
        )
        altered = dict(tool_input)
        altered["model"] = "gpt-5.6-sol"
        with self.assertRaises(CapsuleError):
            parse_message(altered["message"].replace("gpt-5.6-luna", "gpt-5.6-sol"))
        malformed = tool_input["message"].replace("CAPSULE_SHA256:", "CAPSULE_SHA256: sha256:" , 1)
        with self.assertRaises(CapsuleError):
            parse_message(malformed)

    def test_wire_json_must_be_duplicate_free_and_exactly_canonical(self) -> None:
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "judgment": "routine",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
            }
        )
        message = native["message"]
        duplicate = message.replace(
            '"protocol":"cco.v6"',
            '"protocol":"ignored","protocol":"cco.v6"',
            1,
        )
        noncanonical = message.replace("CAPSULE_JSON: {", "CAPSULE_JSON: { ", 1)
        for altered in (duplicate, noncanonical):
            with self.subTest(altered=altered[:64]), self.assertRaises(CapsuleError):
                parse_message(altered)

    def test_result_contains_only_dispatch_identity_and_observation(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "kind": "work",
                "judgment": "routine",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
            }
        )
        capsule = parse_message(tool_input["message"])
        result = compile_result(
            capsule,
            status="complete",
            disposition="retire",
            changed=["src/capsule.py"],
            verified=["V01"],
        )
        self.assertLess(len(result.encode()), 800)
        payload = json.loads(result.split("RESULT_JSON: ", 1)[1])
        self.assertEqual(payload["dispatch_sha256"], capsule["capsule_sha256"])
        self.assertEqual(payload["payload"]["verified"], ["V01"])

        duplicate = result.replace(
            '"protocol":"cco.v6"',
            '"protocol":"ignored","protocol":"cco.v6"',
            1,
        )
        with self.assertRaises(CapsuleError):
            parse_result_message(duplicate)

    def test_worker_cannot_claim_primary_acceptance(self) -> None:
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "kind": "work",
                "judgment": "routine",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": route(),
            }
        )
        with self.assertRaises(CapsuleError):
            compile_result(
                parse_message(tool_input["message"]),
                status="complete",
                disposition="accept",
            )

    def test_capsule_requires_full_route_identity(self) -> None:
        spec = {
            "baseline": "sha256:" + "b" * 64,
            "contract": contract(),
            "kind": "work",
            "judgment": "routine",
            "node": "n01_capsule",
            "purpose": "implementation",
            "route": {"model": "gpt-5.6-luna"},
        }
        with self.assertRaises(CapsuleError):
            compile_dispatch(spec)

    def test_capsule_derives_compact_route_binding_from_the_complete_plan(self) -> None:
        plan = route("gpt-5.6-sol", "max")
        tool_input = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "judgment": "routine",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": plan,
            }
        )

        capsule = parse_message(tool_input["message"])
        self.assertEqual(
            capsule["route"],
            {
                "plan_sha256": plan["plan_sha256"],
                "rank": 1,
                "selected": {"effort": "max", "model": "gpt-5.6-sol"},
            },
        )
        self.assertNotIn("route_plan", capsule)
        self.assertNotIn("candidates", capsule["route"])

    def test_caller_cannot_supply_route_selection_or_plan_identity(self) -> None:
        base = {
            "baseline": "sha256:" + "b" * 64,
            "contract": contract(),
            "judgment": "routine",
            "kind": "work",
            "node": "n01_capsule",
            "purpose": "implementation",
            "route_plan": route(),
        }
        for fragment in (
            {
                "plan_sha256": "sha256:" + "a" * 64,
                "rank": 1,
                "selected": {"effort": "max", "model": "gpt-5.6-sol"},
            },
            {"rank": 1},
            {"selected": {"effort": "max", "model": "gpt-5.6-sol"}},
        ):
            with self.subTest(fragment=fragment), self.assertRaises(CapsuleError):
                compile_dispatch({**base, "route": fragment})

    def test_tampered_or_mismatched_route_plan_is_rejected(self) -> None:
        plan = route()
        tampered = json.loads(json.dumps(plan))
        tampered["routes"][1]["selected"] = {
            "effort": "max",
            "model": "gpt-5.6-sol",
        }
        with self.assertRaises(CapsuleError):
            compile_dispatch(
                {
                    "baseline": "sha256:" + "b" * 64,
                    "contract": contract(),
                    "judgment": "routine",
                    "kind": "work",
                    "node": "n01_capsule",
                    "purpose": "implementation",
                    "route_plan": tampered,
                }
            )

    def test_fixed_user_route_still_requires_a_route_plan(self) -> None:
        plan = route("gpt-5.6-sol", "xhigh")
        native = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": contract(),
                "judgment": "routine",
                "kind": "work",
                "node": "n01_capsule",
                "purpose": "implementation",
                "route_plan": plan,
            }
        )
        self.assertEqual(native["model"], "gpt-5.6-sol")
        self.assertEqual(native["reasoning_effort"], "xhigh")

    def test_repository_scope_aliases_are_rejected(self) -> None:
        for path in ("../outside.py", "src/../outside.py", "src/.git/config", "src\\x.py"):
            with self.subTest(path=path), self.assertRaises(CapsuleError):
                compile_dispatch(
                    {
                        "baseline": "sha256:" + "b" * 64,
                        "contract": contract(),
                        "judgment": "routine",
                        "kind": "work",
                        "node": "n01_capsule",
                        "purpose": "implementation",
                        "route_plan": route(),
                        "scopes": [{"kind": "exact", "path": path}],
                    }
                )

    def test_batch_compiles_only_selector_admitted_ready_nodes_at_native_capacity(self) -> None:
        plan = route()
        nodes = [
            {
                "dispatch": {
                    "baseline": "sha256:" + "b" * 64,
                    "contract": contract(),
                    "judgment": "routine",
                    "kind": "work",
                    "node": "n01_capsule",
                    "purpose": "implementation",
                    "scopes": [{"kind": "exact", "path": "src/a.py"}],
                },
                "selection": {
                    "dependencies_ready": True,
                    "node": "n01_capsule",
                    "responsibility": "capsule-a",
                    "scope": [{"kind": "exact", "path": "src/a.py"}],
                },
            },
            {
                "dispatch": {
                    "baseline": "sha256:" + "b" * 64,
                    "contract": contract(),
                    "judgment": "routine",
                    "kind": "work",
                    "node": "n02_capsule",
                    "purpose": "implementation",
                    "scopes": [{"kind": "exact", "path": "src/a.py"}],
                },
                "selection": {
                    "dependencies_ready": True,
                    "node": "n02_capsule",
                    "responsibility": "capsule-b",
                    "scope": [{"kind": "exact", "path": "src/a.py"}],
                },
            },
            {
                "dispatch": {
                    "baseline": "sha256:" + "b" * 64,
                    "contract": contract(),
                    "judgment": "routine",
                    "kind": "work",
                    "node": "n03_capsule",
                    "purpose": "implementation",
                    "scopes": [{"kind": "exact", "path": "src/c.py"}],
                },
                "selection": {
                    "dependencies_ready": True,
                    "node": "n03_capsule",
                    "responsibility": "capsule-c",
                    "scope": [{"kind": "exact", "path": "src/c.py"}],
                },
            },
        ]

        compiled = compile_dispatch_batch(
            nodes,
            route_plan=plan,
            native_capacity=2,
        )

        self.assertEqual([item["task_name"] for item in compiled], [
            "work_n01_capsule_routine_g01",
            "work_n03_capsule_routine_g01",
        ])
        self.assertTrue(all(set(item) == {
            "agent_type", "fork_turns", "message", "model",
            "reasoning_effort", "task_name",
        } for item in compiled))


if __name__ == "__main__":
    unittest.main()

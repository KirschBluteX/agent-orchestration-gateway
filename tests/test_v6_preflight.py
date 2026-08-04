from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks"
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path[:0] = [str(HOOKS), str(SCRIPTS)]

import agent_preflight  # noqa: E402
from packet_compiler import CapsuleError, compile_continuation, compile_dispatch  # noqa: E402
from tests.v6_test_support import dispatch_decision, fixed_route_plan  # noqa: E402


def native_input(*, kind: str = "work", purpose: str = "implementation") -> dict[str, object]:
    judgment = "complex" if kind == "review" else "routine"
    selected_model = "gpt-5.6-luna"
    decision = dispatch_decision(
        purpose=purpose,
        judgment=judgment,
        selected_model=selected_model,
    )
    spec: dict[str, object] = {
        "acceptance": decision["derived"]["acceptance"],
        "baseline": "sha256:" + "b" * 64,
        "contract": {"node": "n01_v6", "objective": "bounded result"},
        "decision": decision,
        "graph_sha256": "sha256:" + "a" * 64,
        "judgment": judgment,
        "kind": kind,
        "node": "n01_v6" if kind != "review" else "review_e01",
        "purpose": purpose,
        "route_plan": fixed_route_plan(
            purpose=purpose,
            judgment=judgment,
            model=selected_model,
        ),
    }
    if kind == "review":
        spec.update(
            {
                "current_state": "sha256:" + "c" * 64,
                "epoch": "e01",
                "evidence": {"records": []},
                "mode": "fresh",
            }
        )
    return compile_dispatch(spec)


class V6PreflightTests(unittest.TestCase):
    def test_compact_continuation_is_bound_to_same_owner_and_cursor(self) -> None:
        initial = native_input()
        from packet_compiler import parse_message

        target = "/root/" + initial["task_name"]
        continuation = compile_continuation(
            parse_message(initial["message"]),
            target=target,
            delta={"request": "report the focused verification result"},
        )

        capsule = agent_preflight.validate_v6_continuation(continuation)
        self.assertEqual(capsule["execution"]["cursor"], 1)
        with self.assertRaises(Exception):
            agent_preflight.validate_v6_continuation(
                {**continuation, "target": "/root/work_other_routine_g01"}
            )

    def test_compiler_output_round_trips_through_preflight(self) -> None:
        tool_input = native_input()
        capsule = agent_preflight.validate_dispatch(tool_input)
        self.assertEqual(capsule["protocol"], "cco.v6")

    def test_preflight_rejects_native_route_or_role_substitution(self) -> None:
        original = native_input()
        for field, value in (
            ("model", "gpt-5.6-sol"),
            ("reasoning_effort", "high"),
            ("agent_type", "cost_orchestrator_read_leaf"),
            ("task_name", "work_other_routine_r01"),
            ("fork_turns", "1"),
        ):
            with self.subTest(field=field), self.assertRaises(Exception):
                agent_preflight.validate_dispatch({**original, field: value})

    def test_review_uses_fresh_read_leaf(self) -> None:
        tool_input = native_input(kind="review", purpose="acceptance")
        capsule = agent_preflight.validate_dispatch(tool_input)
        self.assertEqual(tool_input["agent_type"], "cost_orchestrator_read_leaf")
        self.assertEqual(capsule["mode"], "fresh")
        self.assertEqual(tool_input["fork_turns"], "none")

    def test_old_v5_envelope_is_not_a_hidden_compatibility_path(self) -> None:
        with self.assertRaises((CapsuleError, Exception)):
            agent_preflight.validate_dispatch(
                {
                    "agent_type": "cost_orchestrator_write_leaf",
                    "fork_turns": "none",
                    "message": "CCO_WORK cco.v5\nNODE: n01",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "task_name": "work_n01_routine_r01",
                }
            )


if __name__ == "__main__":
    unittest.main()

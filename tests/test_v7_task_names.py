from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from packet_compiler import (  # noqa: E402
    CapsuleError,
    compile_continuation,
    compile_dispatch,
    normalize_capsule,
    parse_message,
)


SHA = "sha256:" + "1" * 64


def dispatch(*, role: str = "worker", node: str = "n01_manifest", model: str = "gpt-5.6-luna", effort: str = "max", epoch: str | None = None) -> dict[str, object]:
    acceptance = {"mode": "independent", "reasons": ["explicit_independent_review"]} if role == "reviewer" else {"mode": "primary", "reasons": []}
    spec: dict[str, object] = {
        "acceptance": acceptance,
        "acceptance_ids": ["A01"],
        "assurance": "guarded" if role == "reviewer" else "mechanical",
        "baseline": SHA,
        "contract": {"contract_rev": 1, "node": node, "objective": "bounded task"},
        "fork_turns": "none",
        "generation": 1,
        "graph_sha256": SHA,
        "mode": "fresh" if role == "reviewer" else "light",
        "node": node,
        "role": role,
        "route": {
            "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
            "decision_sha256": SHA,
            "plan_sha256": SHA,
            "rank": 1,
            "selected": {"effort": effort, "model": model},
        },
        "scopes": [{"kind": "exact", "path": "owned.txt"}],
    }
    if epoch is not None:
        spec["epoch"] = epoch
    return compile_dispatch(spec)


class V7TaskNameTests(unittest.TestCase):
    def test_route_and_effort_are_visible_for_each_logical_role(self) -> None:
        self.assertEqual(dispatch()["task_name"], "worker_n01_manifest_luna_max_g01")
        self.assertEqual(
            dispatch(role="explorer", model="gpt-5.6-terra")["task_name"],
            "explorer_n01_manifest_terra_max_g01",
        )
        self.assertEqual(
            dispatch(role="reviewer", model="gpt-5.6-terra", epoch="e01")["task_name"],
            "review_e01_n01_manifest_terra_max_g01",
        )

    def test_oversized_name_is_deterministically_compacted(self) -> None:
        node = "n" + "x" * 63
        first = dispatch(
            role="reviewer",
            node=node,
            model="vendor-" + "model" * 20,
            effort="maximum_effort_level",
            epoch="e" + "9" * 40,
        )["task_name"]
        second = dispatch(
            role="reviewer",
            node=node,
            model="vendor-" + "model" * 20,
            effort="maximum_effort_level",
            epoch="e" + "9" * 40,
        )["task_name"]
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 96)
        self.assertRegex(first, r"^review_.*_h[0-9a-f]{8}_.*_g01$")

    def test_fallback_changes_owner_but_continuation_keeps_it(self) -> None:
        luna = dispatch()
        terra = dispatch(model="gpt-5.6-terra")
        self.assertNotEqual(luna["task_name"], terra["task_name"])
        capsule = parse_message(luna["message"])
        continued = compile_continuation(
            capsule,
            target="/root/" + str(luna["task_name"]),
            delta={"new_evidence": "A01"},
        )
        self.assertEqual(
            parse_message(continued["message"])["execution"]["task_name"],
            luna["task_name"],
        )

    def test_forged_route_aware_name_is_rejected(self) -> None:
        capsule = parse_message(dispatch()["message"])
        capsule["execution"]["task_name"] = "worker_n01_manifest_terra_max_g01"
        with self.assertRaisesRegex(CapsuleError, "does not match"):
            normalize_capsule(capsule)


if __name__ == "__main__":
    unittest.main()

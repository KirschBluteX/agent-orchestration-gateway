from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from routing_catalog import RoutingCatalogError, resolve_route_plan  # noqa: E402


CATALOG = {
    "models": [
        {
            "multi_agent_version": "v2",
            "slug": model,
            "supported_reasoning_levels": [
                {"effort": "max"},
                {"effort": "xhigh"},
                {"effort": "high"},
            ],
        }
        for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
    ]
}


def request(role: str, assurance: str, constraints: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "assurance": assurance,
        "constraints": constraints
        or {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
        "node": "n01",
        "role": role,
    }


class RoutingTests(unittest.TestCase):
    def test_static_defaults_prefer_luna_mechanical_and_terra_bounded(self) -> None:
        mechanical = resolve_route_plan([request("worker", "mechanical")], CATALOG)
        bounded = resolve_route_plan([request("worker", "bounded")], CATALOG)
        reviewer = resolve_route_plan([request("reviewer", "guarded")], CATALOG)
        self.assertEqual(mechanical["routes"][0]["candidates"][0]["model"], "gpt-5.6-luna")
        self.assertEqual(bounded["routes"][0]["candidates"][0]["model"], "gpt-5.6-terra")
        self.assertEqual(
            reviewer["routes"][0]["candidates"],
            [
                {"effort": "max", "model": "gpt-5.6-terra"},
                {"effort": "xhigh", "model": "gpt-5.6-terra"},
                {"effort": "high", "model": "gpt-5.6-terra"},
            ],
        )

    def test_sol_requires_explicit_current_pin(self) -> None:
        automatic = resolve_route_plan([request("worker", "guarded")], CATALOG)
        self.assertNotIn("sol", automatic["routes"][0]["candidates"][0]["model"])
        pinned = resolve_route_plan(
            [
                request(
                    "worker",
                    "guarded",
                    {
                        "fixed_effort": "max",
                        "fixed_model": "gpt-5.6-sol",
                        "source": "user",
                    },
                )
            ],
            CATALOG,
        )
        self.assertEqual(pinned["routes"][0]["candidates"][0]["model"], "gpt-5.6-sol")

    def test_unsupported_exact_pin_fails_closed(self) -> None:
        with self.assertRaises(RoutingCatalogError):
            resolve_route_plan(
                [
                    request(
                        "worker",
                        "bounded",
                        {
                            "fixed_effort": "ultra",
                            "fixed_model": "gpt-5.6-terra",
                            "source": "user",
                        },
                    )
                ],
                CATALOG,
            )


if __name__ == "__main__":
    unittest.main()

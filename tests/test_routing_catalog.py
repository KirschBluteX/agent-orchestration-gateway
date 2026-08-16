from __future__ import annotations

from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from routing_catalog import (  # noqa: E402
    RoutingCatalogError,
    resolve_route_plan,
)


def catalog(*models: tuple[str, tuple[str, ...]]) -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": slug,
                "supported_reasoning_levels": [
                    {"effort": effort} for effort in efforts
                ],
            }
            for slug, efforts in models
        ]
    }


def request(
    *,
    assurance: str,
    role: str = "worker",
    constraints: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "assurance": assurance,
        "constraints": constraints
        or {
            "fixed_effort": None,
            "fixed_model": None,
            "source": "automatic",
        },
        "node": "node",
        "role": role,
    }


class RoutingCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = catalog(
            ("gpt-5.6-luna", ("max", "xhigh", "high")),
            ("gpt-5.6-terra", ("max", "xhigh", "high")),
            ("gpt-5.6-sol", ("ultra",)),
        )

    def test_mechanical_prefers_all_luna_efforts_before_terra(self) -> None:
        route = resolve_route_plan(
            [request(assurance="mechanical")], self.catalog
        )["routes"][0]

        self.assertEqual(
            route["candidates"],
            [
                {"effort": "max", "model": "gpt-5.6-luna"},
                {"effort": "xhigh", "model": "gpt-5.6-luna"},
                {"effort": "high", "model": "gpt-5.6-luna"},
                {"effort": "max", "model": "gpt-5.6-terra"},
                {"effort": "xhigh", "model": "gpt-5.6-terra"},
                {"effort": "high", "model": "gpt-5.6-terra"},
            ],
        )

    def test_v1_or_missing_native_metadata_offers_luna_first(self) -> None:
        for label, version in (("v1", "v1"), ("missing", None)):
            with self.subTest(version=label):
                host_catalog = catalog(
                    ("gpt-5.6-luna", ("max",)),
                    ("gpt-5.6-terra", ("max",)),
                )
                luna = host_catalog["models"][0]
                if version is None:
                    del luna["multi_agent_version"]
                else:
                    luna["multi_agent_version"] = version

                route = resolve_route_plan(
                    [request(assurance="mechanical")], host_catalog
                )["routes"][0]

                self.assertEqual(
                    route["candidates"],
                    [
                        {"effort": "max", "model": "gpt-5.6-luna"},
                        {"effort": "max", "model": "gpt-5.6-terra"},
                    ],
                )

    def test_explicitly_disabled_native_model_is_not_offered(self) -> None:
        host_catalog = catalog(
            ("gpt-5.6-luna", ("max",)),
            ("gpt-5.6-terra", ("max",)),
        )
        host_catalog["models"][0]["multi_agent_version"] = "disabled"

        route = resolve_route_plan(
            [request(assurance="mechanical")], host_catalog
        )["routes"][0]

        self.assertEqual(
            route["candidates"],
            [{"effort": "max", "model": "gpt-5.6-terra"}],
        )

    def test_picker_visibility_does_not_override_native_availability(self) -> None:
        cases = (
            {"show_in_picker": False},
            {"hidden": True},
            {"visibility": "hide"},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                host_catalog = catalog(
                    ("gpt-5.6-luna", ("max",)),
                    ("gpt-5.6-terra", ("max",)),
                )
                host_catalog["models"][0].update(metadata)

                route = resolve_route_plan(
                    [request(assurance="mechanical")], host_catalog
                )["routes"][0]

                self.assertEqual(
                    route["candidates"][0],
                    {"effort": "max", "model": "gpt-5.6-luna"},
                )

    def test_complex_routes_use_terra_without_an_implicit_luna_fallback(self) -> None:
        cases = [
            request(assurance="bounded"),
            request(assurance="guarded"),
            request(assurance="guarded", role="reviewer"),
        ]
        for item in cases:
            with self.subTest(role=item["role"], assurance=item["assurance"]):
                candidates = resolve_route_plan([item], self.catalog)["routes"][0][
                    "candidates"
                ]
                self.assertEqual(
                    candidates,
                    [
                        {"effort": "max", "model": "gpt-5.6-terra"},
                        {"effort": "xhigh", "model": "gpt-5.6-terra"},
                        {"effort": "high", "model": "gpt-5.6-terra"},
                    ],
                )
        luna_only = catalog(("gpt-5.6-luna", ("max",)))
        with self.assertRaisesRegex(RoutingCatalogError, "keep the node in Primary"):
            resolve_route_plan([request(assurance="bounded")], luna_only)

    def test_user_fixed_pin_is_host_validated_and_may_select_sol(self) -> None:
        route = resolve_route_plan(
            [
                request(
                    assurance="bounded",
                    constraints={
                        "fixed_effort": "ultra",
                        "fixed_model": "gpt-5.6-sol",
                        "source": "user",
                    },
                )
            ],
            self.catalog,
        )["routes"][0]

        self.assertEqual(
            route["candidates"],
            [{"effort": "ultra", "model": "gpt-5.6-sol"}],
        )

    def test_unsupported_exact_pin_fails_closed(self) -> None:
        with self.assertRaisesRegex(RoutingCatalogError, "not supported"):
            resolve_route_plan(
                [
                    request(
                        assurance="bounded",
                        constraints={
                            "fixed_effort": "ultra",
                            "fixed_model": "gpt-5.6-terra",
                            "source": "user",
                        },
                    )
                ],
                self.catalog,
            )

if __name__ == "__main__":
    unittest.main()

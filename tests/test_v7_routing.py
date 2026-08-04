from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from routing_catalog import (  # noqa: E402
    RoutingCatalogError,
    advance_route_plan,
    load_route_policy,
    native_capability_records,
    resolve_route_plan,
)


def native_catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-luna",
                "supported_reasoning_levels": [
                    {"effort": "max"},
                    {"effort": "xhigh"},
                ],
            },
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-terra",
                "supported_reasoning_levels": [
                    {"effort": "max"},
                    {"effort": "high"},
                ],
            },
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-sol",
                "supported_reasoning_levels": [{"effort": "max"}],
            },
        ]
    }


class V7RoutingTests(unittest.TestCase):
    def test_plan_is_bound_by_node_role_and_assurance(self) -> None:
        plan = resolve_route_plan(
            [
                {
                    "assurance": "mechanical",
                    "constraints": {
                        "fixed_effort": None,
                        "fixed_model": None,
                        "source": "automatic",
                    },
                    "node": "n01_worker",
                    "role": "worker",
                },
                {
                    "assurance": "bounded",
                    "constraints": {
                        "fixed_effort": None,
                        "fixed_model": None,
                        "source": "automatic",
                    },
                    "node": "n02_explorer",
                    "role": "explorer",
                },
            ],
            native_catalog(),
        )

        self.assertEqual(plan["protocol"], "cco.route-plan.v5")
        self.assertEqual(
            [
                (route["node"], route["role"], route["assurance"])
                for route in plan["routes"]
            ],
            [
                ("n01_worker", "worker", "mechanical"),
                ("n02_explorer", "explorer", "bounded"),
            ],
        )
        self.assertEqual(
            [route["selected"]["model"] for route in plan["routes"]],
            ["gpt-5.6-luna", "gpt-5.6-terra"],
        )
        self.assertNotIn("purpose", plan["routes"][0])
        self.assertNotIn("judgment", plan["routes"][0])

    def test_builtin_route_adapts_effort_but_an_exact_user_pin_never_falls_back(self) -> None:
        catalog = {
            "models": [
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "xhigh"}],
                },
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [{"effort": "max"}],
                },
            ]
        }
        automatic = {
            "assurance": "mechanical",
            "constraints": {
                "fixed_effort": None,
                "fixed_model": None,
                "source": "automatic",
            },
            "node": "n01_worker",
            "role": "worker",
        }
        plan = resolve_route_plan([automatic], catalog)
        self.assertEqual(
            plan["routes"][0]["selected"],
            {"effort": "xhigh", "model": "gpt-5.6-luna"},
        )

        exact_pin = {
            **automatic,
            "constraints": {
                "fixed_effort": "max",
                "fixed_model": "gpt-5.6-luna",
                "source": "user",
            },
        }
        with self.assertRaisesRegex(RoutingCatalogError, "not supported"):
            resolve_route_plan([exact_pin], catalog)

    def test_nonstandard_effort_requires_a_current_user_pin(self) -> None:
        catalog = {
            "models": [
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                },
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [{"effort": "high"}],
                },
            ]
        }
        automatic = {
            "assurance": "mechanical",
            "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
            "node": "n01_worker",
            "role": "worker",
        }
        self.assertEqual(
            resolve_route_plan([automatic], catalog)["routes"][0]["selected"],
            {"effort": "high", "model": "gpt-5.6-terra"},
        )
        pinned = {
            **automatic,
            "constraints": {
                "fixed_effort": "ultra",
                "fixed_model": "gpt-5.6-luna",
                "source": "user",
            },
        }
        self.assertEqual(
            resolve_route_plan([pinned], catalog)["routes"][0]["selected"],
            {"effort": "ultra", "model": "gpt-5.6-luna"},
        )

    def test_sol_requires_a_current_user_pin_and_static_fallback_exhausts(self) -> None:
        automatic = {
            "assurance": "mechanical",
            "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
            "node": "n01_worker",
            "role": "worker",
        }
        plan = resolve_route_plan([automatic], native_catalog())
        self.assertNotIn("sol", " ".join(item["model"] for item in plan["routes"][0]["candidates"]))
        advanced = advance_route_plan(
            plan,
            node="n01_worker",
            rejected_model="gpt-5.6-luna",
            rejected_effort="max",
            rejection_ticket="native:rejected-r01",
        )
        self.assertEqual(advanced["routes"][0]["selected"]["model"], "gpt-5.6-terra")
        with self.assertRaisesRegex(RoutingCatalogError, "exhausted"):
            advance_route_plan(
                advanced,
                node="n01_worker",
                rejected_model="gpt-5.6-terra",
                rejected_effort="max",
                rejection_ticket="native:rejected-r02",
            )

        pinned = resolve_route_plan(
            [
                {
                    **automatic,
                    "constraints": {
                        "fixed_effort": "max",
                        "fixed_model": "gpt-5.6-sol",
                        "source": "user",
                    },
                }
            ],
            native_catalog(),
        )
        self.assertEqual(pinned["routes"][0]["selected"]["model"], "gpt-5.6-sol")
        self.assertEqual(len(pinned["routes"][0]["candidates"]), 1)

    def test_native_catalog_requires_explicit_supported_agent_metadata(self) -> None:
        records = native_capability_records(
            {
                "models": [
                    {
                        "multi_agent_version": "v1",
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    },
                    {
                        "multi_agent_version": "future",
                        "slug": "gpt-5.6-sol",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    },
                    {
                        "slug": "gpt-5.6-terra",
                        "supported_reasoning_levels": [{"effort": "max"}],
                    },
                ]
            }
        )
        self.assertEqual(records, [{"effort": "max", "model": "gpt-5.6-luna"}])

    def test_trusted_project_policy_overrides_global_policy_without_silent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            repo = root / "repo"
            (repo / ".codex").mkdir(parents=True)
            home.mkdir()
            home_config = (
                f'trusted_project_roots = ["{repo.as_posix()}"]\n\n'
                "[routes.worker.mechanical]\n"
                'candidates = [{ model = "gpt-5.6-luna", effort = "max" }]\n'
            )
            (home / "cco.toml").write_text(home_config, encoding="utf-8")
            (repo / ".codex" / "cco.toml").write_text(
                "[routes.worker.mechanical]\n"
                'candidates = [{ model = "gpt-5.6-terra", effort = "max" }]\n',
                encoding="utf-8",
            )

            loaded = load_route_policy(repo, codex_home=home)
            self.assertTrue(loaded["project_trusted"])
            self.assertEqual(
                loaded["policy"]["worker"]["mechanical"]["candidates"],
                [{"effort": "max", "model": "gpt-5.6-terra"}],
            )

            (repo / ".codex" / "cco.toml").write_text(
                "[routes.worker.mechanical]\n"
                'candidates = [{ model = "gpt-5.6-sol", effort = "max" }]\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RoutingCatalogError, "cannot include Sol"):
                load_route_policy(repo, codex_home=home)

    def test_configured_exact_pairs_preserve_order_even_for_the_same_model(self) -> None:
        request = {
            "assurance": "mechanical",
            "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
            "node": "n01_worker",
            "role": "worker",
        }
        policy = {
            "worker": {
                "mechanical": {
                    "candidates": [
                        {"model": "gpt-5.6-luna", "effort": "xhigh"},
                        {"model": "gpt-5.6-luna", "effort": "max"},
                        {"model": "gpt-5.6-terra", "effort": "high"},
                    ]
                }
            }
        }
        plan = resolve_route_plan([request], native_catalog(), policy=policy)
        self.assertEqual(
            plan["routes"][0]["candidates"],
            [
                {"effort": "xhigh", "model": "gpt-5.6-luna"},
                {"effort": "max", "model": "gpt-5.6-luna"},
                {"effort": "high", "model": "gpt-5.6-terra"},
            ],
        )

    def test_automatic_policy_cannot_configure_a_nonstandard_effort(self) -> None:
        request = {
            "assurance": "mechanical",
            "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
            "node": "n01_worker",
            "role": "worker",
        }
        policy = {
            "worker": {
                "mechanical": {
                    "candidates": [{"model": "gpt-5.6-luna", "effort": "ultra"}]
                }
            }
        }
        catalog = {
            "models": [
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                }
            ]
        }
        with self.assertRaisesRegex(RoutingCatalogError, "automatic configuration.*effort"):
            resolve_route_plan([request], catalog, policy=policy)


if __name__ == "__main__":
    unittest.main()

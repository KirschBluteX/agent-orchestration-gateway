from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routing_catalog as routing_module  # noqa: E402

from routing_catalog import (  # noqa: E402
    RADAR_URL,
    STATE_FILENAME,
    FetchResult,
    RoutingCatalogError,
    advance_route,
    advance_route_plan,
    fetch_radar,
    load_radar_snapshot,
    load_native_catalog,
    normalize_snapshot,
    read_routing_state,
    render_resolution,
    resolve_graph_route,
    resolve_graph_route_plan,
    resolve_route,
    resolve_route_plan,
    route_plan_sha256,
    routing_lock,
    stabilize_route,
    validate_route_plan,
    validate_route_decision,
    write_json_atomic,
)


def point(
    model: str,
    effort: str,
    *,
    passed: int,
    valid_tasks: int = 100,
    cost: float = 1.0,
    minutes: float = 20.0,
) -> dict[str, object]:
    return {
        "model": model,
        "effort": effort,
        "iq": passed / valid_tasks * 150,
        "passed": passed,
        "valid_tasks": valid_tasks,
        "average_price_usd": cost,
        "price_samples": valid_tasks,
        "average_minutes": minutes,
        "duration_samples": valid_tasks,
        "incomplete_cost_samples": 0,
        "total_runs": valid_tasks * 3,
        "latest_graded_at": "2026-08-03T09:00:00+00:00",
    }


def radar_payload(*points: dict[str, object]) -> dict[str, object]:
    return {
        "schema": 2,
        "type": "distributed_intelligence_efficiency",
        "source": "https://api.codexradar.com/api/v1/table",
        "metrics_source": "https://api.codexradar.com/api/v1/model-metrics",
        "source_updated_at": "2026-08-03T09:00:00+00:00",
        "models": len({item["model"] for item in points}),
        "points": list(points),
        "history": {},
        "fingerprint": "a" * 64,
        "method": {"iq": "latest valid result per task; pass_rate * 150"},
    }


def native_catalog(*pairs: tuple[str, str]) -> dict[str, object]:
    efforts: dict[str, list[str]] = {}
    for model, effort in pairs:
        efforts.setdefault(model, []).append(effort)
    return {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [
                    {"effort": effort} for effort in model_efforts
                ],
            }
            for model, model_efforts in efforts.items()
        ]
    }


class RoutingCatalogBehaviorTests(unittest.TestCase):
    def test_native_capabilities_accept_explicit_multi_agent_backend_versions(self) -> None:
        catalog = {
            "models": [
                {
                    "multi_agent_version": "v1",
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "max"}],
                },
                {
                    "multi_agent_version": "v2",
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [{"effort": "max"}],
                },
                {
                    "multi_agent_version": None,
                    "slug": "gpt-5.5",
                    "supported_reasoning_levels": [{"effort": "xhigh"}],
                },
            ]
        }

        self.assertEqual(
            routing_module.native_capability_records(catalog),
            [
                {"effort": "max", "model": "gpt-5.6-luna"},
                {"effort": "max", "model": "gpt-5.6-terra"},
            ],
        )

    def test_loaded_normalized_snapshot_can_be_resolved_end_to_end(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=77, cost=0.5, minutes=30.0)
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = load_radar_snapshot(
                Path(temp_dir),
                now=now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            decision = resolve_route(
                loaded.snapshot,
                native_catalog(("gpt-5.6-luna", "max")),
                "routine",
                now=now,
            )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-luna")

    def test_guarded_routes_exclude_luna_without_blocking_deterministic_work(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=74, cost=0.5, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=72, cost=4.0, minutes=30.0),
        )
        catalog = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        deterministic = resolve_route(
            radar,
            catalog,
            purpose="implementation",
            judgment="complex",
            assurance="deterministic",
            now=now,
        )
        guarded = resolve_route(
            radar,
            catalog,
            purpose="implementation",
            judgment="complex",
            assurance="guarded",
            now=now,
        )
        user_fixed = resolve_route(
            {},
            catalog,
            purpose="implementation",
            judgment="complex",
            assurance="guarded",
            fixed_model="gpt-5.6-luna",
            fixed_effort="max",
            now=now,
        )

        self.assertEqual(deterministic["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(guarded["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(user_fixed["selected"]["model"], "gpt-5.6-luna")

    def test_complex_luna_requires_wilson_lower_bound_above_iq_floor(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=70, cost=20.0, minutes=60.0),
        )
        catalog = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        routine = resolve_route(
            radar,
            catalog,
            purpose="implementation",
            judgment="routine",
            assurance="deterministic",
            now=now,
        )
        complex_route = resolve_route(
            radar,
            catalog,
            purpose="implementation",
            judgment="complex",
            assurance="deterministic",
            now=now,
        )

        self.assertEqual(routine["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(complex_route["selected"]["model"], "gpt-5.6-terra")

    def test_validator_rejects_low_confidence_complex_luna_without_user_pin(self) -> None:
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        decision, _state = stabilize_route(
            resolve_route(
                radar_payload(
                    point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=20.0)
                ),
                native_catalog(("gpt-5.6-luna", "max")),
                purpose="implementation",
                judgment="complex",
                assurance="deterministic",
                fixed_model="gpt-5.6-luna",
                now=now,
            ),
            None,
        )
        self.assertEqual(validate_route_decision(decision, now=now), decision)

        tampered = json.loads(json.dumps(decision))
        tampered["constraints"]["fixed_model"] = None
        context = tampered["placement_context"]
        tampered["policy_sha256"] = routing_module.policy_sha256(
            "complex",
            tampered["policy"],
            assurance="deterministic",
            purpose="implementation",
            fixed_model=None,
            fixed_effort=None,
            primary_model=context["primary_model"],
            primary_effort=context["primary_effort"],
        )
        tampered["decision_sha256"] = routing_module.route_decision_sha256(tampered)

        with self.assertRaisesRegex(RoutingCatalogError, "low-confidence Luna"):
            validate_route_decision(tampered, now=now)

    def test_route_plan_batches_multiple_purpose_judgment_keys_in_order(self) -> None:
        plan = resolve_route_plan(
            [
                {
                    "assurance": "deterministic",
                    "purpose": "implementation",
                    "judgment": "routine",
                },
                {
                    "assurance": "deterministic",
                    "purpose": "acceptance",
                    "judgment": "complex",
                },
            ],
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=74, cost=4.0, minutes=28.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(plan["protocol"], "cco.route-plan.v2")
        self.assertFalse(plan["needs_refresh"])
        self.assertEqual(
            [(route["purpose"], route["judgment"]) for route in plan["routes"]],
            [("acceptance", "complex"), ("implementation", "routine")],
        )
        self.assertTrue(all(len(route["candidates"]) <= 3 for route in plan["routes"]))

    def test_route_plan_separates_deterministic_and_guarded_routes(self) -> None:
        requests = [
            {
                "assurance": "deterministic",
                "judgment": "complex",
                "placement_benefits": [
                    {"evidence": ["contract:A01"], "kind": "closed_execution"}
                ],
                "purpose": "implementation",
            },
            {
                "assurance": "guarded",
                "judgment": "complex",
                "placement_benefits": [
                    {"evidence": ["contract:A02"], "kind": "closed_execution"}
                ],
                "purpose": "implementation",
            },
        ]
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=74, cost=0.5, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=72, cost=4.0, minutes=30.0),
        )

        plan = resolve_route_plan(
            requests,
            radar,
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [
                (route["assurance"], route["selected"]["model"])
                for route in plan["routes"]
            ],
            [
                ("deterministic", "gpt-5.6-luna"),
                ("guarded", "gpt-5.6-terra"),
            ],
        )
        tampered = json.loads(json.dumps(plan))
        guarded_route = next(
            route for route in tampered["routes"] if route["assurance"] == "guarded"
        )
        guarded_route["candidates"][0]["model"] = "gpt-5.6-luna"
        guarded_route["selected"]["model"] = "gpt-5.6-luna"
        tampered["plan_sha256"] = route_plan_sha256(tampered)
        with self.assertRaisesRegex(RoutingCatalogError, "guarded"):
            validate_route_plan(tampered)

    def test_route_plan_requires_explicit_derived_assurance(self) -> None:
        with self.assertRaisesRegex(RoutingCatalogError, "assurance"):
            resolve_route_plan(
                [{"purpose": "implementation", "judgment": "routine"}],
                radar_payload(
                    point(
                        "gpt-5.6-luna",
                        "max",
                        passed=74,
                        cost=0.5,
                        minutes=20.0,
                    )
                ),
                native_catalog(("gpt-5.6-luna", "max")),
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            )

    def test_route_plan_validator_requires_hash_and_active_candidate_consistency(self) -> None:
        plan = resolve_route_plan(
            [
                {
                    "assurance": "deterministic",
                    "purpose": "implementation",
                    "judgment": "routine",
                }
            ],
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=74, cost=4.0, minutes=28.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(validate_route_plan(plan), plan)
        self.assertEqual(route_plan_sha256(plan), plan["plan_sha256"])

        hash_tampered = json.loads(json.dumps(plan))
        hash_tampered["needs_refresh"] = True
        with self.assertRaises(RoutingCatalogError):
            validate_route_plan(hash_tampered)

        semantic_tampered = json.loads(json.dumps(plan))
        active = semantic_tampered["routes"][0]
        active["selected"] = {
            "effort": active["selected"]["effort"],
            "model": "gpt-5.6-sol",
        }
        semantic_tampered["plan_sha256"] = route_plan_sha256(semantic_tampered)
        with self.assertRaises(RoutingCatalogError):
            validate_route_plan(semantic_tampered)

        invented_reason = json.loads(json.dumps(plan))
        invented_reason["routes"][0]["placement"]["reason"] = "looks_cheap"
        invented_reason["plan_sha256"] = route_plan_sha256(invented_reason)
        with self.assertRaises(RoutingCatalogError):
            validate_route_plan(invented_reason)

    def test_route_plan_validator_rejects_noncanonical_route_order(self) -> None:
        plan = resolve_route_plan(
            [
                {
                    "assurance": "deterministic",
                    "purpose": "implementation",
                    "judgment": "routine",
                },
                {
                    "assurance": "deterministic",
                    "purpose": "acceptance",
                    "judgment": "complex",
                },
            ],
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=74, cost=4.0, minutes=28.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        reordered = json.loads(json.dumps(plan))
        reordered["routes"].reverse()
        reordered["plan_sha256"] = route_plan_sha256(reordered)

        with self.assertRaises(RoutingCatalogError):
            validate_route_plan(reordered)

    def test_route_plan_advance_only_changes_rank_selected_and_ticket(self) -> None:
        plan = resolve_route_plan(
            [
                {
                    "assurance": "deterministic",
                    "purpose": "implementation",
                    "judgment": "complex",
                }
            ],
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=73, cost=4.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        active = plan["routes"][0]["selected"]

        advanced = advance_route_plan(
            plan,
            purpose="implementation",
            judgment="complex",
            rejected_model=active["model"],
            rejected_effort=active["effort"],
            rejection_ticket="native:unsupported-model-effort",
        )

        before = dict(plan["routes"][0])
        after = dict(advanced["routes"][0])
        for field in ("decision_sha256", "candidates", "placement"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["dispatch"]["rank"], 2)
        self.assertEqual(
            after["dispatch"]["rejection_tickets"],
            ["native:unsupported-model-effort"],
        )

    def test_requires_iq_strictly_above_90_and_native_capability(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=60, cost=0.4),
                point("gpt-5.6-terra", "max", passed=61, cost=4.0),
                point("deepseek-v4-flash", "max", passed=66, cost=0.1),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            decision["selected"],
            {
                "model": "gpt-5.6-terra",
                "effort": "max",
                "iq": 91.5,
                "average_price_usd": 4.0,
                "average_minutes": 20.0,
            },
        )
        self.assertEqual(decision["eligible_count"], 1)

    def test_zero_pass_candidate_does_not_invalidate_the_whole_snapshot(self) -> None:
        snapshot = normalize_snapshot(
            radar_payload(
                point("new-model", "low", passed=0, cost=0.2, minutes=10.0),
                point("gpt-5.6-luna", "max", passed=70, cost=0.5, minutes=30.0),
            ),
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot["points"][0]["passed"], 70)
        self.assertEqual(snapshot["points"][1]["passed"], 0)

    def test_untrusted_numeric_and_identifier_extremes_fail_as_catalog_errors(self) -> None:
        oversized = point("gpt-5.6-luna", "max", passed=70)
        oversized["valid_tasks"] = 10**10000
        invalid_name = point("bad\nmodel", "max", passed=70)

        for candidate in (oversized, invalid_name):
            with self.subTest(model=candidate["model"]):
                with self.assertRaises(RoutingCatalogError):
                    normalize_snapshot(
                        radar_payload(candidate),
                        now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                    )

    def test_incomplete_metric_coverage_cannot_win_on_a_biased_low_mean(self) -> None:
        incomplete = point(
            "incomplete-model",
            "max",
            passed=80,
            cost=0.01,
            minutes=10.0,
        )
        incomplete["price_samples"] = 30
        incomplete["incomplete_cost_samples"] = 70
        decision = resolve_route(
            radar_payload(
                incomplete,
                point("complete-model", "max", passed=75, cost=1.0, minutes=20.0),
            ),
            native_catalog(
                ("incomplete-model", "max"),
                ("complete-model", "max"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "complete-model")
        self.assertEqual(decision["eligible_count"], 1)

    def test_lane_policy_selects_the_pareto_net_value_knee(self) -> None:
        radar = radar_payload(
            point(
                "gpt-5.6-luna",
                "max",
                passed=80,
                valid_tasks=112,
                cost=0.457763,
                minutes=31.8505,
            ),
            point(
                "gpt-5.6-terra",
                "max",
                passed=71,
                valid_tasks=112,
                cost=4.021433,
                minutes=31.4889,
            ),
            point(
                "gpt-5.5",
                "xhigh",
                passed=75,
                valid_tasks=112,
                cost=5.758608,
                minutes=23.5683,
            ),
            point(
                "gpt-5.6-sol",
                "xhigh",
                passed=78,
                valid_tasks=112,
                cost=6.289043,
                minutes=25.5226,
            ),
            point(
                "gpt-5.6-sol",
                "max",
                passed=80,
                valid_tasks=112,
                cost=9.493976,
                minutes=35.2171,
            ),
            point(
                "gpt-5.6-sol",
                "ultra",
                passed=80,
                valid_tasks=112,
                cost=20.42426,
                minutes=51.7758,
            ),
        )
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
            ("gpt-5.5", "xhigh"),
            ("gpt-5.6-sol", "xhigh"),
            ("gpt-5.6-sol", "max"),
            ("gpt-5.6-sol", "ultra"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        routine = resolve_route(radar, native, "routine", now=now)
        complex_route = resolve_route(radar, native, "complex", now=now)

        self.assertEqual(
            (routine["selected"]["model"], routine["selected"]["effort"]),
            ("gpt-5.6-luna", "max"),
        )
        self.assertEqual(
            (
                complex_route["selected"]["model"],
                complex_route["selected"]["effort"],
            ),
            ("gpt-5.6-luna", "max"),
        )
        self.assertEqual(
            complex_route["selection_method"], "strict_pareto_fixed_anchor_mcda"
        )
        self.assertNotIn(
            "gpt-5.6-sol",
            [
                candidate["model"]
                for candidate in complex_route["pareto_frontier"]
            ],
        )

    def test_equal_observed_value_prefers_the_better_supported_estimate(self) -> None:
        decision = resolve_route(
            radar_payload(
                point(
                    "gpt-5.6-luna",
                    "max",
                    passed=65,
                    valid_tasks=100,
                    cost=1.0,
                    minutes=20.0,
                ),
                point(
                    "gpt-5.6-terra",
                    "max",
                    passed=52,
                    valid_tasks=80,
                    cost=1.0,
                    minutes=20.0,
                ),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-luna")
        estimates = {
            item["model"]: item["iq_standard_error"]
            for item in decision["eligible_candidates"]
        }
        self.assertLess(estimates["gpt-5.6-luna"], estimates["gpt-5.6-terra"])

    def test_small_resource_premium_can_buy_a_large_quality_gain(self) -> None:
        decision = resolve_route(
            radar_payload(
                point(
                    "gpt-5.6-luna",
                    "max",
                    passed=62,
                    cost=1.0,
                    minutes=20.0,
                ),
                point(
                    "gpt-5.6-sol",
                    "xhigh",
                    passed=72,
                    cost=1.2,
                    minutes=21.0,
                ),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-luna")

    def test_sol_cannot_auto_win_without_non_overlapping_wilson_iq_advantage(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-terra", "max", passed=74, cost=4.0, minutes=30.0),
                point("gpt-5.6-sol", "max", passed=75, cost=1.0, minutes=20.0),
            ),
            native_catalog(
                ("gpt-5.6-terra", "max"),
                ("gpt-5.6-sol", "max"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-terra")

    def test_sol_can_auto_win_with_strict_wilson_iq_advantage(self) -> None:
        decision = resolve_route(
            radar_payload(
                point(
                    "gpt-5.6-terra",
                    "max",
                    passed=650,
                    valid_tasks=1000,
                    cost=4.0,
                    minutes=30.0,
                ),
                point(
                    "gpt-5.6-sol",
                    "max",
                    passed=850,
                    valid_tasks=1000,
                    cost=5.0,
                    minutes=30.0,
                ),
            ),
            native_catalog(
                ("gpt-5.6-terra", "max"),
                ("gpt-5.6-sol", "max"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-sol")

    def test_cost_and_time_penalties_remain_monotonic_above_anchors(self) -> None:
        decision = resolve_route(
            radar_payload(
                point(
                    "gpt-5.6-luna",
                    "max",
                    passed=70,
                    cost=25.0,
                    minutes=60.0,
                ),
                point(
                    "gpt-5.6-sol",
                    "xhigh",
                    passed=71,
                    cost=2500.0,
                    minutes=600.0,
                ),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-luna")

    def test_strict_pareto_frontier_cannot_be_emptied_by_epsilon_cycles(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("model-a", "high", passed=67, cost=1.02, minutes=11.1),
                point("model-b", "high", passed=66, cost=1.00, minutes=10.2),
                point("model-c", "high", passed=67, cost=1.06, minutes=10.0),
            ),
            native_catalog(
                ("model-a", "high"),
                ("model-b", "high"),
                ("model-c", "high"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertGreaterEqual(len(decision["pareto_frontier"]), 1)

    def test_small_all_success_sample_has_nonzero_uncertainty(self) -> None:
        decision = resolve_route(
            radar_payload(
                point(
                    "gpt-5.6-luna",
                    "max",
                    passed=30,
                    valid_tasks=30,
                    cost=1.0,
                    minutes=20.0,
                )
            ),
            native_catalog(("gpt-5.6-luna", "max")),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertGreater(
            decision["eligible_candidates"][0]["iq_uncertainty_95"], 0
        )

    def test_cache_uses_one_hour_ttl_and_keeps_only_normalized_lkg(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        calls: list[str | None] = []

        def fetch(etag: str | None) -> FetchResult:
            calls.append(etag)
            return FetchResult(status=200, payload=radar, etag='"radar-v1"')

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            orphan = cache_dir / "radar-lkg-v1.abandoned.tmp"
            orphan.write_text("garbage", encoding="utf-8")
            stale_timestamp = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc).timestamp()
            os.utime(orphan, (stale_timestamp, stale_timestamp))
            first = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=fetch,
            )
            second = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 59, tzinfo=timezone.utc),
                fetcher=fetch,
            )
            third = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 11, 0, 1, tzinfo=timezone.utc),
                fetcher=fetch,
                force_refresh=True,
            )

            self.assertEqual(calls, [None, '"radar-v1"'])
            self.assertEqual(
                first.snapshot["snapshot_sha256"],
                second.snapshot["snapshot_sha256"],
            )
            self.assertEqual(
                second.snapshot["snapshot_sha256"],
                third.snapshot["snapshot_sha256"],
            )
            self.assertEqual(first.status, "refreshed")
            self.assertEqual(second.status, "fresh_cache")
            self.assertEqual(third.status, "refreshed")
            self.assertFalse(orphan.exists())
            self.assertEqual(
                sorted(path.name for path in cache_dir.iterdir()),
                ["radar-lkg-v1.json"],
            )
            stored = json.loads(
                (cache_dir / "radar-lkg-v1.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("history", stored["snapshot"])
            self.assertLess(
                (cache_dir / "radar-lkg-v1.json").stat().st_size,
                128 * 1024,
            )

    def test_expired_but_bounded_lkg_returns_immediately_and_requests_refresh(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        calls: list[str | None] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda etag: (
                    calls.append(etag)
                    or FetchResult(status=200, payload=radar, etag='"radar-v1"')
                ),
            )
            stale = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail(
                    "stale-while-revalidate must not fetch on the dispatch path"
                ),
            )

        self.assertEqual(calls, [None])
        self.assertFalse(first.needs_refresh)
        self.assertTrue(stale.needs_refresh)
        self.assertEqual(stale.status, "last_known_good")

    def test_fresh_foreign_temporary_is_not_deleted_by_another_loader(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            active = cache_dir / "radar-lkg-v1.other-process.tmp"
            active.write_text("still active", encoding="utf-8")

            load_radar_snapshot(
                cache_dir,
                now=datetime.now(timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )

            self.assertTrue(active.exists())

    def test_failed_atomic_replace_removes_its_own_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            with mock.patch(
                "routing_catalog.os.replace", side_effect=OSError("injected")
            ):
                with self.assertRaises(RoutingCatalogError):
                    write_json_atomic(
                        directory / "routing-state-v2.json",
                        {"protocol": "test"},
                        prefix="routing-state-v2.",
                    )

            self.assertEqual(list(directory.glob("routing-state-v2.*.tmp")), [])

    def test_short_lived_lock_blocks_overlap_and_is_removed_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            now = datetime.now(timezone.utc)
            with routing_lock(cache_dir, now=now):
                with self.assertRaises(RoutingCatalogError):
                    with routing_lock(cache_dir, now=now, wait_seconds=0):
                        self.fail("overlapping lock unexpectedly acquired")
            self.assertFalse((cache_dir / "routing-v1.lock").exists())

    def test_routing_lock_is_non_blocking_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            now = datetime.now(timezone.utc)
            with routing_lock(cache_dir, now=now):
                with mock.patch(
                    "routing_catalog.time.sleep",
                    side_effect=AssertionError("routing lock must never sleep"),
                ):
                    with self.assertRaises(RoutingCatalogError):
                        with routing_lock(cache_dir, now=now):
                            self.fail("overlapping lock unexpectedly acquired")

    def test_refresh_failure_uses_bounded_last_known_good(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )

            def unavailable(_etag: str | None) -> FetchResult:
                raise OSError("network unavailable")

            fallback = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=unavailable,
                force_refresh=True,
            )

            self.assertEqual(fallback.status, "last_known_good")
            self.assertEqual(
                fallback.snapshot["snapshot_sha256"],
                first.snapshot["snapshot_sha256"],
            )

    def test_304_cannot_extend_a_source_past_72_hours(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        first_now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            load_radar_snapshot(
                cache_dir,
                now=first_now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )

            with self.assertRaises(RoutingCatalogError):
                load_radar_snapshot(
                    cache_dir,
                    now=first_now + timedelta(hours=73),
                    fetcher=lambda _etag: FetchResult(
                        status=304, payload=None, etag='"radar-v1"'
                    ),
                    force_refresh=True,
                )

    def test_older_200_response_does_not_replace_a_newer_lkg(self) -> None:
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        newer = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        newer["source_updated_at"] = "2026-08-03T11:00:00+00:00"
        older = radar_payload(
            point("gpt-5.6-luna", "max", passed=65, cost=0.4, minutes=29.0)
        )
        older["source_updated_at"] = "2026-08-03T10:00:00+00:00"

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first = load_radar_snapshot(
                cache_dir,
                now=now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=newer, etag='"newer"'
                ),
            )
            fallback = load_radar_snapshot(
                cache_dir,
                now=now + timedelta(hours=2),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=older, etag='"older"'
                ),
                force_refresh=True,
            )

        self.assertEqual(fallback.status, "last_known_good")
        self.assertEqual(
            fallback.snapshot["snapshot_sha256"], first.snapshot["snapshot_sha256"]
        )

    def test_future_cache_timestamp_is_discarded_before_routing(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            load_radar_snapshot(
                cache_dir,
                now=now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            cache_file = cache_dir / "radar-lkg-v1.json"
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            cache["fetched_at"] = (now + timedelta(hours=2)).isoformat()
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
            observed: list[str | None] = []

            refreshed = load_radar_snapshot(
                cache_dir,
                now=now,
                fetcher=lambda etag: (
                    observed.append(etag)
                    or FetchResult(status=200, payload=radar, etag='"radar-v2"')
                ),
            )

            self.assertEqual(observed, [None])
            self.assertEqual(refreshed.status, "refreshed")

    def test_fetch_uses_the_canonical_apex_endpoint_and_conditional_etag(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        )
        body = json.dumps(radar).encode("utf-8")
        observed: dict[str, object] = {}

        class Response:
            status = 200
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "ETag": '"radar-v2"',
            }

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def geturl(self) -> str:
                return RADAR_URL

            def read(self, _size: int = -1) -> bytes:
                return body

        def open_request(request: object, *, timeout: float) -> Response:
            observed["url"] = request.full_url  # type: ignore[attr-defined]
            observed["etag"] = request.get_header("If-none-match")  # type: ignore[attr-defined]
            observed["user_agent"] = request.get_header("User-agent")  # type: ignore[attr-defined]
            observed["timeout"] = timeout
            return Response()

        result = fetch_radar('"radar-v1"', opener=open_request)

        self.assertEqual(observed["url"], RADAR_URL)
        self.assertEqual(observed["etag"], '"radar-v1"')
        self.assertEqual(
            observed["user_agent"],
            "codex-cost-orchestrator/0.8.0 routing-catalog",
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.etag, '"radar-v2"')
        self.assertEqual(result.payload, radar)

    def test_lane_preferences_are_explicit_and_user_overridable(self) -> None:
        radar = radar_payload(
            point(
                "gpt-5.6-luna",
                "max",
                passed=72,
                valid_tasks=112,
                cost=0.457763,
                minutes=31.8505,
            ),
            point(
                "gpt-5.6-sol",
                "xhigh",
                passed=78,
                valid_tasks=112,
                cost=6.289043,
                minutes=25.5226,
            ),
        )
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-sol", "xhigh"),
        )

        decision = resolve_route(
            radar,
            native,
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            policy_overrides={
                "quality_weight": 0.70,
                "cost_weight": 0.20,
                "time_weight": 0.10,
            },
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(decision["policy"]["quality_weight"], 0.70)

    def test_one_user_fixed_dimension_constrains_the_adaptive_choice(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
                point("gpt-5.6-sol", "high", passed=72, cost=3.0, minutes=20.0),
                point("gpt-5.6-terra", "max", passed=73, cost=4.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "high"),
                ("gpt-5.6-terra", "max"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            fixed_model="gpt-5.6-sol",
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-sol")
        self.assertEqual(decision["constraints"]["fixed_model"], "gpt-5.6-sol")

    def test_fully_fixed_user_pair_skips_radar_and_only_checks_native_capability(self) -> None:
        decision = resolve_route(
            {"this": "is intentionally not a Radar payload"},
            native_catalog(("gpt-5.6-sol", "max")),
            "routine",
            fixed_model="gpt-5.6-sol",
            fixed_effort="max",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-sol", "max"),
        )
        self.assertEqual(decision["selection_method"], "fixed_user_pair_native_capability")

    def test_native_catalog_identity_is_bound_to_the_decision(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        luna_only = resolve_route(
            radar,
            native_catalog(("gpt-5.6-luna", "max")),
            "routine",
            now=now,
        )
        both = resolve_route(
            radar,
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
            ),
            "routine",
            now=now,
        )

        self.assertNotEqual(
            luna_only["native_catalog_sha256"], both["native_catalog_sha256"]
        )
        self.assertNotEqual(luna_only["decision_sha256"], both["decision_sha256"])

    def test_native_catalog_is_cached_by_codex_version_and_content_fingerprint(self) -> None:
        payload = json.dumps(
            native_catalog(("gpt-5.6-luna", "max"))
        ).encode("utf-8")
        calls: list[tuple[str, ...]] = []

        class Result:
            returncode = 0
            stderr = b""

            def __init__(self, stdout: bytes) -> None:
                self.stdout = stdout

        def runner(command: list[str], **_kwargs: object) -> Result:
            calls.append(tuple(command[1:]))
            if command[1:] == ["--version"]:
                return Result(b"codex-cli 1.2.3\n")
            return Result(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            executable = directory / "codex-test"
            executable.write_bytes(b"codex-build-one")
            first = load_native_catalog(
                directory, executable=executable, runner=runner
            )
            second = load_native_catalog(
                directory, executable=executable, runner=runner
            )

        self.assertEqual(first, second)
        self.assertEqual(calls, [("--version",), ("debug", "models", "--bundled")])

    def test_route_decision_hash_is_canonical_and_order_independent(self) -> None:
        luna = point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0)
        terra = point("gpt-5.6-terra", "max", passed=64, cost=4.0, minutes=31.0)
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        forward = resolve_route(
            radar_payload(luna, terra), native, "routine", now=now
        )
        reverse = resolve_route(
            radar_payload(terra, luna), native, "routine", now=now
        )

        self.assertRegex(forward["decision_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(forward["decision_sha256"], reverse["decision_sha256"])

    def test_stabilized_decision_is_self_validating_and_dispatch_bound(self) -> None:
        recommendation = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=73, cost=4.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        decision, _state = stabilize_route(recommendation, None)

        validated = validate_route_decision(
            decision,
            lane="routine",
            model=decision["selected"]["model"],
            effort=decision["selected"]["effort"],
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        tampered = json.loads(json.dumps(decision))
        tampered["selected"]["model"] = "gpt-5.6-sol"

        self.assertEqual(validated, decision)
        with self.assertRaises(RoutingCatalogError):
            validate_route_decision(
                tampered,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            )

    def test_pre_thread_native_rejection_can_only_advance_the_bound_order(self) -> None:
        recommendation = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=73, cost=4.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        decision, _state = stabilize_route(recommendation, None)
        rejected = decision["selected"]
        fallback = advance_route(
            decision,
            rejected_model=rejected["model"],
            rejected_effort=rejected["effort"],
        )

        self.assertEqual(fallback["dispatch"]["rank"], 2)
        self.assertEqual(fallback["dispatch"]["reason"], "native_rejection_fallback")
        self.assertNotEqual(fallback["selected"], rejected)
        self.assertNotEqual(fallback["decision_sha256"], decision["decision_sha256"])

    def test_advance_only_updates_rank_and_appends_a_rejection_ticket(self) -> None:
        recommendation = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0),
                point("gpt-5.6-terra", "max", passed=73, cost=4.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-terra", "max"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )
        decision, _state = stabilize_route(recommendation, None)
        rejected = decision["selected"]

        fallback = advance_route(
            decision,
            rejected_model=rejected["model"],
            rejected_effort=rejected["effort"],
            rejection_ticket="native:unsupported-model-effort",
        )

        self.assertEqual(fallback["dispatch"]["rank"], 2)
        self.assertEqual(
            fallback["dispatch"]["rejection_tickets"],
            ["native:unsupported-model-effort"],
        )
        for field in (
            "eligible_candidates",
            "fallback_order",
            "pareto_frontier",
            "policy",
            "policy_sha256",
        ):
            self.assertEqual(fallback[field], decision[field])

    def test_new_eligible_winner_switches_without_persisting_candidate_history(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        initial_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=62, cost=6.0, minutes=25.0),
        )
        first_change_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=80, cost=0.6, minutes=20.0),
        )
        first_change_radar["fingerprint"] = "b" * 64
        second_change_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=81, cost=0.6, minutes=20.0),
        )
        second_change_radar["fingerprint"] = "c" * 64
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        initial, state = stabilize_route(
            resolve_route(initial_radar, native, "routine", now=now), None
        )
        switched, state = stabilize_route(
            resolve_route(first_change_radar, native, "routine", now=now), state
        )
        stable, state = stabilize_route(
            resolve_route(second_change_radar, native, "routine", now=now), state
        )

        self.assertEqual(initial["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(initial["hysteresis"]["status"], "initialized")
        self.assertEqual(switched["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(switched["hysteresis"]["status"], "switched")
        self.assertEqual(stable["hysteresis"]["status"], "stable")
        self.assertEqual(
            set(state["lanes"]["implementation:routine:deterministic"]),
            {"active", "policy_sha256"},
        )

    def test_materially_better_ready_route_switches_without_pending_history(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        initial, state = stabilize_route(
            resolve_route(
                radar_payload(
                    point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
                    point("gpt-5.6-terra", "max", passed=69, cost=2.0, minutes=21.0),
                ),
                native,
                "complex",
                now=now,
            ),
            None,
        )
        changed = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=80, cost=0.8, minutes=19.0),
        )
        changed["fingerprint"] = "b" * 64
        switched, _state = stabilize_route(
            resolve_route(changed, native, "complex", now=now), state
        )

        self.assertEqual(initial["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(switched["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(switched["hysteresis"]["status"], "switched")

    def test_fingerprint_only_change_keeps_the_same_active_route(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        initial_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=69, cost=2.0, minutes=21.0),
        )
        winning_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=80, cost=0.8, minutes=19.0),
        )
        same_measurements = json.loads(json.dumps(winning_radar))
        same_measurements["fingerprint"] = "c" * 64

        _initial, state = stabilize_route(
            resolve_route(initial_radar, native, "complex", now=now), None
        )
        switched, state = stabilize_route(
            resolve_route(winning_radar, native, "complex", now=now), state
        )
        stable, _state = stabilize_route(
            resolve_route(same_measurements, native, "complex", now=now), state
        )

        self.assertEqual(switched["hysteresis"]["status"], "switched")
        self.assertEqual(stable["hysteresis"]["status"], "stable")
        self.assertEqual(stable["selected"]["model"], "gpt-5.6-terra")

    def test_policy_change_applies_immediately_instead_of_using_stale_hysteresis(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=0.5, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=78, cost=6.0, minutes=25.0),
        )
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        _initial, state = stabilize_route(
            resolve_route(radar, native, "routine", now=now), None
        )
        changed_policy, _state = stabilize_route(
            resolve_route(
                radar,
                native,
                "routine",
                now=now,
                policy_overrides={
                    "quality_weight": 0.90,
                    "cost_weight": 0.05,
                    "time_weight": 0.05,
                },
            ),
            state,
        )

        self.assertEqual(changed_policy["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(changed_policy["hysteresis"]["status"], "switched_policy")

    def test_default_render_is_quiet_and_explanation_is_opt_in(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=70, cost=0.5, minutes=30.0),
                point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
            ),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        quiet = render_resolution(decision, explain=False)
        explained = render_resolution(decision, explain=True)

        self.assertEqual(
            set(quiet),
            {
                "candidates",
                "decision_sha256",
                "effort",
                "judgment",
                "lane",
                "model",
                "placement",
                "purpose",
            },
        )
        self.assertLessEqual(len(quiet["candidates"]), 3)
        self.assertNotIn("tradeoffs", quiet)
        self.assertIn("tradeoffs", explained)
        self.assertIn("eligible_candidates", explained)

    def test_purpose_and_judgment_select_distinct_policies(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=68, cost=0.4, minutes=20.0),
            point("gpt-5.6-terra", "max", passed=80, cost=9.0, minutes=35.0),
        )
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        implementation, _ = stabilize_route(
            resolve_route(
                radar,
                native,
                purpose="implementation",
                judgment="routine",
                now=now,
            ),
            None,
        )
        acceptance, _ = stabilize_route(
            resolve_route(
                radar,
                native,
                purpose="acceptance",
                judgment="complex",
                now=now,
            ),
            None,
        )

        self.assertEqual(implementation["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(acceptance["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(implementation["purpose"], "implementation")
        self.assertEqual(acceptance["judgment"], "complex")
        validate_route_decision(implementation)
        validate_route_decision(acceptance)

    def test_acceptance_softly_avoids_exact_primary_route(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-sol", "max", passed=80, cost=8.0, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=79, cost=4.5, minutes=30.0),
        )
        native = native_catalog(
            ("gpt-5.6-sol", "max"),
            ("gpt-5.6-terra", "max"),
        )

        decision, _ = stabilize_route(
            resolve_route(
                radar,
                native,
                purpose="acceptance",
                judgment="complex",
                primary_model="gpt-5.6-sol",
                primary_effort="max",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            ),
            None,
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-terra", "max"),
        )
        self.assertEqual(
            decision["placement_context"]["primary_effort"], "max"
        )
        validate_route_decision(decision)

    def test_acceptance_prefers_terra_when_sol_has_no_clear_iq_advantage(self) -> None:
        decision, _ = stabilize_route(
            resolve_route(
                radar_payload(
                    point(
                        "gpt-5.6-sol",
                        "xhigh",
                        passed=80,
                        cost=8.0,
                        minutes=30.0,
                    ),
                    point(
                        "gpt-5.6-terra",
                        "max",
                        passed=79,
                        cost=4.5,
                        minutes=30.0,
                    ),
                ),
                native_catalog(
                    ("gpt-5.6-sol", "xhigh"),
                    ("gpt-5.6-terra", "max"),
                ),
                purpose="acceptance",
                judgment="complex",
                primary_model="gpt-5.6-sol",
                primary_effort="max",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            ),
            None,
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-terra", "max"),
        )
        validate_route_decision(decision)

    def test_acceptance_does_not_let_resource_value_override_sol_iq_gate(self) -> None:
        decision, _ = stabilize_route(
            resolve_route(
                radar_payload(
                    point(
                        "gpt-5.6-sol",
                        "max",
                        passed=80,
                        cost=8.0,
                        minutes=30.0,
                    ),
                    point(
                        "gpt-5.6-terra",
                        "max",
                        passed=79,
                        cost=5.0,
                        minutes=30.0,
                    ),
                ),
                native_catalog(
                    ("gpt-5.6-sol", "max"),
                    ("gpt-5.6-terra", "max"),
                ),
                purpose="acceptance",
                judgment="complex",
                primary_model="gpt-5.6-sol",
                primary_effort="max",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            ),
            None,
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-terra", "max"),
        )
        validate_route_decision(decision)

    def test_acceptance_preserves_a_fully_fixed_user_route(self) -> None:
        decision, _ = stabilize_route(
            resolve_route(
                radar_payload(
                    point(
                        "gpt-5.6-sol",
                        "max",
                        passed=80,
                        cost=8.0,
                        minutes=30.0,
                    ),
                    point(
                        "gpt-5.6-terra",
                        "max",
                        passed=79,
                        cost=4.5,
                        minutes=30.0,
                    ),
                ),
                native_catalog(
                    ("gpt-5.6-sol", "max"),
                    ("gpt-5.6-terra", "max"),
                ),
                purpose="acceptance",
                judgment="complex",
                fixed_model="gpt-5.6-sol",
                fixed_effort="max",
                primary_model="gpt-5.6-sol",
                primary_effort="max",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            ),
            None,
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-sol", "max"),
        )
        validate_route_decision(decision)

    def test_primary_effort_change_invalidates_route_hysteresis(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-sol", "max", passed=80, cost=8.0, minutes=30.0),
            point("gpt-5.6-terra", "max", passed=79, cost=4.5, minutes=30.0),
        )
        native = native_catalog(
            ("gpt-5.6-sol", "max"),
            ("gpt-5.6-terra", "max"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        first, state = stabilize_route(
            resolve_route(
                radar,
                native,
                purpose="acceptance",
                judgment="complex",
                primary_model="gpt-5.6-sol",
                primary_effort="max",
                now=now,
            ),
            None,
        )
        second, _ = stabilize_route(
            resolve_route(
                radar,
                native,
                purpose="acceptance",
                judgment="complex",
                primary_model="gpt-5.6-sol",
                primary_effort="xhigh",
                now=now,
            ),
            state,
        )

        self.assertNotEqual(first["policy_sha256"], second["policy_sha256"])
        self.assertEqual(first["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(second["selected"]["model"], "gpt-5.6-terra")
        self.assertEqual(second["hysteresis"]["status"], "switched_policy")
        validate_route_decision(first)
        validate_route_decision(second)

    def test_cli_accepts_primary_model_and_effort_context(self) -> None:
        args = routing_module.parser().parse_args(
            [
                "resolve",
                "--purpose",
                "acceptance",
                "--judgment",
                "complex",
                "--primary-model",
                "gpt-5.6-sol",
                "--primary-effort",
                "max",
            ]
        )

        self.assertEqual(args.primary_model, "gpt-5.6-sol")
        self.assertEqual(args.primary_effort, "max")

    def test_cli_exposes_rejection_ticket_without_requiring_route_rebuild(self) -> None:
        args = routing_module.parser().parse_args(
            [
                "advance-plan",
                "--purpose",
                "implementation",
                "--judgment",
                "routine",
                "--rejected-model",
                "gpt-5.6-luna",
                "--rejected-effort",
                "max",
                "--rejection-ticket",
                "native:unsupported-model-effort",
            ]
        )

        self.assertEqual(args.command, "advance-plan")
        self.assertEqual(args.rejection_ticket, "native:unsupported-model-effort")

    def test_same_model_execution_only_child_is_reclaimed(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-sol", "max", passed=80, cost=9.0, minutes=35.0)
        )
        native = native_catalog(("gpt-5.6-sol", "max"))
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        reclaimed = resolve_route(
            radar,
            native,
            purpose="implementation",
            judgment="complex",
            primary_model="gpt-5.6-sol",
            placement_benefits=[
                {
                    "evidence": ["contract:sha256:" + "a" * 64],
                    "kind": "closed_execution",
                }
            ],
            now=now,
        )
        delegated = resolve_route(
            radar,
            native,
            purpose="implementation",
            judgment="complex",
            primary_model="gpt-5.6-sol",
            placement_benefits=[
                {"evidence": ["peer:n02"], "kind": "parallel_ready"}
            ],
            now=now,
        )

        self.assertEqual(reclaimed["placement"]["target"], "primary")
        self.assertEqual(
            reclaimed["placement"]["reason"], "same_model_execution_only"
        )
        self.assertEqual(delegated["placement"]["target"], "child")
        self.assertEqual(delegated["placement"]["reason"], "parallel_ready")

    def test_route_price_alone_never_authorizes_a_child(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=76, cost=0.5, minutes=30.0)
            ),
            native_catalog(("gpt-5.6-luna", "max")),
            purpose="implementation",
            judgment="complex",
            primary_model="gpt-5.6-sol",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            decision["placement"],
            {"reason": "no_structural_benefit", "target": "primary"},
        )

    def test_placement_benefit_cli_is_evidence_bearing_and_canonical(self) -> None:
        parsed = routing_module.parse_placement_benefits(
            [
                "parallel_ready=peer:n03",
                "closed_execution=contract:sha256:" + "a" * 64,
                "parallel_ready=peer:n02",
            ]
        )

        self.assertEqual(
            parsed,
            [
                {
                    "evidence": ["contract:sha256:" + "a" * 64],
                    "kind": "closed_execution",
                },
                {
                    "evidence": ["peer:n02", "peer:n03"],
                    "kind": "parallel_ready",
                },
            ],
        )

    def test_legacy_lane_maps_to_implementation_purpose(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=72, cost=0.5, minutes=30.0)
            ),
            native_catalog(("gpt-5.6-luna", "max")),
            "routine",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(decision["purpose"], "implementation")
        self.assertEqual(decision["judgment"], "routine")
        self.assertEqual(decision["lane"], "routine")

    def test_graph_resolution_persists_only_lkg_and_small_hysteresis_state(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
        )
        calls: list[str | None] = []
        scheduled: list[list[str]] = []

        def fetch(etag: str | None) -> FetchResult:
            calls.append(etag)
            return FetchResult(status=200, payload=radar, etag='"radar-v1"')

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first, loaded_first = resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=fetch,
                native_loader=lambda: native_catalog(
                    ("gpt-5.6-luna", "max"),
                    ("gpt-5.6-sol", "xhigh"),
                ),
                scheduler=scheduled.append,
            )
            second, loaded_second = resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 10, 30, tzinfo=timezone.utc),
                fetcher=fetch,
                native_loader=lambda: native_catalog(
                    ("gpt-5.6-luna", "max"),
                    ("gpt-5.6-sol", "xhigh"),
                ),
                scheduler=scheduled.append,
            )
            state = read_routing_state(cache_dir / STATE_FILENAME)

            self.assertEqual(calls, [None])
            self.assertEqual(scheduled, [])
            self.assertEqual(loaded_first.status, "refreshed")
            self.assertEqual(loaded_second.status, "fresh_cache")
            self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
            self.assertIsNotNone(state)
            self.assertEqual(
                set(state["lanes"]["implementation:routine:deterministic"]),
                {"active", "policy_sha256"},
            )
            self.assertEqual(
                sorted(path.name for path in cache_dir.iterdir()),
                ["radar-lkg-v1.json", "routing-state-v2.json"],
            )
            self.assertLess((cache_dir / STATE_FILENAME).stat().st_size, 4096)

    def test_graph_route_returns_stale_lkg_with_refresh_signal_off_critical_path(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        calls: list[str | None] = []

        def fetch(etag: str | None) -> FetchResult:
            calls.append(etag)
            return FetchResult(status=200, payload=radar, etag='"radar-v1"')

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=fetch,
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
            )
            _decision, loaded = resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("dispatch must not synchronously refresh"),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=lambda _command: None,
            )

        self.assertEqual(calls, [None])
        self.assertEqual(loaded.status, "last_known_good")
        self.assertTrue(loaded.needs_refresh)

    def test_graph_route_schedules_one_shot_refresh_for_a_stale_lkg(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        scheduled: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=scheduled.append,
            )
            _decision, loaded = resolve_graph_route(
                cache_dir,
                "routine",
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("dispatch must not refresh Radar"),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=scheduled.append,
            )

        self.assertEqual(loaded.status, "last_known_good")
        self.assertTrue(loaded.needs_refresh)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][2], "refresh")
        self.assertIn("--cache-dir", scheduled[0])
        self.assertIn("--expected-fetched-at", scheduled[0])

    def test_same_stale_snapshot_schedules_only_one_live_refresh_process(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        scheduled: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            stale = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("stale load must not refresh"),
            )
            first = routing_module.schedule_radar_refresh(
                cache_dir, stale, scheduler=scheduled.append
            )
            second = routing_module.schedule_radar_refresh(
                cache_dir, stale, scheduler=scheduled.append
            )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(scheduled), 1)

    def test_stale_malformed_refresh_request_cannot_disable_future_refreshes(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        scheduled: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            stale = load_radar_snapshot(
                cache_dir,
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("stale load must not refresh"),
            )
            request = cache_dir / routing_module.RADAR_REFRESH_REQUEST_FILENAME
            request.write_text("not-json\n", encoding="utf-8")
            os.utime(request, (0, 0))

            reserved = routing_module.schedule_radar_refresh(
                cache_dir, stale, scheduler=scheduled.append
            )

        self.assertTrue(reserved)
        self.assertEqual(len(scheduled), 1)

    def test_refresh_helper_lock_prevents_duplicate_network_refreshes(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        initial_now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        refresh_now = datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc)
        calls: list[str | None] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            stale = load_radar_snapshot(
                cache_dir,
                now=initial_now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            with routing_module.radar_refresh_lock(cache_dir, now=refresh_now):
                self.assertFalse(
                    routing_module.refresh_radar_snapshot(
                        cache_dir,
                        expected_fetched_at=stale.fetched_at,
                        now=refresh_now,
                        fetcher=lambda _etag: self.fail(
                            "a held refresh lock must prevent a network call"
                        ),
                    )
                )
            self.assertTrue(
                routing_module.refresh_radar_snapshot(
                    cache_dir,
                    expected_fetched_at=stale.fetched_at,
                    now=refresh_now,
                    fetcher=lambda etag: (
                        calls.append(etag)
                        or FetchResult(status=200, payload=radar, etag='"radar-v2"')
                    ),
                )
            )
            self.assertFalse(
                routing_module.refresh_radar_snapshot(
                    cache_dir,
                    expected_fetched_at=stale.fetched_at,
                    now=refresh_now + timedelta(minutes=1),
                    fetcher=lambda _etag: self.fail(
                        "a completed refresh must not be repeated"
                    ),
                )
            )

        self.assertEqual(calls, ['"radar-v1"'])

    def test_refresh_command_runs_the_one_shot_helper(self) -> None:
        expected = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(
                sys,
                "argv",
                [
                    "routing_catalog.py",
                    "refresh",
                    "--cache-dir",
                    temp_dir,
                    "--expected-fetched-at",
                    expected.isoformat(),
                ],
            ):
                with mock.patch(
                    "routing_catalog.refresh_radar_snapshot", return_value=True
                ) as refresh:
                    self.assertEqual(routing_module.main(), 0)

        refresh.assert_called_once_with(
            Path(temp_dir), expected_fetched_at=expected
        )

    def test_refresh_scheduler_uses_argument_list_without_a_shell(self) -> None:
        command = [sys.executable, "routing_catalog.py", "refresh"]

        with mock.patch("routing_catalog.subprocess.Popen") as spawn:
            routing_module.launch_radar_refresh(command)

        expected = {
            "stdin": routing_module.subprocess.DEVNULL,
            "stdout": routing_module.subprocess.DEVNULL,
            "stderr": routing_module.subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            expected["creationflags"] = routing_module.subprocess.CREATE_NO_WINDOW
        else:
            expected["start_new_session"] = True
        spawn.assert_called_once_with(command, **expected)

    def test_refresh_helper_atomically_replaces_only_the_normalized_lkg(self) -> None:
        initial = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        updated = radar_payload(
            point("gpt-5.6-luna", "max", passed=76, cost=0.4, minutes=29.0)
        )
        updated["source_updated_at"] = "2026-08-03T10:30:00+00:00"
        initial_now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        refresh_now = datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            stale = load_radar_snapshot(
                cache_dir,
                now=initial_now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=initial, etag='"radar-v1"'
                ),
            )
            self.assertTrue(
                routing_module.refresh_radar_snapshot(
                    cache_dir,
                    expected_fetched_at=stale.fetched_at,
                    now=refresh_now,
                    fetcher=lambda _etag: FetchResult(
                        status=200, payload=updated, etag='"radar-v2"'
                    ),
                )
            )

            stored = json.loads(
                (cache_dir / "radar-lkg-v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                sorted(path.name for path in cache_dir.iterdir()),
                ["radar-lkg-v1.json"],
            )
            self.assertNotIn("history", stored["snapshot"])
            self.assertEqual(stored["etag"], '"radar-v2"')
            self.assertEqual(stored["fetched_at"], refresh_now.isoformat())

    def test_refresh_helper_reports_failure_when_it_only_falls_back_to_lkg(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        initial_now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        refresh_now = datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            stale = load_radar_snapshot(
                cache_dir,
                now=initial_now,
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
            )
            refreshed = routing_module.refresh_radar_snapshot(
                cache_dir,
                expected_fetched_at=stale.fetched_at,
                now=refresh_now,
                fetcher=lambda _etag: FetchResult(
                    status=503, payload=None, etag=None
                ),
            )
            stored = json.loads(
                (cache_dir / "radar-lkg-v1.json").read_text(encoding="utf-8")
            )

        self.assertFalse(refreshed)
        self.assertEqual(stored["fetched_at"], initial_now.isoformat())

    def test_graph_route_with_fully_fixed_pair_never_loads_radar(self) -> None:
        scheduled: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            decision, loaded = resolve_graph_route(
                Path(temp_dir),
                "complex",
                fixed_model="gpt-5.6-sol",
                fixed_effort="max",
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("fixed route must not load Radar"),
                native_loader=lambda: native_catalog(("gpt-5.6-sol", "max")),
                scheduler=scheduled.append,
            )

        self.assertEqual(decision["selected"], {"model": "gpt-5.6-sol", "effort": "max"})
        self.assertEqual(loaded.status, "not_required")
        self.assertFalse(loaded.needs_refresh)
        self.assertEqual(scheduled, [])

    def test_graph_route_plan_loads_shared_inputs_once(self) -> None:
        calls: list[str | None] = []
        scheduled: list[list[str]] = []
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            plan, loaded = resolve_graph_route_plan(
                Path(temp_dir),
                [
                    {
                        "assurance": "deterministic",
                        "purpose": "analysis_inspect",
                        "judgment": "routine",
                    },
                    {
                        "assurance": "deterministic",
                        "purpose": "implementation",
                        "judgment": "complex",
                    },
                ],
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda etag: (
                    calls.append(etag)
                    or FetchResult(status=200, payload=radar, etag='"radar-v1"')
                ),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=scheduled.append,
            )
            state = read_routing_state(Path(temp_dir) / STATE_FILENAME)

        self.assertEqual(calls, [None])
        self.assertEqual(scheduled, [])
        self.assertEqual(len(plan["routes"]), 2)
        self.assertFalse(plan["needs_refresh"])
        self.assertEqual(loaded.status, "refreshed")
        self.assertIsNotNone(state)
        self.assertEqual(
            set(state["lanes"]),
            {
                "analysis_inspect:routine:deterministic",
                "implementation:complex:deterministic",
            },
        )

    def test_graph_route_plan_propagates_needs_refresh_from_stale_lkg(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0)
        )
        request = [
            {
                "assurance": "deterministic",
                "purpose": "implementation",
                "judgment": "routine",
            }
        ]
        scheduled: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            resolve_graph_route_plan(
                directory,
                request,
                now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                fetcher=lambda _etag: FetchResult(
                    status=200, payload=radar, etag='"radar-v1"'
                ),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=scheduled.append,
            )
            plan, loaded = resolve_graph_route_plan(
                directory,
                request,
                now=datetime(2026, 8, 3, 11, 1, tzinfo=timezone.utc),
                fetcher=lambda _etag: self.fail("dispatch must not refresh Radar"),
                native_loader=lambda: native_catalog(("gpt-5.6-luna", "max")),
                scheduler=scheduled.append,
            )

        self.assertTrue(plan["needs_refresh"])
        self.assertTrue(loaded.needs_refresh)
        self.assertEqual(len(scheduled), 1)

    def test_refresh_ttl_cannot_be_configured_below_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RoutingCatalogError):
                load_radar_snapshot(
                    Path(temp_dir),
                    now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
                    refresh_interval=timedelta(minutes=9),
                    fetcher=lambda _etag: FetchResult(status=500, payload=None, etag=None),
                )


if __name__ == "__main__":
    unittest.main()

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

from routing_catalog import (  # noqa: E402
    RADAR_URL,
    STATE_FILENAME,
    FetchResult,
    RoutingCatalogError,
    advance_route,
    fetch_radar,
    load_radar_snapshot,
    normalize_snapshot,
    read_routing_state,
    render_resolution,
    resolve_graph_route,
    resolve_route,
    routing_lock,
    stabilize_route,
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
                passed=72,
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
            ("gpt-5.6-sol", "xhigh"),
        )
        self.assertEqual(
            complex_route["selection_method"], "strict_pareto_fixed_anchor_mcda"
        )
        self.assertNotIn(
            ("gpt-5.6-sol", "ultra"),
            [
                (candidate["model"], candidate["effort"])
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
            "complex",
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
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-sol", "xhigh"),
        )

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
                        directory / "routing-state-v1.json",
                        {"protocol": "test"},
                        prefix="routing-state-v1.",
                    )

            self.assertEqual(list(directory.glob("routing-state-v1.*.tmp")), [])

    def test_short_lived_lock_blocks_overlap_and_is_removed_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            now = datetime.now(timezone.utc)
            with routing_lock(cache_dir, now=now):
                with self.assertRaises(RoutingCatalogError):
                    with routing_lock(cache_dir, now=now, wait_seconds=0):
                        self.fail("overlapping lock unexpectedly acquired")
            self.assertFalse((cache_dir / "routing-v1.lock").exists())

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
            observed["timeout"] = timeout
            return Response()

        result = fetch_radar('"radar-v1"', opener=open_request)

        self.assertEqual(observed["url"], RADAR_URL)
        self.assertEqual(observed["etag"], '"radar-v1"')
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

        self.assertEqual(
            (decision["selected"]["model"], decision["selected"]["effort"]),
            ("gpt-5.6-sol", "xhigh"),
        )
        self.assertEqual(decision["policy"]["quality_weight"], 0.70)

    def test_one_user_fixed_dimension_constrains_the_adaptive_choice(self) -> None:
        decision = resolve_route(
            radar_payload(
                point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
                point("gpt-5.6-sol", "high", passed=72, cost=3.0, minutes=20.0),
                point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "high"),
                ("gpt-5.6-sol", "xhigh"),
            ),
            "complex",
            now=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
            fixed_model="gpt-5.6-sol",
        )

        self.assertEqual(decision["selected"]["model"], "gpt-5.6-sol")
        self.assertEqual(decision["constraints"]["fixed_model"], "gpt-5.6-sol")

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
                point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
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
                point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
            ),
            native_catalog(
                ("gpt-5.6-luna", "max"),
                ("gpt-5.6-sol", "xhigh"),
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

    def test_new_winner_requires_two_distinct_snapshots_before_switching(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-sol", "xhigh"),
        )
        initial_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=62, cost=6.0, minutes=25.0),
        )
        first_change_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=80, cost=0.6, minutes=20.0),
        )
        first_change_radar["fingerprint"] = "b" * 64
        second_change_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=66, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=81, cost=0.6, minutes=20.0),
        )
        second_change_radar["fingerprint"] = "c" * 64
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

        initial, state = stabilize_route(
            resolve_route(initial_radar, native, "routine", now=now), None
        )
        pending, state = stabilize_route(
            resolve_route(first_change_radar, native, "routine", now=now), state
        )
        switched, state = stabilize_route(
            resolve_route(second_change_radar, native, "routine", now=now), state
        )

        self.assertEqual(initial["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(initial["hysteresis"]["status"], "initialized")
        self.assertEqual(pending["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(pending["hysteresis"]["status"], "pending")
        self.assertEqual(switched["selected"]["model"], "gpt-5.6-sol")
        self.assertEqual(switched["hysteresis"]["status"], "switched")

    def test_still_eligible_active_route_does_not_bypass_hysteresis(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-sol", "xhigh"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        initial, state = stabilize_route(
            resolve_route(
                radar_payload(
                    point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
                    point("gpt-5.6-sol", "xhigh", passed=69, cost=2.0, minutes=21.0),
                ),
                native,
                "complex",
                now=now,
            ),
            None,
        )
        changed = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-sol", "xhigh", passed=80, cost=0.8, minutes=19.0),
        )
        changed["fingerprint"] = "b" * 64
        pending, _state = stabilize_route(
            resolve_route(changed, native, "complex", now=now), state
        )

        self.assertEqual(initial["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(pending["selected"]["model"], "gpt-5.6-luna")
        self.assertEqual(pending["hysteresis"]["status"], "pending")

    def test_fingerprint_only_change_does_not_count_as_new_measurement(self) -> None:
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-sol", "xhigh"),
        )
        now = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
        initial_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-sol", "xhigh", passed=69, cost=2.0, minutes=21.0),
        )
        winning_radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=1.0, minutes=20.0),
            point("gpt-5.6-sol", "xhigh", passed=80, cost=0.8, minutes=19.0),
        )
        same_measurements = json.loads(json.dumps(winning_radar))
        same_measurements["fingerprint"] = "c" * 64

        _initial, state = stabilize_route(
            resolve_route(initial_radar, native, "complex", now=now), None
        )
        pending, state = stabilize_route(
            resolve_route(winning_radar, native, "complex", now=now), state
        )
        still_pending, _state = stabilize_route(
            resolve_route(same_measurements, native, "complex", now=now), state
        )

        self.assertEqual(pending["hysteresis"]["pending_count"], 1)
        self.assertEqual(still_pending["hysteresis"]["pending_count"], 1)
        self.assertEqual(still_pending["selected"]["model"], "gpt-5.6-luna")

    def test_policy_change_applies_immediately_instead_of_using_stale_hysteresis(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=70, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
        )
        native = native_catalog(
            ("gpt-5.6-luna", "max"),
            ("gpt-5.6-sol", "xhigh"),
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

        self.assertEqual(changed_policy["selected"]["model"], "gpt-5.6-sol")
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
            set(quiet), {"decision_sha256", "effort", "lane", "model"}
        )
        self.assertIn("tradeoffs", explained)
        self.assertIn("eligible_candidates", explained)

    def test_graph_resolution_persists_only_lkg_and_small_hysteresis_state(self) -> None:
        radar = radar_payload(
            point("gpt-5.6-luna", "max", passed=75, cost=0.5, minutes=30.0),
            point("gpt-5.6-sol", "xhigh", passed=78, cost=6.0, minutes=25.0),
        )
        calls: list[str | None] = []

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
            )
            state = read_routing_state(cache_dir / STATE_FILENAME)

            self.assertEqual(calls, [None])
            self.assertEqual(loaded_first.status, "refreshed")
            self.assertEqual(loaded_second.status, "fresh_cache")
            self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
            self.assertIsNotNone(state)
            self.assertEqual(
                sorted(path.name for path in cache_dir.iterdir()),
                ["radar-lkg-v1.json", "routing-state-v1.json"],
            )
            self.assertLess((cache_dir / STATE_FILENAME).stat().st_size, 4096)

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

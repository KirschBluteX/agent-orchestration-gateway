#!/usr/bin/env python3
"""Resolve auditable worker routes from a validated CodexRadar snapshot."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from decision_policy import (
    DecisionPolicyError,
    PLACEMENT_BENEFITS,
    ROUTE_ASSURANCES,
    normalize_placement_benefits,
    select_placement as select_policy_placement,
)


PROTOCOL = "cco.routing.v1"
ROUTE_DECISION_PROTOCOL = "cco.routing-decision.v2"
ROUTE_PLAN_PROTOCOL = "cco.route-plan.v2"
ROUTE_PLAN_DOMAIN = b"cco.route-plan.v2\0"
SNAPSHOT_DOMAIN = b"cco.routing-snapshot.v1\0"
MEASUREMENT_DOMAIN = b"cco.routing-measurement.v1\0"
DECISION_DOMAIN = b"cco.routing-decision.v2\0"
NATIVE_CATALOG_DOMAIN = b"cco.routing-native-catalog.v1\0"
POLICY_DOMAIN = b"cco.routing-policy.v1\0"
RADAR_URL = "https://codexradar.com/data/intelligence-efficiency.json"
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CACHE_FILE_MAX_BYTES = 512 * 1024
STATE_FILE_MAX_BYTES = 64 * 1024
NATIVE_CATALOG_MAX_BYTES = 4 * 1024 * 1024
NATIVE_CATALOG_CACHE_PROTOCOL = "cco.native-catalog-cache.v1"
NATIVE_CATALOG_CACHE_FILENAME = "native-catalog-v1.json"
MIN_IQ = 90.0
MIN_SAMPLES = 30
MAX_SAMPLE_COUNT = 1_000_000
MIN_COHORT_RATIO = 0.80
MIN_METRIC_COVERAGE = 0.50
MAX_SOURCE_AGE = timedelta(hours=72)
MAX_FUTURE_SKEW = timedelta(minutes=15)
REFRESH_INTERVAL = timedelta(hours=1)
MIN_REFRESH_INTERVAL = timedelta(minutes=10)
MAX_REFRESH_INTERVAL = timedelta(hours=24)
LANES = frozenset({"routine", "complex"})
PURPOSES = frozenset(
    {"analysis_inspect", "analysis_probe", "implementation", "acceptance"}
)
NATIVE_MULTI_AGENT_VERSIONS = frozenset({"v1", "v2"})
CACHE_PROTOCOL = "cco.routing-cache.v1"
CACHE_FILENAME = "radar-lkg-v1.json"
CACHE_TEMP_GLOB = "radar-lkg-v1.*.tmp"
STATE_PROTOCOL = "cco.routing-state.v2"
STATE_FILENAME = "routing-state-v2.json"
STATE_TEMP_GLOB = "routing-state-v2.*.tmp"
LOCK_FILENAME = "routing-v1.lock"
RADAR_REFRESH_LOCK_FILENAME = "radar-refresh-v1.lock"
RADAR_REFRESH_REQUEST_FILENAME = "radar-refresh-request-v1.json"
LOCK_STALE_AFTER = timedelta(minutes=2)
TEMP_STALE_AFTER = timedelta(hours=1)
SWITCH_MARGIN = 0.01
REQUIRED_WINNING_SNAPSHOTS = 1
MAX_FALLBACK_CANDIDATES = 3
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
REJECTION_TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
PLACEMENT_REASONS = PLACEMENT_BENEFITS | {
    "independent_acceptance",
    "no_structural_benefit",
    "same_model_execution_only",
}
DEFAULT_POLICIES = {
    "analysis_inspect": {
        "routine": {
            "quality_weight": 0.30,
            "cost_weight": 0.60,
            "time_weight": 0.10,
        },
        "complex": {
            "quality_weight": 0.55,
            "cost_weight": 0.35,
            "time_weight": 0.10,
        },
    },
    "analysis_probe": {
        "routine": {
            "quality_weight": 0.40,
            "cost_weight": 0.50,
            "time_weight": 0.10,
        },
        "complex": {
            "quality_weight": 0.65,
            "cost_weight": 0.25,
            "time_weight": 0.10,
        },
    },
    "implementation": {
        "routine": {
        "quality_weight": 0.35,
        "cost_weight": 0.55,
        "time_weight": 0.10,
        },
        "complex": {
        "quality_weight": 0.70,
        "cost_weight": 0.20,
        "time_weight": 0.10,
        },
    },
    "acceptance": {
        "routine": {
            "quality_weight": 0.70,
            "cost_weight": 0.25,
            "time_weight": 0.05,
        },
        "complex": {
            "quality_weight": 0.85,
            "cost_weight": 0.10,
            "time_weight": 0.05,
        },
    },
}
POLICY_COMMON = {
        "uncertainty_weight": 0.05,
        "minimum_iq_exclusive": MIN_IQ,
        "cost_anchor_usd": 25.0,
        "time_anchor_minutes": 60.0,
}


class RoutingCatalogError(ValueError):
    pass


class RoutingCacheBusy(RoutingCatalogError):
    pass


def reject_json_constant(value: str) -> None:
    raise RoutingCatalogError(f"non-finite JSON constant is not allowed: {value}")


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RoutingCatalogError(f"duplicate JSON key is not allowed: {key}")
        value[key] = item
    return value


def load_json_bytes(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RoutingCatalogError(f"{label} is not valid UTF-8 JSON") from error


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


@dataclass(frozen=True)
class FetchResult:
    status: int
    payload: object | None
    etag: str | None


@dataclass(frozen=True)
class LoadedSnapshot:
    snapshot: dict[str, Any]
    status: str
    fetched_at: datetime
    needs_refresh: bool = False


def fetch_radar(
    etag: str | None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> FetchResult:
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-cost-orchestrator/0.8.0 routing-catalog",
    }
    if etag is not None:
        if not etag or len(etag) > 256 or "\r" in etag or "\n" in etag:
            raise RoutingCatalogError("cached radar etag is invalid")
        headers["If-None-Match"] = etag
    request = Request(RADAR_URL, headers=headers, method="GET")
    try:
        response_context = opener(request, timeout=HTTP_TIMEOUT_SECONDS)
    except HTTPError as error:
        if error.code == 304:
            return FetchResult(status=304, payload=None, etag=error.headers.get("ETag"))
        raise RoutingCatalogError(f"radar fetch failed with status {error.code}") from error
    except (OSError, URLError) as error:
        raise RoutingCatalogError("radar fetch failed") from error
    with response_context as response:
        status = int(getattr(response, "status", 200))
        if response.geturl() != RADAR_URL:
            raise RoutingCatalogError("radar response redirected outside the canonical endpoint")
        if status == 304:
            return FetchResult(status=304, payload=None, etag=response.headers.get("ETag"))
        if status != 200:
            raise RoutingCatalogError(f"radar fetch failed with status {status}")
        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise RoutingCatalogError("radar response is not JSON")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                raise RoutingCatalogError("radar content length is invalid") from error
            if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                raise RoutingCatalogError("radar response exceeds the size limit")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RoutingCatalogError("radar response exceeds the size limit")
        payload = load_json_bytes(body, "radar response")
        return FetchResult(status=200, payload=payload, etag=response.headers.get("ETag"))


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RoutingCatalogError(f"{field} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RoutingCatalogError(f"{field} must be an RFC3339 string") from error
    if parsed.tzinfo is None:
        raise RoutingCatalogError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def finite_number(value: object, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingCatalogError(f"{field} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise RoutingCatalogError(f"{field} is out of range") from error
    if not math.isfinite(number) or number < minimum:
        raise RoutingCatalogError(f"{field} is out of range")
    return number


def positive_number(value: object, field: str) -> float:
    number = finite_number(value, field)
    if number <= 0:
        raise RoutingCatalogError(f"{field} must be positive")
    return number


def positive_int(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SAMPLE_COUNT
    ):
        raise RoutingCatalogError(f"{field} must be a positive integer")
    return value


def nonnegative_int(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_SAMPLE_COUNT
    ):
        raise RoutingCatalogError(f"{field} must be a non-negative integer")
    return value


def native_capability_records(catalog: object) -> list[dict[str, str]]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise RoutingCatalogError("native model catalog is malformed")
    if not 1 <= len(catalog["models"]) <= 256:
        raise RoutingCatalogError("native model catalog has an invalid model count")
    backend_annotated = any(
        isinstance(model, dict) and "multi_agent_version" in model
        for model in catalog["models"]
    )
    pairs: set[tuple[str, str]] = set()
    for model in catalog["models"]:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("slug"), str)
            or MODEL_RE.fullmatch(model["slug"]) is None
        ):
            raise RoutingCatalogError("native model catalog is malformed")
        if (
            backend_annotated
            and model.get("multi_agent_version") not in NATIVE_MULTI_AGENT_VERSIONS
        ):
            continue
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list) or not 1 <= len(levels) <= 32:
            raise RoutingCatalogError("native model catalog is malformed")
        for level in levels:
            if (
                not isinstance(level, dict)
                or not isinstance(level.get("effort"), str)
                or EFFORT_RE.fullmatch(level["effort"]) is None
            ):
                raise RoutingCatalogError("native model catalog is malformed")
            pairs.add((model["slug"], level["effort"]))
            if len(pairs) > 1024:
                raise RoutingCatalogError("native model catalog has too many capabilities")
    if not pairs:
        raise RoutingCatalogError("native model catalog contains no capabilities")
    return [
        {"effort": effort, "model": model}
        for model, effort in sorted(pairs, key=lambda item: (item[0], item[1]))
    ]


def native_capabilities(catalog: object) -> frozenset[tuple[str, str]]:
    return frozenset(
        (record["model"], record["effort"])
        for record in native_capability_records(catalog)
    )


def native_catalog_sha256(catalog: object) -> str:
    records = native_capability_records(catalog)
    return "sha256:" + hashlib.sha256(
        NATIVE_CATALOG_DOMAIN + canonical_bytes(records)
    ).hexdigest()


def normalize_point(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingCatalogError("radar point must be an object")
    model = value.get("model")
    effort = value.get("effort")
    if (
        not isinstance(model, str)
        or MODEL_RE.fullmatch(model) is None
        or not isinstance(effort, str)
        or EFFORT_RE.fullmatch(effort) is None
    ):
        raise RoutingCatalogError("radar point model and effort must be non-empty strings")
    passed = nonnegative_int(value.get("passed"), "passed")
    valid_tasks = positive_int(value.get("valid_tasks"), "valid_tasks")
    if passed > valid_tasks:
        raise RoutingCatalogError("passed cannot exceed valid_tasks")
    iq = finite_number(value.get("iq"), "iq")
    expected_iq = passed / valid_tasks * 150.0
    if abs(iq - expected_iq) > 0.01:
        raise RoutingCatalogError("iq does not match passed / valid_tasks * 150")
    price_samples = positive_int(value.get("price_samples"), "price_samples")
    duration_samples = positive_int(value.get("duration_samples"), "duration_samples")
    point = {
        "model": model,
        "effort": effort,
        "iq": round(iq, 6),
        "passed": passed,
        "valid_tasks": valid_tasks,
        "average_price_usd": positive_number(
            value.get("average_price_usd"), "average_price_usd"
        ),
        "price_samples": price_samples,
        "average_minutes": positive_number(
            value.get("average_minutes"), "average_minutes"
        ),
        "duration_samples": duration_samples,
        "incomplete_cost_samples": nonnegative_int(
            value.get("incomplete_cost_samples", 0), "incomplete_cost_samples"
        ),
    }
    if price_samples > valid_tasks or duration_samples > valid_tasks:
        raise RoutingCatalogError("metric sample counts cannot exceed valid_tasks")
    if point["incomplete_cost_samples"] > valid_tasks - price_samples:
        raise RoutingCatalogError("incomplete cost samples exceed the task cohort")
    return point


def normalize_snapshot(payload: object, *, now: datetime) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RoutingCatalogError("radar payload must be an object")
    if payload.get("schema") != 2 or payload.get("type") != "distributed_intelligence_efficiency":
        raise RoutingCatalogError("unsupported radar schema")
    source_updated_at = parse_timestamp(payload.get("source_updated_at"), "source_updated_at")
    current = now.astimezone(timezone.utc)
    if source_updated_at - current > MAX_FUTURE_SKEW:
        raise RoutingCatalogError("radar source timestamp is in the future")
    if current - source_updated_at > MAX_SOURCE_AGE:
        raise RoutingCatalogError("radar source is too old")
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise RoutingCatalogError("radar fingerprint is invalid")
    values = payload.get("points")
    if not isinstance(values, list) or not values or len(values) > 128:
        raise RoutingCatalogError("radar points must contain 1 to 128 records")
    points = sorted(
        (normalize_point(value) for value in values),
        key=lambda item: (item["model"], item["effort"]),
    )
    identities = [(item["model"], item["effort"]) for item in points]
    if len(set(identities)) != len(identities):
        raise RoutingCatalogError("radar points contain duplicate model/effort pairs")
    snapshot = {
        "protocol": PROTOCOL,
        "source": "https://codexradar.com/data/intelligence-efficiency.json",
        "source_updated_at": source_updated_at.isoformat(),
        "fingerprint": fingerprint,
        "points": points,
    }
    snapshot["measurement_sha256"] = measurement_sha256(snapshot)
    snapshot["snapshot_sha256"] = snapshot_sha256(snapshot)
    return snapshot


def measurement_sha256(snapshot: dict[str, Any]) -> str:
    points = snapshot.get("points")
    if not isinstance(points, list):
        raise RoutingCatalogError("routing snapshot points are malformed")
    return "sha256:" + hashlib.sha256(
        MEASUREMENT_DOMAIN + canonical_bytes(points)
    ).hexdigest()


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    preimage = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    return "sha256:" + hashlib.sha256(
        SNAPSHOT_DOMAIN + canonical_bytes(preimage)
    ).hexdigest()


def route_decision_sha256(decision: dict[str, Any]) -> str:
    preimage = {key: value for key, value in decision.items() if key != "decision_sha256"}
    return "sha256:" + hashlib.sha256(
        DECISION_DOMAIN + canonical_bytes(preimage)
    ).hexdigest()


def route_plan_sha256(plan: Mapping[str, Any]) -> str:
    """Return the canonical identity for a compact route-plan v1 object."""

    if not isinstance(plan, Mapping):
        raise RoutingCatalogError("route plan must be an object")
    preimage = {key: value for key, value in plan.items() if key != "plan_sha256"}
    try:
        encoded = canonical_bytes(preimage)
    except (TypeError, ValueError) as error:
        raise RoutingCatalogError("route plan cannot be canonically encoded") from error
    return "sha256:" + hashlib.sha256(ROUTE_PLAN_DOMAIN + encoded).hexdigest()


def validate_normalized_snapshot(
    snapshot: object, *, now: datetime
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise RoutingCatalogError("routing snapshot is malformed")
    required = {
        "fingerprint",
        "measurement_sha256",
        "points",
        "protocol",
        "snapshot_sha256",
        "source",
        "source_updated_at",
    }
    if set(snapshot) != required or snapshot.get("protocol") != PROTOCOL:
        raise RoutingCatalogError("routing snapshot is malformed")
    if snapshot.get("source") != RADAR_URL:
        raise RoutingCatalogError("routing snapshot source is invalid")
    fingerprint = snapshot.get("fingerprint")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise RoutingCatalogError("routing snapshot fingerprint is invalid")
    source_updated_at = parse_timestamp(
        snapshot.get("source_updated_at"), "source_updated_at"
    )
    current = now.astimezone(timezone.utc)
    if source_updated_at - current > MAX_FUTURE_SKEW:
        raise RoutingCatalogError("routing snapshot source timestamp is in the future")
    if current - source_updated_at > MAX_SOURCE_AGE:
        raise RoutingCatalogError("routing snapshot source is too old")
    values = snapshot.get("points")
    if not isinstance(values, list) or not values or len(values) > 128:
        raise RoutingCatalogError("routing snapshot points are malformed")
    points: list[dict[str, Any]] = []
    for value in values:
        point = normalize_point(value)
        if value != point:
            raise RoutingCatalogError("routing snapshot point schema is not canonical")
        points.append(point)
    if points != sorted(points, key=lambda item: (item["model"], item["effort"])):
        raise RoutingCatalogError("routing snapshot points are not canonical")
    identities = [(item["model"], item["effort"]) for item in points]
    if len(identities) != len(set(identities)):
        raise RoutingCatalogError("routing snapshot points contain duplicates")
    normalized = dict(snapshot)
    if normalized.get("measurement_sha256") != measurement_sha256(normalized):
        raise RoutingCatalogError("routing snapshot measurement hash mismatch")
    if normalized.get("snapshot_sha256") != snapshot_sha256(normalized):
        raise RoutingCatalogError("routing snapshot hash mismatch")
    return normalized


def is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ) or getattr(path, "is_junction", lambda: False)()


def validate_cache_directory(cache_dir: Path) -> None:
    if not cache_dir.is_dir() or is_reparse(cache_dir):
        raise RoutingCatalogError("routing cache directory must be a real directory")


def cleanup_stale_temps(cache_dir: Path, *, now: datetime) -> None:
    cutoff = now.astimezone(timezone.utc).timestamp() - TEMP_STALE_AFTER.total_seconds()
    for pattern in (CACHE_TEMP_GLOB, STATE_TEMP_GLOB):
        candidates = cache_dir.glob(pattern)
        for candidate in candidates:
            try:
                if candidate.lstat().st_mtime > cutoff:
                    continue
                candidate.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            except OSError:
                # A concurrent writer may still own the file. Its own finally block
                # remains responsible for immediate cleanup.
                continue


def cleanup_cache_temps(cache_dir: Path) -> None:
    """Compatibility wrapper: remove only stale abandoned routing temporaries."""
    cleanup_stale_temps(cache_dir, now=datetime.now(timezone.utc))


def read_cache(
    cache_file: Path, *, now: datetime, allow_stale_source: bool = True
) -> dict[str, Any] | None:
    if not cache_file.exists():
        return None
    if not cache_file.is_file() or is_reparse(cache_file):
        raise RoutingCatalogError("routing cache must be a real file")
    try:
        if cache_file.stat().st_size > CACHE_FILE_MAX_BYTES:
            raise RoutingCatalogError("routing cache exceeds the size limit")
        value = load_json_bytes(cache_file.read_bytes(), "routing cache")
    except OSError as error:
        raise RoutingCatalogError("routing cache could not be read") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"etag", "fetched_at", "protocol", "snapshot"}
        or value.get("protocol") != CACHE_PROTOCOL
    ):
        raise RoutingCatalogError("routing cache is malformed")
    snapshot = value.get("snapshot")
    validation_now = now
    if allow_stale_source and isinstance(snapshot, dict):
        source = parse_timestamp(snapshot.get("source_updated_at"), "source_updated_at")
        if now.astimezone(timezone.utc) - source > MAX_SOURCE_AGE:
            validation_now = source + MAX_SOURCE_AGE
    value["snapshot"] = validate_normalized_snapshot(snapshot, now=validation_now)
    fetched_at = parse_timestamp(value.get("fetched_at"), "fetched_at")
    if fetched_at > now.astimezone(timezone.utc):
        raise RoutingCatalogError("routing cache fetched_at is in the future")
    etag = value.get("etag")
    if etag is not None and (
        not isinstance(etag, str)
        or not etag
        or len(etag) > 256
        or "\r" in etag
        or "\n" in etag
    ):
        raise RoutingCatalogError("routing cache etag is malformed")
    return value


def write_json_atomic(path: Path, value: dict[str, Any], *, prefix: str) -> None:
    encoded = canonical_bytes(value) + b"\n"
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=prefix,
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(encoded)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, path)
        staged_path = None
    except OSError as error:
        raise RoutingCatalogError("could not update routing state") from error
    finally:
        if staged_path is not None:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


def write_cache(cache_file: Path, value: dict[str, Any]) -> None:
    write_json_atomic(cache_file, value, prefix="radar-lkg-v1.")


def validate_refresh_interval(value: timedelta) -> timedelta:
    if not MIN_REFRESH_INTERVAL <= value <= MAX_REFRESH_INTERVAL:
        raise RoutingCatalogError("refresh interval must be between 10 minutes and 24 hours")
    return value


def load_radar_snapshot(
    cache_dir: Path,
    *,
    now: datetime,
    fetcher: Callable[[str | None], FetchResult],
    refresh_interval: timedelta = REFRESH_INTERVAL,
    force_refresh: bool = False,
) -> LoadedSnapshot:
    refresh_interval = validate_refresh_interval(refresh_interval)
    cache_dir.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(cache_dir)
    cleanup_stale_temps(cache_dir, now=now)
    cache_file = cache_dir / CACHE_FILENAME
    try:
        cached = read_cache(cache_file, now=now)
    except RoutingCatalogError:
        try:
            cache_file.unlink(missing_ok=True)
        except OSError as error:
            raise RoutingCatalogError("malformed routing cache could not be removed") from error
        cached = None
    current = now.astimezone(timezone.utc)
    if cached is not None:
        fetched_at = parse_timestamp(cached["fetched_at"], "fetched_at")
        source_updated_at = parse_timestamp(
            cached["snapshot"]["source_updated_at"], "source_updated_at"
        )
        source_is_current = current - source_updated_at <= MAX_SOURCE_AGE
        if source_is_current and current - fetched_at < refresh_interval:
            return LoadedSnapshot(cached["snapshot"], "fresh_cache", fetched_at)
        if source_is_current and not force_refresh:
            # Dispatch is stale-while-revalidate: a bounded LKG is immediately
            # usable and the caller can schedule a refresh off the critical path.
            return LoadedSnapshot(
                cached["snapshot"],
                "last_known_good",
                fetched_at,
                needs_refresh=True,
            )
    try:
        try:
            cached_source = (
                parse_timestamp(cached["snapshot"]["source_updated_at"], "source_updated_at")
                if cached is not None
                else None
            )
            etag = (
                cached.get("etag")
                if cached is not None
                and cached_source is not None
                and current - cached_source <= MAX_SOURCE_AGE
                else None
            )
            result = fetcher(etag)
            if result.status == 304:
                if cached is None:
                    raise RoutingCatalogError(
                        "radar returned 304 without a cached snapshot"
                    )
                source_updated_at = parse_timestamp(
                    cached["snapshot"]["source_updated_at"], "source_updated_at"
                )
                if current - source_updated_at > MAX_SOURCE_AGE:
                    raise RoutingCatalogError("radar source is too old to revalidate")
                refreshed = {
                    **cached,
                    "fetched_at": current.isoformat(),
                    "etag": result.etag or cached.get("etag"),
                }
                write_cache(cache_file, refreshed)
                return LoadedSnapshot(cached["snapshot"], "revalidated", current)
            if result.status != 200 or result.payload is None:
                raise RoutingCatalogError(
                    f"radar fetch failed with status {result.status}"
                )
            snapshot = normalize_snapshot(result.payload, now=current)
            if cached is not None:
                prior_source = parse_timestamp(
                    cached["snapshot"]["source_updated_at"], "source_updated_at"
                )
                next_source = parse_timestamp(
                    snapshot["source_updated_at"], "source_updated_at"
                )
                if next_source < prior_source:
                    raise RoutingCatalogError("radar response would roll back the LKG")
                if (
                    next_source == prior_source
                    and snapshot["measurement_sha256"]
                    != cached["snapshot"]["measurement_sha256"]
                ):
                    raise RoutingCatalogError(
                        "radar response changed measurements without advancing source time"
                    )
            write_cache(
                cache_file,
                {
                    "protocol": CACHE_PROTOCOL,
                    "fetched_at": current.isoformat(),
                    "etag": result.etag,
                    "snapshot": snapshot,
                },
            )
            return LoadedSnapshot(snapshot, "refreshed", current)
        except (OSError, RoutingCatalogError) as error:
            if cached is None:
                if isinstance(error, RoutingCatalogError):
                    raise
                raise RoutingCatalogError("radar fetch failed") from error
            source_updated_at = parse_timestamp(
                cached["snapshot"].get("source_updated_at"), "source_updated_at"
            )
            if current - source_updated_at > MAX_SOURCE_AGE:
                raise RoutingCatalogError("last-known-good radar snapshot is too old") from error
            fetched_at = parse_timestamp(cached["fetched_at"], "fetched_at")
            return LoadedSnapshot(cached["snapshot"], "last_known_good", fetched_at)
    finally:
        cleanup_stale_temps(cache_dir, now=now)


def launch_radar_refresh(command: list[str]) -> None:
    """Start the one-shot refresh helper without holding the dispatch path open."""

    isolation: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **isolation,
    )


def _reserve_refresh_request(cache_dir: Path, loaded: LoadedSnapshot) -> Path | None:
    """Atomically reserve one process launch for an exact stale snapshot."""

    request = cache_dir / RADAR_REFRESH_REQUEST_FILENAME
    expected = loaded.fetched_at.astimezone(timezone.utc).isoformat()
    payload = canonical_bytes(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expected_fetched_at": expected,
        }
    )
    for _attempt in range(2):
        try:
            descriptor = os.open(
                request,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                if is_reparse(request):
                    return None
                age = time.time() - request.stat().st_mtime
            except OSError:
                return None
            try:
                existing = load_json_bytes(
                    request.read_bytes(), "radar refresh request"
                )
            except (OSError, RoutingCatalogError):
                if age <= LOCK_STALE_AFTER.total_seconds():
                    return None
                try:
                    request.unlink()
                except OSError:
                    return None
                continue
            if (
                isinstance(existing, dict)
                and existing.get("expected_fetched_at") == expected
                and age <= LOCK_STALE_AFTER.total_seconds()
            ):
                return None
            try:
                request.unlink()
            except OSError:
                return None
            continue
        except OSError:
            return None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        return request
    return None


def _clear_refresh_request(cache_dir: Path, expected_fetched_at: datetime) -> None:
    request = cache_dir / RADAR_REFRESH_REQUEST_FILENAME
    try:
        if not request.is_file() or is_reparse(request):
            return
        value = load_json_bytes(request.read_bytes(), "radar refresh request")
        if isinstance(value, dict) and value.get("expected_fetched_at") == (
            expected_fetched_at.astimezone(timezone.utc).isoformat()
        ):
            request.unlink(missing_ok=True)
    except (OSError, RoutingCatalogError):
        return


def schedule_radar_refresh(
    cache_dir: Path,
    loaded: LoadedSnapshot,
    *,
    scheduler: Callable[[list[str]], Any] = launch_radar_refresh,
) -> bool:
    """Schedule a refresh only for a source-valid stale LKG."""

    if not loaded.needs_refresh:
        return False
    directory = Path(os.path.abspath(cache_dir.expanduser()))
    request = _reserve_refresh_request(directory, loaded)
    if request is None:
        return False
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "refresh",
        "--cache-dir",
        str(directory),
        "--expected-fetched-at",
        loaded.fetched_at.astimezone(timezone.utc).isoformat(),
    ]
    try:
        scheduler(command)
    except OSError:
        # Revalidation is explicitly off the dispatch path.  A later stale read
        # can retry if this best-effort scheduling attempt cannot start.
        request.unlink(missing_ok=True)
        return False
    return True


def dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    no_worse = (
        left["iq_lower_95"] >= right["iq_lower_95"]
        and left["average_price_usd"] <= right["average_price_usd"]
        and left["average_minutes"] <= right["average_minutes"]
    )
    materially_better = (
        left["iq_lower_95"] > right["iq_lower_95"]
        or left["average_price_usd"] < right["average_price_usd"]
        or left["average_minutes"] < right["average_minutes"]
    )
    return no_worse and materially_better


def pareto_frontier(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if not any(
            other is not candidate and dominates(other, candidate)
            for other in candidates
        )
    ]


def wilson_iq_interval(candidate: dict[str, Any]) -> tuple[float, float]:
    passed = candidate["passed"]
    samples = candidate["valid_tasks"]
    probability = passed / samples
    z = 1.959963984540054
    z_squared = z * z
    denominator = 1.0 + z_squared / samples
    center = (probability + z_squared / (2.0 * samples)) / denominator
    margin = (
        z
        * math.sqrt(
            probability * (1.0 - probability) / samples
            + z_squared / (4.0 * samples * samples)
        )
        / denominator
    )
    return max(0.0, center - margin) * 150.0, min(1.0, center + margin) * 150.0


def enrich_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    lower, upper = wilson_iq_interval(candidate)
    enriched = dict(candidate)
    enriched["iq_lower_95"] = round(lower, 6)
    enriched["iq_upper_95"] = round(upper, 6)
    enriched["iq_uncertainty_95"] = round((upper - lower) / 2.0, 6)
    enriched["price_coverage"] = round(
        candidate["price_samples"] / candidate["valid_tasks"], 6
    )
    enriched["duration_coverage"] = round(
        candidate["duration_samples"] / candidate["valid_tasks"], 6
    )
    return enriched


def _preferred_family(model: str) -> bool:
    lowered = model.casefold()
    return "luna" in lowered or "terra" in lowered


def _luna_family(model: str) -> bool:
    return "luna" in model.casefold()


def _sol_family(model: str) -> bool:
    return "sol" in model.casefold()


def apply_model_preference_gate(
    candidates: list[dict[str, Any]], *, fixed_model: str | None
) -> list[dict[str, Any]]:
    """Prefer Luna/Terra; admit Sol only for a statistically clear IQ gain."""

    if fixed_model is not None:
        return candidates
    preferred = [candidate for candidate in candidates if _preferred_family(candidate["model"])]
    if not preferred:
        return candidates
    best_preferred_upper = max(candidate["iq_upper_95"] for candidate in preferred)
    return [
        candidate
        for candidate in candidates
        if not _sol_family(candidate["model"])
        or candidate["iq_lower_95"] > best_preferred_upper
    ]


def apply_route_assurance_gate(
    candidates: list[dict[str, Any]],
    *,
    assurance: str,
    fixed_model: str | None,
) -> list[dict[str, Any]]:
    """Keep Luna for deterministic work and require a stronger automatic pool otherwise."""

    if assurance not in ROUTE_ASSURANCES:
        raise RoutingCatalogError("route assurance is invalid")
    if assurance == "deterministic" or fixed_model is not None:
        return candidates
    return [
        candidate
        for candidate in candidates
        if not _luna_family(candidate["model"])
    ]


def apply_judgment_confidence_gate(
    candidates: list[dict[str, Any]],
    *,
    judgment: str,
    fixed_model: str | None,
    minimum_iq: float,
) -> list[dict[str, Any]]:
    """Require high-confidence Luna evidence for bounded-effect work."""

    if judgment not in LANES:
        raise RoutingCatalogError("routing judgment is invalid")
    if judgment == "routine" or fixed_model is not None:
        return candidates
    return [
        candidate
        for candidate in candidates
        if not _luna_family(candidate["model"])
        or candidate["iq_lower_95"] > minimum_iq
    ]


def iq_standard_error(candidate: dict[str, Any]) -> float:
    """Retained for decision compatibility; Wilson uncertainty drives selection."""
    probability = candidate["passed"] / candidate["valid_tasks"]
    return 150.0 * math.sqrt(
        probability * (1.0 - probability) / candidate["valid_tasks"]
    )


def route_dimensions(
    lane: str | None,
    *,
    purpose: str | None,
    judgment: str | None,
) -> tuple[str, str]:
    resolved_purpose = "implementation" if purpose is None else purpose
    resolved_judgment = lane if judgment is None else judgment
    if resolved_purpose not in PURPOSES:
        raise RoutingCatalogError(f"unsupported purpose: {resolved_purpose}")
    if resolved_judgment not in LANES:
        raise RoutingCatalogError(f"unsupported judgment: {resolved_judgment}")
    if lane is not None and lane != resolved_judgment:
        raise RoutingCatalogError("lane and judgment must match")
    return resolved_purpose, resolved_judgment


def parse_placement_benefits(values: list[str] | None) -> list[dict[str, Any]]:
    """Parse repeatable ``kind=evidence`` CLI facts into one canonical list."""

    grouped: dict[str, set[str]] = {}
    for value in values or []:
        if not isinstance(value, str) or "=" not in value:
            raise RoutingCatalogError(
                "placement benefit must use kind=evidence"
            )
        kind, evidence = value.split("=", 1)
        if not kind or not evidence:
            raise RoutingCatalogError(
                "placement benefit must use kind=evidence"
            )
        grouped.setdefault(kind, set()).add(evidence)
    candidate = [
        {"evidence": sorted(grouped[kind]), "kind": kind}
        for kind in sorted(grouped)
    ]
    try:
        return normalize_placement_benefits(candidate)
    except DecisionPolicyError as error:
        raise RoutingCatalogError(str(error)) from error


def routing_policy(
    judgment: str,
    overrides: dict[str, float] | None,
    *,
    purpose: str = "implementation",
) -> dict[str, float]:
    if purpose not in PURPOSES or judgment not in LANES:
        raise RoutingCatalogError("routing purpose or judgment is invalid")
    policy = {**POLICY_COMMON, **DEFAULT_POLICIES[purpose][judgment]}
    if overrides is not None:
        unknown = set(overrides) - set(policy)
        if unknown:
            raise RoutingCatalogError(
                f"unknown routing policy field: {sorted(unknown)[0]}"
            )
        policy.update(overrides)
    for field, value in policy.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RoutingCatalogError(f"routing policy {field} must be numeric")
        policy[field] = float(value)
        if not math.isfinite(policy[field]) or policy[field] < 0:
            raise RoutingCatalogError(f"routing policy {field} is out of range")
    if abs(
        policy["quality_weight"]
        + policy["cost_weight"]
        + policy["time_weight"]
        - 1.0
    ) > 1e-9:
        raise RoutingCatalogError("quality, cost, and time weights must sum to 1")
    if policy["cost_anchor_usd"] <= 0 or policy["time_anchor_minutes"] <= 0:
        raise RoutingCatalogError("routing policy anchors must be positive")
    if not MIN_IQ <= policy["minimum_iq_exclusive"] < 150.0:
        raise RoutingCatalogError("minimum_iq_exclusive must be at least 90 and below 150")
    return policy


def policy_sha256(
    judgment: str,
    policy: dict[str, float],
    *,
    assurance: str,
    purpose: str,
    fixed_model: str | None,
    fixed_effort: str | None,
    primary_model: str | None,
    primary_effort: str | None,
) -> str:
    preimage = {
        "assurance": assurance,
        "fixed_effort": fixed_effort,
        "fixed_model": fixed_model,
        "judgment": judgment,
        "policy": policy,
        "primary_effort": primary_effort,
        "primary_model": primary_model,
        "purpose": purpose,
    }
    return "sha256:" + hashlib.sha256(
        POLICY_DOMAIN + canonical_bytes(preimage)
    ).hexdigest()


def placement_context(
    *,
    primary_model: str | None,
    primary_effort: str | None,
    benefits: object,
) -> dict[str, Any]:
    if primary_model is not None and (
        not isinstance(primary_model, str) or MODEL_RE.fullmatch(primary_model) is None
    ):
        raise RoutingCatalogError("primary_model must be a valid model identifier")
    if primary_effort is not None and (
        not isinstance(primary_effort, str)
        or EFFORT_RE.fullmatch(primary_effort) is None
    ):
        raise RoutingCatalogError("primary_effort must be a valid effort identifier")
    try:
        normalized = normalize_placement_benefits(benefits)
    except DecisionPolicyError as error:
        raise RoutingCatalogError(str(error)) from error
    return {
        "benefits": normalized,
        "primary_effort": primary_effort,
        "primary_model": primary_model,
    }


def select_placement(
    selected: dict[str, Any], *, purpose: str, context: dict[str, Any]
) -> dict[str, str]:
    try:
        return select_policy_placement(
            purpose=purpose,
            primary_model=context["primary_model"],
            selected_model=selected.get("model"),
            benefits=context["benefits"],
        )
    except (DecisionPolicyError, KeyError) as error:
        raise RoutingCatalogError("placement context is invalid") from error


def candidate_net_value(
    candidate: dict[str, Any], policy: dict[str, float]
) -> float:
    minimum_iq = policy["minimum_iq_exclusive"]
    quality_utility = (
        candidate["iq_lower_95"] - minimum_iq
    ) / (150.0 - minimum_iq)
    cost_burden = (
        math.log1p(candidate["average_price_usd"])
        / math.log1p(policy["cost_anchor_usd"])
    )
    time_burden = candidate["average_minutes"] / policy["time_anchor_minutes"]
    missing_metric_fraction = (
        2.0 - candidate["price_coverage"] - candidate["duration_coverage"]
    ) / 2.0
    uncertainty_burden = (
        candidate["iq_uncertainty_95"] / 150.0
        + missing_metric_fraction
    )
    return (
        policy["quality_weight"] * quality_utility
        - policy["cost_weight"] * cost_burden
        - policy["time_weight"] * time_burden
        - policy["uncertainty_weight"] * uncertainty_burden
    )


def selected_record(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": candidate["model"],
        "effort": candidate["effort"],
        "iq": candidate["iq"],
        "average_price_usd": candidate["average_price_usd"],
        "average_minutes": candidate["average_minutes"],
    }


def scored_record(candidate: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "model",
        "effort",
        "iq",
        "passed",
        "valid_tasks",
        "average_price_usd",
        "price_samples",
        "average_minutes",
        "duration_samples",
        "incomplete_cost_samples",
        "iq_lower_95",
        "iq_upper_95",
        "iq_uncertainty_95",
        "price_coverage",
        "duration_coverage",
        "iq_standard_error",
        "net_value",
    )
    return {field: candidate[field] for field in fields}


def score_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate["net_value"],
        candidate["iq_lower_95"],
        candidate["iq"],
        -candidate["average_price_usd"],
        -candidate["average_minutes"],
        candidate["model"],
        candidate["effort"],
    )


def select_recommended_candidate(
    frontier: list[dict[str, Any]],
    *,
    purpose: str,
    context: dict[str, Any],
    fixed_model: str | None,
    fixed_effort: str | None,
) -> dict[str, Any]:
    """Select one deterministic winner, softly avoiding an exact review duplicate."""

    winner = max(frontier, key=score_sort_key)
    primary_pair = (context.get("primary_model"), context.get("primary_effort"))
    winner_pair = (winner["model"], winner["effort"])
    if (
        purpose != "acceptance"
        or None in primary_pair
        or winner_pair != primary_pair
        or (fixed_model is not None and fixed_effort is not None)
    ):
        return winner
    alternatives = [
        candidate
        for candidate in frontier
        if (candidate["model"], candidate["effort"]) != primary_pair
    ]
    if not alternatives:
        return winner
    alternative = max(alternatives, key=score_sort_key)
    intervals_overlap = (
        alternative["iq_lower_95"] <= winner["iq_upper_95"]
        and winner["iq_lower_95"] <= alternative["iq_upper_95"]
    )
    net_value_gap = winner["net_value"] - alternative["net_value"]
    if intervals_overlap and net_value_gap <= SWITCH_MARGIN:
        return alternative
    return winner


def _fixed_route_hash(label: str, pair: tuple[str, str]) -> str:
    """Create a deterministic sentinel hash for a Radar-free fixed route."""

    return "sha256:" + hashlib.sha256(
        b"cco.routing-fixed.v1\0"
        + canonical_bytes({"label": label, "model": pair[0], "effort": pair[1]})
    ).hexdigest()


def _fixed_user_pair_decision(
    *,
    native_catalog: object,
    assurance: str,
    purpose: str,
    judgment: str,
    fixed_model: str,
    fixed_effort: str,
    now: datetime,
    policy_overrides: dict[str, float] | None,
    primary_model: str | None,
    primary_effort: str | None,
    placement_benefits: object,
) -> dict[str, Any]:
    """Build a compact capability-only decision for a fully fixed user pair."""

    capabilities = native_capabilities(native_catalog)
    pair = (fixed_model, fixed_effort)
    if pair not in capabilities:
        raise RoutingCatalogError(
            "fixed user route is not supported by the native model catalog"
        )
    policy = routing_policy(judgment, policy_overrides, purpose=purpose)
    context = placement_context(
        primary_model=primary_model,
        primary_effort=primary_effort,
        benefits=[] if placement_benefits is None else placement_benefits,
    )
    selected = {"model": fixed_model, "effort": fixed_effort}
    decision = {
        "assurance": assurance,
        "protocol": ROUTE_DECISION_PROTOCOL,
        "lane": judgment,
        "purpose": purpose,
        "judgment": judgment,
        "snapshot_sha256": _fixed_route_hash("snapshot", pair),
        "measurement_sha256": _fixed_route_hash("measurement", pair),
        "source_updated_at": now.astimezone(timezone.utc).isoformat(),
        "eligible_count": 1,
        "selection_method": "fixed_user_pair_native_capability",
        "native_catalog_sha256": native_catalog_sha256(native_catalog),
        "native_catalog_source": "codex debug models --bundled",
        "constraints": {"fixed_effort": fixed_effort, "fixed_model": fixed_model},
        "policy": policy,
        "policy_sha256": policy_sha256(
            judgment,
            policy,
            assurance=assurance,
            purpose=purpose,
            fixed_model=fixed_model,
            fixed_effort=fixed_effort,
            primary_model=primary_model,
            primary_effort=primary_effort,
        ),
        "eligible_candidates": [dict(selected)],
        "pareto_frontier": [dict(selected)],
        "fallback_order": [dict(selected)],
        "selected": dict(selected),
        "recommended": dict(selected),
        "placement_context": context,
        "placement": select_placement(selected, purpose=purpose, context=context),
        "hysteresis": {
            "status": "fixed",
            "pending_count": 0,
            "required_winning_snapshots": 0,
        },
        "dispatch": {
            "rank": 1,
            "reason": "selected",
            "rejection_tickets": [],
            "rejected_routes": [],
        },
    }
    decision["decision_sha256"] = route_decision_sha256(decision)
    return decision


def resolve_route(
    radar_payload: object,
    native_catalog: object,
    lane: str | None = None,
    *,
    assurance: str = "deterministic",
    purpose: str | None = None,
    judgment: str | None = None,
    now: datetime | None = None,
    policy_overrides: dict[str, float] | None = None,
    fixed_model: str | None = None,
    fixed_effort: str | None = None,
    primary_model: str | None = None,
    primary_effort: str | None = None,
    placement_benefits: object = None,
) -> dict[str, Any]:
    resolved_purpose, resolved_judgment = route_dimensions(
        lane, purpose=purpose, judgment=judgment
    )
    if assurance not in ROUTE_ASSURANCES:
        raise RoutingCatalogError("route assurance is invalid")
    current = now or datetime.now(timezone.utc)
    # A fully user-fixed pair is a native capability assertion.  It must not
    # fetch, parse, score, or otherwise depend on Radar availability.
    if fixed_model is not None and fixed_effort is not None:
        for value, label, pattern in (
            (fixed_model, "fixed_model", MODEL_RE),
            (fixed_effort, "fixed_effort", EFFORT_RE),
        ):
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise RoutingCatalogError(f"{label} must be a non-empty string")
        return _fixed_user_pair_decision(
            native_catalog=native_catalog,
            assurance=assurance,
            purpose=resolved_purpose,
            judgment=resolved_judgment,
            fixed_model=fixed_model,
            fixed_effort=fixed_effort,
            now=current,
            policy_overrides=policy_overrides,
            primary_model=primary_model,
            primary_effort=primary_effort,
            placement_benefits=placement_benefits,
        )
    if isinstance(radar_payload, dict) and radar_payload.get("protocol") == PROTOCOL:
        snapshot = validate_normalized_snapshot(radar_payload, now=current)
    else:
        snapshot = normalize_snapshot(radar_payload, now=current)
    for value, label, pattern in (
        (fixed_model, "fixed_model", MODEL_RE),
        (fixed_effort, "fixed_effort", EFFORT_RE),
    ):
        if value is not None and (
            not isinstance(value, str) or pattern.fullmatch(value) is None
        ):
            raise RoutingCatalogError(f"{label} must be a non-empty string")
    capabilities = native_capabilities(native_catalog)
    policy = routing_policy(
        resolved_judgment, policy_overrides, purpose=resolved_purpose
    )
    qualified = [
        point
        for point in snapshot["points"]
        if point["iq"] > policy["minimum_iq_exclusive"]
        and point["valid_tasks"] >= MIN_SAMPLES
        and point["price_samples"] >= MIN_SAMPLES
        and point["duration_samples"] >= MIN_SAMPLES
        and (point["model"], point["effort"]) in capabilities
        and (fixed_model is None or point["model"] == fixed_model)
        and (fixed_effort is None or point["effort"] == fixed_effort)
    ]
    if not qualified:
        raise RoutingCatalogError("no native radar candidate satisfies the routing gates")
    cohort_reference = max(point["valid_tasks"] for point in qualified)
    eligible = [
        enrich_candidate(point)
        for point in qualified
        if point["valid_tasks"] / cohort_reference >= MIN_COHORT_RATIO
        and point["price_samples"] / point["valid_tasks"] >= MIN_METRIC_COVERAGE
        and point["duration_samples"] / point["valid_tasks"] >= MIN_METRIC_COVERAGE
    ]
    if not eligible:
        raise RoutingCatalogError("no candidate has sufficiently comparable metric coverage")
    eligible = apply_route_assurance_gate(
        eligible,
        assurance=assurance,
        fixed_model=fixed_model,
    )
    if not eligible:
        raise RoutingCatalogError("no candidate satisfies the route assurance gate")
    eligible = apply_judgment_confidence_gate(
        eligible,
        judgment=resolved_judgment,
        fixed_model=fixed_model,
        minimum_iq=policy["minimum_iq_exclusive"],
    )
    if not eligible:
        raise RoutingCatalogError("no candidate satisfies the judgment confidence gate")
    eligible = apply_model_preference_gate(eligible, fixed_model=fixed_model)
    if not eligible:
        raise RoutingCatalogError("no candidate satisfies the model preference gate")
    frontier = pareto_frontier(eligible)
    if not frontier:
        raise RoutingCatalogError("strict Pareto selection produced no candidates")
    scored = [
        {
            **candidate,
            "iq_standard_error": round(iq_standard_error(candidate), 6),
            "net_value": round(candidate_net_value(candidate, policy), 9),
        }
        for candidate in frontier
    ]
    eligible_scored = [
        {
            **candidate,
            "iq_standard_error": round(iq_standard_error(candidate), 6),
            "net_value": round(candidate_net_value(candidate, policy), 9),
        }
        for candidate in eligible
    ]
    dispatch_context = placement_context(
        primary_model=primary_model,
        primary_effort=primary_effort,
        benefits=[] if placement_benefits is None else placement_benefits,
    )
    selected = select_recommended_candidate(
        scored,
        purpose=resolved_purpose,
        context=dispatch_context,
        fixed_model=fixed_model,
        fixed_effort=fixed_effort,
    )
    ranked = [selected, *sorted(
        [candidate for candidate in scored if candidate is not selected],
        key=score_sort_key,
        reverse=True,
    )]
    policy_identity = policy_sha256(
        resolved_judgment,
        policy,
        assurance=assurance,
        purpose=resolved_purpose,
        fixed_model=fixed_model,
        fixed_effort=fixed_effort,
        primary_model=primary_model,
        primary_effort=primary_effort,
    )
    decision = {
        "assurance": assurance,
        "protocol": ROUTE_DECISION_PROTOCOL,
        "lane": resolved_judgment,
        "purpose": resolved_purpose,
        "judgment": resolved_judgment,
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "measurement_sha256": snapshot["measurement_sha256"],
        "source_updated_at": snapshot["source_updated_at"],
        "eligible_count": len(eligible),
        "selection_method": "strict_pareto_fixed_anchor_mcda",
        "native_catalog_sha256": native_catalog_sha256(native_catalog),
        "native_catalog_source": "codex debug models --bundled",
        "constraints": {
            "fixed_effort": fixed_effort,
            "fixed_model": fixed_model,
        },
        "policy": policy,
        "policy_sha256": policy_identity,
        "eligible_candidates": [
            scored_record(candidate)
            for candidate in sorted(
                eligible_scored,
                key=lambda item: (item["model"], item["effort"]),
            )
        ],
        "pareto_frontier": [
            scored_record(candidate)
            for candidate in sorted(
                scored,
                key=lambda item: (
                    item["average_price_usd"],
                    item["average_minutes"],
                    -item["iq"],
                    item["model"],
                    item["effort"],
                ),
            )
        ],
        "fallback_order": [
            {"effort": candidate["effort"], "model": candidate["model"]}
            for candidate in ranked[:MAX_FALLBACK_CANDIDATES]
        ],
        "selected": selected_record(selected),
        "placement_context": dispatch_context,
        "placement": select_placement(
            selected_record(selected),
            purpose=resolved_purpose,
            context=dispatch_context,
        ),
    }
    decision["decision_sha256"] = route_decision_sha256(decision)
    return decision


def _normalize_route_plan_requests(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RoutingCatalogError("route plan requests must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str]] = set()
    allowed = {
        "assurance",
        "fixed_effort",
        "fixed_model",
        "judgment",
        "placement_benefits",
        "primary_effort",
        "primary_model",
        "purpose",
    }
    for index, request in enumerate(value):
        if not isinstance(request, dict) or not set(request) <= allowed:
            raise RoutingCatalogError(f"route plan request {index} is malformed")
        purpose, judgment = route_dimensions(
            None,
            purpose=request.get("purpose"),
            judgment=request.get("judgment"),
        )
        if "assurance" not in request:
            raise RoutingCatalogError(
                f"route plan request {index} requires derived assurance"
            )
        assurance = request["assurance"]
        if assurance not in ROUTE_ASSURANCES:
            raise RoutingCatalogError(f"route plan request {index} assurance is invalid")
        key = (purpose, judgment, assurance)
        if key in keys:
            raise RoutingCatalogError("route plan contains a duplicate route key")
        keys.add(key)
        normalized.append(
            {
                "assurance": assurance,
                "fixed_effort": request.get("fixed_effort"),
                "fixed_model": request.get("fixed_model"),
                "judgment": judgment,
                "placement_benefits": request.get("placement_benefits"),
                "primary_effort": request.get("primary_effort"),
                "primary_model": request.get("primary_model"),
                "purpose": purpose,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (item["purpose"], item["judgment"], item["assurance"]),
    )


def _compact_route(decision: dict[str, Any]) -> dict[str, Any]:
    candidates = decision.get("fallback_order")
    selected = decision.get("selected")
    if not isinstance(candidates, list) or not isinstance(selected, dict):
        raise RoutingCatalogError("route decision cannot be compacted")
    return {
        "assurance": decision["assurance"],
        "candidates": [dict(candidate) for candidate in candidates[:3]],
        "decision_sha256": decision["decision_sha256"],
        "dispatch": {"rank": 1, "rejection_tickets": []},
        "fixed": decision.get("selection_method") == "fixed_user_pair_native_capability",
        "judgment": decision["judgment"],
        "placement": decision["placement"],
        "purpose": decision["purpose"],
        "selected": {"effort": selected["effort"], "model": selected["model"]},
    }


def _resolve_route_plan_with_state(
    requests: object,
    radar_payload: object,
    native_catalog: object,
    *,
    now: datetime | None = None,
    needs_refresh: bool = False,
    state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve several purpose/judgment/assurance routes in one local batch."""

    current = now or datetime.now(timezone.utc)
    normalized_requests = _normalize_route_plan_requests(requests)
    capabilities = native_capability_records(native_catalog)
    normalized_native = {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [
                    {"effort": item["effort"]}
                    for item in capabilities
                    if item["model"] == model
                ],
            }
            for model in sorted({item["model"] for item in capabilities})
        ]
    }
    needs_radar = any(
        request["fixed_model"] is None or request["fixed_effort"] is None
        for request in normalized_requests
    )
    if needs_radar:
        normalized_radar = (
            validate_normalized_snapshot(radar_payload, now=current)
            if isinstance(radar_payload, dict)
            and radar_payload.get("protocol") == PROTOCOL
            else normalize_snapshot(radar_payload, now=current)
        )
    else:
        normalized_radar = radar_payload
    routes = []
    next_state = state
    for request in normalized_requests:
        decision = resolve_route(
            normalized_radar,
            normalized_native,
            assurance=request["assurance"],
            purpose=request["purpose"],
            judgment=request["judgment"],
            fixed_model=request["fixed_model"],
            fixed_effort=request["fixed_effort"],
            primary_model=request["primary_model"],
            primary_effort=request["primary_effort"],
            placement_benefits=request["placement_benefits"],
            now=current,
        )
        decision, next_state = stabilize_route(decision, next_state)
        routes.append(_compact_route(decision))
    plan = {
        "native_catalog_sha256": native_catalog_sha256(normalized_native),
        "needs_refresh": bool(needs_refresh),
        "protocol": ROUTE_PLAN_PROTOCOL,
        "routes": routes,
    }
    plan["plan_sha256"] = route_plan_sha256(plan)
    if next_state is None:  # requests is non-empty, kept for type narrowing
        next_state = {"protocol": STATE_PROTOCOL, "lanes": {}}
    return plan, next_state


def resolve_route_plan(
    requests: object,
    radar_payload: object,
    native_catalog: object,
    *,
    now: datetime | None = None,
    needs_refresh: bool = False,
) -> dict[str, Any]:
    """Resolve several purpose/judgment/assurance routes in one local batch."""

    plan, _state = _resolve_route_plan_with_state(
        requests,
        radar_payload,
        native_catalog,
        now=now,
        needs_refresh=needs_refresh,
    )
    return plan


def resolve_graph_route_plan(
    cache_dir: Path,
    requests: object,
    *,
    now: datetime | None = None,
    refresh_interval: timedelta = REFRESH_INTERVAL,
    fetcher: Callable[[str | None], FetchResult] = fetch_radar,
    native_loader: Callable[[], dict[str, Any]] | None = None,
    scheduler: Callable[[list[str]], Any] = launch_radar_refresh,
) -> tuple[dict[str, Any], LoadedSnapshot]:
    """Load shared routing inputs once and resolve a whole graph route batch."""

    current = now or datetime.now(timezone.utc)
    normalized_requests = _normalize_route_plan_requests(requests)
    directory = Path(os.path.abspath(Path(cache_dir).expanduser()))
    directory.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(directory)
    all_fixed = all(
        request["fixed_model"] is not None and request["fixed_effort"] is not None
        for request in normalized_requests
    )
    loaded = (
        LoadedSnapshot({}, "not_required", current)
        if all_fixed
        else load_radar_snapshot(
            directory,
            now=current,
            fetcher=fetcher,
            refresh_interval=refresh_interval,
        )
    )
    native_catalog = (
        native_loader() if native_loader is not None else load_native_catalog(directory)
    )
    state_file = directory / STATE_FILENAME
    try:
        with routing_lock(directory, now=current):
            plan, next_state = _resolve_route_plan_with_state(
                normalized_requests,
                loaded.snapshot,
                native_catalog,
                now=current,
                needs_refresh=loaded.needs_refresh,
                state=read_routing_state(state_file),
            )
            write_routing_state(state_file, next_state)
            cleanup_stale_temps(directory, now=current)
    except RoutingCacheBusy:
        # State only dampens route churn.  It must never turn a concurrent graph
        # creation into polling or a blocked dispatch path.
        plan = resolve_route_plan(
            normalized_requests,
            loaded.snapshot,
            native_catalog,
            now=current,
            needs_refresh=loaded.needs_refresh,
        )
    schedule_radar_refresh(directory, loaded, scheduler=scheduler)
    return plan, loaded


def routing_state_key(purpose: str, judgment: str, assurance: str) -> str:
    if purpose not in PURPOSES or judgment not in LANES or assurance not in ROUTE_ASSURANCES:
        raise RoutingCatalogError("routing state key is invalid")
    return f"{purpose}:{judgment}:{assurance}"


def stabilize_route(
    recommendation: dict[str, Any], state: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    lane = recommendation.get("lane")
    purpose = recommendation.get("purpose")
    judgment = recommendation.get("judgment")
    assurance = recommendation.get("assurance")
    if lane not in LANES:
        raise RoutingCatalogError("route recommendation lane is invalid")
    if purpose not in PURPOSES or judgment != lane or assurance not in ROUTE_ASSURANCES:
        raise RoutingCatalogError("route recommendation purpose or judgment is invalid")
    if recommendation.get("decision_sha256") != route_decision_sha256(recommendation):
        raise RoutingCatalogError("route recommendation hash mismatch")
    if recommendation.get("selection_method") == "fixed_user_pair_native_capability":
        selected = recommendation.get("selected")
        if (
            not isinstance(selected, dict)
            or set(selected) != {"model", "effort"}
            or not isinstance(recommendation.get("placement_context"), dict)
        ):
            raise RoutingCatalogError("fixed route recommendation is malformed")
        if state is None:
            next_state: dict[str, Any] = {"protocol": STATE_PROTOCOL, "lanes": {}}
        else:
            next_state = json.loads(json.dumps(validate_routing_state(state)))
        state_key = routing_state_key(purpose, lane, assurance)
        next_state["lanes"][state_key] = {
            "active": {"model": selected["model"], "effort": selected["effort"]},
            "policy_sha256": recommendation["policy_sha256"],
        }
        decision = {
            key: value
            for key, value in recommendation.items()
            if key != "decision_sha256"
        }
        decision["recommended"] = dict(selected)
        decision["selected"] = dict(selected)
        decision["hysteresis"] = {
            "status": "fixed",
            "pending_count": 0,
            "required_winning_snapshots": 0,
        }
        decision["fallback_order"] = [dict(selected)]
        decision["dispatch"] = {
            "rank": 1,
            "reason": "selected",
            "rejection_tickets": [],
            "rejected_routes": [],
        }
        decision["decision_sha256"] = route_decision_sha256(decision)
        return decision, next_state
    frontier = recommendation.get("pareto_frontier")
    eligible = recommendation.get("eligible_candidates")
    selected = recommendation.get("selected")
    if (
        not isinstance(frontier, list)
        or not isinstance(eligible, list)
        or not isinstance(selected, dict)
    ):
        raise RoutingCatalogError("route recommendation is malformed")
    frontier_candidates = {
        (candidate.get("model"), candidate.get("effort")): candidate
        for candidate in frontier
        if isinstance(candidate, dict)
    }
    eligible_candidates = {
        (candidate.get("model"), candidate.get("effort")): candidate
        for candidate in eligible
        if isinstance(candidate, dict)
    }
    recommended_pair = (selected.get("model"), selected.get("effort"))
    recommended_candidate = frontier_candidates.get(recommended_pair)
    if recommended_candidate is None:
        raise RoutingCatalogError("recommended route is outside the Pareto frontier")
    if state is None:
        next_state: dict[str, Any] = {"protocol": STATE_PROTOCOL, "lanes": {}}
    else:
        next_state = json.loads(json.dumps(validate_routing_state(state)))
    state_key = routing_state_key(purpose, lane, assurance)
    lane_state = next_state["lanes"].get(state_key)
    status = "initialized"
    actual_candidate = recommended_candidate
    if isinstance(lane_state, dict):
        active = lane_state.get("active")
        active_pair = (
            active.get("model") if isinstance(active, dict) else None,
            active.get("effort") if isinstance(active, dict) else None,
        )
        active_candidate = eligible_candidates.get(active_pair)
        prior_policy = lane_state.get("policy_sha256")
        if prior_policy != recommendation.get("policy_sha256"):
            status = "switched_policy"
        elif active_pair == recommended_pair:
            status = "stable"
        elif active_candidate is None:
            status = "switched_ineligible"
        else:
            value_delta = float(recommended_candidate["net_value"]) - float(
                active_candidate["net_value"]
            )
            if value_delta < SWITCH_MARGIN:
                status = "stable_margin"
                actual_candidate = active_candidate
            else:
                status = "switched"
    next_state["lanes"][state_key] = {
        "active": {
            "model": actual_candidate["model"],
            "effort": actual_candidate["effort"],
        },
        "policy_sha256": recommendation["policy_sha256"],
    }
    decision = {
        key: value
        for key, value in recommendation.items()
        if key != "decision_sha256"
    }
    decision["recommended"] = recommendation["selected"]
    decision["selected"] = selected_record(actual_candidate)
    context = decision.get("placement_context")
    if not isinstance(context, dict):
        raise RoutingCatalogError("route recommendation placement context is invalid")
    decision["placement"] = select_placement(
        decision["selected"], purpose=purpose, context=context
    )
    decision["hysteresis"] = {
        "status": status,
        "pending_count": 0,
        "required_winning_snapshots": REQUIRED_WINNING_SNAPSHOTS,
    }
    selected_pair = (actual_candidate["model"], actual_candidate["effort"])
    fallback = [
        item
        for item in recommendation.get("fallback_order", [])
        if (item.get("model"), item.get("effort")) != selected_pair
    ]
    decision["fallback_order"] = [
        {"effort": actual_candidate["effort"], "model": actual_candidate["model"]},
        *fallback[: MAX_FALLBACK_CANDIDATES - 1],
    ]
    decision["dispatch"] = {
        "rank": 1,
        "reason": "selected",
        "rejection_tickets": [],
        "rejected_routes": [],
    }
    decision["decision_sha256"] = route_decision_sha256(decision)
    return decision, next_state


def render_resolution(decision: dict[str, Any], *, explain: bool) -> dict[str, Any]:
    selected = decision.get("selected")
    if not isinstance(selected, dict):
        raise RoutingCatalogError("routing decision has no selected route")
    quiet = {
        "candidates": [
            {"effort": item.get("effort"), "model": item.get("model")}
            for item in decision.get("fallback_order", [])[:3]
            if isinstance(item, dict)
        ],
        "decision_sha256": decision.get("decision_sha256"),
        "effort": selected.get("effort"),
        "judgment": decision.get("judgment"),
        "lane": decision.get("lane"),
        "model": selected.get("model"),
        "placement": decision.get("placement"),
        "purpose": decision.get("purpose"),
    }
    if not explain:
        return quiet
    quiet["assurance"] = decision.get("assurance")
    eligible = decision.get("eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        raise RoutingCatalogError("routing decision has no eligible candidates")
    reference = min(
        eligible,
        key=lambda candidate: (
            candidate["average_price_usd"],
            candidate["average_minutes"],
            -candidate["iq"],
            candidate["model"],
            candidate["effort"],
        ),
    )
    selected_scored = next(
        (
            candidate
            for candidate in eligible
            if candidate["model"] == selected["model"]
            and candidate["effort"] == selected["effort"]
        ),
        None,
    )
    if selected_scored is None:
        raise RoutingCatalogError("selected route is outside eligible candidates")
    return {
        **quiet,
        "eligible_candidates": eligible,
        "tradeoffs": {
            "reference": selected_record(reference),
            "selected_minus_reference": {
                "cost_usd": round(
                    selected_scored["average_price_usd"]
                    - reference["average_price_usd"],
                    6,
                ),
                "iq": round(selected_scored["iq"] - reference["iq"], 6),
                "minutes": round(
                    selected_scored["average_minutes"]
                    - reference["average_minutes"],
                    6,
                ),
                "net_value": round(
                    selected_scored["net_value"] - reference["net_value"], 9
                ),
            },
        },
    }


def validate_route_decision(
    value: object,
    *,
    assurance: str | None = None,
    lane: str | None = None,
    purpose: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    required = {
        "assurance",
        "constraints",
        "decision_sha256",
        "dispatch",
        "eligible_candidates",
        "eligible_count",
        "fallback_order",
        "hysteresis",
        "judgment",
        "lane",
        "measurement_sha256",
        "native_catalog_sha256",
        "native_catalog_source",
        "pareto_frontier",
        "placement",
        "placement_context",
        "policy",
        "policy_sha256",
        "protocol",
        "purpose",
        "recommended",
        "selected",
        "selection_method",
        "snapshot_sha256",
        "source_updated_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RoutingCatalogError("routing decision schema is malformed")
    if value.get("protocol") != ROUTE_DECISION_PROTOCOL:
        raise RoutingCatalogError("routing decision protocol is invalid")
    decision_lane = value.get("lane")
    if decision_lane not in LANES or (lane is not None and decision_lane != lane):
        raise RoutingCatalogError("routing decision lane is invalid")
    decision_purpose = value.get("purpose")
    judgment = value.get("judgment")
    if (
        decision_purpose not in PURPOSES
        or judgment != decision_lane
        or (purpose is not None and decision_purpose != purpose)
    ):
        raise RoutingCatalogError("routing decision purpose or judgment is invalid")
    decision_assurance = value.get("assurance")
    if (
        decision_assurance not in ROUTE_ASSURANCES
        or (assurance is not None and decision_assurance != assurance)
    ):
        raise RoutingCatalogError("routing decision assurance is invalid")
    for key in (
        "decision_sha256",
        "measurement_sha256",
        "native_catalog_sha256",
        "policy_sha256",
        "snapshot_sha256",
    ):
        if not isinstance(value.get(key), str) or SHA256_RE.fullmatch(value[key]) is None:
            raise RoutingCatalogError(f"routing decision {key} is invalid")
    if value["decision_sha256"] != route_decision_sha256(value):
        raise RoutingCatalogError("routing decision hash mismatch")
    if value.get("native_catalog_source") != "codex debug models --bundled":
        raise RoutingCatalogError("routing decision native catalog source is invalid")
    if value.get("selection_method") == "fixed_user_pair_native_capability":
        constraints = value.get("constraints")
        selected = value.get("selected")
        recommended = value.get("recommended")
        fallback = value.get("fallback_order")
        candidates = value.get("eligible_candidates")
        frontier = value.get("pareto_frontier")
        if (
            not isinstance(constraints, dict)
            or set(constraints) != {"fixed_effort", "fixed_model"}
            or not isinstance(constraints["fixed_model"], str)
            or not isinstance(constraints["fixed_effort"], str)
            or not isinstance(selected, dict)
            or set(selected) != {"model", "effort"}
            or selected.get("model") != constraints.get("fixed_model")
            or selected.get("effort") != constraints.get("fixed_effort")
            or recommended != selected
            or not isinstance(fallback, list)
            or fallback != [selected]
            or value.get("eligible_count") != 1
            or candidates != [selected]
            or frontier != [selected]
        ):
            raise RoutingCatalogError("fixed route decision is malformed")
        for key, pattern in (("fixed_model", MODEL_RE), ("fixed_effort", EFFORT_RE)):
            if pattern.fullmatch(constraints[key]) is None:
                raise RoutingCatalogError("fixed route constraint is malformed")
        context = value.get("placement_context")
        if not isinstance(context, dict) or set(context) != {
            "benefits",
            "primary_effort",
            "primary_model",
        }:
            raise RoutingCatalogError("fixed route placement context is malformed")
        expected_context = placement_context(
            primary_model=context["primary_model"],
            primary_effort=context["primary_effort"],
            benefits=context["benefits"],
        )
        if context != expected_context:
            raise RoutingCatalogError("fixed route placement context is invalid")
        policy_value = value.get("policy")
        if not isinstance(policy_value, dict):
            raise RoutingCatalogError("fixed route policy is malformed")
        policy = routing_policy(decision_lane, policy_value, purpose=decision_purpose)
        expected_policy_sha = policy_sha256(
            decision_lane,
            policy,
            assurance=decision_assurance,
            purpose=decision_purpose,
            fixed_model=constraints["fixed_model"],
            fixed_effort=constraints["fixed_effort"],
            primary_model=context["primary_model"],
            primary_effort=context["primary_effort"],
        )
        if value["policy_sha256"] != expected_policy_sha:
            raise RoutingCatalogError("fixed route policy hash mismatch")
        hysteresis = value.get("hysteresis")
        if hysteresis != {
            "pending_count": 0,
            "required_winning_snapshots": 0,
            "status": "fixed",
        }:
            raise RoutingCatalogError("fixed route hysteresis is malformed")
        if value.get("placement") != select_placement(
            selected, purpose=decision_purpose, context=context
        ):
            raise RoutingCatalogError("fixed route placement is invalid")
        dispatch = value.get("dispatch")
        if dispatch != {
            "rank": 1,
            "reason": "selected",
            "rejection_tickets": [],
            "rejected_routes": [],
        }:
            raise RoutingCatalogError("fixed route dispatch state is malformed")
        if model is not None and selected["model"] != model:
            raise RoutingCatalogError("routing decision model does not match dispatch")
        if effort is not None and selected["effort"] != effort:
            raise RoutingCatalogError("routing decision effort does not match dispatch")
        return value
    if value.get("selection_method") != "strict_pareto_fixed_anchor_mcda":
        raise RoutingCatalogError("routing decision selection method is invalid")
    timestamp = parse_timestamp(value.get("source_updated_at"), "source_updated_at")
    if now is not None:
        current = now.astimezone(timezone.utc)
        if timestamp - current > MAX_FUTURE_SKEW or current - timestamp > MAX_SOURCE_AGE:
            raise RoutingCatalogError("routing decision source timestamp is out of range")
    constraints = value.get("constraints")
    if not isinstance(constraints, dict) or set(constraints) != {
        "fixed_effort",
        "fixed_model",
    }:
        raise RoutingCatalogError("routing decision constraints are malformed")
    for key, pattern in (("fixed_model", MODEL_RE), ("fixed_effort", EFFORT_RE)):
        fixed = constraints[key]
        if fixed is not None and (
            not isinstance(fixed, str) or pattern.fullmatch(fixed) is None
        ):
            raise RoutingCatalogError("routing decision constraint is malformed")
    context = value.get("placement_context")
    if not isinstance(context, dict) or set(context) != {
        "benefits",
        "primary_effort",
        "primary_model",
    }:
        raise RoutingCatalogError("routing decision placement context is malformed")
    expected_context = placement_context(
        primary_model=context["primary_model"],
        primary_effort=context["primary_effort"],
        benefits=context["benefits"],
    )
    if context != expected_context:
        raise RoutingCatalogError("routing decision placement context is invalid")
    policy_value = value.get("policy")
    if not isinstance(policy_value, dict):
        raise RoutingCatalogError("routing decision policy is malformed")
    policy = routing_policy(decision_lane, policy_value, purpose=decision_purpose)
    expected_policy_sha = policy_sha256(
        decision_lane,
        policy,
        assurance=decision_assurance,
        purpose=decision_purpose,
        fixed_model=constraints["fixed_model"],
        fixed_effort=constraints["fixed_effort"],
        primary_model=context["primary_model"],
        primary_effort=context["primary_effort"],
    )
    if value["policy_sha256"] != expected_policy_sha:
        raise RoutingCatalogError("routing decision policy hash mismatch")
    candidates_value = value.get("eligible_candidates")
    if not isinstance(candidates_value, list) or not candidates_value:
        raise RoutingCatalogError("routing decision candidates are malformed")
    candidates: list[dict[str, Any]] = []
    for candidate in candidates_value:
        if not isinstance(candidate, dict):
            raise RoutingCatalogError("routing decision candidate is malformed")
        base_keys = {
            "average_minutes",
            "average_price_usd",
            "duration_samples",
            "effort",
            "incomplete_cost_samples",
            "iq",
            "model",
            "passed",
            "price_samples",
            "valid_tasks",
        }
        base = {key: candidate.get(key) for key in base_keys}
        normalized = normalize_point(base)
        enriched = enrich_candidate(normalized)
        enriched["iq_standard_error"] = round(iq_standard_error(enriched), 6)
        enriched["net_value"] = round(candidate_net_value(enriched, policy), 9)
        expected = scored_record(enriched)
        if candidate != expected:
            raise RoutingCatalogError("routing decision candidate score is invalid")
        if candidate["iq"] <= policy["minimum_iq_exclusive"]:
            raise RoutingCatalogError("routing decision candidate fails the IQ gate")
        if (
            candidate["valid_tasks"] < MIN_SAMPLES
            or candidate["price_samples"] < MIN_SAMPLES
            or candidate["duration_samples"] < MIN_SAMPLES
            or candidate["price_coverage"] < MIN_METRIC_COVERAGE
            or candidate["duration_coverage"] < MIN_METRIC_COVERAGE
        ):
            raise RoutingCatalogError("routing decision candidate fails sample gates")
        candidates.append(candidate)
    pairs = [(item["model"], item["effort"]) for item in candidates]
    if pairs != sorted(set(pairs)):
        raise RoutingCatalogError("routing decision candidates are not canonical")
    if value.get("eligible_count") != len(candidates):
        raise RoutingCatalogError("routing decision eligible count is invalid")
    if (
        decision_assurance == "guarded"
        and constraints["fixed_model"] is None
        and any(_luna_family(candidate["model"]) for candidate in candidates)
    ):
        raise RoutingCatalogError("guarded routing decision contains Luna")
    if (
        decision_lane == "complex"
        and constraints["fixed_model"] is None
        and any(
            _luna_family(candidate["model"])
            and candidate["iq_lower_95"] <= policy["minimum_iq_exclusive"]
            for candidate in candidates
        )
    ):
        raise RoutingCatalogError(
            "complex routing decision contains low-confidence Luna"
        )
    cohort_reference = max(item["valid_tasks"] for item in candidates)
    if any(
        item["valid_tasks"] / cohort_reference < MIN_COHORT_RATIO
        for item in candidates
    ):
        raise RoutingCatalogError("routing decision candidate cohorts are incomparable")
    frontier_value = value.get("pareto_frontier")
    if not isinstance(frontier_value, list) or not frontier_value:
        raise RoutingCatalogError("routing decision Pareto frontier is malformed")
    by_pair = {(item["model"], item["effort"]): item for item in candidates}
    for item in frontier_value:
        if not isinstance(item, dict) or by_pair.get(
            (item.get("model"), item.get("effort"))
        ) != item:
            raise RoutingCatalogError("routing decision frontier candidate is invalid")
    expected_frontier = pareto_frontier(candidates)
    if sorted(frontier_value, key=canonical_bytes) != sorted(
        expected_frontier, key=canonical_bytes
    ):
        raise RoutingCatalogError("routing decision Pareto frontier is invalid")
    recommended_pair = value.get("recommended")
    selected_pair = value.get("selected")
    if not isinstance(recommended_pair, dict) or not isinstance(selected_pair, dict):
        raise RoutingCatalogError("routing decision selection is malformed")
    winner = select_recommended_candidate(
        expected_frontier,
        purpose=decision_purpose,
        context=context,
        fixed_model=constraints["fixed_model"],
        fixed_effort=constraints["fixed_effort"],
    )
    if recommended_pair != selected_record(winner):
        raise RoutingCatalogError("routing decision recommendation is invalid")
    selected_candidate = by_pair.get(
        (selected_pair.get("model"), selected_pair.get("effort"))
    )
    if selected_candidate is None or selected_pair != selected_record(selected_candidate):
        raise RoutingCatalogError("routing decision selected route is invalid")
    if constraints["fixed_model"] is not None and selected_pair["model"] != constraints["fixed_model"]:
        raise RoutingCatalogError("routing decision violates the fixed model")
    if constraints["fixed_effort"] is not None and selected_pair["effort"] != constraints["fixed_effort"]:
        raise RoutingCatalogError("routing decision violates the fixed effort")
    if model is not None and selected_pair["model"] != model:
        raise RoutingCatalogError("routing decision model does not match dispatch")
    if effort is not None and selected_pair["effort"] != effort:
        raise RoutingCatalogError("routing decision effort does not match dispatch")
    expected_placement = select_placement(
        selected_pair, purpose=decision_purpose, context=context
    )
    if value.get("placement") != expected_placement:
        raise RoutingCatalogError("routing decision placement is invalid")
    hysteresis = value.get("hysteresis")
    if (
        not isinstance(hysteresis, dict)
        or set(hysteresis)
        != {"pending_count", "required_winning_snapshots", "status"}
        or hysteresis.get("status")
        not in {
            "initialized",
            "stable",
            "stable_margin",
            "switched",
            "switched_ineligible",
            "switched_policy",
        }
        or hysteresis.get("required_winning_snapshots")
        != REQUIRED_WINNING_SNAPSHOTS
        or isinstance(hysteresis.get("pending_count"), bool)
        or not isinstance(hysteresis.get("pending_count"), int)
        or not 0 <= hysteresis["pending_count"] < REQUIRED_WINNING_SNAPSHOTS
    ):
        raise RoutingCatalogError("routing decision hysteresis is malformed")
    if hysteresis["pending_count"] != 0:
        raise RoutingCatalogError("routing decision hysteresis count is stale")
    fallback = value.get("fallback_order")
    if (
        not isinstance(fallback, list)
        or not 1 <= len(fallback) <= MAX_FALLBACK_CANDIDATES
    ):
        raise RoutingCatalogError("routing decision fallback order is malformed")
    fallback_pairs: list[tuple[str, str]] = []
    for index, item in enumerate(fallback):
        route = validate_route_pair(item, f"fallback route {index}")
        pair = (route["model"], route["effort"])
        if pair not in by_pair:
            raise RoutingCatalogError("routing decision fallback route is ineligible")
        fallback_pairs.append(pair)
    if len(fallback_pairs) != len(set(fallback_pairs)):
        raise RoutingCatalogError("routing decision fallback order is invalid")
    recommended_tuple = (recommended_pair["model"], recommended_pair["effort"])
    base_pair = fallback_pairs[0]
    held_status = hysteresis["status"] == "stable_margin"
    if held_status == (base_pair == recommended_tuple):
        raise RoutingCatalogError("routing decision hysteresis selection is inconsistent")
    ranked_frontier = [
        winner,
        *sorted(
            [candidate for candidate in expected_frontier if candidate is not winner],
            key=score_sort_key,
            reverse=True,
        ),
    ]
    expected_fallback = [base_pair]
    expected_fallback.extend(
        (item["model"], item["effort"])
        for item in ranked_frontier
        if (item["model"], item["effort"]) != base_pair
    )
    if fallback_pairs != expected_fallback[:MAX_FALLBACK_CANDIDATES]:
        raise RoutingCatalogError("routing decision fallback ranking is invalid")
    dispatch = value.get("dispatch")
    if not isinstance(dispatch, dict) or set(dispatch) != {
        "rank",
        "reason",
        "rejection_tickets",
        "rejected_routes",
    }:
        raise RoutingCatalogError("routing decision dispatch state is malformed")
    rank = dispatch.get("rank")
    if (
        isinstance(rank, bool)
        or not isinstance(rank, int)
        or not 1 <= rank <= len(fallback_pairs)
    ):
        raise RoutingCatalogError("routing decision dispatch rank is invalid")
    rejected = dispatch.get("rejected_routes")
    tickets = dispatch.get("rejection_tickets")
    if not isinstance(rejected, list):
        raise RoutingCatalogError("routing decision rejected routes are malformed")
    if (
        not isinstance(tickets, list)
        or len(tickets) != rank - 1
        or any(
            not isinstance(ticket, str)
            or REJECTION_TICKET_RE.fullmatch(ticket) is None
            for ticket in tickets
        )
    ):
        raise RoutingCatalogError("routing decision rejection tickets are malformed")
    rejected_pairs = []
    for index, item in enumerate(rejected):
        route = validate_route_pair(item, f"rejected route {index}")
        rejected_pairs.append((route["model"], route["effort"]))
    if rejected_pairs != fallback_pairs[: rank - 1]:
        raise RoutingCatalogError("routing decision rejection chain is invalid")
    expected_reason = "selected" if rank == 1 else "native_rejection_fallback"
    if dispatch.get("reason") != expected_reason:
        raise RoutingCatalogError("routing decision dispatch reason is invalid")
    if fallback_pairs[rank - 1] != (
        selected_pair["model"],
        selected_pair["effort"],
    ):
        raise RoutingCatalogError("routing decision selected fallback is invalid")
    return value


def advance_route(
    decision: dict[str, Any],
    *,
    rejected_model: str,
    rejected_effort: str,
    rejection_ticket: str = "native:rejected",
) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise RoutingCatalogError("routing decision is malformed")
    if decision.get("decision_sha256") != route_decision_sha256(decision):
        raise RoutingCatalogError("routing decision hash mismatch")
    if (
        not isinstance(rejection_ticket, str)
        or REJECTION_TICKET_RE.fullmatch(rejection_ticket) is None
    ):
        raise RoutingCatalogError("native rejection ticket is invalid")
    selected = decision.get("selected")
    if not isinstance(selected, dict):
        raise RoutingCatalogError("routing decision selection is malformed")
    if (selected["model"], selected["effort"]) != (
        rejected_model,
        rejected_effort,
    ):
        raise RoutingCatalogError("rejected route does not match the active selection")
    fallback_value = decision.get("fallback_order")
    if not isinstance(fallback_value, list):
        raise RoutingCatalogError("routing decision fallback order is malformed")
    fallback = [
        validate_route_pair(item, f"fallback route {index}")
        for index, item in enumerate(fallback_value)
    ]
    dispatch = decision.get("dispatch")
    if (
        not isinstance(dispatch, dict)
        or set(dispatch)
        != {"rank", "reason", "rejection_tickets", "rejected_routes"}
        or isinstance(dispatch.get("rank"), bool)
        or not isinstance(dispatch.get("rank"), int)
        or not isinstance(dispatch.get("rejection_tickets"), list)
        or not isinstance(dispatch.get("rejected_routes"), list)
    ):
        raise RoutingCatalogError("routing decision dispatch state is malformed")
    if any(
        not isinstance(ticket, str)
        or REJECTION_TICKET_RE.fullmatch(ticket) is None
        for ticket in dispatch["rejection_tickets"]
    ):
        raise RoutingCatalogError("routing decision rejection tickets are malformed")
    current_rank = dispatch["rank"]
    if not 1 <= current_rank <= len(fallback):
        raise RoutingCatalogError("routing decision dispatch rank is invalid")
    rejected_chain = [
        validate_route_pair(item, f"rejected route {index}")
        for index, item in enumerate(dispatch["rejected_routes"])
    ]
    if rejected_chain != fallback[: current_rank - 1]:
        raise RoutingCatalogError("routing decision rejection chain is invalid")
    if len(dispatch["rejection_tickets"]) != current_rank - 1:
        raise RoutingCatalogError("routing decision rejection tickets are malformed")
    if selected.get("model") != fallback[current_rank - 1]["model"] or selected.get(
        "effort"
    ) != fallback[current_rank - 1]["effort"]:
        raise RoutingCatalogError("routing decision selected fallback is invalid")
    if current_rank >= len(fallback):
        raise RoutingCatalogError("adaptive routing fallback order is exhausted")
    next_decision = json.loads(json.dumps(decision))
    rejected = fallback[current_rank - 1]
    next_route = fallback[current_rank]
    candidates = {
        (item["model"], item["effort"]): item
        for item in next_decision.get("eligible_candidates", [])
        if isinstance(item, dict)
    }
    next_candidate = candidates.get((next_route["model"], next_route["effort"]))
    if next_candidate is None:
        raise RoutingCatalogError("routing decision fallback route is not bound")
    next_decision["dispatch"] = {
        "rank": current_rank + 1,
        "reason": "native_rejection_fallback",
        "rejection_tickets": [
            *next_decision["dispatch"]["rejection_tickets"],
            rejection_ticket,
        ],
        "rejected_routes": [
            *next_decision["dispatch"]["rejected_routes"],
            rejected,
        ],
    }
    next_decision["selected"] = selected_record(next_candidate)
    next_decision["placement"] = select_placement(
        next_decision["selected"],
        purpose=next_decision["purpose"],
        context=next_decision["placement_context"],
    )
    next_decision.pop("decision_sha256", None)
    next_decision["decision_sha256"] = route_decision_sha256(next_decision)
    return next_decision


def advance_route_plan(
    plan: object,
    *,
    assurance: str = "deterministic",
    purpose: str,
    judgment: str,
    rejected_model: str,
    rejected_effort: str,
    rejection_ticket: str,
) -> dict[str, Any]:
    """Advance one compact route without rescoring or rebuilding its batch."""

    if assurance not in ROUTE_ASSURANCES:
        raise RoutingCatalogError("route plan assurance is invalid")
    if not isinstance(plan, dict) or plan.get("protocol") != ROUTE_PLAN_PROTOCOL:
        raise RoutingCatalogError("route plan is malformed")
    plan_hash = plan.get("plan_sha256")
    if not isinstance(plan_hash, str) or SHA256_RE.fullmatch(plan_hash) is None:
        raise RoutingCatalogError("route plan hash is malformed")
    plan_preimage = {key: value for key, value in plan.items() if key != "plan_sha256"}
    expected_plan_hash = route_plan_sha256(plan_preimage)
    if plan_hash != expected_plan_hash:
        raise RoutingCatalogError("route plan hash mismatch")
    routes = plan.get("routes")
    if not isinstance(routes, list):
        raise RoutingCatalogError("route plan routes are malformed")
    if (
        not isinstance(rejection_ticket, str)
        or REJECTION_TICKET_RE.fullmatch(rejection_ticket) is None
    ):
        raise RoutingCatalogError("native rejection ticket is invalid")
    updated = json.loads(json.dumps(plan))
    matches = [
        route
        for route in updated["routes"]
        if isinstance(route, dict)
        and route.get("purpose") == purpose
        and route.get("judgment") == judgment
        and route.get("assurance") == assurance
    ]
    if len(matches) != 1:
        raise RoutingCatalogError("route plan key is missing or ambiguous")
    route = matches[0]
    if route.get("fixed") is True:
        raise RoutingCatalogError("fixed route plan cannot advance")
    candidates = route.get("candidates")
    selected = route.get("selected")
    dispatch = route.get("dispatch")
    if not isinstance(candidates, list) or not isinstance(selected, dict):
        raise RoutingCatalogError("route plan entry is malformed")
    if dispatch is None:
        dispatch = {"rank": 1, "rejection_tickets": []}
    if (
        not isinstance(dispatch, dict)
        or set(dispatch) != {"rank", "rejection_tickets"}
        or not isinstance(dispatch["rank"], int)
        or not isinstance(dispatch["rejection_tickets"], list)
    ):
        raise RoutingCatalogError("route plan dispatch state is malformed")
    if any(
        not isinstance(ticket, str)
        or REJECTION_TICKET_RE.fullmatch(ticket) is None
        for ticket in dispatch["rejection_tickets"]
    ):
        raise RoutingCatalogError("route plan rejection tickets are malformed")
    rank = dispatch["rank"]
    if (
        selected.get("model") != rejected_model
        or selected.get("effort") != rejected_effort
    ):
        raise RoutingCatalogError("rejected route does not match the active selection")
    if rank >= len(candidates):
        raise RoutingCatalogError("adaptive routing fallback order is exhausted")
    route["dispatch"] = {
        "rank": rank + 1,
        "rejection_tickets": [*dispatch["rejection_tickets"], rejection_ticket],
    }
    route["selected"] = dict(candidates[rank])
    updated.pop("plan_sha256")
    updated["plan_sha256"] = route_plan_sha256(updated)
    return updated


def validate_route_pair(value: object, label: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"effort", "model"}
        or not isinstance(value.get("model"), str)
        or MODEL_RE.fullmatch(value["model"]) is None
        or not isinstance(value.get("effort"), str)
        or EFFORT_RE.fullmatch(value["effort"]) is None
    ):
        raise RoutingCatalogError(f"{label} is malformed")
    return {"effort": value["effort"], "model": value["model"]}


def validate_route_plan(value: object) -> dict[str, Any]:
    """Validate one complete, compact, hash-self-consistent route plan.

    A dispatch capsule carries only the selected pair, rank, and this plan's
    identity.  This validator is therefore deliberately stricter than the
    route-plan advancement helper: a caller cannot construct a partial plan or
    detach the active pair from the hashed fallback order.
    """

    required = {
        "native_catalog_sha256",
        "needs_refresh",
        "plan_sha256",
        "protocol",
        "routes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RoutingCatalogError("route plan is malformed")
    if value["protocol"] != ROUTE_PLAN_PROTOCOL:
        raise RoutingCatalogError("route plan protocol is invalid")
    if (
        not isinstance(value["native_catalog_sha256"], str)
        or SHA256_RE.fullmatch(value["native_catalog_sha256"]) is None
    ):
        raise RoutingCatalogError("route plan native catalog hash is malformed")
    if type(value["needs_refresh"]) is not bool:
        raise RoutingCatalogError("route plan refresh state is malformed")
    if (
        not isinstance(value["plan_sha256"], str)
        or SHA256_RE.fullmatch(value["plan_sha256"]) is None
    ):
        raise RoutingCatalogError("route plan hash is malformed")
    if value["plan_sha256"] != route_plan_sha256(value):
        raise RoutingCatalogError("route plan hash mismatch")
    routes = value["routes"]
    if not isinstance(routes, list) or not routes:
        raise RoutingCatalogError("route plan routes are malformed")
    if len(routes) > len(PURPOSES) * len(LANES) * len(ROUTE_ASSURANCES):
        raise RoutingCatalogError("route plan has too many routes")

    route_fields = {
        "assurance",
        "candidates",
        "decision_sha256",
        "dispatch",
        "fixed",
        "judgment",
        "placement",
        "purpose",
        "selected",
    }
    normalized_routes: list[dict[str, Any]] = []
    route_keys: list[tuple[str, str, str]] = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict) or set(route) != route_fields:
            raise RoutingCatalogError(f"route plan entry {index} is malformed")
        purpose = route["purpose"]
        judgment = route["judgment"]
        assurance = route["assurance"]
        if (
            purpose not in PURPOSES
            or judgment not in LANES
            or assurance not in ROUTE_ASSURANCES
        ):
            raise RoutingCatalogError("route plan purpose or judgment is invalid")
        key = (purpose, judgment, assurance)
        route_keys.append(key)
        fixed = route["fixed"]
        if type(fixed) is not bool:
            raise RoutingCatalogError("route plan fixed state is malformed")
        candidates = route["candidates"]
        if (
            not isinstance(candidates, list)
            or not candidates
            or len(candidates) > MAX_FALLBACK_CANDIDATES
        ):
            raise RoutingCatalogError("route plan candidates are malformed")
        normalized_candidates = [
            validate_route_pair(candidate, f"route plan candidate {index}")
            for candidate in candidates
        ]
        if len({(pair["model"], pair["effort"]) for pair in normalized_candidates}) != len(
            normalized_candidates
        ):
            raise RoutingCatalogError("route plan candidates must be duplicate-free")
        if (
            assurance == "guarded"
            and not fixed
            and any(_luna_family(pair["model"]) for pair in normalized_candidates)
        ):
            raise RoutingCatalogError("guarded route plan contains Luna")
        dispatch = route["dispatch"]
        if (
            not isinstance(dispatch, dict)
            or set(dispatch) != {"rank", "rejection_tickets"}
            or type(dispatch["rank"]) is not int
            or not 1 <= dispatch["rank"] <= len(normalized_candidates)
            or not isinstance(dispatch["rejection_tickets"], list)
        ):
            raise RoutingCatalogError("route plan dispatch state is malformed")
        tickets = dispatch["rejection_tickets"]
        if fixed and (
            len(normalized_candidates) != 1
            or dispatch["rank"] != 1
            or tickets
        ):
            raise RoutingCatalogError("fixed route plan entry is malformed")
        if (
            any(
                not isinstance(ticket, str)
                or REJECTION_TICKET_RE.fullmatch(ticket) is None
                for ticket in tickets
            )
            or len(set(tickets)) != len(tickets)
        ):
            raise RoutingCatalogError("route plan rejection tickets are malformed")
        selected = validate_route_pair(route["selected"], "route plan selected route")
        if selected != normalized_candidates[dispatch["rank"] - 1]:
            raise RoutingCatalogError("route plan selected route does not match rank")
        decision_sha256 = route["decision_sha256"]
        if not isinstance(decision_sha256, str) or SHA256_RE.fullmatch(decision_sha256) is None:
            raise RoutingCatalogError("route plan decision hash is malformed")
        placement = route["placement"]
        if (
            not isinstance(placement, dict)
            or set(placement) != {"reason", "target"}
            or placement["reason"] not in PLACEMENT_REASONS
            or placement["target"] not in {"child", "primary"}
        ):
            raise RoutingCatalogError("route plan placement is malformed")
        if purpose == "acceptance" and placement != {
            "reason": "independent_acceptance",
            "target": "child",
        }:
            raise RoutingCatalogError("acceptance route plan placement is invalid")
        normalized_routes.append(
            {
                "assurance": assurance,
                "candidates": normalized_candidates,
                "decision_sha256": decision_sha256,
                "dispatch": {
                    "rank": dispatch["rank"],
                    "rejection_tickets": list(tickets),
                },
                "fixed": fixed,
                "judgment": judgment,
                "placement": {
                    "reason": placement["reason"],
                    "target": placement["target"],
                },
                "purpose": purpose,
                "selected": selected,
            }
        )
    if route_keys != sorted(route_keys) or len(set(route_keys)) != len(route_keys):
        raise RoutingCatalogError("route plan routes must be sorted and duplicate-free")
    normalized = {
        "native_catalog_sha256": value["native_catalog_sha256"],
        "needs_refresh": value["needs_refresh"],
        "protocol": ROUTE_PLAN_PROTOCOL,
        "routes": normalized_routes,
        "plan_sha256": value["plan_sha256"],
    }
    if route_plan_sha256(normalized) != normalized["plan_sha256"]:
        raise RoutingCatalogError("route plan canonical form does not match hash")
    return normalized


def validate_routing_state(value: object) -> dict[str, Any]:
    legacy_route_keys = set(LANES) | {
        f"{purpose}:{judgment}"
        for purpose in PURPOSES - {"implementation"}
        for judgment in LANES
    }
    route_keys = legacy_route_keys | {
        routing_state_key(purpose, judgment, assurance)
        for purpose in PURPOSES
        for judgment in LANES
        for assurance in ROUTE_ASSURANCES
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"lanes", "protocol"}
        or value.get("protocol") != STATE_PROTOCOL
        or not isinstance(value.get("lanes"), dict)
        or not set(value["lanes"]) <= route_keys
    ):
        raise RoutingCatalogError("routing hysteresis state is malformed")
    normalized: dict[str, Any] = {"protocol": STATE_PROTOCOL, "lanes": {}}
    for lane in sorted(value["lanes"]):
        lane_state = value["lanes"][lane]
        required = {"active", "policy_sha256"}
        if not isinstance(lane_state, dict) or set(lane_state) != required:
            raise RoutingCatalogError("routing hysteresis lane state is malformed")
        active = validate_route_pair(lane_state["active"], "active route")
        policy_identity = lane_state["policy_sha256"]
        if not isinstance(policy_identity, str) or SHA256_RE.fullmatch(policy_identity) is None:
            raise RoutingCatalogError("routing hysteresis policy hash is malformed")
        normalized["lanes"][lane] = {
            "active": active,
            "policy_sha256": policy_identity,
        }
    return normalized


def read_routing_state(state_file: Path) -> dict[str, Any] | None:
    if not state_file.exists():
        return None
    if not state_file.is_file() or is_reparse(state_file):
        raise RoutingCatalogError("routing hysteresis state must be a real file")
    try:
        if state_file.stat().st_size > STATE_FILE_MAX_BYTES:
            raise RoutingCatalogError("routing hysteresis state exceeds the size limit")
        value = load_json_bytes(state_file.read_bytes(), "routing hysteresis state")
    except OSError as error:
        raise RoutingCatalogError("routing hysteresis state could not be read") from error
    return validate_routing_state(value)


def write_routing_state(state_file: Path, state: dict[str, Any]) -> None:
    normalized = validate_routing_state(state)
    encoded = canonical_bytes(normalized) + b"\n"
    if state_file.exists():
        try:
            if state_file.read_bytes() == encoded:
                return
        except OSError as error:
            raise RoutingCatalogError("routing hysteresis state could not be read") from error
    write_json_atomic(state_file, normalized, prefix="routing-state-v2.")


@contextmanager
def routing_lock(
    cache_dir: Path,
    *,
    now: datetime,
    wait_seconds: float = 0.0,
) -> Any:
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or not 0.0 <= float(wait_seconds) <= 0.25
    ):
        raise RoutingCatalogError("routing lock wait must be between 0 and 0.25 seconds")
    lock_file = cache_dir / LOCK_FILENAME
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    deadline = time.monotonic() + wait_seconds
    encoded = canonical_bytes(
        {
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "token": token,
        }
    )
    while True:
        try:
            descriptor = os.open(
                lock_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                age = now.astimezone(timezone.utc).timestamp() - lock_file.stat().st_mtime
                if age > LOCK_STALE_AFTER.total_seconds():
                    lock_file.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RoutingCacheBusy("routing cache is busy")
            time.sleep(0.05)
            continue
        except OSError as error:
            raise RoutingCatalogError("routing cache lock could not be acquired") from error
        opened_identity = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                current_identity = lock_file.stat()
                if (
                    current_identity.st_dev == opened_identity.st_dev
                    and current_identity.st_ino == opened_identity.st_ino
                ):
                    lock_file.unlink(missing_ok=True)
            except OSError:
                pass
        return


@contextmanager
def radar_refresh_lock(cache_dir: Path, *, now: datetime) -> Any:
    """Acquire the non-blocking lock that serializes one-shot Radar refreshes."""

    lock_file = cache_dir / RADAR_REFRESH_LOCK_FILENAME
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    encoded = canonical_bytes(
        {
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "token": token,
        }
    )
    while True:
        try:
            descriptor = os.open(
                lock_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                age = now.astimezone(timezone.utc).timestamp() - lock_file.stat().st_mtime
                if age > LOCK_STALE_AFTER.total_seconds():
                    lock_file.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass
            raise RoutingCacheBusy("radar refresh is already running")
        except OSError as error:
            raise RoutingCatalogError("radar refresh lock could not be acquired") from error
        opened_identity = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            try:
                current_identity = lock_file.stat()
                if (
                    current_identity.st_dev == opened_identity.st_dev
                    and current_identity.st_ino == opened_identity.st_ino
                ):
                    lock_file.unlink(missing_ok=True)
            except OSError:
                pass
        return


def refresh_radar_snapshot(
    cache_dir: Path,
    *,
    expected_fetched_at: datetime,
    now: datetime | None = None,
    fetcher: Callable[[str | None], FetchResult] = fetch_radar,
) -> bool:
    """Refresh one scheduled LKG only if it has not already been superseded."""

    if expected_fetched_at.tzinfo is None:
        raise RoutingCatalogError("expected refresh timestamp must include a timezone")
    current = now or datetime.now(timezone.utc)
    expected = expected_fetched_at.astimezone(timezone.utc)
    directory = Path(os.path.abspath(cache_dir.expanduser()))
    directory.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(directory)
    try:
        with radar_refresh_lock(directory, now=current):
            cached = read_cache(directory / CACHE_FILENAME, now=current)
            if cached is None:
                return False
            fetched_at = parse_timestamp(cached["fetched_at"], "fetched_at")
            if fetched_at != expected:
                _clear_refresh_request(directory, expected)
                return False
            loaded = load_radar_snapshot(
                directory,
                now=current,
                fetcher=fetcher,
                force_refresh=True,
            )
            refreshed = loaded.status in {"refreshed", "revalidated"}
            if refreshed:
                _clear_refresh_request(directory, expected)
            return refreshed
    except RoutingCacheBusy:
        return False


def default_cache_dir() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    base = Path(configured_home) if configured_home else Path.home() / ".codex"
    return base / "cache" / "codex-cost-orchestrator"


def load_native_catalog(
    cache_dir: Path | None = None,
    *,
    executable: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Load the bundled catalog once per Codex version/content fingerprint."""

    if executable is None:
        executable_name = "codex.cmd" if os.name == "nt" else "codex"
        resolved = shutil.which(executable_name)
        executable = Path(resolved) if resolved is not None else None
    if executable is None:
        raise RoutingCatalogError("Codex CLI is unavailable")
    cache_directory = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    cache_directory.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(cache_directory)
    cache_file = cache_directory / NATIVE_CATALOG_CACHE_FILENAME
    try:
        identity_parts = [executable.read_bytes()]
        package_manifest = executable.parent / "node_modules" / "@openai" / "codex" / "package.json"
        if package_manifest.is_file():
            identity_parts.append(package_manifest.read_bytes())
        executable_fingerprint = hashlib.sha256(b"\0".join(identity_parts)).hexdigest()
    except OSError as error:
        raise RoutingCatalogError("Codex native catalog identity is unavailable") from error
    if cache_file.exists():
        try:
            cached = load_json_bytes(cache_file.read_bytes(), "native catalog cache")
            if (
                isinstance(cached, dict)
                and cached.get("protocol") == NATIVE_CATALOG_CACHE_PROTOCOL
                and isinstance(cached.get("codex_version"), str)
                and cached.get("executable_fingerprint") == executable_fingerprint
            ):
                manifest_version = None
                if len(identity_parts) > 1:
                    manifest = load_json_bytes(
                        identity_parts[1], "Codex package manifest"
                    )
                    if isinstance(manifest, dict) and isinstance(
                        manifest.get("version"), str
                    ):
                        manifest_version = manifest["version"]
                if manifest_version is not None and manifest_version not in cached[
                    "codex_version"
                ]:
                    raise RoutingCatalogError("native catalog cache version mismatch")
                catalog = cached.get("catalog")
                native_capability_records(catalog)
                if cached.get("catalog_sha256") == native_catalog_sha256(catalog):
                    return catalog
        except (OSError, RoutingCatalogError):
            pass
    try:
        version_result = runner(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RoutingCatalogError("Codex native catalog identity is unavailable") from error
    if version_result.returncode != 0 or len(version_result.stdout) > 512:
        raise RoutingCatalogError("Codex version command failed")
    try:
        version = version_result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise RoutingCatalogError("Codex version is not valid UTF-8") from error
    if not version:
        raise RoutingCatalogError("Codex version is empty")
    try:
        completed = runner(
            [str(executable), "debug", "models", "--bundled"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RoutingCatalogError("Codex native model catalog is unavailable") from error
    if completed.returncode != 0:
        raise RoutingCatalogError("Codex native model catalog command failed")
    if len(completed.stdout) > NATIVE_CATALOG_MAX_BYTES:
        raise RoutingCatalogError("Codex native model catalog exceeds the size limit")
    value = load_json_bytes(completed.stdout, "Codex native model catalog")
    native_capability_records(value)
    write_json_atomic(
        cache_file,
        {
            "catalog": value,
            "catalog_sha256": native_catalog_sha256(value),
            "codex_version": version,
            "executable_fingerprint": executable_fingerprint,
            "protocol": NATIVE_CATALOG_CACHE_PROTOCOL,
        },
        prefix="native-catalog-v1.",
    )
    return value


def resolve_graph_route(
    cache_dir: Path,
    lane: str | None = None,
    *,
    assurance: str = "deterministic",
    purpose: str | None = None,
    judgment: str | None = None,
    now: datetime | None = None,
    refresh_interval: timedelta = REFRESH_INTERVAL,
    policy_overrides: dict[str, float] | None = None,
    fixed_model: str | None = None,
    fixed_effort: str | None = None,
    primary_model: str | None = None,
    primary_effort: str | None = None,
    placement_benefits: object = None,
    fetcher: Callable[[str | None], FetchResult] = fetch_radar,
    native_loader: Callable[[], dict[str, Any]] = load_native_catalog,
    scheduler: Callable[[list[str]], Any] = launch_radar_refresh,
) -> tuple[dict[str, Any], LoadedSnapshot]:
    current = now or datetime.now(timezone.utc)
    cache_dir = Path(os.path.abspath(cache_dir.expanduser()))
    cache_dir.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(cache_dir)
    # Network/CLI/catalog work never holds the state lock.  The lock protects
    # only the tiny read-stabilize-write critical section below.
    if fixed_model is not None and fixed_effort is not None:
        loaded = LoadedSnapshot({}, "not_required", current)
    else:
        loaded = load_radar_snapshot(
            cache_dir,
            now=current,
            fetcher=fetcher,
            refresh_interval=refresh_interval,
        )
    native_catalog = (
        native_loader()
        if native_loader is not load_native_catalog
        else load_native_catalog(cache_dir)
    )
    recommendation = resolve_route(
        loaded.snapshot,
        native_catalog,
        lane,
        assurance=assurance,
        purpose=purpose,
        judgment=judgment,
        now=current,
        policy_overrides=policy_overrides,
        fixed_model=fixed_model,
        fixed_effort=fixed_effort,
        primary_model=primary_model,
        primary_effort=primary_effort,
        placement_benefits=placement_benefits,
    )
    with routing_lock(cache_dir, now=current):
        state_file = cache_dir / STATE_FILENAME
        state = read_routing_state(state_file)
        decision, next_state = stabilize_route(recommendation, state)
        write_routing_state(state_file, next_state)
        cleanup_stale_temps(cache_dir, now=current)
    schedule_radar_refresh(cache_dir, loaded, scheduler=scheduler)
    return decision, loaded


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Resolve a bounded, auditable Codex worker route."
    )
    subparsers = root.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--lane", choices=sorted(LANES))
    resolve.add_argument(
        "--assurance", choices=sorted(ROUTE_ASSURANCES), default="deterministic"
    )
    resolve.add_argument("--purpose", choices=sorted(PURPOSES))
    resolve.add_argument("--judgment", choices=sorted(LANES))
    resolve.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    resolve.add_argument("--refresh-ttl-minutes", type=int, default=60)
    resolve.add_argument("--fixed-model")
    resolve.add_argument("--fixed-effort")
    resolve.add_argument("--primary-model")
    resolve.add_argument("--primary-effort")
    resolve.add_argument(
        "--placement-benefit",
        action="append",
        default=[],
        metavar="KIND=EVIDENCE",
        help="Bind one evidence-bearing structural reason for a child; repeat as needed.",
    )
    resolve.add_argument("--quality-weight", type=float)
    resolve.add_argument("--cost-weight", type=float)
    resolve.add_argument("--time-weight", type=float)
    resolve.add_argument("--uncertainty-weight", type=float)
    resolve.add_argument("--minimum-iq-exclusive", type=float)
    resolve.add_argument("--cost-anchor-usd", type=float)
    resolve.add_argument("--time-anchor-minutes", type=float)
    output = resolve.add_mutually_exclusive_group()
    output.add_argument(
        "--packet",
        action="store_true",
        help="Emit the complete canonical decision for packet binding.",
    )
    output.add_argument(
        "--explain",
        action="store_true",
        help="Emit opt-in trade-off details for diagnostics.",
    )
    resolve_plan = subparsers.add_parser(
        "resolve-plan",
        help="Resolve ordered purpose/judgment/assurance requests from stdin.",
    )
    resolve_plan.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    resolve_plan.add_argument("--refresh-ttl-minutes", type=int, default=60)
    refresh = subparsers.add_parser(
        "refresh",
        help="Refresh one scheduled Radar LKG outside the route dispatch path.",
    )
    refresh.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    refresh.add_argument("--expected-fetched-at", required=True)
    advance = subparsers.add_parser(
        "advance",
        help="Advance an existing decision after a pre-thread native rejection.",
    )
    advance.add_argument("--rejected-model", required=True)
    advance.add_argument("--rejected-effort", required=True)
    advance.add_argument("--rejection-ticket", default="native:rejected")
    advance_plan = subparsers.add_parser(
        "advance-plan",
        help="Advance one bound route-plan entry without rescoring the batch.",
    )
    advance_plan.add_argument("--purpose", choices=sorted(PURPOSES), required=True)
    advance_plan.add_argument("--judgment", choices=sorted(LANES), required=True)
    advance_plan.add_argument(
        "--assurance", choices=sorted(ROUTE_ASSURANCES), default="deterministic"
    )
    advance_plan.add_argument("--rejected-model", required=True)
    advance_plan.add_argument("--rejected-effort", required=True)
    advance_plan.add_argument("--rejection-ticket", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "refresh":
            refresh_radar_snapshot(
                args.cache_dir,
                expected_fetched_at=parse_timestamp(
                    args.expected_fetched_at, "expected_fetched_at"
                ),
            )
            return 0
        if args.command == "advance":
            raw = sys.stdin.buffer.read(CACHE_FILE_MAX_BYTES + 1)
            if len(raw) > CACHE_FILE_MAX_BYTES:
                raise RoutingCatalogError("routing decision input exceeds the size limit")
            decision = load_json_bytes(raw, "routing decision input")
            advanced = advance_route(
                decision,
                rejected_model=args.rejected_model,
                rejected_effort=args.rejected_effort,
                rejection_ticket=args.rejection_ticket,
            )
            print(canonical_bytes(advanced).decode("ascii"))
            return 0
        if args.command == "advance-plan":
            raw = sys.stdin.buffer.read(CACHE_FILE_MAX_BYTES + 1)
            if len(raw) > CACHE_FILE_MAX_BYTES:
                raise RoutingCatalogError("route plan input exceeds the size limit")
            plan = load_json_bytes(raw, "route plan input")
            advanced = advance_route_plan(
                plan,
                assurance=args.assurance,
                purpose=args.purpose,
                judgment=args.judgment,
                rejected_model=args.rejected_model,
                rejected_effort=args.rejected_effort,
                rejection_ticket=args.rejection_ticket,
            )
            print(canonical_bytes(advanced).decode("ascii"))
            return 0
        if args.command == "resolve-plan":
            raw = sys.stdin.buffer.read(CACHE_FILE_MAX_BYTES + 1)
            if len(raw) > CACHE_FILE_MAX_BYTES:
                raise RoutingCatalogError("route requests input exceeds the size limit")
            requests = load_json_bytes(raw, "route requests input")
            plan, _loaded = resolve_graph_route_plan(
                args.cache_dir,
                requests,
                refresh_interval=timedelta(minutes=args.refresh_ttl_minutes),
            )
            print(canonical_bytes(plan).decode("ascii"))
            return 0
        refresh = timedelta(minutes=args.refresh_ttl_minutes)
        policy_fields = {
            "quality_weight": args.quality_weight,
            "cost_weight": args.cost_weight,
            "time_weight": args.time_weight,
            "uncertainty_weight": args.uncertainty_weight,
            "minimum_iq_exclusive": args.minimum_iq_exclusive,
            "cost_anchor_usd": args.cost_anchor_usd,
            "time_anchor_minutes": args.time_anchor_minutes,
        }
        overrides = {
            key: value for key, value in policy_fields.items() if value is not None
        }
        decision, _loaded = resolve_graph_route(
            args.cache_dir,
            args.lane,
            assurance=args.assurance,
            purpose=args.purpose,
            judgment=args.judgment,
            refresh_interval=refresh,
            policy_overrides=overrides or None,
            fixed_model=args.fixed_model,
            fixed_effort=args.fixed_effort,
            primary_model=args.primary_model,
            primary_effort=args.primary_effort,
            placement_benefits=parse_placement_benefits(args.placement_benefit),
        )
        if args.packet:
            output: dict[str, Any] = decision
        else:
            output = render_resolution(decision, explain=args.explain)
        if args.explain:
            print(json.dumps(output, sort_keys=True, indent=2, ensure_ascii=True))
        else:
            print(canonical_bytes(output).decode("ascii"))
        return 0
    except (OSError, RoutingCatalogError) as error:
        failure = {
            "error": str(error),
            "protocol": "cco.routing-error.v1",
        }
        print(canonical_bytes(failure).decode("ascii"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

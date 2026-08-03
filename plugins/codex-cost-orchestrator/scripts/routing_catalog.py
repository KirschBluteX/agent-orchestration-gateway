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
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROTOCOL = "cco.routing.v1"
SNAPSHOT_DOMAIN = b"cco.routing-snapshot.v1\0"
MEASUREMENT_DOMAIN = b"cco.routing-measurement.v1\0"
DECISION_DOMAIN = b"cco.routing-decision.v1\0"
NATIVE_CATALOG_DOMAIN = b"cco.routing-native-catalog.v1\0"
POLICY_DOMAIN = b"cco.routing-policy.v1\0"
RADAR_URL = "https://codexradar.com/data/intelligence-efficiency.json"
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CACHE_FILE_MAX_BYTES = 512 * 1024
STATE_FILE_MAX_BYTES = 64 * 1024
NATIVE_CATALOG_MAX_BYTES = 4 * 1024 * 1024
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
CACHE_PROTOCOL = "cco.routing-cache.v1"
CACHE_FILENAME = "radar-lkg-v1.json"
CACHE_TEMP_GLOB = "radar-lkg-v1.*.tmp"
STATE_PROTOCOL = "cco.routing-state.v1"
STATE_FILENAME = "routing-state-v1.json"
STATE_TEMP_GLOB = "routing-state-v1.*.tmp"
LOCK_FILENAME = "routing-v1.lock"
LOCK_STALE_AFTER = timedelta(minutes=2)
TEMP_STALE_AFTER = timedelta(hours=1)
SWITCH_MARGIN = 0.01
REQUIRED_WINNING_SNAPSHOTS = 2
MAX_FALLBACK_CANDIDATES = 3
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
DEFAULT_POLICIES = {
    "routine": {
        "quality_weight": 0.35,
        "cost_weight": 0.55,
        "time_weight": 0.10,
        "uncertainty_weight": 0.05,
        "minimum_iq_exclusive": MIN_IQ,
        "cost_anchor_usd": 25.0,
        "time_anchor_minutes": 60.0,
    },
    "complex": {
        "quality_weight": 0.70,
        "cost_weight": 0.20,
        "time_weight": 0.10,
        "uncertainty_weight": 0.05,
        "minimum_iq_exclusive": MIN_IQ,
        "cost_anchor_usd": 25.0,
        "time_anchor_minutes": 60.0,
    },
}


class RoutingCatalogError(ValueError):
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


def fetch_radar(
    etag: str | None,
    *,
    opener: Callable[..., Any] = urlopen,
) -> FetchResult:
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-cost-orchestrator/0.5 routing-catalog",
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
    pairs: set[tuple[str, str]] = set()
    for model in catalog["models"]:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("slug"), str)
            or MODEL_RE.fullmatch(model["slug"]) is None
        ):
            raise RoutingCatalogError("native model catalog is malformed")
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


def iq_standard_error(candidate: dict[str, Any]) -> float:
    """Retained for decision compatibility; Wilson uncertainty drives selection."""
    probability = candidate["passed"] / candidate["valid_tasks"]
    return 150.0 * math.sqrt(
        probability * (1.0 - probability) / candidate["valid_tasks"]
    )


def routing_policy(lane: str, overrides: dict[str, float] | None) -> dict[str, float]:
    policy = dict(DEFAULT_POLICIES[lane])
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
    lane: str,
    policy: dict[str, float],
    *,
    fixed_model: str | None,
    fixed_effort: str | None,
) -> str:
    preimage = {
        "fixed_effort": fixed_effort,
        "fixed_model": fixed_model,
        "lane": lane,
        "policy": policy,
    }
    return "sha256:" + hashlib.sha256(
        POLICY_DOMAIN + canonical_bytes(preimage)
    ).hexdigest()


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


def resolve_route(
    radar_payload: object,
    native_catalog: object,
    lane: str,
    *,
    now: datetime | None = None,
    policy_overrides: dict[str, float] | None = None,
    fixed_model: str | None = None,
    fixed_effort: str | None = None,
) -> dict[str, Any]:
    if lane not in LANES:
        raise RoutingCatalogError(f"unsupported lane: {lane}")
    current = now or datetime.now(timezone.utc)
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
    policy = routing_policy(lane, policy_overrides)
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
    selected = max(scored, key=score_sort_key)
    ranked = sorted(scored, key=score_sort_key, reverse=True)
    policy_identity = policy_sha256(
        lane,
        policy,
        fixed_model=fixed_model,
        fixed_effort=fixed_effort,
    )
    decision = {
        "protocol": PROTOCOL,
        "lane": lane,
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
    }
    decision["decision_sha256"] = route_decision_sha256(decision)
    return decision


def stabilize_route(
    recommendation: dict[str, Any], state: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    lane = recommendation.get("lane")
    if lane not in LANES:
        raise RoutingCatalogError("route recommendation lane is invalid")
    if recommendation.get("decision_sha256") != route_decision_sha256(recommendation):
        raise RoutingCatalogError("route recommendation hash mismatch")
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
    lane_state = next_state["lanes"].get(lane)
    status = "initialized"
    pending_count = 0
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
                pending = lane_state.get("pending")
                previous_measurement = lane_state.get(
                    "pending_measurement_sha256"
                )
                if (
                    isinstance(pending, dict)
                    and (pending.get("model"), pending.get("effort"))
                    == recommended_pair
                ):
                    pending_count = int(lane_state.get("pending_count", 0))
                    if previous_measurement != recommendation["measurement_sha256"]:
                        pending_count += 1
                else:
                    pending_count = 1
                if pending_count >= REQUIRED_WINNING_SNAPSHOTS:
                    status = "switched"
                    pending_count = 0
                else:
                    status = "pending"
                    actual_candidate = active_candidate
    pending_state = None
    pending_measurement = None
    if status == "pending":
        pending_state = {
            "model": recommended_candidate["model"],
            "effort": recommended_candidate["effort"],
        }
        pending_measurement = recommendation["measurement_sha256"]
    next_state["lanes"][lane] = {
        "active": {
            "model": actual_candidate["model"],
            "effort": actual_candidate["effort"],
        },
        "pending": pending_state,
        "pending_count": pending_count,
        "pending_measurement_sha256": pending_measurement,
        "policy_sha256": recommendation["policy_sha256"],
    }
    decision = {
        key: value
        for key, value in recommendation.items()
        if key != "decision_sha256"
    }
    decision["recommended"] = recommendation["selected"]
    decision["selected"] = selected_record(actual_candidate)
    decision["hysteresis"] = {
        "status": status,
        "pending_count": pending_count,
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
        "rejected_routes": [],
    }
    decision["decision_sha256"] = route_decision_sha256(decision)
    return decision, next_state


def render_resolution(decision: dict[str, Any], *, explain: bool) -> dict[str, Any]:
    selected = decision.get("selected")
    if not isinstance(selected, dict):
        raise RoutingCatalogError("routing decision has no selected route")
    quiet = {
        "decision_sha256": decision.get("decision_sha256"),
        "effort": selected.get("effort"),
        "lane": decision.get("lane"),
        "model": selected.get("model"),
    }
    if not explain:
        return quiet
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
    lane: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    required = {
        "constraints",
        "decision_sha256",
        "dispatch",
        "eligible_candidates",
        "eligible_count",
        "fallback_order",
        "hysteresis",
        "lane",
        "measurement_sha256",
        "native_catalog_sha256",
        "native_catalog_source",
        "pareto_frontier",
        "policy",
        "policy_sha256",
        "protocol",
        "recommended",
        "selected",
        "selection_method",
        "snapshot_sha256",
        "source_updated_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RoutingCatalogError("routing decision schema is malformed")
    if value.get("protocol") != PROTOCOL:
        raise RoutingCatalogError("routing decision protocol is invalid")
    decision_lane = value.get("lane")
    if decision_lane not in LANES or (lane is not None and decision_lane != lane):
        raise RoutingCatalogError("routing decision lane is invalid")
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
    policy_value = value.get("policy")
    if not isinstance(policy_value, dict):
        raise RoutingCatalogError("routing decision policy is malformed")
    policy = routing_policy(decision_lane, policy_value)
    expected_policy_sha = policy_sha256(
        decision_lane,
        policy,
        fixed_model=constraints["fixed_model"],
        fixed_effort=constraints["fixed_effort"],
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
    winner = max(expected_frontier, key=score_sort_key)
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
    hysteresis = value.get("hysteresis")
    if (
        not isinstance(hysteresis, dict)
        or set(hysteresis)
        != {"pending_count", "required_winning_snapshots", "status"}
        or hysteresis.get("status")
        not in {
            "initialized",
            "pending",
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
    if hysteresis["status"] == "pending":
        if hysteresis["pending_count"] < 1:
            raise RoutingCatalogError("routing decision pending hysteresis is invalid")
    elif hysteresis["pending_count"] != 0:
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
    held_status = hysteresis["status"] in {"pending", "stable_margin"}
    if held_status == (base_pair == recommended_tuple):
        raise RoutingCatalogError("routing decision hysteresis selection is inconsistent")
    ranked_frontier = sorted(expected_frontier, key=score_sort_key, reverse=True)
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
    if not isinstance(rejected, list):
        raise RoutingCatalogError("routing decision rejected routes are malformed")
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
    decision: dict[str, Any], *, rejected_model: str, rejected_effort: str
) -> dict[str, Any]:
    validated = validate_route_decision(decision)
    selected = validated["selected"]
    if (selected["model"], selected["effort"]) != (
        rejected_model,
        rejected_effort,
    ):
        raise RoutingCatalogError("rejected route does not match the active selection")
    fallback = validated["fallback_order"]
    current_rank = validated["dispatch"]["rank"]
    if current_rank >= len(fallback):
        raise RoutingCatalogError("adaptive routing fallback order is exhausted")
    next_decision = json.loads(json.dumps(validated))
    rejected = fallback[current_rank - 1]
    next_route = fallback[current_rank]
    candidates = {
        (item["model"], item["effort"]): item
        for item in next_decision["eligible_candidates"]
    }
    next_candidate = candidates[(next_route["model"], next_route["effort"])]
    next_decision["dispatch"] = {
        "rank": current_rank + 1,
        "reason": "native_rejection_fallback",
        "rejected_routes": [
            *next_decision["dispatch"]["rejected_routes"],
            rejected,
        ],
    }
    next_decision["selected"] = selected_record(next_candidate)
    next_decision.pop("decision_sha256", None)
    next_decision["decision_sha256"] = route_decision_sha256(next_decision)
    validate_route_decision(next_decision)
    return next_decision


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


def validate_routing_state(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"lanes", "protocol"}
        or value.get("protocol") != STATE_PROTOCOL
        or not isinstance(value.get("lanes"), dict)
        or not set(value["lanes"]) <= LANES
    ):
        raise RoutingCatalogError("routing hysteresis state is malformed")
    normalized: dict[str, Any] = {"protocol": STATE_PROTOCOL, "lanes": {}}
    for lane in sorted(value["lanes"]):
        lane_state = value["lanes"][lane]
        required = {
            "active",
            "pending",
            "pending_count",
            "pending_measurement_sha256",
            "policy_sha256",
        }
        if not isinstance(lane_state, dict) or set(lane_state) != required:
            raise RoutingCatalogError("routing hysteresis lane state is malformed")
        active = validate_route_pair(lane_state["active"], "active route")
        policy_identity = lane_state["policy_sha256"]
        if not isinstance(policy_identity, str) or SHA256_RE.fullmatch(policy_identity) is None:
            raise RoutingCatalogError("routing hysteresis policy hash is malformed")
        pending_count = lane_state["pending_count"]
        if (
            isinstance(pending_count, bool)
            or not isinstance(pending_count, int)
            or not 0 <= pending_count < REQUIRED_WINNING_SNAPSHOTS
        ):
            raise RoutingCatalogError("routing hysteresis pending count is malformed")
        pending = lane_state["pending"]
        pending_measurement = lane_state["pending_measurement_sha256"]
        if pending is None:
            if pending_count != 0 or pending_measurement is not None:
                raise RoutingCatalogError("routing hysteresis pending state is inconsistent")
            normalized_pending = None
        else:
            normalized_pending = validate_route_pair(pending, "pending route")
            if (
                pending_count < 1
                or normalized_pending == active
                or not isinstance(pending_measurement, str)
                or SHA256_RE.fullmatch(pending_measurement) is None
            ):
                raise RoutingCatalogError("routing hysteresis pending state is inconsistent")
        normalized["lanes"][lane] = {
            "active": active,
            "pending": normalized_pending,
            "pending_count": pending_count,
            "pending_measurement_sha256": pending_measurement,
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
    write_json_atomic(state_file, normalized, prefix="routing-state-v1.")


@contextmanager
def routing_lock(
    cache_dir: Path,
    *,
    now: datetime,
    wait_seconds: float = 30.0,
) -> Any:
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
                raise RoutingCatalogError("routing cache is busy")
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


def default_cache_dir() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    base = Path(configured_home) if configured_home else Path.home() / ".codex"
    return base / "cache" / "codex-cost-orchestrator"


def load_native_catalog() -> dict[str, Any]:
    executable_name = "codex.cmd" if os.name == "nt" else "codex"
    executable = shutil.which(executable_name)
    if executable is None:
        raise RoutingCatalogError("Codex CLI is unavailable")
    try:
        completed = subprocess.run(
            [executable, "debug", "models", "--bundled"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RoutingCatalogError("Codex native model catalog is unavailable") from error
    if completed.returncode != 0:
        raise RoutingCatalogError("Codex native model catalog command failed")
    if len(completed.stdout) > NATIVE_CATALOG_MAX_BYTES:
        raise RoutingCatalogError("Codex native model catalog exceeds the size limit")
    value = load_json_bytes(completed.stdout, "Codex native model catalog")
    native_capability_records(value)
    return value


def resolve_graph_route(
    cache_dir: Path,
    lane: str,
    *,
    now: datetime | None = None,
    refresh_interval: timedelta = REFRESH_INTERVAL,
    policy_overrides: dict[str, float] | None = None,
    fixed_model: str | None = None,
    fixed_effort: str | None = None,
    fetcher: Callable[[str | None], FetchResult] = fetch_radar,
    native_loader: Callable[[], dict[str, Any]] = load_native_catalog,
) -> tuple[dict[str, Any], LoadedSnapshot]:
    current = now or datetime.now(timezone.utc)
    cache_dir = Path(os.path.abspath(cache_dir.expanduser()))
    cache_dir.mkdir(parents=True, exist_ok=True)
    validate_cache_directory(cache_dir)
    with routing_lock(cache_dir, now=current):
        loaded = load_radar_snapshot(
            cache_dir,
            now=current,
            fetcher=fetcher,
            refresh_interval=refresh_interval,
        )
        native_catalog = native_loader()
        recommendation = resolve_route(
            loaded.snapshot,
            native_catalog,
            lane,
            now=current,
            policy_overrides=policy_overrides,
            fixed_model=fixed_model,
            fixed_effort=fixed_effort,
        )
        state_file = cache_dir / STATE_FILENAME
        state = read_routing_state(state_file)
        decision, next_state = stabilize_route(recommendation, state)
        write_routing_state(state_file, next_state)
        cleanup_stale_temps(cache_dir, now=current)
        return decision, loaded


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Resolve a bounded, auditable Codex worker route."
    )
    subparsers = root.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--lane", choices=sorted(LANES), required=True)
    resolve.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    resolve.add_argument("--refresh-ttl-minutes", type=int, default=60)
    resolve.add_argument("--fixed-model")
    resolve.add_argument("--fixed-effort")
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
    advance = subparsers.add_parser(
        "advance",
        help="Advance an existing decision after a pre-thread native rejection.",
    )
    advance.add_argument("--rejected-model", required=True)
    advance.add_argument("--rejected-effort", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "advance":
            raw = sys.stdin.buffer.read(CACHE_FILE_MAX_BYTES + 1)
            if len(raw) > CACHE_FILE_MAX_BYTES:
                raise RoutingCatalogError("routing decision input exceeds the size limit")
            decision = load_json_bytes(raw, "routing decision input")
            advanced = advance_route(
                decision,
                rejected_model=args.rejected_model,
                rejected_effort=args.rejected_effort,
            )
            print(canonical_bytes(advanced).decode("ascii"))
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
            refresh_interval=refresh,
            policy_overrides=overrides or None,
            fixed_model=args.fixed_model,
            fixed_effort=args.fixed_effort,
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

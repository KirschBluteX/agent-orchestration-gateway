#!/usr/bin/env python3
"""Network-free aog.v1 route policy and native capability adapter.

The module has one deep interface: resolve a complete node route plan from a
static policy, explicit user pins, and the host's native capability catalogue.
It never contacts a network service, records usage data, or invokes another model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping

ROUTE_PLAN_PROTOCOL = "aog.route.v1"
NATIVE_CATALOG_MAX_BYTES = 4 * 1024 * 1024
MAX_FALLBACK_CANDIDATES = 6
ASSURANCES = frozenset({"mechanical", "bounded", "guarded"})
ROLES = frozenset({"explorer", "worker", "reviewer"})
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

DEFAULT_ROUTE_MODELS: dict[tuple[str, str], tuple[str, ...]] = {
    ("explorer", "mechanical"): ("gpt-5.6-luna", "gpt-5.6-terra"),
    ("worker", "mechanical"): ("gpt-5.6-luna", "gpt-5.6-terra"),
    ("explorer", "bounded"): ("gpt-5.6-terra",),
    ("worker", "bounded"): ("gpt-5.6-terra",),
    ("explorer", "guarded"): ("gpt-5.6-terra",),
    ("worker", "guarded"): ("gpt-5.6-terra",),
    ("reviewer", "guarded"): ("gpt-5.6-terra",),
}
EFFORT_PRIORITY = ("max", "xhigh", "high")


class RoutingCatalogError(ValueError):
    """A route cannot be safely resolved from local facts."""


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingCatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_bytes(raw: bytes, label: str, *, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise RoutingCatalogError(f"{label} exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RoutingCatalogError) as error:
        raise RoutingCatalogError(f"{label} is not valid JSON") from error


def native_capability_records(catalog: object) -> list[dict[str, str]]:
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get("models"), list):
        raise RoutingCatalogError("native capability catalogue is malformed")
    records: set[tuple[str, str]] = set()
    for index, model in enumerate(catalog["models"]):
        if not isinstance(model, Mapping):
            raise RoutingCatalogError(f"native model {index} is malformed")
        if model.get("multi_agent_version") == "disabled":
            continue
        slug = model.get("slug")
        if not isinstance(slug, str) or MODEL_RE.fullmatch(slug) is None:
            continue
        levels = model.get("supported_reasoning_levels")
        if not isinstance(levels, list):
            continue
        for level in levels:
            if isinstance(level, Mapping):
                effort = level.get("effort")
                if isinstance(effort, str) and EFFORT_RE.fullmatch(effort):
                    records.add((slug, effort))
    return [
        {"effort": effort, "model": model}
        for model, effort in sorted(records, key=lambda pair: (pair[0], pair[1]))
    ]


def _normalize_constraints(value: object, label: str) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or set(value) != {"fixed_effort", "fixed_model", "source"}:
        raise RoutingCatalogError(f"{label} is malformed")
    fixed_model = value["fixed_model"]
    fixed_effort = value["fixed_effort"]
    source = value["source"]
    if fixed_model is not None and (not isinstance(fixed_model, str) or MODEL_RE.fullmatch(fixed_model) is None):
        raise RoutingCatalogError(f"{label}.fixed_model is malformed")
    if fixed_effort is not None and (not isinstance(fixed_effort, str) or EFFORT_RE.fullmatch(fixed_effort) is None):
        raise RoutingCatalogError(f"{label}.fixed_effort is malformed")
    if source not in {"automatic", "user"}:
        raise RoutingCatalogError(f"{label}.source is invalid")
    if source == "automatic" and (fixed_model is not None or fixed_effort is not None):
        raise RoutingCatalogError(f"{label} pins require source=user")
    if source == "user" and fixed_model is None and fixed_effort is None:
        raise RoutingCatalogError(f"{label} source=user requires a model or effort pin")
    return {
        "fixed_effort": fixed_effort,
        "fixed_model": fixed_model,
        "source": source,
    }


def _normalize_request(value: object, index: int) -> dict[str, Any]:
    required = {"assurance", "constraints", "node", "role"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise RoutingCatalogError(f"route request {index} is malformed")
    node = value["node"]
    role = value["role"]
    assurance = value["assurance"]
    if not isinstance(node, str) or NODE_RE.fullmatch(node) is None:
        raise RoutingCatalogError(f"route request {index}.node is malformed")
    if role not in ROLES or assurance not in ASSURANCES:
        raise RoutingCatalogError(f"route request {index} role or assurance is invalid")
    if role == "reviewer" and assurance != "guarded":
        raise RoutingCatalogError("reviewer routes must be guarded")
    return {
        "assurance": assurance,
        "constraints": _normalize_constraints(value["constraints"], f"route request {index}.constraints"),
        "node": node,
        "role": role,
    }


def _effort_order(supported: set[str], requested: str | None = None) -> list[str]:
    if requested is not None:
        return [requested] if requested in supported else []
    return [effort for effort in EFFORT_PRIORITY if effort in supported]


def _default_models(role: str, assurance: str) -> list[str]:
    return list(DEFAULT_ROUTE_MODELS[(role, assurance)])


def _candidate_pairs(
    request: Mapping[str, Any], native_records: list[dict[str, str]]
) -> list[dict[str, str]]:
    constraints = request["constraints"]
    fixed_model = constraints["fixed_model"]
    fixed_effort = constraints["fixed_effort"]
    supported: dict[str, set[str]] = {}
    for record in native_records:
        supported.setdefault(record["model"], set()).add(record["effort"])

    if constraints["source"] == "automatic":
        candidates: list[dict[str, str]] = []
        for model in _default_models(request["role"], request["assurance"]):
            for effort in _effort_order(supported.get(model, set())):
                candidates.append({"effort": effort, "model": model})
                if len(candidates) >= MAX_FALLBACK_CANDIDATES:
                    break
            if len(candidates) >= MAX_FALLBACK_CANDIDATES:
                break
    else:
        if fixed_model is not None:
            model_candidates = [fixed_model]
        else:
            model_candidates = _default_models(
                request["role"], request["assurance"]
            )
        candidates = []
        for model in model_candidates:
            for effort in _effort_order(supported.get(model, set()), fixed_effort):
                pair = {"effort": effort, "model": model}
                if pair not in candidates:
                    candidates.append(pair)
                if len(candidates) >= MAX_FALLBACK_CANDIDATES:
                    break
            if len(candidates) >= MAX_FALLBACK_CANDIDATES:
                break

    if (
        constraints["source"] == "user"
        and fixed_model is not None
        and fixed_effort is not None
    ):
        if not candidates:
            raise RoutingCatalogError("user-fixed model/effort pair is not supported by native Agents")
        return candidates[:1]
    if not candidates:
        raise RoutingCatalogError("route has no supported native Agent candidate; keep the node in Primary")
    return candidates


def resolve_route_plan(
    requests: object,
    native_catalog: object,
) -> dict[str, Any]:
    """Resolve all node routes in one deterministic local operation."""

    if not isinstance(requests, list) or not requests:
        raise RoutingCatalogError("route requests must be a non-empty list")
    normalized = [_normalize_request(value, index) for index, value in enumerate(requests)]
    if len({item["node"] for item in normalized}) != len(normalized):
        raise RoutingCatalogError("route request nodes must be unique")
    records = native_capability_records(native_catalog)
    routes: list[dict[str, Any]] = []
    for request in normalized:
        candidates = _candidate_pairs(request, records)
        routes.append(
            {
                "assurance": request["assurance"],
                "candidates": candidates,
                "node": request["node"],
                "role": request["role"],
            }
        )
    routes.sort(key=lambda item: item["node"])
    return {
        "protocol": ROUTE_PLAN_PROTOCOL,
        "routes": routes,
    }


def load_native_catalog(*, executable: Path | None = None, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Read the bundled native capability catalogue once."""

    if executable is None:
        name = "codex.cmd" if os.name == "nt" else "codex"
        resolved = shutil.which(name)
        executable = Path(resolved) if resolved else None
    if executable is None:
        raise RoutingCatalogError("Codex CLI is unavailable")
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
        raise RoutingCatalogError("Codex native model catalogue is unavailable") from error
    if completed.returncode != 0:
        raise RoutingCatalogError("Codex native model catalogue command failed")
    return load_json_bytes(
        completed.stdout,
        "Codex native model catalogue",
        max_bytes=NATIVE_CATALOG_MAX_BYTES,
    )

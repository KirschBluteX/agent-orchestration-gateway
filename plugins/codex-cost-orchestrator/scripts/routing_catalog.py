#!/usr/bin/env python3
"""Network-free v8 route policy and native capability adapter.

The module has one deep interface: resolve a complete node route plan from a
static policy, explicit user pins, and the host's native capability catalogue.
It never contacts Radar, records billing data, or invokes another model.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - installer requires Python 3.11+
    tomllib = None  # type: ignore[assignment]

from decision_policy import ASSURANCES, ROLES
from protocol_hash import canonical_bytes


ROUTE_PLAN_PROTOCOL = "cco.route-plan.v5"
ROUTE_PLAN_DOMAIN = b"cco.route-plan.v5\0"
STATIC_DEFAULTS_PROTOCOL = "cco.static-route-defaults.v2"
STATIC_DEFAULTS_DOMAIN = b"cco.static-route-defaults.v2\0"
NATIVE_CATALOG_DOMAIN = b"cco.routing-native-catalog.v2\0"
INPUT_MAX_BYTES = 1024 * 1024
NATIVE_CATALOG_MAX_BYTES = 4 * 1024 * 1024
MAX_FALLBACK_CANDIDATES = 4
NATIVE_MULTI_AGENT_VERSIONS = frozenset({"v1", "v2"})
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
REJECTION_TICKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

DEFAULT_ROUTE_MODELS: dict[tuple[str, str], tuple[str, ...]] = {
    ("explorer", "mechanical"): ("gpt-5.6-luna", "gpt-5.6-terra"),
    ("worker", "mechanical"): ("gpt-5.6-luna", "gpt-5.6-terra"),
    ("explorer", "bounded"): ("gpt-5.6-terra", "gpt-5.6-luna"),
    ("worker", "bounded"): ("gpt-5.6-terra", "gpt-5.6-luna"),
    ("explorer", "guarded"): ("gpt-5.6-terra",),
    ("worker", "guarded"): ("gpt-5.6-terra",),
    ("reviewer", "mechanical"): ("gpt-5.6-terra",),
    ("reviewer", "bounded"): ("gpt-5.6-terra",),
    ("reviewer", "guarded"): ("gpt-5.6-terra",),
}
EFFORT_PRIORITY = ("max", "xhigh", "high")
STATIC_DEFAULTS = {
    "protocol": STATIC_DEFAULTS_PROTOCOL,
    "routes": {
        f"{role}.{assurance}": list(models)
        for (role, assurance), models in sorted(DEFAULT_ROUTE_MODELS.items())
    },
    "effort_priority": list(EFFORT_PRIORITY),
}


class RoutingCatalogError(ValueError):
    """A route cannot be safely resolved from local facts."""


def _sha(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(value)).hexdigest()


STATIC_DEFAULTS_SHA256 = _sha(STATIC_DEFAULTS_DOMAIN, STATIC_DEFAULTS)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingCatalogError(f"{label} must be non-empty text")
    return value


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingCatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_bytes(raw: bytes, label: str) -> Any:
    if len(raw) > INPUT_MAX_BYTES:
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
        visibility_values = (
            ("show_in_picker", True),
            ("hidden", False),
            ("disabled", False),
        )
        if any(
            key in model
            and (type(model[key]) is not bool or model[key] is not required)
            for key, required in visibility_values
        ):
            continue
        if "visibility" in model and model.get("visibility") not in {"list", "visible", "picker"}:
            continue
        version = model.get("multi_agent_version")
        if version not in NATIVE_MULTI_AGENT_VERSIONS:
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


def native_catalog_sha256(catalog: object) -> str:
    records = native_capability_records(catalog)
    return _sha(NATIVE_CATALOG_DOMAIN, {"records": records})


def route_plan_sha256(plan: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha(ROUTE_PLAN_DOMAIN, unsigned)


def validate_route_pair(value: object, label: str = "route pair") -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"effort", "model"}:
        raise RoutingCatalogError(f"{label} is malformed")
    model = value["model"]
    effort = value["effort"]
    if not isinstance(model, str) or MODEL_RE.fullmatch(model) is None:
        raise RoutingCatalogError(f"{label}.model is malformed")
    if not isinstance(effort, str) or EFFORT_RE.fullmatch(effort) is None:
        raise RoutingCatalogError(f"{label}.effort is malformed")
    return {"effort": effort, "model": model}


def route_pair_is_fully_fixed(constraints: Mapping[str, Any]) -> bool:
    return constraints.get("fixed_model") is not None and constraints.get("fixed_effort") is not None


def validate_route_constraints(value: object, label: str = "route constraints") -> dict[str, str | None]:
    """Compatibility adapter for prepared-artifact validation."""

    return _normalize_constraints(value, label)


def _sol_family(model: str) -> bool:
    return "sol" in model.casefold()


def _luna_family(model: str) -> bool:
    return "luna" in model.casefold()


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


def _validate_policy_candidates(role: str, assurance: str, candidates: object, label: str) -> list[dict[str, str]]:
    if not isinstance(candidates, list) or not candidates:
        raise RoutingCatalogError(f"{label}.candidates must be a non-empty list")
    normalized: list[dict[str, str]] = []
    for index, candidate in enumerate(candidates):
        pair = validate_route_pair(candidate, f"{label}.candidates[{index}]")
        if pair["effort"] not in EFFORT_PRIORITY:
            raise RoutingCatalogError(
                "automatic configuration effort must be max, xhigh, or high"
            )
        if _sol_family(pair["model"]):
            raise RoutingCatalogError("automatic configuration cannot include Sol")
        if (role == "reviewer" or assurance == "guarded") and _luna_family(pair["model"]):
            raise RoutingCatalogError("reviewer/guarded configuration cannot include Luna")
        normalized.append(pair)
    if len({(item["model"], item["effort"]) for item in normalized}) != len(normalized):
        raise RoutingCatalogError(f"{label}.candidates must be duplicate-free")
    return normalized


def normalize_route_policy(value: object, label: str = "route policy") -> dict[str, list[dict[str, str]]]:
    """Normalize role×assurance candidate overrides without weakening floors."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RoutingCatalogError(f"{label} is malformed")
    routes = value.get("routes", value)
    if not isinstance(routes, Mapping):
        raise RoutingCatalogError(f"{label}.routes is malformed")
    normalized: dict[str, list[dict[str, str]]] = {}
    for role, assurance_map in routes.items():
        if role not in ROLES or not isinstance(assurance_map, Mapping):
            raise RoutingCatalogError(f"{label} contains an invalid role")
        for assurance, route in assurance_map.items():
            if assurance not in ASSURANCES or not isinstance(route, Mapping):
                raise RoutingCatalogError(f"{label} contains an invalid assurance")
            key = f"{role}.{assurance}"
            normalized[key] = _validate_policy_candidates(
                role, assurance, route.get("candidates"), f"{label}.{key}"
            )
    return normalized


def _policy_models(policy: Mapping[str, list[dict[str, str]]], role: str, assurance: str) -> list[dict[str, str]]:
    key = f"{role}.{assurance}"
    if key in policy:
        return [dict(item) for item in policy[key]]
    return [{"model": model, "effort": "max"} for model in _default_models(role, assurance)]


def _policy_document(policy: Mapping[str, list[dict[str, str]]]) -> dict[str, Any]:
    """Return the nested public policy shape accepted by ``resolve_route_plan``."""

    document: dict[str, Any] = {}
    for key, candidates in policy.items():
        role, assurance = key.split(".", 1)
        document.setdefault(role, {})[assurance] = {
            "candidates": [dict(candidate) for candidate in candidates]
        }
    return document


def _candidate_pairs(request: Mapping[str, Any], native_records: list[dict[str, str]], policy: Mapping[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    constraints = request["constraints"]
    fixed_model = constraints["fixed_model"]
    fixed_effort = constraints["fixed_effort"]
    supported: dict[str, set[str]] = {}
    for record in native_records:
        supported.setdefault(record["model"], set()).add(record["effort"])

    policy_key = f"{request['role']}.{request['assurance']}"
    if constraints["source"] == "automatic" and policy_key in policy:
        exact = [
            dict(pair)
            for pair in policy[policy_key]
            if pair["effort"] in supported.get(pair["model"], set())
        ][:MAX_FALLBACK_CANDIDATES]
        if not exact:
            raise RoutingCatalogError(
                "configured route has no supported native Agent candidate; keep the node in Primary"
            )
        return exact

    if constraints["source"] == "user" and fixed_model is not None:
        model_candidates = [fixed_model]
    else:
        configured = _policy_models(policy, request["role"], request["assurance"])
        model_candidates = [item["model"] for item in configured]

    candidates: list[dict[str, str]] = []
    for model in model_candidates:
        efforts = supported.get(model, set())
        requested_effort = fixed_effort
        if constraints["source"] == "user" and fixed_model is None:
            # An effort-only pin still permits the policy's model order.
            requested_effort = fixed_effort
        elif constraints["source"] == "automatic" and policy_key in policy:
            # A project/global policy entry is an exact user-authored pair.  The
            # built-in table is model-only and therefore adapts effort locally.
            configured_pair = next(
                (item for item in _policy_models(policy, request["role"], request["assurance"]) if item["model"] == model),
                None,
            )
            requested_effort = configured_pair["effort"] if configured_pair else None
        for effort in _effort_order(efforts, requested_effort):
            pair = {"effort": effort, "model": model}
            if pair not in candidates:
                candidates.append(pair)
            if constraints["source"] == "automatic":
                # Automatic routes choose one effort per model.  A model-only user
                # pin keeps the model fixed while precompiling its effort fallback.
                break
        if len(candidates) >= MAX_FALLBACK_CANDIDATES:
            break

    if constraints["source"] == "user" and route_pair_is_fully_fixed(constraints):
        if not candidates:
            raise RoutingCatalogError("user-fixed model/effort pair is not supported by native Agents")
        return candidates[:1]
    if not candidates:
        raise RoutingCatalogError("route has no supported native Agent candidate; keep the node in Primary")
    return candidates


def _route_decision_sha256(request: Mapping[str, Any], candidates: list[dict[str, str]], policy_sha256: str) -> str:
    return _sha(
        ROUTE_PLAN_DOMAIN,
        {
            "assurance": request["assurance"],
            "candidates": candidates,
            "constraints": request["constraints"],
            "node": request["node"],
            "policy_sha256": policy_sha256,
            "role": request["role"],
        },
    )


def resolve_route_plan(
    requests: object,
    native_catalog: object,
    *,
    policy: object = None,
) -> dict[str, Any]:
    """Resolve all node routes in one deterministic local operation."""

    if not isinstance(requests, list) or not requests:
        raise RoutingCatalogError("route requests must be a non-empty list")
    normalized = [_normalize_request(value, index) for index, value in enumerate(requests)]
    if len({item["node"] for item in normalized}) != len(normalized):
        raise RoutingCatalogError("route request nodes must be unique")
    records = native_capability_records(native_catalog)
    normalized_policy = normalize_route_policy(policy)
    policy_identity = _sha(
        STATIC_DEFAULTS_DOMAIN,
        {"defaults": STATIC_DEFAULTS, "overrides": normalized_policy},
    )
    routes: list[dict[str, Any]] = []
    for request in normalized:
        candidates = _candidate_pairs(request, records, normalized_policy)
        routes.append(
            {
                "assurance": request["assurance"],
                "candidates": candidates,
                "constraints": dict(request["constraints"]),
                "decision_sha256": _route_decision_sha256(request, candidates, policy_identity),
                "dispatch": {"rank": 1, "rejection_tickets": []},
                "node": request["node"],
                "role": request["role"],
                "selected": dict(candidates[0]),
                "status": "ready",
            }
        )
    routes.sort(key=lambda item: item["node"])
    plan: dict[str, Any] = {
        "native_catalog_sha256": native_catalog_sha256(native_catalog),
        "plan_sha256": "",
        "policy_sha256": policy_identity,
        "protocol": ROUTE_PLAN_PROTOCOL,
        "routes": routes,
    }
    plan["plan_sha256"] = route_plan_sha256(plan)
    return validate_route_plan(plan)


def _validate_route_entry(route: object, index: int) -> dict[str, Any]:
    fields = {
        "assurance", "candidates", "constraints", "decision_sha256", "dispatch",
        "node", "role", "selected", "status"
    }
    if not isinstance(route, Mapping) or set(route) != fields:
        raise RoutingCatalogError(f"route plan entry {index} is malformed")
    node = route["node"]
    role = route["role"]
    assurance = route["assurance"]
    status = route["status"]
    if not isinstance(node, str) or NODE_RE.fullmatch(node) is None or role not in ROLES or assurance not in ASSURANCES:
        raise RoutingCatalogError(f"route plan entry {index} identity is invalid")
    if status != "ready":
        raise RoutingCatalogError("route plan contains an unusable entry")
    constraints = _normalize_constraints(route["constraints"], f"route plan entry {index}.constraints")
    candidates = route["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_FALLBACK_CANDIDATES:
        raise RoutingCatalogError("route plan candidates are malformed")
    normalized_candidates = [validate_route_pair(candidate, f"route plan candidate {index}") for candidate in candidates]
    if len({(item["model"], item["effort"]) for item in normalized_candidates}) != len(normalized_candidates):
        raise RoutingCatalogError("route plan candidates must be duplicate-free")
    if constraints["source"] == "user" and constraints["fixed_model"] is not None and any(item["model"] != constraints["fixed_model"] for item in normalized_candidates):
        raise RoutingCatalogError("route plan violates fixed model")
    if constraints["source"] == "user" and constraints["fixed_effort"] is not None and any(item["effort"] != constraints["fixed_effort"] for item in normalized_candidates):
        raise RoutingCatalogError("route plan violates fixed effort")
    if constraints["source"] == "automatic":
        if any(_sol_family(item["model"]) for item in normalized_candidates):
            raise RoutingCatalogError("automatic route contains Sol")
        if (role == "reviewer" or assurance == "guarded") and any(_luna_family(item["model"]) for item in normalized_candidates):
            raise RoutingCatalogError("guarded/reviewer route contains Luna")
    dispatch = route["dispatch"]
    if not isinstance(dispatch, Mapping) or set(dispatch) != {"rank", "rejection_tickets"}:
        raise RoutingCatalogError("route dispatch state is malformed")
    rank = dispatch["rank"]
    tickets = dispatch["rejection_tickets"]
    if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= len(normalized_candidates) or not isinstance(tickets, list):
        raise RoutingCatalogError("route dispatch state is malformed")
    if len(tickets) != rank - 1 or len(set(tickets)) != len(tickets) or any(not isinstance(ticket, str) or REJECTION_TICKET_RE.fullmatch(ticket) is None for ticket in tickets):
        raise RoutingCatalogError("route rejection tickets are malformed")
    selected = validate_route_pair(route["selected"], "route plan selected route")
    if selected != normalized_candidates[rank - 1]:
        raise RoutingCatalogError("route selected pair does not match dispatch rank")
    decision_hash = route["decision_sha256"]
    if not isinstance(decision_hash, str) or SHA256_RE.fullmatch(decision_hash) is None:
        raise RoutingCatalogError("route decision hash is malformed")
    return {
        "assurance": assurance,
        "candidates": normalized_candidates,
        "constraints": constraints,
        "decision_sha256": decision_hash,
        "dispatch": {"rank": rank, "rejection_tickets": list(tickets)},
        "node": node,
        "role": role,
        "selected": selected,
        "status": status,
    }


def validate_route_plan(value: object) -> dict[str, Any]:
    required = {"native_catalog_sha256", "plan_sha256", "policy_sha256", "protocol", "routes"}
    if not isinstance(value, Mapping) or set(value) != required or value["protocol"] != ROUTE_PLAN_PROTOCOL:
        raise RoutingCatalogError("route plan is malformed")
    for key in ("native_catalog_sha256", "policy_sha256", "plan_sha256"):
        if not isinstance(value[key], str) or SHA256_RE.fullmatch(value[key]) is None:
            raise RoutingCatalogError(f"route plan {key} is malformed")
    routes = value["routes"]
    if not isinstance(routes, list) or not routes:
        raise RoutingCatalogError("route plan routes are malformed")
    normalized_routes = [_validate_route_entry(route, index) for index, route in enumerate(routes)]
    if [route["node"] for route in normalized_routes] != sorted(route["node"] for route in normalized_routes):
        raise RoutingCatalogError("route plan routes must be sorted by node")
    if len({route["node"] for route in normalized_routes}) != len(normalized_routes):
        raise RoutingCatalogError("route plan routes must be unique by node")
    normalized = {
        "native_catalog_sha256": value["native_catalog_sha256"],
        "plan_sha256": value["plan_sha256"],
        "policy_sha256": value["policy_sha256"],
        "protocol": ROUTE_PLAN_PROTOCOL,
        "routes": normalized_routes,
    }
    if route_plan_sha256(normalized) != normalized["plan_sha256"]:
        raise RoutingCatalogError("route plan hash mismatch")
    return normalized


def advance_route_plan(
    plan: object,
    *,
    node: str,
    rejected_model: str,
    rejected_effort: str,
    rejection_ticket: str,
) -> dict[str, Any]:
    validated = validate_route_plan(plan)
    if not isinstance(node, str) or NODE_RE.fullmatch(node) is None or not isinstance(rejection_ticket, str) or REJECTION_TICKET_RE.fullmatch(rejection_ticket) is None:
        raise RoutingCatalogError("route fallback identity is invalid")
    updated = copy.deepcopy(validated)
    matches = [route for route in updated["routes"] if route["node"] == node]
    if len(matches) != 1:
        raise RoutingCatalogError("route node is missing")
    route = matches[0]
    if route_pair_is_fully_fixed(route["constraints"]):
        raise RoutingCatalogError("fixed route cannot advance")
    if route["selected"] != {"effort": rejected_effort, "model": rejected_model}:
        raise RoutingCatalogError("rejected route does not match active selection")
    rank = route["dispatch"]["rank"]
    if rank >= len(route["candidates"]):
        raise RoutingCatalogError("static routing fallback order is exhausted")
    route["dispatch"] = {
        "rank": rank + 1,
        "rejection_tickets": [*route["dispatch"]["rejection_tickets"], rejection_ticket],
    }
    route["selected"] = dict(route["candidates"][rank])
    updated["plan_sha256"] = route_plan_sha256(updated)
    return validate_route_plan(updated)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _load_toml(path: Path, label: str) -> dict[str, Any]:
    if tomllib is None:
        raise RoutingCatalogError("Python 3.11 or newer is required for route configuration")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RoutingCatalogError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise RoutingCatalogError(f"{label} is malformed")
    return value


def _canonical_root(value: Path) -> str:
    return os.path.normcase(str(value.expanduser().resolve()))


def load_route_policy(repo: Path, *, codex_home: Path | None = None) -> dict[str, Any]:
    """Load global and explicitly trusted project policy without network state."""

    home = (codex_home or _codex_home()).resolve()
    global_path = home / "cco.toml"
    global_config = _load_toml(global_path, "global CCO configuration") if global_path.is_file() else {}
    trusted = global_config.get("trusted_project_roots", [])
    if not isinstance(trusted, list) or any(not isinstance(item, str) for item in trusted):
        raise RoutingCatalogError("trusted_project_roots must be a list of paths")
    project_root = _canonical_root(Path(repo))
    trusted_roots = {_canonical_root(Path(item)) for item in trusted}
    project_path = Path(repo).resolve() / ".codex" / "cco.toml"
    project_config: dict[str, Any] = {}
    if project_root in trusted_roots and project_path.is_file():
        project_config = _load_toml(project_path, "project CCO configuration")
    global_policy = normalize_route_policy(global_config.get("routes"), "global CCO routes")
    project_policy = normalize_route_policy(project_config.get("routes"), "project CCO routes")
    merged = {**global_policy, **project_policy}
    identity = _sha(
        STATIC_DEFAULTS_DOMAIN,
        {"global": global_policy, "project": project_policy, "trusted": project_root in trusted_roots},
    )
    return {
        "policy": _policy_document(merged),
        "policy_sha256": identity,
        "project_trusted": project_root in trusted_roots,
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
    return load_json_bytes(completed.stdout, "Codex native model catalogue")


def resolve_graph_route_plan(requests: object, *, native_loader: Callable[[], dict[str, Any]] = load_native_catalog, policy: object = None) -> dict[str, Any]:
    return resolve_route_plan(requests, native_loader(), policy=policy)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Resolve a network-free static Codex Agent route plan.")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve-plan", help="Resolve ordered static route requests from stdin")
    advance = sub.add_parser("advance-plan", help="Advance one bound route after pre-thread rejection")
    advance.add_argument("--node", required=True)
    advance.add_argument("--rejected-model", required=True)
    advance.add_argument("--rejected-effort", required=True)
    advance.add_argument("--rejection-ticket", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        raw = sys.stdin.buffer.read(INPUT_MAX_BYTES + 1)
        value = load_json_bytes(raw, "routing input")
        if args.command == "resolve-plan":
            output = resolve_graph_route_plan(value)
        else:
            output = advance_route_plan(
                value,
                node=args.node,
                rejected_model=args.rejected_model,
                rejected_effort=args.rejected_effort,
                rejection_ticket=args.rejection_ticket,
            )
        print(canonical_bytes(output).decode("utf-8"))
        return 0
    except RoutingCatalogError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

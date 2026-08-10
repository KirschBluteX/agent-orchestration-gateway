#!/usr/bin/env python3
"""Network-free cco.v9 route policy and native capability adapter.

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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - installer requires Python 3.11+
    tomllib = None  # type: ignore[assignment]

ROUTE_PLAN_PROTOCOL = "cco.route.v1"
NATIVE_CATALOG_MAX_BYTES = 4 * 1024 * 1024
MAX_FALLBACK_CANDIDATES = 6
ASSURANCES = frozenset({"mechanical", "bounded", "guarded"})
ROLES = frozenset({"explorer", "worker", "reviewer"})
NATIVE_MULTI_AGENT_VERSION = "v2"
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
        if version != NATIVE_MULTI_AGENT_VERSION:
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


def _sol_family(model: str) -> bool:
    return "sol" in model.casefold()


def _luna_family(model: str) -> bool:
    return "luna" in model.casefold()


def _terra_family(model: str) -> bool:
    return "terra" in model.casefold()


def _automatic_model_allowed(role: str, assurance: str, model: str) -> bool:
    if _sol_family(model):
        return False
    if role in {"explorer", "worker"} and assurance == "mechanical":
        return _luna_family(model) or _terra_family(model)
    return _terra_family(model)


def _automatic_candidate_order(
    role: str, assurance: str, pair: Mapping[str, str]
) -> tuple[int, str, int]:
    model = pair["model"]
    family = 0 if _luna_family(model) else 1
    if role not in {"explorer", "worker"} or assurance != "mechanical":
        family = 0
    return (family, model, EFFORT_PRIORITY.index(pair["effort"]))


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
        if not _automatic_model_allowed(role, assurance, pair["model"]):
            raise RoutingCatalogError(
                "automatic configuration violates the Luna/Terra route policy"
            )
        normalized.append(pair)
    if len({(item["model"], item["effort"]) for item in normalized}) != len(normalized):
        raise RoutingCatalogError(f"{label}.candidates must be duplicate-free")
    if normalized != sorted(
        normalized,
        key=lambda item: _automatic_candidate_order(role, assurance, item),
    ):
        raise RoutingCatalogError(
            f"{label}.candidates must preserve automatic route preference"
        )
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
            if role == "reviewer" and assurance != "guarded":
                raise RoutingCatalogError(f"{label} reviewer routes must be guarded")
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
    if constraints["source"] == "automatic":
        if policy_key in policy:
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
            configured = _policy_models(
                policy, request["role"], request["assurance"]
            )
            model_candidates = []
            for configured_pair in configured:
                if configured_pair["model"] not in model_candidates:
                    model_candidates.append(configured_pair["model"])
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

    if constraints["source"] == "user" and route_pair_is_fully_fixed(constraints):
        if not candidates:
            raise RoutingCatalogError("user-fixed model/effort pair is not supported by native Agents")
        return candidates[:1]
    if not candidates:
        raise RoutingCatalogError("route has no supported native Agent candidate; keep the node in Primary")
    return candidates


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
    routes: list[dict[str, Any]] = []
    for request in normalized:
        candidates = _candidate_pairs(request, records, normalized_policy)
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
    return {
        "policy": _policy_document(merged),
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
    return load_json_bytes(
        completed.stdout,
        "Codex native model catalogue",
        max_bytes=NATIVE_CATALOG_MAX_BYTES,
    )

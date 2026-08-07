#!/usr/bin/env python3
"""Canonical cco.v8 dispatch, continuation, and result envelopes."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping

from decision_policy import ASSURANCES, require_role
from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    parse_canonical_json_object,
    require_canonical_task_path,
    require_repository_path,
    require_repository_scope,
)
from routing_catalog import RoutingCatalogError, validate_route_pair, validate_route_plan


PROTOCOL = "cco.v8"
DISPATCH_HEADER = "CCO_DISPATCH cco.v8"
RESULT_HEADER = "CCO_RESULT cco.v8"
CAPSULE_DOMAIN = b"cco.dispatch-capsule.v8\0"
RESULT_DOMAIN = b"cco.result-capsule.v8\0"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,95}$")
EPOCH = re.compile(r"^e[0-9]{2,}$")
ACCEPTANCE_ID = re.compile(r"^A[0-9]{2,}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
FAILURE_SIGNATURE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,255}$")
POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
READ_ROLE = "cost_orchestrator_read_leaf"
WRITE_ROLE = "cost_orchestrator_write_leaf"
PHYSICAL_ROLES = frozenset({READ_ROLE, WRITE_ROLE})
MODES = frozenset({"light", "strict", "fresh", "delta"})
STATUSES = frozenset({"complete", "partial", "blocked"})
DISPOSITIONS = frozenset({"continue", "accept", "retire"})
MAX_WIRE_BYTES = 1024 * 1024


class CapsuleError(ValueError):
    """A v8 envelope violates its complete interface."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapsuleError(f"{label} must be non-empty text")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CapsuleError(f"{label} must be a sha256 identity")
    return value


def _workspace_root(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapsuleError(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise CapsuleError(f"{label} must be an absolute path")
    if os.path.normpath(value) != value:
        raise CapsuleError(f"{label} must be a normalized absolute path")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CapsuleError(f"{label} must be an integer >= {minimum}")
    return value


def _canonical(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapsuleError(f"{label} must be an object")
    try:
        encoded = canonical_bytes(dict(value))
        return parse_canonical_json_object(encoded.decode("utf-8"), label)
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(dict(value))).hexdigest()


def capsule_sha256(capsule: Mapping[str, Any]) -> str:
    value = dict(capsule)
    value.pop("capsule_sha256", None)
    return _digest(CAPSULE_DOMAIN, value)


def result_sha256(result: Mapping[str, Any]) -> str:
    value = dict(result)
    value.pop("result_sha256", None)
    return _digest(RESULT_DOMAIN, value)


def _physical_role(role: str) -> str:
    if role == "worker":
        return WRITE_ROLE
    if role in {"explorer", "reviewer"}:
        return READ_ROLE
    raise CapsuleError(f"unsupported logical role: {role}")


def _route_component(value: str) -> str:
    tokens = [token for token in re.split(r"[^a-z0-9]+", value.casefold()) if token]
    for family in ("luna", "terra", "sol"):
        if family in tokens:
            return family
    return "_".join(tokens)


def _bounded_name_component(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]
    return value[: limit - 8].rstrip("_") + "_h" + digest


def _task_name(
    *,
    role: str,
    node: str,
    generation: int,
    epoch: str | None,
    model: str,
    effort: str,
) -> str:
    model_component = _bounded_name_component(_route_component(model), limit=18)
    effort_component = _bounded_name_component(_route_component(effort), limit=14)
    route = f"{model_component}_{effort_component}"
    if role == "reviewer":
        if epoch is None:
            raise CapsuleError("reviewer requires an epoch")
        prefix = "review_" + _bounded_name_component(epoch, limit=14)
    else:
        prefix = role
    intended = f"{prefix}_{node}_{route}_g{generation:02d}"
    if len(intended) <= 96:
        return intended
    digest = hashlib.sha256(intended.encode("utf-8")).hexdigest()[:8]
    suffix = f"_h{digest}_{route}_g{generation:02d}"
    node_budget = 96 - len(prefix) - 1 - len(suffix)
    if node_budget < 1:
        raise CapsuleError("route-aware task name cannot fit the protocol limit")
    node_prefix = node[:node_budget].rstrip("_") or node[0]
    return f"{prefix}_{node_prefix}{suffix}"


def _scope_list(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise CapsuleError(f"{label} must be a list")
    try:
        scopes = [require_repository_scope(item, f"{label}[{i}]") for i, item in enumerate(value)]
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    if scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"])):
        raise CapsuleError(f"{label} must be sorted")
    if len({(item["kind"], item["path"]) for item in scopes}) != len(scopes):
        raise CapsuleError(f"{label} must be duplicate-free")
    return scopes


def _route_binding(value: object) -> dict[str, Any]:
    required = {"constraints", "decision_sha256", "plan_sha256", "rank", "selected"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise CapsuleError("route binding is malformed")
    constraints = value["constraints"]
    if not isinstance(constraints, Mapping) or set(constraints) != {"fixed_effort", "fixed_model", "source"}:
        raise CapsuleError("route constraints are malformed")
    for key in ("fixed_model", "fixed_effort"):
        item = constraints[key]
        if item is not None and not isinstance(item, str):
            raise CapsuleError(f"route constraints {key} is malformed")
    if constraints["source"] not in {"automatic", "user"}:
        raise CapsuleError("route constraint source is invalid")
    selected = validate_route_pair(value["selected"], "route.selected")
    rank = _integer(value["rank"], "route.rank", minimum=1)
    return {
        "constraints": {
            "fixed_effort": constraints["fixed_effort"],
            "fixed_model": constraints["fixed_model"],
            "source": constraints["source"],
        },
        "decision_sha256": _sha(value["decision_sha256"], "route.decision_sha256"),
        "plan_sha256": _sha(value["plan_sha256"], "route.plan_sha256"),
        "rank": rank,
        "selected": selected,
    }


def _acceptance(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"mode", "reasons"}:
        raise CapsuleError("acceptance is malformed")
    if value["mode"] not in {"primary", "independent"} or not isinstance(value["reasons"], list):
        raise CapsuleError("acceptance mode or reasons is invalid")
    reasons = value["reasons"]
    if any(not isinstance(item, str) or not item for item in reasons) or reasons != sorted(set(reasons)):
        raise CapsuleError("acceptance reasons must be sorted and duplicate-free")
    return {"mode": value["mode"], "reasons": list(reasons)}


def _acceptance_ids(value: object, label: str = "acceptance_ids") -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or ACCEPTANCE_ID.fullmatch(item) is None for item in value)
        or value != sorted(set(value))
    ):
        raise CapsuleError(f"{label} must be sorted, unique acceptance IDs")
    return list(value)


def normalize_capsule(capsule: object) -> dict[str, Any]:
    value = _canonical(capsule, "capsule")
    required = {
        "acceptance", "acceptance_ids", "assurance", "baseline", "capsule_sha256", "contract",
        "execution", "generation", "graph_sha256", "mode", "node", "role",
        "route", "scopes", "protocol", "workspace_root",
    }
    optional = {"current_state", "delta", "epoch", "evidence", "previous_capsule_sha256"}
    if set(value) - (required | optional) or not required <= set(value):
        raise CapsuleError("capsule fields are incomplete or unsupported")
    if value["protocol"] != PROTOCOL:
        raise CapsuleError("capsule protocol is invalid")
    role = require_role(value["role"])
    if value["assurance"] not in ASSURANCES:
        raise CapsuleError("capsule assurance is invalid")
    if value["mode"] not in MODES:
        raise CapsuleError("capsule mode is invalid")
    node = value["node"]
    if not isinstance(node, str) or NODE.fullmatch(node) is None:
        raise CapsuleError("capsule node is invalid")
    generation = _integer(value["generation"], "capsule.generation", minimum=1)
    _sha(value["baseline"], "capsule.baseline")
    _sha(value["graph_sha256"], "capsule.graph_sha256")
    workspace_root = _workspace_root(value["workspace_root"], "capsule.workspace_root")
    contract = _canonical(value["contract"], "capsule.contract")
    if contract.get("node") != node:
        raise CapsuleError("contract node does not match capsule node")
    scopes = _scope_list(value["scopes"], "capsule.scopes")
    acceptance = _acceptance(value["acceptance"])
    acceptance_ids = _acceptance_ids(value["acceptance_ids"], "capsule.acceptance_ids")
    route = _route_binding(value["route"])
    execution = value["execution"]
    if not isinstance(execution, Mapping) or set(execution) != {"cursor", "fork_turns", "task_name"}:
        raise CapsuleError("capsule execution is malformed")
    cursor = _integer(execution["cursor"], "execution.cursor", minimum=0)
    fork_turns = execution["fork_turns"]
    if fork_turns != "none" and (not isinstance(fork_turns, str) or POSITIVE_INTEGER.fullmatch(fork_turns) is None):
        raise CapsuleError("execution.fork_turns must be none or a positive integer")
    task_name = execution["task_name"]
    if not isinstance(task_name, str) or TASK_NAME.fullmatch(task_name) is None:
        raise CapsuleError("execution.task_name is invalid")
    selected = route["selected"]
    expected_task_name = _task_name(
        role=role,
        node=node,
        generation=generation,
        epoch=value.get("epoch"),
        model=selected["model"],
        effort=selected["effort"],
    )
    if task_name != expected_task_name:
        raise CapsuleError("execution.task_name does not match the selected route")
    if role == "reviewer" and fork_turns != "none":
        raise CapsuleError("reviewer must use fork_turns=none")
    if value.get("epoch") is not None and (not isinstance(value["epoch"], str) or EPOCH.fullmatch(value["epoch"]) is None):
        raise CapsuleError("capsule epoch is invalid")
    if role == "reviewer" and value.get("epoch") is None:
        raise CapsuleError("reviewer epoch is required")
    if value.get("evidence") is not None:
        _canonical(value["evidence"], "capsule.evidence")
    if value.get("current_state") is not None:
        _sha(value["current_state"], "capsule.current_state")
    delta = None
    if value.get("delta") is not None:
        delta = _canonical(value["delta"], "capsule.delta")
    if value.get("previous_capsule_sha256") is not None:
        _sha(value["previous_capsule_sha256"], "capsule.previous_capsule_sha256")
    continuation_fields = {"delta", "previous_capsule_sha256"}
    if cursor == 0:
        if continuation_fields.intersection(value) or value["mode"] == "delta":
            raise CapsuleError("initial capsule cannot use continuation fields or delta mode")
        if role == "reviewer" and value["mode"] != "fresh":
            raise CapsuleError("initial reviewer capsule must use fresh mode")
        if role != "reviewer" and value["mode"] not in {"light", "strict"}:
            raise CapsuleError("initial explorer/worker capsule must use light or strict mode")
    else:
        if value["mode"] != "delta":
            raise CapsuleError("continuation requires delta mode")
        if not continuation_fields <= set(value) or not delta:
            raise CapsuleError("continuation requires a non-empty delta and previous identity")
    declared = _sha(value["capsule_sha256"], "capsule.capsule_sha256")
    normalized = dict(value)
    normalized.update(
        {
            "acceptance": acceptance,
            "acceptance_ids": acceptance_ids,
            "assurance": value["assurance"],
            "contract": contract,
            "execution": {"cursor": cursor, "fork_turns": fork_turns, "task_name": task_name},
            "generation": generation,
            "role": role,
            "route": route,
            "scopes": scopes,
            "workspace_root": workspace_root,
        }
    )
    if capsule_sha256(normalized) != declared:
        raise CapsuleError("capsule hash does not match")
    normalized["capsule_sha256"] = declared
    return normalized


def _route_from_plan(plan_value: object, *, node: str, role: str, assurance: str) -> dict[str, Any]:
    try:
        plan = validate_route_plan(plan_value)
    except RoutingCatalogError as error:
        raise CapsuleError(f"route plan is invalid: {error}") from error
    matches = [route for route in plan["routes"] if route["node"] == node and route["role"] == role and route["assurance"] == assurance]
    if len(matches) != 1:
        raise CapsuleError("route plan lacks one exact node route")
    route = matches[0]
    return _route_binding(
        {
            "constraints": route["constraints"],
            "decision_sha256": route["decision_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "rank": route["dispatch"]["rank"],
            "selected": route["selected"],
        }
    )


def compile_dispatch(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Compile one v8 native spawn request from a prepared node."""

    required = {
        "acceptance", "acceptance_ids", "assurance", "baseline", "contract", "fork_turns", "generation",
        "graph_sha256", "mode", "node", "role", "route", "scopes", "workspace_root",
    }
    if set(spec) - (required | {"current_state", "epoch", "evidence", "route_plan"}) or not required <= set(spec):
        raise CapsuleError("dispatch specification is incomplete")
    role = require_role(spec["role"])
    if spec["assurance"] not in ASSURANCES:
        raise CapsuleError("dispatch assurance is invalid")
    if spec["mode"] not in MODES:
        raise CapsuleError("dispatch mode is invalid")
    node = spec["node"]
    if not isinstance(node, str) or NODE.fullmatch(node) is None:
        raise CapsuleError("dispatch node is invalid")
    generation = _integer(spec["generation"], "dispatch.generation", minimum=1)
    epoch = spec.get("epoch")
    if epoch is not None and (not isinstance(epoch, str) or EPOCH.fullmatch(epoch) is None):
        raise CapsuleError("dispatch epoch is invalid")
    route = spec.get("route")
    if "route_plan" in spec:
        route = _route_from_plan(spec["route_plan"], node=node, role=role, assurance=spec["assurance"])
    route = _route_binding(route)
    contract = _canonical(spec["contract"], "dispatch.contract")
    if contract.get("node") != node:
        raise CapsuleError("dispatch contract node mismatch")
    acceptance = _acceptance(spec["acceptance"])
    if role == "reviewer" and acceptance["mode"] != "independent":
        raise CapsuleError("reviewer requires independent acceptance")
    capsule: dict[str, Any] = {
        "acceptance": acceptance,
        "acceptance_ids": _acceptance_ids(spec["acceptance_ids"], "dispatch.acceptance_ids"),
        "assurance": spec["assurance"],
        "baseline": _sha(spec["baseline"], "dispatch.baseline"),
        "contract": contract,
        "execution": {
            "cursor": 0,
            "fork_turns": spec["fork_turns"],
            "task_name": _task_name(
                role=role,
                node=node,
                generation=generation,
                epoch=epoch,
                model=route["selected"]["model"],
                effort=route["selected"]["effort"],
            ),
        },
        "generation": generation,
        "graph_sha256": _sha(spec["graph_sha256"], "dispatch.graph_sha256"),
        "mode": spec["mode"],
        "node": node,
        "protocol": PROTOCOL,
        "role": role,
        "route": route,
        "scopes": _scope_list(spec["scopes"], "dispatch.scopes"),
        "workspace_root": _workspace_root(spec["workspace_root"], "dispatch.workspace_root"),
    }
    for name in ("current_state", "epoch", "evidence"):
        if name in spec:
            capsule[name] = spec[name]
    capsule["capsule_sha256"] = capsule_sha256(capsule)
    normalized = normalize_capsule(capsule)
    selected = normalized["route"]["selected"]
    return {
        "agent_type": _physical_role(role),
        "fork_turns": normalized["execution"]["fork_turns"],
        "message": _render_dispatch(normalized),
        "model": selected["model"],
        "reasoning_effort": selected["effort"],
        "task_name": normalized["execution"]["task_name"],
    }


def compile_continuation(capsule: Mapping[str, Any], *, target: str, delta: Mapping[str, Any]) -> dict[str, str]:
    previous = normalize_capsule(capsule)
    try:
        require_canonical_task_path(target, "continuation target")
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    if target != "/root/" + previous["execution"]["task_name"]:
        raise CapsuleError("continuation target does not own the capsule")
    update = _canonical(delta, "continuation delta")
    if not update:
        raise CapsuleError("continuation delta must not be empty")
    continued = deepcopy(previous)
    old_hash = continued.pop("capsule_sha256")
    continued["delta"] = update
    continued["execution"]["cursor"] += 1
    continued["mode"] = "delta"
    continued["previous_capsule_sha256"] = old_hash
    continued["capsule_sha256"] = capsule_sha256(continued)
    return {"message": _render_dispatch(continued), "target": target}


def _render_dispatch(capsule: Mapping[str, Any]) -> str:
    normalized = normalize_capsule(capsule)
    return f"{DISPATCH_HEADER}\nCAPSULE_SHA256: {normalized['capsule_sha256']}\nCAPSULE_JSON: {canonical_bytes(normalized).decode('utf-8')}"


def parse_message(message: object) -> dict[str, Any]:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_WIRE_BYTES:
        raise CapsuleError("dispatch message is invalid or too large")
    lines = message.split("\n")
    if len(lines) != 3 or lines[0] != DISPATCH_HEADER or not lines[1].startswith("CAPSULE_SHA256: ") or not lines[2].startswith("CAPSULE_JSON: "):
        raise CapsuleError("dispatch message is not a compact v8 envelope")
    declared = _sha(lines[1][len("CAPSULE_SHA256: "):], "wire capsule hash")
    try:
        parsed = parse_canonical_json_object(lines[2][len("CAPSULE_JSON: "):], "CAPSULE_JSON")
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    capsule = normalize_capsule(parsed)
    if capsule["capsule_sha256"] != declared:
        raise CapsuleError("wire capsule hash does not match capsule")
    return capsule


def parse_result_message(message: object) -> dict[str, Any]:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_WIRE_BYTES:
        raise CapsuleError("result message is invalid or too large")
    lines = message.split("\n")
    if len(lines) != 3 or lines[0] != RESULT_HEADER or not lines[1].startswith("RESULT_SHA256: ") or not lines[2].startswith("RESULT_JSON: "):
        raise CapsuleError("result message is not a compact v8 envelope")
    declared = _sha(lines[1][len("RESULT_SHA256: "):], "wire result hash")
    try:
        result = parse_canonical_json_object(lines[2][len("RESULT_JSON: "):], "RESULT_JSON")
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    required = {"dispatch_sha256", "disposition", "payload", "protocol", "result_sha256", "status"}
    if set(result) != required or result["protocol"] != PROTOCOL:
        raise CapsuleError("result fields are invalid")
    _sha(result["dispatch_sha256"], "result.dispatch_sha256")
    if result["status"] not in STATUSES or result["disposition"] not in DISPOSITIONS or not isinstance(result["payload"], Mapping):
        raise CapsuleError("result status, disposition, or payload is invalid")
    if result["result_sha256"] != declared or result_sha256(result) != declared:
        raise CapsuleError("result hash does not match")
    result["payload"] = _result_payload(result["payload"], status=result["status"])
    return result


def _sorted_text(values: object, label: str) -> list[str]:
    if (
        not isinstance(values, list)
        or any(not isinstance(item, str) or not item.strip() for item in values)
        or values != sorted(set(values))
    ):
        raise CapsuleError(f"{label} must be sorted, unique text")
    return list(values)


def _result_payload(value: object, *, status: str) -> dict[str, Any]:
    required = {"blockers", "changed_paths", "deviations", "evidence", "failure_signature", "summary"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise CapsuleError("result payload fields are incomplete or unsupported")
    summary = value["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise CapsuleError("result summary must be non-empty text")
    changed = _sorted_text(value["changed_paths"], "result changed_paths")
    try:
        changed = [require_repository_path(path, f"result.changed_paths[{index}]") for index, path in enumerate(changed)]
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    evidence = value["evidence"]
    if (
        not isinstance(evidence, Mapping)
        or any(not isinstance(key, str) or ACCEPTANCE_ID.fullmatch(key) is None for key in evidence)
        or any(not isinstance(observation, str) or not observation.strip() for observation in evidence.values())
    ):
        raise CapsuleError("result acceptance evidence is malformed")
    normalized_evidence = {key: evidence[key] for key in sorted(evidence)}
    blockers = _sorted_text(value["blockers"], "result blockers")
    deviations = _sorted_text(value["deviations"], "result deviations")
    if status == "blocked" and not blockers:
        raise CapsuleError("blocked result requires at least one blocker")
    failure_signature = value["failure_signature"]
    failed = status != "complete" or bool(blockers) or bool(deviations)
    if failed:
        if not isinstance(failure_signature, str) or FAILURE_SIGNATURE.fullmatch(failure_signature) is None:
            raise CapsuleError("non-complete or deviating result requires a canonical failure signature")
    elif failure_signature is not None:
        raise CapsuleError("successful result must not contain a failure signature")
    return {
        "blockers": blockers,
        "changed_paths": changed,
        "deviations": deviations,
        "evidence": normalized_evidence,
        "failure_signature": failure_signature,
        "summary": summary,
    }


def validate_result_for_dispatch(
    result: Mapping[str, Any],
    *,
    role: str,
    acceptance_ids: object,
) -> dict[str, Any]:
    normalized_role = require_role(role)
    expected_ids = _acceptance_ids(acceptance_ids)
    parsed = parse_result_message(
        f"{RESULT_HEADER}\nRESULT_SHA256: {result.get('result_sha256')}\nRESULT_JSON: {canonical_bytes(dict(result)).decode('utf-8')}"
    )
    evidence_ids = set(parsed["payload"]["evidence"])
    if not evidence_ids <= set(expected_ids):
        raise CapsuleError("result evidence contains an undeclared acceptance ID")
    if parsed["status"] == "complete" and evidence_ids != set(expected_ids):
        raise CapsuleError("complete result must cover every acceptance evidence ID")
    if normalized_role != "worker" and parsed["payload"]["changed_paths"]:
        raise CapsuleError("read-only result cannot declare changed paths")
    return parsed


def compile_result(capsule: Mapping[str, Any], *, status: str, disposition: str, **payload: Any) -> str:
    normalized = normalize_capsule(capsule)
    if status not in STATUSES or disposition not in DISPOSITIONS:
        raise CapsuleError("result status or disposition is invalid")
    if disposition == "accept" and normalized["role"] != "reviewer":
        raise CapsuleError("only a reviewer may return accept")
    if disposition == "accept" and status != "complete":
        raise CapsuleError("review acceptance requires complete status")
    normalized_payload = _result_payload(payload, status=status)
    evidence_ids = set(normalized_payload["evidence"])
    if not evidence_ids <= set(normalized["acceptance_ids"]):
        raise CapsuleError("result evidence contains an undeclared acceptance ID")
    if status == "complete" and evidence_ids != set(normalized["acceptance_ids"]):
        raise CapsuleError("complete result must cover every acceptance evidence ID")
    if normalized["role"] != "worker" and normalized_payload["changed_paths"]:
        raise CapsuleError("read-only result cannot declare changed paths")
    result: dict[str, Any] = {
        "dispatch_sha256": normalized["capsule_sha256"],
        "disposition": disposition,
        "payload": normalized_payload,
        "protocol": PROTOCOL,
        "status": status,
    }
    result["result_sha256"] = result_sha256(result)
    return f"{RESULT_HEADER}\nRESULT_SHA256: {result['result_sha256']}\nRESULT_JSON: {canonical_bytes(result).decode('utf-8')}"

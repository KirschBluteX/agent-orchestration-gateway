#!/usr/bin/env python3
"""Compile the small, canonical CCO v6 dispatch capsule.

The compiler is deliberately deterministic and side-effect free.  It is the only
module callers need to know when creating a native Agent request.  Hooks validate
the returned capsule independently; they never trust a compiler result merely
because it was produced locally.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping

from decision_policy import (
    DecisionPolicyError,
    ROUTE_ASSURANCES,
    normalize_dispatch_decision,
    select_ready_nodes,
)
from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    parse_canonical_json_object,
    require_canonical_task_path,
    require_repository_scope,
)
from routing_catalog import RoutingCatalogError, validate_route_plan


PROTOCOL = "cco.v6"
DISPATCH_HEADER = "CCO_DISPATCH cco.v6"
RESULT_HEADER = "CCO_RESULT cco.v6"
CAPSULE_DOMAIN = b"cco.dispatch-capsule.v6\0"
RESULT_DOMAIN = b"cco.result-capsule.v6\0"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
EPOCH = re.compile(r"^e[0-9]{2,}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
PURPOSES = frozenset(
    {"analysis_inspect", "analysis_probe", "implementation", "acceptance"}
)
JUDGMENTS = frozenset({"routine", "complex"})
WRITE_ROLE = "cost_orchestrator_write_leaf"
READ_ROLE = "cost_orchestrator_read_leaf"
ROLES = frozenset({WRITE_ROLE, READ_ROLE})
MODES = frozenset({"light", "strict", "fresh", "delta"})
KINDS = frozenset({"work", "analysis", "review"})
STATUSES = frozenset({"complete", "partial", "blocked"})
MAX_WIRE_BYTES = 1024 * 1024


class CapsuleError(ValueError):
    """A dispatch capsule is incomplete, non-canonical, or inconsistent."""


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CapsuleError(f"{label} must be a sha256 identity")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapsuleError(f"{label} must be non-empty text")
    return value


def _enum(value: object, allowed: frozenset[str], label: str) -> str:
    value = _text(value, label)
    if value not in allowed:
        raise CapsuleError(f"{label} is not supported: {value}")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CapsuleError(f"{label} must be an integer >= {minimum}")
    return value


def _canonical(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{label} must be an object")
    try:
        encoded = canonical_bytes(value).decode("utf-8")
    except (ProtocolHashError, UnicodeError) as error:
        raise CapsuleError(f"{label} is not canonical JSON") from error
    try:
        parsed = json.loads(encoded)
    except json.JSONDecodeError as error:  # pragma: no cover - canonical_bytes is JSON
        raise CapsuleError(f"{label} is not JSON") from error
    if parsed != value:
        raise CapsuleError(f"{label} changed during canonicalization")
    return deepcopy(value)


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    encoded = canonical_bytes(dict(value))
    return "sha256:" + hashlib.sha256(domain + encoded).hexdigest()


def capsule_sha256(capsule: Mapping[str, Any]) -> str:
    """Return the root identity after removing the self-referential field."""

    value = dict(capsule)
    value.pop("capsule_sha256", None)
    return _digest(CAPSULE_DOMAIN, value)


def result_sha256(result: Mapping[str, Any]) -> str:
    value = dict(result)
    value.pop("result_sha256", None)
    return _digest(RESULT_DOMAIN, value)


def _role_for(purpose: str) -> str:
    return WRITE_ROLE if purpose == "implementation" else READ_ROLE


def _task_name(
    *, kind: str, node: str, judgment: str, generation: int, epoch: str | None
) -> str:
    suffix = f"g{generation:02d}"
    if kind == "review":
        if epoch is None:
            raise CapsuleError("review requires epoch")
        return f"review_{epoch}_{suffix}"
    prefix = "analyze" if kind == "analysis" else "work"
    return f"{prefix}_{node}_{judgment}_{suffix}"


def _route_binding(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "plan_sha256",
        "rank",
        "selected",
    }:
        raise CapsuleError("route binding is malformed")
    selected = value.get("selected")
    if not isinstance(selected, dict) or set(selected) != {"effort", "model"}:
        raise CapsuleError("route selected pair is malformed")
    model = _text(selected.get("model"), "route.selected.model")
    effort = _text(selected.get("effort"), "route.selected.effort")
    if MODEL.fullmatch(model) is None or EFFORT.fullmatch(effort) is None:
        raise CapsuleError("route selected model/effort is malformed")
    rank = _integer(value.get("rank"), "route.rank", minimum=1)
    return {
        "plan_sha256": _sha(value.get("plan_sha256"), "route.plan_sha256"),
        "rank": rank,
        "selected": {"effort": effort, "model": model},
    }


def _route_from_plan(
    plan_value: object,
    *,
    assurance: str,
    purpose: str,
    judgment: str,
) -> dict[str, Any]:
    try:
        plan = validate_route_plan(plan_value)
    except RoutingCatalogError as error:
        raise CapsuleError(f"route plan is invalid: {error}") from error
    matches = [
        route
        for route in plan["routes"]
        if route["purpose"] == purpose
        and route["judgment"] == judgment
        and route["assurance"] == assurance
    ]
    if len(matches) != 1:
        raise CapsuleError("route plan key is missing or ambiguous")
    route = matches[0]
    if route["placement"]["target"] != "child":
        raise CapsuleError("route plan keeps this work in Primary")
    return _route_binding(
        {
            "plan_sha256": plan["plan_sha256"],
            "rank": route["dispatch"]["rank"],
            "selected": route["selected"],
        }
    )


def _scope_list(value: object, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CapsuleError(f"{label} must be a list")
    output: list[dict[str, str]] = []
    try:
        output = [
            require_repository_scope(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    canonical = sorted(output, key=lambda item: (item["kind"], item["path"]))
    if output != canonical or len({(x["kind"], x["path"]) for x in output}) != len(output):
        raise CapsuleError(f"{label} must be sorted and duplicate-free")
    return output


def normalize_capsule(capsule: object) -> dict[str, Any]:
    """Validate a capsule and return a detached canonical copy."""

    value = _canonical(capsule, "capsule")
    if value.get("protocol") != PROTOCOL:
        raise CapsuleError("capsule protocol is invalid")
    kind = _enum(value.get("kind"), KINDS, "capsule.kind")
    purpose = _enum(value.get("purpose"), PURPOSES, "capsule.purpose")
    judgment = _enum(value.get("judgment"), JUDGMENTS, "capsule.judgment")
    mode = _enum(value.get("mode"), MODES, "capsule.mode")
    if kind == "work" and purpose != "implementation":
        raise CapsuleError("work purpose must be implementation")
    if kind == "analysis" and purpose not in {"analysis_inspect", "analysis_probe"}:
        raise CapsuleError("analysis purpose is invalid")
    if kind == "review" and purpose != "acceptance":
        raise CapsuleError("review purpose must be acceptance")
    if kind == "review" and mode not in {"fresh", "delta"}:
        raise CapsuleError("review mode must be fresh or delta")
    if kind != "review" and mode not in {"light", "strict"}:
        raise CapsuleError("worker mode must be light or strict")
    node = _text(value.get("node"), "capsule.node")
    if NODE.fullmatch(node) is None:
        raise CapsuleError("capsule.node is malformed")
    epoch = value.get("epoch")
    if kind == "review":
        if not isinstance(epoch, str) or EPOCH.fullmatch(epoch) is None:
            raise CapsuleError("review epoch is malformed")
    elif epoch is not None:
        raise CapsuleError("non-review capsule cannot carry epoch")
    role = _enum(value.get("role"), ROLES, "capsule.role")
    if role != _role_for(purpose):
        raise CapsuleError("capsule role does not match purpose")
    execution = _canonical(value.get("execution"), "capsule.execution")
    generation = _integer(execution.get("generation"), "execution.generation", minimum=1)
    cursor = _integer(execution.get("cursor"), "execution.cursor")
    if set(execution) != {"cursor", "fork_turns", "generation", "task_name"}:
        raise CapsuleError("capsule.execution has unsupported fields")
    fork_turns = execution.get("fork_turns", "none")
    if fork_turns != "none" and (
        not isinstance(fork_turns, str)
        or POSITIVE_INTEGER.fullmatch(fork_turns) is None
    ):
        raise CapsuleError("execution.fork_turns is invalid")
    route = _route_binding(value.get("route"))
    if "decision" in value:
        try:
            decision = normalize_dispatch_decision(
                value.get("decision"),
                selected_model=route["selected"]["model"],
            )
        except DecisionPolicyError as error:
            raise CapsuleError(f"capsule decision is invalid: {error}") from error
        if decision != value.get("decision"):
            raise CapsuleError("capsule decision is not canonical")
        derived = decision["derived"]
        if purpose != derived["purpose"] or judgment != derived["judgment"]:
            raise CapsuleError("capsule labels do not match the derived decision")
        if "acceptance" in value and value.get("acceptance") != derived["acceptance"]:
            raise CapsuleError("capsule acceptance does not match the derived decision")
    _sha(value.get("baseline"), "capsule.baseline")
    if "graph_sha256" in value:
        _sha(value.get("graph_sha256"), "capsule.graph_sha256")
    _scope_list(value.get("scopes", []), "capsule.scopes")
    _canonical(value.get("contract"), "capsule.contract")
    if kind == "review":
        _canonical(value.get("evidence"), "capsule.evidence")
        _canonical(value.get("acceptance"), "capsule.acceptance")
        if value.get("current_state") is None:
            raise CapsuleError("review current_state is required")
        _sha(value.get("current_state"), "capsule.current_state")
    if value.get("capsule_sha256") != capsule_sha256(value):
        raise CapsuleError("capsule hash does not match content")
    required = {
        "baseline", "capsule_sha256", "contract", "execution", "judgment",
        "kind", "mode", "node", "protocol", "purpose", "requested_effort",
        "requested_model", "role", "route", "scopes",
    }
    optional = {
        "acceptance", "current_state", "decision", "epoch", "evidence", "graph_sha256",
    }
    continuation = {"delta", "previous_capsule_sha256"}
    if set(value) - required - optional - continuation or required - set(value):
        raise CapsuleError("capsule has unsupported or missing fields")
    if cursor == 0 and set(value).intersection(continuation):
        raise CapsuleError("initial capsule cannot carry continuation fields")
    if cursor > 0:
        if not continuation <= set(value):
            raise CapsuleError("continued capsule lacks its previous identity or delta")
        _sha(value["previous_capsule_sha256"], "capsule.previous_capsule_sha256")
        if not _canonical(value["delta"], "capsule.delta"):
            raise CapsuleError("capsule.delta must not be empty")
    # Cross-check all values derived by the compiler.
    expected_task = _task_name(
        kind=kind,
        node=node,
        judgment=judgment,
        generation=generation,
        epoch=epoch,
    )
    if execution.get("task_name") != expected_task:
        raise CapsuleError("execution.task_name is not canonical")
    if (
        route["selected"]["model"] != value.get("requested_model")
        or route["selected"]["effort"] != value.get("requested_effort")
    ):
        raise CapsuleError("requested route is not bound to route selection")
    if kind == "review" and execution.get("fork_turns") != "none":
        raise CapsuleError("review must use fork_turns=none")
    if kind == "review" and cursor > 0 and mode != "delta":
        raise CapsuleError("continued review must use delta mode")
    return value


def compile_dispatch(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build a native ``spawn_agent`` input in one deterministic operation."""

    if not isinstance(spec, Mapping):
        raise CapsuleError("dispatch spec must be an object")
    if "route" in spec:
        raise CapsuleError("route selection must be derived from route_plan")
    kind = _enum(spec.get("kind"), KINDS, "spec.kind")
    purpose = _enum(spec.get("purpose"), PURPOSES, "spec.purpose")
    judgment = _enum(spec.get("judgment"), JUDGMENTS, "spec.judgment")
    if kind == "work" and purpose != "implementation":
        raise CapsuleError("work purpose must be implementation")
    if kind == "analysis" and purpose not in {"analysis_inspect", "analysis_probe"}:
        raise CapsuleError("analysis purpose is invalid")
    if kind == "review" and purpose != "acceptance":
        raise CapsuleError("review purpose is invalid")
    node = _text(spec.get("node"), "spec.node")
    if NODE.fullmatch(node) is None:
        raise CapsuleError("spec.node is malformed")
    epoch = spec.get("epoch")
    if kind == "review" and (not isinstance(epoch, str) or EPOCH.fullmatch(epoch) is None):
        raise CapsuleError("review spec requires epoch")
    if "attempt" in spec or "followup" in spec:
        raise CapsuleError("attempt/followup are replaced by generation/cursor")
    generation = _integer(spec.get("generation", 1), "spec.generation", minimum=1)
    cursor = _integer(spec.get("cursor", 0), "spec.cursor")
    mode = spec.get("mode", "fresh" if kind == "review" else ("strict" if spec.get("strict") else "light"))
    mode = _enum(mode, MODES, "spec.mode")
    assurance = _enum(
        spec.get("assurance", "deterministic"), ROUTE_ASSURANCES, "spec.assurance"
    )
    contract = _canonical(spec.get("contract"), "spec.contract")
    execution = {
        "cursor": cursor,
        "fork_turns": spec.get("fork_turns", "none"),
        "generation": generation,
        "task_name": _task_name(
            kind=kind,
            node=node,
            judgment=judgment,
            generation=generation,
            epoch=epoch,
        ),
    }
    route = _route_from_plan(
        spec.get("route_plan"),
        assurance=assurance,
        purpose=purpose,
        judgment=judgment,
    )
    role = _role_for(purpose)
    capsule: dict[str, Any] = {
        "contract": contract,
        "execution": execution,
        "judgment": judgment,
        "kind": kind,
        "mode": mode,
        "node": node,
        "protocol": PROTOCOL,
        "purpose": purpose,
        "requested_effort": route["selected"]["effort"],
        "requested_model": route["selected"]["model"],
        "role": role,
        "route": route,
        "scopes": _scope_list(spec.get("scopes", []), "spec.scopes"),
        "baseline": _sha(spec.get("baseline"), "spec.baseline"),
    }
    if epoch is not None:
        capsule["epoch"] = epoch
    for name in ("acceptance", "evidence"):
        if name in spec and spec[name] is not None:
            capsule[name] = _canonical(spec[name], f"spec.{name}")
    for name in ("current_state", "graph_sha256"):
        if name in spec:
            capsule[name] = _sha(spec[name], f"spec.{name}")
    if "decision" in spec:
        try:
            capsule["decision"] = normalize_dispatch_decision(
                spec["decision"], selected_model=route["selected"]["model"]
            )
            if capsule["decision"]["derived"]["assurance"] != assurance:
                raise CapsuleError("spec assurance does not match decision facts")
        except DecisionPolicyError as error:
            raise CapsuleError(f"spec.decision is invalid: {error}") from error
    if kind == "review":
        if "acceptance" not in capsule or "evidence" not in capsule:
            raise CapsuleError("review requires one acceptance and one evidence object")
        _sha(capsule.get("current_state"), "spec.current_state")
    capsule["capsule_sha256"] = capsule_sha256(capsule)
    normalize_capsule(capsule)
    message = _render_dispatch(capsule)
    result: dict[str, Any] = {
        "agent_type": role,
        "fork_turns": execution["fork_turns"],
        "message": message,
        "task_name": execution["task_name"],
    }
    result["model"] = route["selected"]["model"]
    result["reasoning_effort"] = route["selected"]["effort"]
    return result


def compile_dispatch_batch(
    nodes: object,
    *,
    route_plan: object,
    native_capacity: int,
) -> list[dict[str, Any]]:
    """Compile one native request for each selector-admitted graph node.

    This is a deterministic adapter, not an Agent runtime: the caller still owns
    the native spawn calls and observes the returned capacity/owners.
    """

    try:
        plan = validate_route_plan(route_plan)
    except RoutingCatalogError as error:
        raise CapsuleError(f"route plan is invalid: {error}") from error
    if not isinstance(nodes, list):
        raise CapsuleError("dispatch batch nodes must be a list")
    normalized: dict[str, dict[str, Any]] = {}
    selector_nodes: list[dict[str, Any]] = []
    for index, item in enumerate(nodes):
        if not isinstance(item, Mapping) or set(item) != {"dispatch", "selection"}:
            raise CapsuleError(f"dispatch batch node {index} is malformed")
        dispatch = item["dispatch"]
        selection = item["selection"]
        if not isinstance(dispatch, Mapping) or not isinstance(selection, Mapping):
            raise CapsuleError(f"dispatch batch node {index} is malformed")
        node = dispatch.get("node")
        if node != selection.get("node") or not isinstance(node, str):
            raise CapsuleError(f"dispatch batch node {index} identity does not match")
        if node in normalized:
            raise CapsuleError("dispatch batch node identities must be unique")
        scopes = _scope_list(dispatch.get("scopes", []), f"batch dispatch {index}.scopes")
        selection_scopes = _scope_list(
            selection.get("scope", []), f"batch selection {index}.scope"
        )
        if scopes != selection_scopes:
            raise CapsuleError(f"dispatch batch node {index} scopes do not match")
        purpose = _enum(dispatch.get("purpose"), PURPOSES, f"batch dispatch {index}.purpose")
        selector_nodes.append(
            {
                "access": "write" if purpose == "implementation" else "read",
                "dependencies_ready": selection.get("dependencies_ready"),
                "node": node,
                "responsibility": selection.get("responsibility"),
                "scope": scopes,
            }
        )
        normalized[node] = dict(dispatch)
    try:
        selected = select_ready_nodes(selector_nodes, native_capacity=native_capacity)
    except DecisionPolicyError as error:
        raise CapsuleError(f"dispatch selection is invalid: {error}") from error
    return [
        compile_dispatch({**normalized[node], "route_plan": plan})
        for node in selected
    ]


def _render_dispatch(capsule: Mapping[str, Any]) -> str:
    normalized = normalize_capsule(capsule)
    return (
        f"{DISPATCH_HEADER}\n"
        f"CAPSULE_SHA256: {normalized['capsule_sha256']}\n"
        f"CAPSULE_JSON: {canonical_bytes(normalized).decode('utf-8')}"
    )


def compile_continuation(
    capsule: Mapping[str, Any], *, target: str, delta: Mapping[str, Any]
) -> dict[str, str]:
    """Compile one same-owner continuation without retry-count ceremony."""

    previous = normalize_capsule(capsule)
    try:
        require_canonical_task_path(target, "continuation target")
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    if target != "/root/" + previous["execution"]["task_name"]:
        raise CapsuleError("continuation target does not own the capsule")
    update = _canonical(dict(delta), "continuation delta")
    if not update:
        raise CapsuleError("continuation delta must not be empty")
    continued = deepcopy(previous)
    previous_sha256 = continued.pop("capsule_sha256")
    continued["previous_capsule_sha256"] = previous_sha256
    continued["delta"] = update
    continued["execution"]["cursor"] += 1
    if continued["kind"] == "review":
        continued["mode"] = "delta"
    continued["capsule_sha256"] = capsule_sha256(continued)
    return {"message": _render_dispatch(continued), "target": target}


def parse_message(message: object) -> dict[str, Any]:
    """Parse the one-field wire envelope used by PreToolUse and lifecycle hooks."""

    if not isinstance(message, str):
        raise CapsuleError("dispatch message must be text")
    try:
        if len(message.encode("utf-8")) > MAX_WIRE_BYTES:
            raise CapsuleError("dispatch message exceeds the size limit")
    except UnicodeEncodeError as error:
        raise CapsuleError("dispatch message is not valid UTF-8 text") from error
    lines = message.split("\n")
    if len(lines) != 3 or lines[0] != DISPATCH_HEADER or not lines[1].startswith("CAPSULE_SHA256: ") or not lines[2].startswith("CAPSULE_JSON: "):
        raise CapsuleError("dispatch message is not a compact v6 envelope")
    declared = _sha(lines[1][len("CAPSULE_SHA256: "):], "CAPSULE_SHA256")
    try:
        parsed = parse_canonical_json_object(
            lines[2][len("CAPSULE_JSON: "):], "CAPSULE_JSON"
        )
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    capsule = normalize_capsule(parsed)
    if declared != capsule["capsule_sha256"]:
        raise CapsuleError("wire capsule hash does not match capsule")
    return capsule


def parse_result_message(message: object) -> dict[str, Any]:
    """Parse and verify the compact result envelope used by SubagentStop."""

    if not isinstance(message, str):
        raise CapsuleError("result message must be text")
    try:
        if len(message.encode("utf-8")) > MAX_WIRE_BYTES:
            raise CapsuleError("result message exceeds the size limit")
    except UnicodeEncodeError as error:
        raise CapsuleError("result message is not valid UTF-8 text") from error
    lines = message.split("\n")
    if (
        len(lines) != 3
        or lines[0] != RESULT_HEADER
        or not lines[1].startswith("RESULT_SHA256: ")
        or not lines[2].startswith("RESULT_JSON: ")
    ):
        raise CapsuleError("result message is not a compact v6 envelope")
    declared = _sha(lines[1][len("RESULT_SHA256: "):], "RESULT_SHA256")
    try:
        result = parse_canonical_json_object(
            lines[2][len("RESULT_JSON: "):], "RESULT_JSON"
        )
    except ProtocolHashError as error:
        raise CapsuleError(str(error)) from error
    if set(result) != {
        "dispatch_sha256",
        "disposition",
        "payload",
        "protocol",
        "result_sha256",
        "status",
    }:
        raise CapsuleError("result has unsupported or missing fields")
    if result.get("protocol") != PROTOCOL:
        raise CapsuleError("result protocol is invalid")
    _sha(result.get("dispatch_sha256"), "result.dispatch_sha256")
    if result.get("status") not in STATUSES:
        raise CapsuleError("result status is invalid")
    if result.get("disposition") not in {"continue", "accept", "retire"}:
        raise CapsuleError("result disposition is invalid")
    if not isinstance(result.get("payload"), dict):
        raise CapsuleError("result payload is invalid")
    if result.get("result_sha256") != declared or result_sha256(result) != declared:
        raise CapsuleError("result hash does not match")
    return result


def compile_result(capsule: Mapping[str, Any], *, status: str, disposition: str, **payload: Any) -> str:
    """Render a minimal result; Primary owns acceptance, not the leaf."""

    normalized = normalize_capsule(capsule)
    if status not in STATUSES:
        raise CapsuleError("result status is invalid")
    if disposition not in {"continue", "accept", "retire"}:
        raise CapsuleError("result disposition is invalid")
    kind = normalized["kind"]
    if disposition == "accept" and kind != "review":
        raise CapsuleError("only a reviewer may return an accept disposition")
    if kind == "review" and disposition == "accept" and status != "complete":
        raise CapsuleError("review acceptance requires a complete result")
    result = {
        "dispatch_sha256": normalized["capsule_sha256"],
        "disposition": disposition,
        "payload": payload,
        "protocol": PROTOCOL,
        "status": status,
    }
    result["result_sha256"] = result_sha256(result)
    return (
        f"{RESULT_HEADER}\n"
        f"RESULT_SHA256: {result['result_sha256']}\n"
        f"RESULT_JSON: {canonical_bytes(result).decode('utf-8')}"
    )

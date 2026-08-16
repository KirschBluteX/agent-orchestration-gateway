#!/usr/bin/env python3
"""Pure compiler for the canonical AOG delegation request.

This module has no workspace, host, lifecycle, planner, or spawn authority.  It
validates a closed request, applies the one assurance ladder, and produces a
canonical DAG for ``control_plane.py prepare``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
import unicodedata


DELEGATION_REQUEST_PROTOCOL = "aog.delegation.v1"
DELEGATION_COMPILATION_PROTOCOL = "aog.delegation-compile.v1"
PLANNER_PROPOSAL_PROTOCOL = "aog.planner-proposal.v1"
PRIMARY_DIRECT = "primary_direct"
DELEGATE = "delegate"

ROLES = frozenset({"explorer", "worker", "reviewer"})
DECISIONS = frozenset({"mechanical", "bounded"})
VERIFICATIONS = frozenset({"deterministic", "semantic", "manual"})
SCOPE_KINDS = frozenset({"exact", "prefix"})
WRITER_ISOLATION_MODES = frozenset({"serial", "cooperative"})
MAX_PLAN_NODES = 128
NODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
ACCEPTANCE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

# Risks are intentionally finite.  They are a declared contract, not a model
# score or an invitation to infer safety from prose.
CANONICAL_RISKS = frozenset(
    {
        "concurrency",
        "deviation",
        "filesystem_transaction",
        "installer",
        "irreversible_action",
        "migration",
        "new_dependency",
        "persistence",
        "public_interface",
        "recovery",
        "retry",
        "scope_expansion",
        "security_auth",
        "test_failure",
    }
)
RISK_ALIASES = {
    "api": "public_interface",
    "authentication": "security_auth",
    "authorization": "security_auth",
    "auth": "security_auth",
    "concurrent": "concurrency",
    "database": "persistence",
    "destructive": "irreversible_action",
    "file_transaction": "filesystem_transaction",
    "filesystem": "filesystem_transaction",
    "install": "installer",
    "interface": "public_interface",
    "irreversible": "irreversible_action",
    "public_api": "public_interface",
    "race": "concurrency",
    "security": "security_auth",
    "storage": "persistence",
}


class DelegationCompilerError(ValueError):
    """A canonical delegation request cannot safely be compiled."""


def _object(value: object, label: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DelegationCompilerError(f"{label} must be an object")
    extra = set(value) - fields
    if extra:
        raise DelegationCompilerError(f"{label} contains unsupported fields")
    if set(value) != fields:
        raise DelegationCompilerError(f"{label} is incomplete")
    return value


def _object_with_optional(
    value: object,
    label: str,
    required: set[str],
    optional: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DelegationCompilerError(f"{label} must be an object")
    if set(value) - (required | optional):
        raise DelegationCompilerError(f"{label} contains unsupported fields")
    if not required <= set(value):
        raise DelegationCompilerError(f"{label} is incomplete")
    return value


def _text(value: object, label: str, *, limit: int = 8_192) -> str:
    if not isinstance(value, str):
        raise DelegationCompilerError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized.encode("utf-8")) > limit:
        raise DelegationCompilerError(f"{label} is empty or too large")
    return normalized


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise DelegationCompilerError(f"{label} must be boolean")
    return value


def _unique_texts(
    value: object,
    label: str,
    *,
    limit: int,
    maximum: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise DelegationCompilerError(f"{label} must be a bounded list")
    items = [_text(item, label, limit=limit) for item in value]
    if len(set(items)) != len(items):
        raise DelegationCompilerError(f"{label} contains duplicates")
    return sorted(items)


def _scope_path(value: object, label: str) -> str:
    path = _text(value, label, limit=4_096).replace("\\", "/")
    if (
        path.startswith("/")
        or path.startswith("//")
        or ":" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise DelegationCompilerError(f"{label} is not a repository-relative path")
    return path


def _normalize_scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise DelegationCompilerError(f"{label} must be a non-empty list")
    scopes: list[dict[str, str]] = []
    for index, item in enumerate(value):
        scope = _object(item, f"{label}[{index}]", {"kind", "path"})
        kind = scope["kind"]
        if kind not in SCOPE_KINDS:
            raise DelegationCompilerError(f"{label}[{index}].kind is invalid")
        scopes.append(
            {
                "kind": kind,
                "path": _scope_path(scope["path"], f"{label}[{index}].path"),
            }
        )
    unique = {(scope["kind"], scope["path"]) for scope in scopes}
    if len(unique) != len(scopes):
        raise DelegationCompilerError(f"{label} contains duplicate scopes")
    return [{"kind": kind, "path": path} for kind, path in sorted(unique)]


def _normalize_pin(value: object, label: str) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or not set(value)
        or set(value) - {"effort", "model"}
    ):
        raise DelegationCompilerError(f"{label} is invalid")
    pin: dict[str, str] = {}
    if "model" in value:
        model = value["model"]
        if not isinstance(model, str) or MODEL_RE.fullmatch(model) is None:
            raise DelegationCompilerError(f"{label}.model is invalid")
        pin["model"] = model
    if "effort" in value:
        effort = value["effort"]
        if not isinstance(effort, str) or EFFORT_RE.fullmatch(effort) is None:
            raise DelegationCompilerError(f"{label}.effort is invalid")
        pin["effort"] = effort
    return {key: pin[key] for key in sorted(pin)}


def _normalize_acceptance(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise DelegationCompilerError("plan acceptance must be a non-empty object")
    acceptance: dict[str, str] = {}
    for raw_id, raw_criterion in value.items():
        acceptance_id = _text(raw_id, "acceptance ID", limit=32)
        if ACCEPTANCE_RE.fullmatch(acceptance_id) is None:
            raise DelegationCompilerError(f"invalid acceptance ID: {acceptance_id}")
        if acceptance_id in acceptance:
            raise DelegationCompilerError(
                f"acceptance IDs collide after normalization: {acceptance_id}"
            )
        acceptance[acceptance_id] = _text(
            raw_criterion,
            f"acceptance {acceptance_id}",
            limit=4_096,
        )
    return {key: acceptance[key] for key in sorted(acceptance)}


def _normalize_writer_isolation(value: object, label: str) -> str:
    if value is None:
        return "serial"
    if not isinstance(value, str) or value not in WRITER_ISOLATION_MODES:
        raise DelegationCompilerError(f"{label} is invalid")
    return value


def _normalize_risks(value: object, label: str) -> list[str]:
    raw = _unique_texts(value, label, limit=64, maximum=32)
    normalized: list[str] = []
    for item in raw:
        key = re.sub(r"[^a-z0-9]+", "_", item.casefold()).strip("_")
        canonical = RISK_ALIASES.get(key, key)
        if canonical not in CANONICAL_RISKS:
            raise DelegationCompilerError(f"{label} contains an unsupported risk label")
        normalized.append(canonical)
    if len(set(normalized)) != len(normalized):
        raise DelegationCompilerError(f"{label} contains equivalent duplicate risks")
    return sorted(normalized)


def derive_assurance(node: Mapping[str, Any]) -> str:
    """Apply the only assurance ladder used by AOG's compiler and router."""

    role = node.get("role")
    verification = node.get("verification")
    risks = node.get("risks")
    decision = node.get("decision")
    if role not in ROLES or verification not in VERIFICATIONS or decision not in DECISIONS:
        raise DelegationCompilerError("assurance source is invalid")
    if not isinstance(risks, list) or any(risk not in CANONICAL_RISKS for risk in risks):
        raise DelegationCompilerError("assurance risks are invalid")
    if role == "reviewer" or verification in {"semantic", "manual"} or risks:
        return "guarded"
    return "mechanical" if decision == "mechanical" else "bounded"


def _normalize_node(
    value: object,
    index: int,
    acceptance: Mapping[str, str],
) -> dict[str, Any]:
    allowed = {
        "acceptance",
        "assurance",
        "context_turns",
        "decision",
        "depends_on",
        "id",
        "objective",
        "pin",
        "review_of",
        "risks",
        "role",
        "scopes",
        "verification",
    }
    required = {"acceptance", "id", "objective", "role", "scopes"}
    if not isinstance(value, Mapping) or set(value) - allowed or not required <= set(value):
        raise DelegationCompilerError(f"plan node {index} is invalid")
    node_id = _text(value["id"], f"plan node {index}.id", limit=48)
    if NODE_RE.fullmatch(node_id) is None:
        raise DelegationCompilerError(f"invalid node ID: {node_id}")
    role = value["role"]
    if role not in ROLES:
        raise DelegationCompilerError(f"plan node {node_id} has an invalid role")
    ids = _unique_texts(
        value["acceptance"],
        f"plan node {node_id} acceptance",
        limit=32,
        maximum=32,
    )
    if not ids or any(item not in acceptance for item in ids):
        raise DelegationCompilerError(f"plan node {node_id} acceptance IDs are invalid")
    dependencies = _unique_texts(
        value.get("depends_on", []),
        f"plan node {node_id} dependency",
        limit=48,
        maximum=128,
    )
    if node_id in dependencies:
        raise DelegationCompilerError(f"plan node {node_id} dependencies are invalid")
    decision = value.get("decision", "bounded")
    if decision not in DECISIONS:
        raise DelegationCompilerError(f"plan node {node_id}.decision is invalid")
    verification = value.get("verification", "deterministic")
    if verification not in VERIFICATIONS:
        raise DelegationCompilerError(f"plan node {node_id}.verification is invalid")
    risks = _normalize_risks(value.get("risks", []), f"plan node {node_id} risk")
    context_turns = value.get("context_turns", 0)
    if (
        isinstance(context_turns, bool)
        or not isinstance(context_turns, int)
        or not 0 <= context_turns <= 32
    ):
        raise DelegationCompilerError(f"plan node {node_id}.context_turns is invalid")
    review_of = value.get("review_of")
    if review_of is not None:
        review_of = _text(review_of, f"plan node {node_id}.review_of", limit=48)
        if role != "reviewer":
            raise DelegationCompilerError("review_of is valid only for reviewers")
    node = {
        "acceptance": ids,
        "context_turns": context_turns,
        "decision": decision,
        "depends_on": dependencies,
        "id": node_id,
        "objective": _text(value["objective"], f"plan node {node_id}.objective"),
        "pin": _normalize_pin(value.get("pin"), f"plan node {node_id}.pin"),
        "review_of": review_of,
        "risks": risks,
        "role": role,
        "scopes": _normalize_scopes(value["scopes"], f"plan node {node_id}.scopes"),
        "verification": verification,
    }
    derived = derive_assurance(node)
    supplied = value.get("assurance")
    if supplied is not None and supplied != derived:
        raise DelegationCompilerError(
            f"plan node {node_id}.assurance does not match the assurance ladder"
        )
    node["assurance"] = derived
    return node


def _validate_graph(nodes: list[dict[str, Any]]) -> None:
    node_ids = {item["id"] for item in nodes}
    if len(node_ids) != len(nodes):
        raise DelegationCompilerError("plan node IDs must be unique")
    for node in nodes:
        unknown = [item for item in node["depends_on"] if item not in node_ids]
        if unknown:
            raise DelegationCompilerError(
                "plan contains unknown dependencies: " + ", ".join(unknown)
            )
        if node["review_of"] is not None:
            if node["review_of"] not in node_ids:
                raise DelegationCompilerError(
                    f"reviewer {node['id']} names an unknown source"
                )
            if node["review_of"] not in node["depends_on"]:
                raise DelegationCompilerError(
                    f"reviewer {node['id']} must depend on its review_of source"
                )
    indegree = {item["id"]: len(item["depends_on"]) for item in nodes}
    children: dict[str, set[str]] = {item["id"]: set() for item in nodes}
    for node in nodes:
        for dependency in node["depends_on"]:
            children[dependency].add(node["id"])
    ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
    visited: list[str] = []
    while ready:
        current = ready.pop(0)
        visited.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(visited) != len(nodes):
        raise DelegationCompilerError("plan dependency graph contains a cycle")


def _final_reviewer(
    acceptance: Mapping[str, str],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    scopes = {
        (scope["kind"], scope["path"])
        for source in sources
        for scope in source["scopes"]
    }
    node = {
        "acceptance": sorted(acceptance),
        "context_turns": 0,
        "decision": "bounded",
        "depends_on": sorted(source["id"] for source in sources),
        "id": "final_review",
        "objective": "Independently review the completed guarded changes and acceptance evidence.",
        "pin": None,
        "review_of": None,
        "risks": [],
        "role": "reviewer",
        "scopes": [
            {"kind": kind, "path": path} for kind, path in sorted(scopes)
        ],
        "verification": "semantic",
    }
    node["assurance"] = derive_assurance(node)
    return node


def _validate_acceptance_ownership(
    acceptance: Mapping[str, str],
    nodes: list[dict[str, Any]],
    *,
    excluded_reviewer_ids: frozenset[str] = frozenset(),
) -> None:
    owners: dict[str, str] = {}
    for node in nodes:
        if node["id"] in excluded_reviewer_ids:
            continue
        for acceptance_id in node["acceptance"]:
            if acceptance_id in owners:
                raise DelegationCompilerError(
                    f"acceptance {acceptance_id} has more than one logical owner"
                )
            owners[acceptance_id] = node["id"]
    if set(owners) != set(acceptance):
        missing = sorted(set(acceptance) - set(owners))
        raise DelegationCompilerError(
            "plan acceptance is unowned: " + ", ".join(missing)
        )


def _validate_final_reviewer(
    reviewer: Mapping[str, Any],
    acceptance: Mapping[str, str],
    sources: list[dict[str, Any]],
) -> None:
    expected_scopes = _final_reviewer(acceptance, sources)["scopes"]
    if (
        reviewer.get("context_turns") != 0
        or reviewer.get("review_of") is not None
        or reviewer.get("assurance") != "guarded"
        or reviewer.get("acceptance") != sorted(acceptance)
        or reviewer.get("depends_on") != sorted(source["id"] for source in sources)
        or reviewer.get("scopes") != expected_scopes
    ):
        raise DelegationCompilerError(
            "guarded plans require one final reviewer after every source node"
        )


def normalize_closed_plan(value: object) -> dict[str, Any]:
    """Validate and canonically order one already-closed DAG plan.

    The compiler inserts one final independent reviewer for a guarded plan that
    writes files unless the *current* plan explicitly sets ``accept_risk``.
    """

    if not isinstance(value, Mapping):
        raise DelegationCompilerError("closed plan must be an object")
    allowed = {"acceptance", "accept_risk", "goal", "nodes", "writer_isolation"}
    if set(value) - allowed:
        raise DelegationCompilerError("closed plan contains unsupported fields")
    if not {"acceptance", "goal", "nodes"} <= set(value):
        raise DelegationCompilerError("closed plan is incomplete")
    acceptance = _normalize_acceptance(value["acceptance"])
    accept_risk = _boolean(value.get("accept_risk", False), "plan accept_risk")
    nodes_value = value["nodes"]
    if (
        not isinstance(nodes_value, list)
        or not nodes_value
        or len(nodes_value) > MAX_PLAN_NODES
    ):
        raise DelegationCompilerError(
            f"plan nodes must contain between 1 and {MAX_PLAN_NODES} items"
        )
    nodes = [_normalize_node(node, index, acceptance) for index, node in enumerate(nodes_value)]
    node_ids = {item["id"] for item in nodes}
    if len(node_ids) != len(nodes):
        raise DelegationCompilerError("plan node IDs must be unique")

    sources = [node for node in nodes if node["role"] != "reviewer"]
    guarded = any(node["assurance"] == "guarded" for node in nodes)
    reviewers = [node for node in nodes if node["role"] == "reviewer"]
    final_reviewer_ids: frozenset[str] = frozenset()
    if guarded and sources and not accept_risk:
        if not reviewers:
            if "final_review" in node_ids:
                raise DelegationCompilerError("final_review is reserved for guarded review")
            nodes.append(_final_reviewer(acceptance, sources))
        elif len(reviewers) == 1:
            _validate_final_reviewer(reviewers[0], acceptance, sources)
        else:
            raise DelegationCompilerError(
                "guarded plans use one final reviewer, not one reviewer per worker"
            )
    elif guarded and sources and reviewers:
        if len(reviewers) != 1:
            raise DelegationCompilerError(
                "guarded plans use one final reviewer, not one reviewer per worker"
            )
        _validate_final_reviewer(reviewers[0], acceptance, sources)

    reviewers = [node for node in nodes if node["role"] == "reviewer"]
    if guarded and sources and reviewers:
        final_reviewer_ids = frozenset(node["id"] for node in reviewers)

    nodes.sort(key=lambda item: item["id"])
    _validate_acceptance_ownership(
        acceptance,
        nodes,
        excluded_reviewer_ids=final_reviewer_ids,
    )
    _validate_graph(nodes)
    return {
        "acceptance": acceptance,
        "accept_risk": accept_risk,
        "goal": _text(value["goal"], "plan goal"),
        "nodes": nodes,
        "writer_isolation": _normalize_writer_isolation(
            value.get("writer_isolation"), "plan writer_isolation"
        ),
    }


def _compile_atomic(work: Mapping[str, Any]) -> dict[str, Any]:
    atomic = _object_with_optional(
        work,
        "atomic work",
        {"goal", "kind", "node"},
        {"accept_risk"},
    )
    node = atomic["node"]
    if not isinstance(node, Mapping):
        raise DelegationCompilerError("atomic work node is invalid")
    if "acceptance" not in node or not isinstance(node["acceptance"], Mapping):
        raise DelegationCompilerError("atomic work node acceptance must be an object")
    acceptance = _normalize_acceptance(node["acceptance"])
    copied_node = dict(node)
    copied_node["acceptance"] = sorted(acceptance)
    plan: dict[str, Any] = {
        "acceptance": acceptance,
        "goal": atomic["goal"],
        "nodes": [copied_node],
    }
    if "accept_risk" in atomic:
        plan["accept_risk"] = atomic["accept_risk"]
    return normalize_closed_plan(plan)


def _compile_planner_proposal(work: Mapping[str, Any]) -> dict[str, Any]:
    proposal = _object(work, "planner work", {"kind", "proposal"})["proposal"]
    if not isinstance(proposal, Mapping) or set(proposal) != {"plan", "protocol"}:
        raise DelegationCompilerError("planner proposal has an invalid schema")
    if proposal["protocol"] != PLANNER_PROPOSAL_PROTOCOL:
        raise DelegationCompilerError("planner proposal protocol is invalid")
    plan = normalize_closed_plan(proposal["plan"])
    if plan["accept_risk"]:
        raise DelegationCompilerError("planner proposals cannot accept Primary risk")
    if plan["writer_isolation"] != "serial":
        raise DelegationCompilerError("planner proposals cannot enable writer isolation")
    if any(node["pin"] is not None for node in plan["nodes"]):
        raise DelegationCompilerError("planner proposals cannot select model routes")
    if any(node["context_turns"] != 0 for node in plan["nodes"]):
        raise DelegationCompilerError("planner proposals cannot inherit Primary context")
    return plan


def _primary(reason: str) -> dict[str, Any]:
    return {
        "disposition": PRIMARY_DIRECT,
        "protocol": DELEGATION_COMPILATION_PROTOCOL,
        "reason": reason,
    }


def compile_delegation_request(value: object) -> dict[str, Any]:
    """Compile one canonical request without I/O or any planner lifecycle."""

    request_fields = {
        "authority",
        "clarification_required",
        "closed",
        "declared_tools",
        "direct",
        "protocol",
        "upper_bound_seconds",
        "work",
    }
    if not isinstance(value, Mapping):
        raise DelegationCompilerError("delegation request must be an object")
    if set(value) - (request_fields | {"writer_isolation"}):
        raise DelegationCompilerError("delegation request contains unsupported fields")
    if not request_fields <= set(value):
        raise DelegationCompilerError("delegation request is incomplete")
    if value["protocol"] != DELEGATION_REQUEST_PROTOCOL:
        raise DelegationCompilerError("delegation request protocol is invalid")
    authority = value["authority"]
    if authority not in {"delegated", "primary"}:
        raise DelegationCompilerError("delegation request authority is invalid")
    clarification_required = _boolean(
        value["clarification_required"], "clarification_required"
    )
    closed = _boolean(value["closed"], "closed")
    direct = _boolean(value["direct"], "direct")
    tools = _unique_texts(
        value["declared_tools"],
        "declared_tools",
        limit=128,
        maximum=32,
    )
    upper_bound = value["upper_bound_seconds"]
    if (
        isinstance(upper_bound, bool)
        or not isinstance(upper_bound, int)
        or not 0 <= upper_bound <= 86_400
    ):
        raise DelegationCompilerError("upper_bound_seconds is invalid")
    requested_isolation = _normalize_writer_isolation(
        value.get("writer_isolation"), "writer_isolation"
    )
    if not closed and not clarification_required:
        raise DelegationCompilerError(
            "an open request must require clarification before it stays in Primary"
        )
    if authority == "primary":
        return _primary("authority")
    if clarification_required:
        return _primary("clarification")
    if direct:
        return _primary("explicit_direct")
    fast_tool = len(tools) == 1 and upper_bound < 30
    work = value["work"]
    if not isinstance(work, Mapping):
        raise DelegationCompilerError("delegated request work is invalid")
    kind = work.get("kind")
    if kind == "atomic":
        plan = _compile_atomic(work)
    elif kind == "dag":
        dag = _object(work, "DAG work", {"kind", "plan"})
        plan = normalize_closed_plan(dag["plan"])
    elif kind == "planner_proposal":
        plan = _compile_planner_proposal(work)
    else:
        raise DelegationCompilerError("delegated request work kind is invalid")
    if fast_tool:
        return _primary("fast_tool")
    plan_isolation = str(plan.get("writer_isolation", "serial"))
    if (
        "writer_isolation" in value
        and plan_isolation != "serial"
        and plan_isolation != requested_isolation
    ):
        raise DelegationCompilerError("request and plan writer_isolation disagree")
    plan = {
        **plan,
        "writer_isolation": (
            requested_isolation if "writer_isolation" in value else plan_isolation
        ),
    }
    return {
        "disposition": DELEGATE,
        "plan": plan,
        "protocol": DELEGATION_COMPILATION_PROTOCOL,
    }

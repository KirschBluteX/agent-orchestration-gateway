#!/usr/bin/env python3
"""Small, canonical decision rules shared by CCO adapters.

This module deliberately contains policy decisions only.  It does not spawn
agents, inspect the repository, or persist state; callers provide the observed
facts and receive a canonical, testable result.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from protocol_hash import (
    ProtocolHashError,
    repository_scopes_overlap,
    require_repository_scope,
)


ROLES = frozenset({"explorer", "reviewer", "worker"})
ASSURANCES = frozenset({"mechanical", "bounded", "guarded"})
DECISION_SPACES = frozenset(
    {"acceptance_equivalent", "bounded_effect", "unbounded", "unresolved"}
)
CLOSURE_FIELDS = frozenset(
    {
        "acceptance_closed",
        "criteria_closed",
        "decision_space",
        "interfaces_closed",
        "objective_closed",
        "ownership_closed",
    }
)
RISK_CATEGORIES = tuple(
    sorted(
        {
            "authentication_authorization",
            "build_release",
            "concurrency",
            "dependency_boundary",
            "destructive_data",
            "external_side_effect",
            "migration",
            "nondeterministic_verification",
            "public_interface",
            "schema",
            "security",
        }
    )
)
RISK_ANSWERS = frozenset({"no", "yes"})
VERIFICATION_STRENGTHS = frozenset({"deterministic", "manual", "nondeterministic"})
ACCEPTANCE_EVENTS = frozenset(
    {
        "deviation",
        "explicit_independent_review",
        "failure",
        "routing_mismatch",
        "scope_surprise",
        "primary_owned_change",
    }
)
PLACEMENT_BENEFITS = frozenset(
    {
        "closed_chain",
        "context_partition",
        "context_recovery",
        "explicit_delegation",
        "independent_evidence",
        "parallel_ready",
        "runtime_isolation",
    }
)
PLACEMENT_PRIORITY = (
    "explicit_delegation",
    "independent_evidence",
    "parallel_ready",
    "context_partition",
    "context_recovery",
    "runtime_isolation",
    "closed_chain",
)
ACCEPTANCE_ID_RE = re.compile(r"^A[0-9]{2,}$")


class DecisionPolicyError(ValueError):
    """Raised when a policy fact is missing or not canonical."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionPolicyError(f"{label} must be a non-empty string")
    return value


def _sorted_unique_text(values: object, label: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise DecisionPolicyError(f"{label} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise DecisionPolicyError(f"{label} contains an invalid value")
    if values != sorted(values) or len(values) != len(set(values)):
        raise DecisionPolicyError(f"{label} must be sorted and duplicate-free")
    return list(values)


def _sorted_unique_identifiers(
    values: object, label: str, *, allow_empty: bool = False
) -> list[str]:
    if allow_empty and values == []:
        return []
    normalized = _sorted_unique_text(values, label)
    if any(ACCEPTANCE_ID_RE.fullmatch(item) is None for item in normalized):
        raise DecisionPolicyError(f"{label} contains an invalid acceptance ID")
    return normalized


def require_role(value: object) -> str:
    """Return one native-aligned logical role or reject it."""

    role = _text(value, "role")
    if role not in ROLES:
        raise DecisionPolicyError(f"unsupported role: {role}")
    return role


def normalize_closure(value: object) -> dict[str, object]:
    """Validate the complete facts needed to close implementation choices."""

    if not isinstance(value, Mapping) or set(value) != CLOSURE_FIELDS:
        raise DecisionPolicyError("closure must contain every canonical closure field")
    decision_space = _text(value["decision_space"], "closure.decision_space")
    if decision_space not in DECISION_SPACES:
        raise DecisionPolicyError(f"unsupported decision space: {decision_space}")
    normalized: dict[str, object] = {"decision_space": decision_space}
    for field in CLOSURE_FIELDS - {"decision_space"}:
        fact = value[field]
        if type(fact) is not bool:
            raise DecisionPolicyError(f"closure.{field} must be boolean")
        normalized[field] = fact
    return {field: normalized[field] for field in sorted(normalized)}


def _closure_assurance(value: object) -> str:
    closure = normalize_closure(value)
    closed = all(
        bool(closure[field])
        for field in CLOSURE_FIELDS - {"decision_space"}
    )
    if not closed or closure["decision_space"] in {"unbounded", "unresolved"}:
        raise DecisionPolicyError("unresolved closure stays in Primary")
    if closure["decision_space"] == "acceptance_equivalent":
        return "mechanical"
    if closure["decision_space"] == "bounded_effect":
        return "bounded"
    raise DecisionPolicyError("closure decision space is unsupported")


def normalize_risk_assessment(value: object) -> dict[str, str]:
    """Require an explicit yes/no answer for every bounded risk category."""

    expected = set(RISK_CATEGORIES)
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DecisionPolicyError(
            "risk assessment must answer every canonical risk category"
        )
    normalized: dict[str, str] = {}
    for category in RISK_CATEGORIES:
        answer = value[category]
        if type(answer) is not str or answer not in RISK_ANSWERS:
            raise DecisionPolicyError(f"risk assessment {category} must be yes or no")
        normalized[category] = answer
    return normalized


def active_risks(value: object) -> tuple[str, ...]:
    """Return the assessed-yes risks in canonical order."""

    risks = normalize_risk_assessment(value)
    return tuple(category for category in RISK_CATEGORIES if risks[category] == "yes")


def require_verification_strength(value: object) -> str:
    """Return one bounded verification-strength value or reject it."""

    strength = _text(value, "verification strength")
    if strength not in VERIFICATION_STRENGTHS:
        raise DecisionPolicyError(f"unsupported verification strength: {strength}")
    return strength


def derive_acceptance(
    *,
    risk_assessment: object,
    required_verification_strengths: object,
    acceptance_ids: object,
    deterministic_graph_coverage: object,
    events: object,
) -> dict[str, object]:
    """Derive Primary eligibility from evidence-bearing structural facts.

    Subjective difficulty and contract count are intentionally absent: a bounded or
    multi-contract graph is eligible when its risks, required checks, graph
    coverage, and lifecycle events are eligible.
    """

    risks = active_risks(risk_assessment)
    declared_ids = _sorted_unique_identifiers(acceptance_ids, "acceptance_ids")
    covered_ids = _sorted_unique_identifiers(
        deterministic_graph_coverage,
        "deterministic_graph_coverage",
        allow_empty=True,
    )
    if not set(covered_ids) <= set(declared_ids):
        raise DecisionPolicyError(
            "deterministic_graph_coverage contains an undeclared acceptance ID"
        )
    if (
        not isinstance(required_verification_strengths, list)
        or not required_verification_strengths
    ):
        raise DecisionPolicyError(
            "required_verification_strengths must be a non-empty list"
        )
    strengths = [
        require_verification_strength(value)
        for value in required_verification_strengths
    ]
    if not isinstance(events, list):
        raise DecisionPolicyError("events must be a list")
    event_values = []
    for event in events:
        event_text = _text(event, "acceptance event")
        if event_text not in ACCEPTANCE_EVENTS:
            raise DecisionPolicyError(f"unsupported acceptance event: {event_text}")
        event_values.append(event_text)
    if event_values != sorted(set(event_values)):
        raise DecisionPolicyError("events must be sorted and duplicate-free")

    reasons = set(event_values)
    if risks:
        reasons.add("declared_risk")
    if any(strength != "deterministic" for strength in strengths):
        reasons.add("verification_not_deterministic")
    if covered_ids != declared_ids:
        reasons.add("graph_verification_incomplete")
    ordered_reasons = sorted(reasons)
    return {
        "mode": "primary" if not ordered_reasons else "independent",
        "reasons": ordered_reasons,
    }


def derive_assurance(
    *,
    role: object,
    closure: object,
    acceptance_facts: object,
) -> str:
    """Derive the v7 mechanical, bounded, or guarded route assurance.

    Closure decides whether meaningful choices remain. Acceptance facts can only
    raise the assurance floor; they never make an unresolved contract dispatchable.
    """

    normalized_role = require_role(role)
    closure_assurance = _closure_assurance(closure)
    if not isinstance(acceptance_facts, Mapping):
        raise DecisionPolicyError("acceptance_facts must be an object")
    acceptance = derive_acceptance(**acceptance_facts)
    if normalized_role == "reviewer" or acceptance["mode"] == "independent":
        return "guarded"
    return closure_assurance


def select_v7_placement(
    *,
    role: object,
    benefits: object,
    direct_action_count: object,
    direct_verification_count: object,
) -> dict[str, str]:
    """Choose Primary or child from the approved structural whitelist.

    A one-action/one-verification task remains in Primary when its only benefit
    is a closed sequential chain. Explicit delegation, isolation, partitioning,
    parallelism, recovery, or independent evidence remain valid child benefits.
    """

    normalized_role = require_role(role)
    if normalized_role == "reviewer":
        return {"reason": "independent_evidence", "target": "child"}
    for label, value in (
        ("direct_action_count", direct_action_count),
        ("direct_verification_count", direct_verification_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DecisionPolicyError(f"{label} must be a non-negative integer")
    facts = normalize_placement_benefits(benefits)
    required_partition_evidence = {
        "capsule:self-contained",
        "context:history-not-required",
    }
    invalid_context_partition = any(
        item["kind"] == "context_partition"
        and not required_partition_evidence <= set(item["evidence"])
        for item in facts
    )
    facts = [
        item
        for item in facts
        if item["kind"] != "context_partition"
        or required_partition_evidence <= set(item["evidence"])
    ]
    kinds = {item["kind"] for item in facts}
    if not kinds:
        if (
            invalid_context_partition
            and direct_action_count <= 1
            and direct_verification_count <= 1
        ):
            return {"reason": "microtask", "target": "primary"}
        return {"reason": "no_structural_benefit", "target": "primary"}
    if (
        kinds == {"closed_chain"}
        and direct_action_count <= 1
        and direct_verification_count <= 1
    ):
        return {"reason": "microtask", "target": "primary"}
    for kind in PLACEMENT_PRIORITY:
        if kind in kinds:
            return {"reason": kind, "target": "child"}
    raise DecisionPolicyError("placement benefits have no usable reason")


def derive_node_decision(value: object) -> dict[str, object]:
    """Expose the complete v7 policy interface through one deep module."""

    fields = {"acceptance_facts", "closure", "placement", "role"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DecisionPolicyError("node decision must contain every v7 fact group")
    role = require_role(value["role"])
    acceptance_facts = value["acceptance_facts"]
    if not isinstance(acceptance_facts, Mapping):
        raise DecisionPolicyError("acceptance_facts must be an object")
    acceptance = derive_acceptance(**acceptance_facts)
    if role == "reviewer" and acceptance["mode"] != "independent":
        acceptance = {
            "mode": "independent",
            "reasons": ["explicit_independent_review"],
        }
    placement_facts = value["placement"]
    expected = {
        "benefits",
        "direct_action_count",
        "direct_verification_count",
    }
    if not isinstance(placement_facts, Mapping) or set(placement_facts) != expected:
        raise DecisionPolicyError("placement facts are malformed")
    return {
        "acceptance": acceptance,
        "acceptance_ids": list(acceptance_facts["acceptance_ids"]),
        "assurance": derive_assurance(
            role=role,
            closure=value["closure"],
            acceptance_facts=acceptance_facts,
        ),
        "placement": select_v7_placement(
            role=role,
            benefits=placement_facts["benefits"],
            direct_action_count=placement_facts["direct_action_count"],
            direct_verification_count=placement_facts["direct_verification_count"],
        ),
        "role": role,
    }


def normalize_placement_benefits(value: object) -> list[dict[str, Any]]:
    """Validate the evidence-bearing facts that can justify a child."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise DecisionPolicyError("placement benefits must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"evidence", "kind"}:
            raise DecisionPolicyError(f"placement benefit {index} is malformed")
        kind = _text(item["kind"], f"placement benefit {index}.kind")
        if kind not in PLACEMENT_BENEFITS:
            raise DecisionPolicyError(f"unsupported placement benefit: {kind}")
        if kind in seen:
            raise DecisionPolicyError(f"duplicate placement benefit: {kind}")
        seen.add(kind)
        evidence = _sorted_unique_text(
            item["evidence"], f"placement benefit {kind}.evidence"
        )
        normalized.append({"evidence": evidence, "kind": kind})
    if [item["kind"] for item in normalized] != sorted(seen):
        raise DecisionPolicyError("placement benefits must be sorted by kind")
    return normalized


def _normalize_dispatch_node(value: object, index: int) -> dict[str, Any]:
    """Normalize the small, evidence-free shape used by capacity selection."""

    if not isinstance(value, Mapping):
        raise DecisionPolicyError(f"dispatch node {index} must be an object")
    required = {"access", "dependencies_ready", "node", "responsibility", "scope"}
    optional = {"downstream_count"}
    if set(value) - (required | optional) or not required <= set(value):
        raise DecisionPolicyError(
            f"dispatch node {index} must contain {sorted(required)}"
        )
    node = _text(value["node"], f"dispatch node {index}.node")
    responsibility = _text(
        value["responsibility"], f"dispatch node {index}.responsibility"
    )
    access = value["access"]
    if access not in {"read", "write"}:
        raise DecisionPolicyError(f"dispatch node {index}.access must be read or write")
    if type(value["dependencies_ready"]) is not bool:
        raise DecisionPolicyError(
            f"dispatch node {index}.dependencies_ready must be boolean"
        )
    downstream_count = value.get("downstream_count", 0)
    if isinstance(downstream_count, bool) or not isinstance(downstream_count, int) or downstream_count < 0:
        raise DecisionPolicyError(
            f"dispatch node {index}.downstream_count must be a non-negative integer"
        )
    scope = value["scope"]
    if not isinstance(scope, list):
        raise DecisionPolicyError(f"dispatch node {index}.scope must be a list")
    try:
        normalized_scope = [
            require_repository_scope(item, f"dispatch node {index}.scope[{position}]")
            for position, item in enumerate(scope)
        ]
    except ProtocolHashError as error:
        raise DecisionPolicyError(str(error)) from error
    if normalized_scope != sorted(
        normalized_scope, key=lambda item: (item["kind"], item["path"])
    ) or len({(item["kind"], item["path"]) for item in normalized_scope}) != len(
        normalized_scope
    ):
        raise DecisionPolicyError(
            f"dispatch node {index}.scope must be sorted and duplicate-free"
        )
    return {
        "access": access,
        "dependencies_ready": value["dependencies_ready"],
        "node": node,
        "responsibility": responsibility,
        "scope": normalized_scope,
        "downstream_count": downstream_count,
    }


def select_ready_nodes(nodes: object, *, native_capacity: int) -> list[str]:
    """Maximize the independent ready set up to observed native capacity.

    CCO deliberately has no second concurrency ceiling.  The only bound is the
    capacity reported by the native runtime.  Selection is deterministic and exact
    up to that cap: a greedy lower bound handles the usual path, followed only when
    needed by bitset branch-and-bound.  Maximum independent set is NP-hard in the
    worst case, but native capacities are small and the search stops as soon as a
    capacity-sized set is proven by construction.
    """

    if (
        isinstance(native_capacity, bool)
        or not isinstance(native_capacity, int)
        or native_capacity < 0
    ):
        raise DecisionPolicyError("native_capacity must be a non-negative integer")
    if not isinstance(nodes, list):
        raise DecisionPolicyError("dispatch nodes must be a list")
    normalized = [
        _normalize_dispatch_node(value, index)
        for index, value in enumerate(nodes)
    ]
    if len({node["node"] for node in normalized}) != len(normalized):
        raise DecisionPolicyError("dispatch node identifiers must be unique")
    ready = [node for node in normalized if node["dependencies_ready"]]

    def conflicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        if left["responsibility"] == right["responsibility"]:
            return True
        if left["access"] == right["access"] == "read":
            return False
        return any(
            repository_scopes_overlap(left_scope, right_scope)
            for left_scope in left["scope"]
            for right_scope in right["scope"]
        )

    conflict_counts = {
        node["node"]: sum(
            1 for other in ready if other is not node and conflicts(node, other)
        )
        for node in ready
    }
    # Prefer the least constraining ready nodes.  This avoids a broad early scope
    # consuming a slot while excluding several mutually compatible leaves.
    ordered = sorted(
        ready,
        key=lambda item: (
            -item["downstream_count"],
            conflict_counts[item["node"]],
            item["node"],
        ),
    )
    selected: list[dict[str, Any]] = []
    for node in ordered:
        if len(selected) >= native_capacity:
            break
        if any(conflicts(node, admitted) for admitted in selected):
            continue
        selected.append(node)
    if not ready or native_capacity == 0:
        return sorted(node["node"] for node in selected)

    index = {node["node"]: position for position, node in enumerate(ordered)}
    conflict_masks = [0] * len(ordered)
    for left_position, left in enumerate(ordered):
        mask = 0
        for right_position, right in enumerate(ordered):
            if left_position != right_position and conflicts(left, right):
                mask |= 1 << right_position
        conflict_masks[left_position] = mask

    best = [index[node["node"]] for node in selected]

    best_downstream = sum(ordered[position]["downstream_count"] for position in best)
    best_size = len(best)

    def search(candidates: int, chosen: list[int], downstream: int) -> None:
        nonlocal best
        nonlocal best_downstream
        nonlocal best_size
        if downstream > best_downstream or (
            downstream == best_downstream and len(chosen) > best_size
        ):
            best = list(chosen)
            best_downstream = downstream
            best_size = len(chosen)
        remaining_slots = native_capacity - len(chosen)
        if not candidates or remaining_slots == 0:
            return
        positions = [
            position
            for position in range(len(ordered))
            if candidates & (1 << position)
        ]
        optimistic_weights = sorted(
            (ordered[position]["downstream_count"] for position in positions),
            reverse=True,
        )[:remaining_slots]
        optimistic_downstream = downstream + sum(optimistic_weights)
        optimistic_size = len(chosen) + min(remaining_slots, len(positions))
        if optimistic_downstream < best_downstream or (
            optimistic_downstream == best_downstream
            and optimistic_size <= best_size
        ):
            return

        bit = candidates & -candidates
        position = bit.bit_length() - 1
        without = candidates ^ bit
        search(
            without & ~conflict_masks[position],
            [*chosen, position],
            downstream + ordered[position]["downstream_count"],
        )
        search(without, chosen, downstream)

    search((1 << len(ordered)) - 1, [], 0)
    return sorted(ordered[position]["node"] for position in best)

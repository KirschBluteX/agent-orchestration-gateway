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


PURPOSES = frozenset(
    {"analysis_inspect", "analysis_probe", "implementation", "acceptance"}
)
JUDGMENTS = frozenset({"routine", "complex"})
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
ROUTE_ASSURANCES = frozenset({"deterministic", "guarded"})
ACCEPTANCE_EVENTS = frozenset(
    {
        "concurrent_execution",
        "deviation",
        "explicit_independent_review",
        "failure",
        "followup",
        "retry",
        "routing_mismatch",
        "scope_surprise",
        "primary_owned_change",
    }
)
# A concurrent dispatch is a placement/capacity fact, not evidence that the
# resulting state needs an independent acceptance reviewer.  Keep the enum in
# the wire protocol for backwards-compatible packet parsing, but do not let it
# independently upgrade the acceptance mode.
INDEPENDENT_ACCEPTANCE_EVENTS = ACCEPTANCE_EVENTS - {"concurrent_execution"}
PLACEMENT_BENEFITS = frozenset(
    {
        "closed_execution",
        "context_compaction",
        "explicit_delegation",
        "independent_evidence",
        "parallel_ready",
        "runtime_isolation",
        "source_partition",
    }
)
PLACEMENT_PRIORITY = (
    "explicit_delegation",
    "independent_evidence",
    "parallel_ready",
    "context_compaction",
    "source_partition",
    "runtime_isolation",
    "closed_execution",
)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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


def require_purpose(value: object) -> str:
    """Return one canonical CCO purpose or reject it."""

    purpose = _text(value, "purpose")
    if purpose not in PURPOSES:
        raise DecisionPolicyError(f"unsupported purpose: {purpose}")
    return purpose


def classify_purpose(
    *,
    repository_mutation: bool,
    diagnostic_process: bool,
    acceptance_verdict: bool,
) -> str:
    """Derive purpose from the effects a closed task is authorized to perform."""

    facts = (repository_mutation, diagnostic_process, acceptance_verdict)
    if any(type(value) is not bool for value in facts):
        raise DecisionPolicyError("purpose facts must be boolean")
    if acceptance_verdict:
        if repository_mutation or diagnostic_process:
            raise DecisionPolicyError(
                "acceptance purpose cannot authorize mutation or diagnostics"
            )
        return "acceptance"
    if repository_mutation:
        return "implementation"
    if diagnostic_process:
        return "analysis_probe"
    return "analysis_inspect"


def normalize_closure(value: object) -> dict[str, object]:
    """Validate the complete facts needed to close implementation judgment."""

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


def derive_judgment(value: object) -> str:
    """Derive routine, complex, or unresolved from one complete closure record."""

    closure = normalize_closure(value)
    return classify_judgment(
        objective_closed=closure["objective_closed"],
        interfaces_closed=closure["interfaces_closed"],
        acceptance_closed=closure["acceptance_closed"],
        ownership_closed=closure["ownership_closed"],
        criteria_closed=closure["criteria_closed"],
        decision_space=closure["decision_space"],
    )


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

    Judgment and contract count are intentionally absent: a complex or
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

    reasons = {
        event
        for event in event_values
        if event in INDEPENDENT_ACCEPTANCE_EVENTS
    }
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


def derive_route_assurance(
    *,
    risk_assessment: object,
    required_verification_strengths: object,
    acceptance_ids: object,
    deterministic_graph_coverage: object,
    events: object,
) -> str:
    """Derive model eligibility from the acceptance facts already in the contract."""

    acceptance = derive_acceptance(
        risk_assessment=risk_assessment,
        required_verification_strengths=required_verification_strengths,
        acceptance_ids=acceptance_ids,
        deterministic_graph_coverage=deterministic_graph_coverage,
        events=events,
    )
    guarded_reasons = set(acceptance["reasons"]) - {"explicit_independent_review"}
    return "guarded" if guarded_reasons else "deterministic"


def normalize_dispatch_decision(
    value: object,
    *,
    selected_model: str | None,
) -> dict[str, object]:
    """Recompute every derived dispatch dimension from one closed fact record."""

    required = {
        "acceptance_facts",
        "closure",
        "derived",
        "effects",
        "placement",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DecisionPolicyError("dispatch decision must contain every fact group")
    effects = value["effects"]
    effect_fields = {
        "acceptance_verdict",
        "diagnostic_process",
        "repository_mutation",
    }
    if not isinstance(effects, Mapping) or set(effects) != effect_fields:
        raise DecisionPolicyError("dispatch effects are malformed")
    purpose = classify_purpose(
        repository_mutation=effects["repository_mutation"],
        diagnostic_process=effects["diagnostic_process"],
        acceptance_verdict=effects["acceptance_verdict"],
    )
    closure = normalize_closure(value["closure"])
    judgment = derive_judgment(closure)
    if judgment == "unresolved":
        raise DecisionPolicyError("unresolved judgment cannot be dispatched")

    acceptance_facts = value["acceptance_facts"]
    acceptance_fields = {
        "acceptance_ids",
        "deterministic_graph_coverage",
        "events",
        "required_verification_strengths",
        "risk_assessment",
    }
    if not isinstance(acceptance_facts, Mapping) or set(acceptance_facts) != acceptance_fields:
        raise DecisionPolicyError("dispatch acceptance facts are malformed")
    acceptance = derive_acceptance(**acceptance_facts)
    assurance = derive_route_assurance(**acceptance_facts)

    placement_facts = value["placement"]
    if not isinstance(placement_facts, Mapping) or set(placement_facts) != {
        "benefits",
        "primary_model",
    }:
        raise DecisionPolicyError("dispatch placement facts are malformed")
    primary_model = placement_facts["primary_model"]
    if primary_model is not None and not isinstance(primary_model, str):
        raise DecisionPolicyError("dispatch primary model is malformed")
    benefits = normalize_placement_benefits(placement_facts["benefits"])
    placement = select_placement(
        purpose=purpose,
        primary_model=primary_model,
        selected_model=selected_model,
        benefits=benefits,
    )
    derived = value["derived"]
    expected_derived = {
        "acceptance": acceptance,
        "assurance": assurance,
        "judgment": judgment,
        "placement": placement,
        "purpose": purpose,
    }
    if not isinstance(derived, Mapping) or dict(derived) != expected_derived:
        raise DecisionPolicyError("dispatch derived decision does not match its facts")
    return {
        "acceptance_facts": {
            "acceptance_ids": list(acceptance_facts["acceptance_ids"]),
            "deterministic_graph_coverage": list(
                acceptance_facts["deterministic_graph_coverage"]
            ),
            "events": list(acceptance_facts["events"]),
            "required_verification_strengths": list(
                acceptance_facts["required_verification_strengths"]
            ),
            "risk_assessment": normalize_risk_assessment(
                acceptance_facts["risk_assessment"]
            ),
        },
        "closure": closure,
        "derived": expected_derived,
        "effects": {name: effects[name] for name in sorted(effect_fields)},
        "placement": {
            "benefits": benefits,
            "primary_model": primary_model,
        },
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


def select_placement(
    *,
    purpose: str,
    primary_model: str | None,
    selected_model: str | None,
    benefits: object = None,
) -> dict[str, str]:
    """Return the only placement permitted by observed structural facts.

    A route is never a reason by itself to create a child.  A same-model child
    is reclaimed only when its sole reason would be closed execution; explicit
    isolation, parallelism, or independent evidence still has structural value.
    """

    require_purpose(purpose)
    if primary_model is not None and MODEL_RE.fullmatch(primary_model) is None:
        raise DecisionPolicyError("primary_model is invalid")
    if selected_model is not None and MODEL_RE.fullmatch(selected_model) is None:
        raise DecisionPolicyError("selected_model is invalid")
    facts = normalize_placement_benefits(benefits)
    kinds = {item["kind"] for item in facts}
    if purpose == "acceptance":
        return {"reason": "independent_acceptance", "target": "child"}
    if not kinds:
        return {"reason": "no_structural_benefit", "target": "primary"}
    if (
        primary_model is not None
        and selected_model is not None
        and primary_model == selected_model
        and kinds == {"closed_execution"}
    ):
        return {
            "reason": "same_model_execution_only",
            "target": "primary",
        }
    for kind in PLACEMENT_PRIORITY:
        if kind in kinds:
            return {"reason": kind, "target": "child"}
    raise DecisionPolicyError("placement benefits have no usable reason")


def classify_judgment(
    *,
    objective_closed: bool,
    interfaces_closed: bool,
    acceptance_closed: bool,
    ownership_closed: bool,
    decision_space: str,
    criteria_closed: bool,
) -> str:
    """Classify a dispatch candidate without guessing at unresolved planning."""

    if not all(
        isinstance(value, bool)
        for value in (
            objective_closed,
            interfaces_closed,
            acceptance_closed,
            ownership_closed,
            criteria_closed,
        )
    ):
        raise DecisionPolicyError("closure facts must be boolean")
    if any(
        not value
        for value in (
            objective_closed,
            interfaces_closed,
            acceptance_closed,
            ownership_closed,
            criteria_closed,
        )
    ):
        return "unresolved"
    if decision_space not in DECISION_SPACES:
        raise DecisionPolicyError(f"unsupported decision space: {decision_space}")
    if decision_space == "acceptance_equivalent":
        return "routine"
    if decision_space == "bounded_effect":
        return "complex"
    if decision_space in {"unresolved", "unbounded"}:
        return "unresolved"
    raise DecisionPolicyError(f"unsupported decision space: {decision_space}")


def canonical_benefit_kinds(value: object) -> tuple[str, ...]:
    """Expose only the stable kind set for route explanations and tests."""

    return tuple(item["kind"] for item in normalize_placement_benefits(value))


def _normalize_dispatch_node(value: object, index: int) -> dict[str, Any]:
    """Normalize the small, evidence-free shape used by capacity selection."""

    if not isinstance(value, Mapping):
        raise DecisionPolicyError(f"dispatch node {index} must be an object")
    required = {"access", "dependencies_ready", "node", "responsibility", "scope"}
    if set(value) != required:
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
        key=lambda item: (conflict_counts[item["node"]], item["node"]),
    )
    selected: list[dict[str, Any]] = []
    for node in ordered:
        if len(selected) >= native_capacity:
            break
        if any(conflicts(node, admitted) for admitted in selected):
            continue
        selected.append(node)
    if len(selected) >= native_capacity or not ready:
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

    def search(candidates: int, chosen: list[int]) -> bool:
        nonlocal best
        if len(chosen) > len(best):
            best = list(chosen)
            if len(best) == native_capacity:
                return True
        if not candidates or len(chosen) + candidates.bit_count() <= len(best):
            return False
        while candidates:
            if len(chosen) + candidates.bit_count() <= len(best):
                return False
            bit = candidates & -candidates
            position = bit.bit_length() - 1
            candidates ^= bit
            compatible = candidates & ~conflict_masks[position]
            if search(compatible, [*chosen, position]):
                return True
        return False

    search((1 << len(ordered)) - 1, [])
    return sorted(ordered[position]["node"] for position in best)

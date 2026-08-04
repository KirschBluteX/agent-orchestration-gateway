from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from routing_catalog import resolve_route_plan  # noqa: E402
from decision_policy import normalize_dispatch_decision  # noqa: E402


def no_risks() -> dict[str, str]:
    return {
        name: "no"
        for name in (
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
        )
    }


def closed_graph_node(
    *,
    node: str = "n01_graph",
    path: str = "src/owned.txt",
    responsibility: str = "owned-file",
) -> dict[str, object]:
    return {
        "acceptance_facts": {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "events": [],
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": no_risks(),
        },
        "closure": {
            "acceptance_closed": True,
            "criteria_closed": True,
            "decision_space": "bounded_effect",
            "interfaces_closed": True,
            "objective_closed": True,
            "ownership_closed": True,
        },
        "contract": {
            "contract_rev": 1,
            "node": node,
            "objective": "change the owned file",
        },
        "effects": {
            "acceptance_verdict": False,
            "diagnostic_process": False,
            "repository_mutation": True,
        },
        "generation": 1,
        "node": node,
        "placement": {
            "benefits": [
                {"evidence": ["contract:A01"], "kind": "closed_execution"}
            ],
            "primary_model": "gpt-5.6-sol",
        },
        "scopes": [{"kind": "exact", "path": path}],
        "selection": {
            "dependencies_ready": True,
            "responsibility": responsibility,
        },
    }


def fixed_route_plan(
    *,
    purpose: str = "implementation",
    judgment: str = "routine",
    model: str = "gpt-5.6-luna",
    effort: str = "max",
) -> dict[str, object]:
    request: dict[str, object] = {
        "assurance": "deterministic",
        "fixed_effort": effort,
        "fixed_model": model,
        "judgment": judgment,
        "placement_benefits": [],
        "purpose": purpose,
    }
    if purpose != "acceptance":
        request["placement_benefits"] = [
            {"evidence": ["contract:test"], "kind": "closed_execution"}
        ]
    native_catalog = {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [{"effort": effort}],
            }
        ]
    }
    return resolve_route_plan([request], {}, native_catalog)


def dispatch_decision(
    *,
    purpose: str = "implementation",
    judgment: str = "routine",
    selected_model: str = "gpt-5.6-luna",
) -> dict[str, object]:
    effects = {
        "acceptance_verdict": purpose == "acceptance",
        "diagnostic_process": purpose == "analysis_probe",
        "repository_mutation": purpose == "implementation",
    }
    closure = {
        "acceptance_closed": True,
        "criteria_closed": True,
        "decision_space": (
            "acceptance_equivalent" if judgment == "routine" else "bounded_effect"
        ),
        "interfaces_closed": True,
        "objective_closed": True,
        "ownership_closed": True,
    }
    risk_assessment = {
        name: "no"
        for name in (
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
        )
    }
    acceptance = (
        {"mode": "independent", "reasons": ["explicit_independent_review"]}
        if purpose == "acceptance"
        else {"mode": "primary", "reasons": []}
    )
    placement = (
        {"reason": "independent_acceptance", "target": "child"}
        if purpose == "acceptance"
        else {"reason": "closed_execution", "target": "child"}
    )
    value = {
        "acceptance_facts": {
            "acceptance_ids": ["A01"],
            "deterministic_graph_coverage": ["A01"],
            "events": ["explicit_independent_review"] if purpose == "acceptance" else [],
            "required_verification_strengths": ["deterministic"],
            "risk_assessment": risk_assessment,
        },
        "closure": closure,
        "derived": {
            "acceptance": acceptance,
            "assurance": "deterministic",
            "judgment": judgment,
            "placement": placement,
            "purpose": purpose,
        },
        "effects": effects,
        "placement": {
            "benefits": (
                []
                if purpose == "acceptance"
                else [{"evidence": ["contract:test"], "kind": "closed_execution"}]
            ),
            "primary_model": "gpt-5.6-sol",
        },
    }
    return normalize_dispatch_decision(value, selected_model=selected_model)

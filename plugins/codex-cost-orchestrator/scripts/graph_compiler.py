#!/usr/bin/env python3
"""Single deterministic entry point for preparing a cco.v7 dispatch graph."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping

from decision_policy import (
    DecisionPolicyError,
    derive_node_decision,
    normalize_risk_assessment,
    select_ready_nodes,
)
from packet_compiler import CapsuleError, compile_dispatch
from prepared_graph import (
    artifact_path,
    graph_scopes,
    graph_sha256,
    load_artifact,
    verify_artifact_workspace,
    write_artifact,
)
from protocol_hash import ProtocolHashError, canonical_bytes, require_repository_scope
from routing_catalog import (
    RoutingCatalogError,
    advance_route_plan,
    load_json_bytes,
    load_native_catalog,
    load_route_policy,
    resolve_route_plan,
)
from workspace_state import (
    DEFAULT_IGNORED_MAX_BYTES,
    DEFAULT_IGNORED_MAX_FILES,
    StateError,
    WORKSPACE_MODES,
    normalize_allow,
    repository_control_roots,
    repository_gitlinks,
    repository_index_records,
    repository_path_spelling_map,
    repository_root,
    state_payload,
)


PROTOCOL = "cco.prepared-graph.v2"
GRAPH_PROTOCOL = "cco.graph.v3"
DISPATCH_BATCH_PROTOCOL = "cco.dispatch-batch.v1"
CLI_INPUT_MAX_BYTES = 4 * 1024 * 1024
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
NODE_FIELDS = frozenset(
    {
        "acceptance_facts",
        "closure",
        "contract",
        "generation",
        "node",
        "placement",
        "role",
        "scopes",
        "selection",
    }
)
OPTIONAL_NODE_FIELDS = frozenset(
    {"current_state", "epoch", "evidence", "fork_turns", "route"}
)


class GraphCompilerError(ValueError):
    """A graph cannot be safely derived or bound to the current workspace."""


def _scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise GraphCompilerError(f"{label} must be a list")
    try:
        scopes = [require_repository_scope(item, f"{label}[{i}]") for i, item in enumerate(value)]
    except ProtocolHashError as error:
        raise GraphCompilerError(str(error)) from error
    if scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"])) or len({(item["kind"], item["path"]) for item in scopes}) != len(scopes):
        raise GraphCompilerError(f"{label} must be sorted and duplicate-free")
    return scopes


def _route_constraints(value: object) -> dict[str, Any]:
    if value is None:
        return {"fixed_effort": None, "fixed_model": None, "source": "automatic"}
    if not isinstance(value, Mapping) or set(value) != {"fixed_effort", "fixed_model", "source"}:
        raise GraphCompilerError("node route constraints are malformed")
    if value["source"] not in {"automatic", "user"}:
        raise GraphCompilerError("node route constraint source is invalid")
    return dict(value)


def _route_for(plan: Mapping[str, Any], node: str) -> dict[str, Any]:
    matches = [route for route in plan["routes"] if route["node"] == node]
    if len(matches) != 1:
        raise GraphCompilerError(f"route plan lacks one exact route for {node}")
    return matches[0]


def _route_bindings(plans: list[Mapping[str, Any]], *, node: str) -> list[dict[str, Any]]:
    return [
        {
            "constraints": dict(route["constraints"]),
            "decision_sha256": route["decision_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "rank": route["dispatch"]["rank"],
            "selected": dict(route["selected"]),
        }
        for plan in plans
        for route in [_route_for(plan, node)]
    ]


def _baseline_path(session_id: str, identity: str) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    absolute = Path(os.path.abspath(root))
    for candidate in (absolute, *absolute.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise GraphCompilerError("ledger directory cannot use a reparse ancestor")
    return artifact_path(absolute.resolve(), session_id, identity)


def _normalize_node(value: object, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - (NODE_FIELDS | OPTIONAL_NODE_FIELDS) or not NODE_FIELDS <= set(value):
        raise GraphCompilerError(f"graph node {index} has unsupported or missing fields")
    node = value["node"]
    if not isinstance(node, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", node):
        raise GraphCompilerError(f"graph node {index}.node is invalid")
    contract = value["contract"]
    if not isinstance(contract, Mapping) or contract.get("node") != node:
        raise GraphCompilerError(f"graph node {index}.contract identity is inconsistent")
    try:
        decision = derive_node_decision(
            {
                "acceptance_facts": value["acceptance_facts"],
                "closure": value["closure"],
                "placement": value["placement"],
                "role": value["role"],
            }
        )
    except DecisionPolicyError as error:
        raise GraphCompilerError(f"node {node} decision facts are invalid: {error}") from error
    scopes = _scopes(value["scopes"], f"graph node {index}.scopes")
    selection = value["selection"]
    selection_required = {"dependencies_ready", "responsibility"}
    if (
        not isinstance(selection, Mapping)
        or set(selection) - (selection_required | {"downstream_count"})
        or not selection_required <= set(selection)
        or type(selection["dependencies_ready"]) is not bool
        or not isinstance(selection["responsibility"], str)
        or not selection["responsibility"]
        or isinstance(selection.get("downstream_count", 0), bool)
        or not isinstance(selection.get("downstream_count", 0), int)
        or selection.get("downstream_count", 0) < 0
    ):
        raise GraphCompilerError(f"graph node {index}.selection is malformed")
    generation = value["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise GraphCompilerError(f"graph node {index}.generation is invalid")
    route_constraints = _route_constraints(value.get("route"))
    return {
        "acceptance_facts": {
            "acceptance_ids": list(value["acceptance_facts"]["acceptance_ids"]),
            "deterministic_graph_coverage": list(value["acceptance_facts"]["deterministic_graph_coverage"]),
            "events": list(value["acceptance_facts"]["events"]),
            "required_verification_strengths": list(value["acceptance_facts"]["required_verification_strengths"]),
            "risk_assessment": normalize_risk_assessment(value["acceptance_facts"]["risk_assessment"]),
        },
        "contract": dict(contract),
        "decision": decision,
        "generation": generation,
        "node": node,
        "optional": {name: value[name] for name in OPTIONAL_NODE_FIELDS if name in value},
        "route_constraints": route_constraints,
        "scopes": scopes,
        "selection": {
            "dependencies_ready": selection["dependencies_ready"],
            "downstream_count": selection.get("downstream_count", 0),
            "responsibility": selection["responsibility"],
        },
    }


def _route_request(item: Mapping[str, Any]) -> dict[str, Any]:
    decision = item["decision"]
    return {
        "assurance": decision["assurance"],
        "constraints": item["route_constraints"],
        "node": item["node"],
        "role": decision["role"],
    }


def _primary_only_result(
    normalized: list[Mapping[str, Any]],
    route_errors: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "baseline": None,
        "baseline_path": None,
        "dispatches": [],
        "fallback_dispatches": {},
        "graph_sha256": None,
        "manifest": None,
        "primary_nodes": sorted(item["node"] for item in normalized),
        "protocol": PROTOCOL,
        "route_errors": {node: route_errors[node] for node in sorted(route_errors)},
        "route_plan": None,
    }


def prepare_dispatch_graph(
    nodes: object,
    *,
    native_capacity: int,
    repo: Path,
    native_catalog: object | None = None,
    policy: object = None,
    codex_home: Path | None = None,
    workspace_mode: str = "light",
    ignored_max_files: int = DEFAULT_IGNORED_MAX_FILES,
    ignored_max_bytes: int = DEFAULT_IGNORED_MAX_BYTES,
) -> dict[str, Any]:
    """Derive, route, baseline, and compile one complete graph in one call."""

    if not isinstance(nodes, list) or not nodes:
        raise GraphCompilerError("graph nodes must be a non-empty list")
    if workspace_mode not in WORKSPACE_MODES:
        raise GraphCompilerError("workspace mode must be light or strict")
    session_id = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise GraphCompilerError("CODEX_THREAD_ID is required for task-local baselines")
    normalized = [_normalize_node(value, index) for index, value in enumerate(nodes)]
    if len({item["node"] for item in normalized}) != len(normalized):
        raise GraphCompilerError("graph node identities must be unique")
    root = repository_root(Path(repo))
    control_roots = repository_control_roots(root)
    index_records = repository_index_records(root)
    gitlinks = repository_gitlinks(root, index_records)
    tracked_spellings = repository_path_spelling_map(index_records)
    directory_spellings: dict[str, frozenset[str]] = {}
    try:
        for item in normalized:
            item["scopes"] = [
                normalize_allow(
                    root,
                    f"{scope['kind']}:{scope['path']}",
                    protected_roots=control_roots,
                    gitlinks=gitlinks,
                    tracked_spellings=tracked_spellings,
                    directory_spellings=directory_spellings,
                )
                for scope in item["scopes"]
            ]
    except StateError as error:
        raise GraphCompilerError(f"graph scope is unsafe: {error}") from error
    child_items = [item for item in normalized if item["decision"]["placement"]["target"] == "child"]
    if not child_items:
        return _primary_only_result(normalized, {})
    route_errors: dict[str, str] = {}
    catalog_error: RoutingCatalogError | None = None
    if native_catalog is None:
        try:
            native_catalog = load_native_catalog()
        except RoutingCatalogError as error:
            catalog_error = error
    loaded_policy = policy
    policy_error: RoutingCatalogError | None = None
    if loaded_policy is None and catalog_error is None:
        try:
            loaded = load_route_policy(root, codex_home=codex_home)
            loaded_policy = loaded["policy"]
        except RoutingCatalogError as error:
            policy_error = error

    routable_items: list[dict[str, Any]] = []
    if catalog_error is not None or policy_error is not None:
        error = catalog_error or policy_error
        for item in child_items:
            route_errors[item["node"]] = str(error)
    else:
        for item in child_items:
            try:
                resolve_route_plan(
                    [_route_request(item)],
                    native_catalog,
                    policy=loaded_policy,
                )
            except RoutingCatalogError as error:
                route_errors[item["node"]] = str(error)
            else:
                routable_items.append(item)
    for item in child_items:
        if item["node"] in route_errors:
            item["decision"]["placement"] = {
                "reason": "route_unavailable",
                "target": "primary",
            }
    child_items = routable_items
    try:
        route_plan = (
            resolve_route_plan(
                [_route_request(item) for item in child_items],
                native_catalog,
                policy=loaded_policy,
            )
            if child_items
            else None
        )
    except RoutingCatalogError as error:  # defensive: per-node probes already passed
        raise GraphCompilerError(f"combined route plan is inconsistent: {error}") from error
    if not child_items:
        return _primary_only_result(normalized, route_errors)

    route_variants: dict[str, list[dict[str, Any]]] = {}
    for item in child_items:
        if route_plan is None:
            raise GraphCompilerError("child route plan is missing")
        variants = [route_plan]
        current = route_plan
        route = _route_for(current, item["node"])
        while route["dispatch"]["rank"] < len(route["candidates"]):
            selected = route["selected"]
            try:
                current = advance_route_plan(
                    current,
                    node=item["node"],
                    rejected_model=selected["model"],
                    rejected_effort=selected["effort"],
                    rejection_ticket=f"native:prethread-rejected-r{route['dispatch']['rank']:02d}",
                )
            except RoutingCatalogError as error:
                raise GraphCompilerError(f"route fallback is invalid: {error}") from error
            variants.append(current)
            route = _route_for(current, item["node"])
        route_variants[item["node"]] = variants

    snapshot = state_payload(
        root,
        control_roots=control_roots,
        index_records=index_records,
        ignored_mode=workspace_mode,
        ignored_max_files=ignored_max_files,
        ignored_max_bytes=ignored_max_bytes,
        scopes=[scope for item in normalized for scope in item["scopes"]],
    )
    manifest_nodes: list[dict[str, Any]] = []
    for item in sorted(normalized, key=lambda entry: entry["node"]):
        bindings = _route_bindings(route_variants[item["node"]], node=item["node"]) if item["node"] in route_variants else []
        manifest_nodes.append(
            {
                "contract": item["contract"],
                "decision": item["decision"],
                "node": item["node"],
                "route_bindings": bindings,
                "scopes": item["scopes"],
            }
        )
    manifest = {
        "baseline": snapshot["state_id"],
        "nodes": manifest_nodes,
        "protocol": GRAPH_PROTOCOL,
        "route_plan_sha256": route_plan["plan_sha256"] if route_plan else None,
        "workspace_mode": workspace_mode,
    }
    identity = graph_sha256(manifest)
    baseline_path = _baseline_path(session_id, identity)
    selector_nodes = [
        {
            "access": "write" if item["decision"]["role"] == "worker" else "read",
            "dependencies_ready": item["selection"]["dependencies_ready"],
            "node": item["node"],
            "responsibility": item["selection"]["responsibility"],
            "scope": item["scopes"],
            "downstream_count": item["selection"].get("downstream_count", 0),
        }
        for item in child_items
    ]
    try:
        selected_nodes = select_ready_nodes(selector_nodes, native_capacity=native_capacity)
    except DecisionPolicyError as error:
        raise GraphCompilerError(f"graph capacity selection failed: {error}") from error
    by_node = {item["node"]: item for item in normalized}
    dispatches: list[dict[str, Any]] = []
    fallback_dispatches: dict[str, list[dict[str, Any]]] = {}
    for node in selected_nodes:
        item = by_node[node]
        decision = item["decision"]
        route = _route_for(route_variants[node][0], node)
        optional = item["optional"]
        spec: dict[str, Any] = {
            "acceptance": decision["acceptance"],
            "acceptance_ids": decision["acceptance_ids"],
            "assurance": decision["assurance"],
            "baseline": snapshot["state_id"],
            "contract": item["contract"],
            "fork_turns": optional.get("fork_turns", "none"),
            "generation": item["generation"],
            "graph_sha256": identity,
            "mode": "fresh" if decision["role"] == "reviewer" else workspace_mode,
            "node": node,
            "role": decision["role"],
            "route": {
                "constraints": route["constraints"],
                "decision_sha256": route["decision_sha256"],
                "plan_sha256": route_variants[node][0]["plan_sha256"],
                "rank": route["dispatch"]["rank"],
                "selected": route["selected"],
            },
            "scopes": item["scopes"],
        }
        for name in ("current_state", "epoch", "evidence"):
            if name in optional:
                spec[name] = optional[name]
        compiled: list[dict[str, Any]] = []
        for variant in route_variants[node]:
            variant_route = _route_for(variant, node)
            variant_spec = dict(spec)
            variant_spec["route"] = {
                "constraints": variant_route["constraints"],
                "decision_sha256": variant_route["decision_sha256"],
                "plan_sha256": variant["plan_sha256"],
                "rank": variant_route["dispatch"]["rank"],
                "selected": variant_route["selected"],
            }
            try:
                compiled.append(compile_dispatch(variant_spec))
            except CapsuleError as error:
                raise GraphCompilerError(f"graph node {node} could not compile: {error}") from error
        dispatches.append(compiled[0])
        fallback_dispatches[node] = compiled[1:]
    write_artifact(root, baseline_path, manifest=manifest, snapshot=snapshot)
    primary_nodes = sorted(item["node"] for item in normalized if item["node"] not in selected_nodes)
    return {
        "baseline": snapshot["state_id"],
        "baseline_path": str(baseline_path),
        "dispatches": dispatches,
        "fallback_dispatches": fallback_dispatches,
        "graph_sha256": identity,
        "manifest": manifest,
        "primary_nodes": primary_nodes,
        "protocol": PROTOCOL,
        "route_errors": {node: route_errors[node] for node in sorted(route_errors)},
        "route_plan": route_plan,
    }


def verify_prepared_graph(prepared: object, *, repo: Path) -> dict[str, Any]:
    required = {"baseline", "baseline_path", "dispatches", "fallback_dispatches", "graph_sha256", "manifest", "primary_nodes", "protocol", "route_errors", "route_plan"}
    if not isinstance(prepared, Mapping) or set(prepared) != required or prepared.get("protocol") != PROTOCOL:
        raise GraphCompilerError("prepared graph is malformed")
    manifest = prepared.get("manifest")
    if not isinstance(manifest, Mapping) or graph_sha256(manifest) != prepared.get("graph_sha256"):
        raise GraphCompilerError("prepared graph identity does not match manifest")
    baseline_path = Path(str(prepared["baseline_path"]))
    try:
        artifact = load_artifact(baseline_path)
    except ValueError as error:
        raise GraphCompilerError(f"prepared graph baseline is invalid: {error}") from error
    if artifact["snapshot"]["state_id"] != prepared["baseline"] or artifact["manifest"] != manifest or artifact["graph_sha256"] != prepared["graph_sha256"]:
        raise GraphCompilerError("prepared graph baseline identity is inconsistent")
    try:
        return verify_artifact_workspace(
            baseline_path,
            repo=repo,
            baseline=str(prepared["baseline"]),
            graph_sha256_value=str(prepared["graph_sha256"]),
            graph_scopes_value=graph_scopes(manifest),
            workspace_mode=str(manifest["workspace_mode"]),
        )
    except ValueError as error:
        raise GraphCompilerError(f"prepared graph verification failed: {error}") from error


def compact_dispatch_batch(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the data Primary needs to invoke native Agent tools."""

    return {
        "baseline": prepared["baseline"],
        "baseline_path": prepared["baseline_path"],
        "dispatches": prepared["dispatches"],
        "fallback_dispatches": prepared["fallback_dispatches"],
        "graph_sha256": prepared["graph_sha256"],
        "primary_nodes": prepared["primary_nodes"],
        "protocol": DISPATCH_BATCH_PROTOCOL,
        "route_errors": prepared["route_errors"],
    }


def _prepare_document(document: object, args: argparse.Namespace) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise GraphCompilerError("prepare input must be a JSON object")
    optional = {
        "codex_home",
        "ignored_max_bytes",
        "ignored_max_files",
        "native_catalog",
        "policy",
    }
    if set(document) - ({"nodes"} | optional) or "nodes" not in document:
        raise GraphCompilerError("prepare input fields are incomplete or unsupported")
    codex_home = document.get("codex_home")
    if codex_home is not None and not isinstance(codex_home, str):
        raise GraphCompilerError("codex_home must be a path string")
    prepared = prepare_dispatch_graph(
        document["nodes"],
        native_capacity=args.native_capacity,
        repo=args.repo,
        native_catalog=document.get("native_catalog"),
        policy=document.get("policy"),
        codex_home=Path(codex_home) if codex_home is not None else None,
        workspace_mode=args.workspace_mode,
        ignored_max_files=document.get("ignored_max_files", DEFAULT_IGNORED_MAX_FILES),
        ignored_max_bytes=document.get("ignored_max_bytes", DEFAULT_IGNORED_MAX_BYTES),
    )
    return prepared if args.full else compact_dispatch_batch(prepared)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Prepare one network-free cco.v7 dispatch graph from JSON stdin."
    )
    root.add_argument("--repo", type=Path, required=True)
    root.add_argument("--native-capacity", type=int, required=True)
    root.add_argument("--workspace-mode", choices=tuple(sorted(WORKSPACE_MODES)), default="light")
    root.add_argument("--full", action="store_true", help="Emit diagnostic manifest and route plan")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        raw = sys.stdin.buffer.read(CLI_INPUT_MAX_BYTES + 1)
        if len(raw) > CLI_INPUT_MAX_BYTES:
            raise GraphCompilerError("prepare input exceeds the size limit")
        document = load_json_bytes(raw, "graph prepare input")
        print(canonical_bytes(_prepare_document(document, args)).decode("utf-8"))
        return 0
    except (GraphCompilerError, RoutingCatalogError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

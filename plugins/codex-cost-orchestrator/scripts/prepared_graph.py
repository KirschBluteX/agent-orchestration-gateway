#!/usr/bin/env python3
"""Persist and verify one compact cco.v7 prepared-workspace artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    repository_scopes_overlap,
    require_repository_scope,
)
from routing_catalog import RoutingCatalogError, validate_route_constraints, validate_route_pair
from workspace_state import (
    StateError,
    normalize_allow,
    repository_control_roots,
    repository_gitlinks,
    repository_index_records,
    repository_path_spelling_map,
    repository_root,
    validate_snapshot,
    verify,
    write_snapshot,
)


ARTIFACT_PROTOCOL = "cco.prepared-workspace.v2"
GRAPH_PROTOCOL = "cco.graph.v4"
LEGACY_GRAPH_PROTOCOL = "cco.graph.v3"
GRAPH_DOMAINS = {
    GRAPH_PROTOCOL: b"cco.graph.v4\0",
    LEGACY_GRAPH_PROTOCOL: b"cco.graph.v3\0",
}
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ROUTE_BINDINGS = 4
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
ARTIFACT_FILENAME = re.compile(r"^.+-[0-9a-f]{64}\.json$")
NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class PreparedGraphError(ValueError):
    """A prepared graph artifact is missing, malformed, or state-inconsistent."""


def graph_sha256(manifest: Mapping[str, Any]) -> str:
    protocol = manifest.get("protocol") if isinstance(manifest, Mapping) else None
    domain = GRAPH_DOMAINS.get(protocol)
    if domain is None:
        raise PreparedGraphError("prepared graph protocol is unsupported")
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(dict(manifest))).hexdigest()


def artifact_path(ledger_root: Path, session_id: str, identity: str) -> Path:
    if SESSION_ID.fullmatch(session_id) is None or SHA256.fullmatch(identity) is None:
        raise PreparedGraphError("prepared workspace identity is invalid")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    return root.parent / "workspace" / f"{session_id}-{identity[7:]}.json"


def cleanup_session_artifacts(ledger_root: Path, session_id: str) -> list[Path]:
    if SESSION_ID.fullmatch(session_id) is None:
        raise PreparedGraphError("prepared workspace session identity is invalid")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    workspace = root.parent / "workspace"
    if not workspace.is_dir():
        return []
    removed: list[Path] = []
    for candidate in workspace.glob(f"{session_id}-*.json"):
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
            removed.append(candidate)
    try:
        workspace.rmdir()
    except OSError:
        pass
    return removed


def cleanup_graph_artifact(ledger_root: Path, session_id: str, identity: str) -> bool:
    """Delete a terminal graph's large artifact while its ledger tombstone remains."""

    path = artifact_path(ledger_root, session_id, identity)
    existed = path.exists() or path.is_symlink()
    if existed:
        path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return existed


def cleanup_stale_artifacts(ledger_root: Path, *, keep_session_id: str, max_age_seconds: float) -> list[Path]:
    if SESSION_ID.fullmatch(keep_session_id) is None or max_age_seconds < 60:
        raise PreparedGraphError("prepared workspace cleanup bounds are invalid")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    workspace = root.parent / "workspace"
    if not workspace.is_dir():
        return []
    now = time.time()
    removed: list[Path] = []
    for candidate in workspace.iterdir():
        if candidate.name.startswith(f"{keep_session_id}-"):
            continue
        if not (ARTIFACT_FILENAME.fullmatch(candidate.name) or candidate.name.startswith(".cco-state-")):
            continue
        try:
            expired = now - candidate.lstat().st_mtime > max_age_seconds
        except OSError:
            continue
        if expired and (candidate.is_symlink() or candidate.is_file()):
            candidate.unlink(missing_ok=True)
            removed.append(candidate)
    try:
        workspace.rmdir()
    except OSError:
        pass
    return removed


def _scopes(value: object, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PreparedGraphError(f"{label} must be a list")
    try:
        scopes = [require_repository_scope(item, f"{label}[{index}]") for index, item in enumerate(value)]
    except ProtocolHashError as error:
        raise PreparedGraphError(str(error)) from error
    if scopes != sorted(scopes, key=lambda item: (item["kind"], item["path"])) or len({(item["kind"], item["path"]) for item in scopes}) != len(scopes):
        raise PreparedGraphError(f"{label} must be sorted and duplicate-free")
    return scopes


def _route_bindings(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ROUTE_BINDINGS:
        raise PreparedGraphError(f"{label} must be a bounded list")
    normalized: list[dict[str, Any]] = []
    for index, binding in enumerate(value):
        required = {"constraints", "decision_sha256", "plan_sha256", "rank", "selected"}
        if not isinstance(binding, Mapping) or set(binding) != required:
            raise PreparedGraphError(f"{label}[{index}] is malformed")
        try:
            constraints = validate_route_constraints(binding["constraints"], f"{label}[{index}].constraints")
            selected = validate_route_pair(binding["selected"], f"{label}[{index}].selected")
        except RoutingCatalogError as error:
            raise PreparedGraphError(str(error)) from error
        rank = binding["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise PreparedGraphError(f"{label}[{index}].rank is invalid")
        if SHA256.fullmatch(str(binding["plan_sha256"])) is None or SHA256.fullmatch(str(binding["decision_sha256"])) is None:
            raise PreparedGraphError(f"{label}[{index}] identity is invalid")
        if constraints["fixed_model"] is not None and constraints["fixed_model"] != selected["model"]:
            raise PreparedGraphError(f"{label}[{index}] violates fixed model")
        if constraints["fixed_effort"] is not None and constraints["fixed_effort"] != selected["effort"]:
            raise PreparedGraphError(f"{label}[{index}] violates fixed effort")
        normalized.append(
            {
                "constraints": constraints,
                "decision_sha256": binding["decision_sha256"],
                "plan_sha256": binding["plan_sha256"],
                "rank": rank,
                "selected": selected,
            }
        )
    if normalized:
        ranks = [item["rank"] for item in normalized]
        if ranks != list(range(1, len(normalized) + 1)) or len({item["plan_sha256"] for item in normalized}) != len(normalized):
            raise PreparedGraphError(f"{label} is not a canonical fallback chain")
    return normalized


def _node_id(value: object, label: str) -> str:
    if not isinstance(value, str) or NODE_ID.fullmatch(value) is None:
        raise PreparedGraphError(f"{label} is invalid")
    return value


def _manifest_header(value: Mapping[str, Any]) -> tuple[str, str | None]:
    baseline = value.get("baseline")
    if SHA256.fullmatch(str(baseline)) is None:
        raise PreparedGraphError("prepared graph baseline identity is invalid")
    route_plan_sha256 = value.get("route_plan_sha256")
    if route_plan_sha256 is not None and SHA256.fullmatch(str(route_plan_sha256)) is None:
        raise PreparedGraphError("prepared graph route plan identity is invalid")
    if value.get("workspace_mode") not in {"light", "strict"}:
        raise PreparedGraphError("prepared graph workspace mode is invalid")
    return str(baseline), route_plan_sha256


def _manifest_node(
    value: object,
    *,
    index: int,
    route_plan_sha256: str | None,
    selection: bool,
    require_child_route: bool,
) -> dict[str, Any]:
    required = {"contract", "decision", "node", "route_bindings", "scopes"}
    optional = {"member_nodes"} if selection else set()
    if selection:
        required.add("selection")
    if not isinstance(value, Mapping) or set(value) - optional != required:
        raise PreparedGraphError(f"prepared graph node {index} is malformed")
    name = _node_id(value["node"], f"prepared graph node {index}.node")
    contract = value["contract"]
    decision = value["decision"]
    if (
        not isinstance(contract, Mapping)
        or contract.get("node") != name
        or not isinstance(decision, Mapping)
    ):
        raise PreparedGraphError(f"prepared graph node {index} is inconsistent")
    bindings = _route_bindings(
        value["route_bindings"], f"prepared graph node {index}.route_bindings"
    )
    placement = decision.get("placement")
    if (
        require_child_route
        and isinstance(placement, Mapping)
        and placement.get("target") == "child"
        and not bindings
    ):
        raise PreparedGraphError(f"prepared graph child node {index} lacks a route")
    if bindings and bindings[0]["plan_sha256"] != route_plan_sha256:
        raise PreparedGraphError("prepared graph route base identity is inconsistent")
    normalized = {
        "contract": dict(contract),
        "decision": dict(decision),
        "node": name,
        "route_bindings": bindings,
        "scopes": _scopes(value["scopes"], f"prepared graph node {index}.scopes"),
    }
    if not selection:
        return normalized

    selection_value = value["selection"]
    selection_fields = {
        "depends_on",
        "dependencies_ready",
        "downstream_count",
        "responsibility",
    }
    if (
        not isinstance(selection_value, Mapping)
        or set(selection_value) != selection_fields
        or not isinstance(selection_value["responsibility"], str)
        or not selection_value["responsibility"]
        or type(selection_value["dependencies_ready"]) is not bool
        or type(selection_value["downstream_count"]) is not int
        or selection_value["downstream_count"] < 0
        or not isinstance(selection_value["depends_on"], list)
    ):
        raise PreparedGraphError(f"prepared graph node {index}.selection is malformed")
    dependencies = [
        _node_id(item, f"prepared graph node {index}.selection.depends_on")
        for item in selection_value["depends_on"]
    ]
    if dependencies != sorted(dependencies) or len(dependencies) != len(set(dependencies)):
        raise PreparedGraphError(
            f"prepared graph node {index}.selection.depends_on is not canonical"
        )
    normalized["selection"] = {
        "depends_on": dependencies,
        "dependencies_ready": selection_value["dependencies_ready"],
        "downstream_count": selection_value["downstream_count"],
        "responsibility": selection_value["responsibility"],
    }
    if "member_nodes" in value:
        members = [
            _node_id(item, f"prepared graph node {index}.member_nodes")
            for item in value["member_nodes"]
        ] if isinstance(value["member_nodes"], list) else None
        if (
            members is None
            or len(members) < 2
            or members != sorted(members)
            or len(members) != len(set(members))
            or name in members
        ):
            raise PreparedGraphError(f"prepared graph node {index}.member_nodes is malformed")
        normalized["member_nodes"] = members
    return normalized


def _manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreparedGraphError("prepared graph manifest is malformed")
    protocol = value.get("protocol")
    legacy_fields = {
        "baseline",
        "nodes",
        "protocol",
        "route_plan_sha256",
        "workspace_mode",
    }
    v4_fields = legacy_fields | {"completed_nodes", "member_mapping"}
    if protocol == LEGACY_GRAPH_PROTOCOL and set(value) == legacy_fields:
        baseline, route_plan_sha256 = _manifest_header(value)
        selection = False
        require_child_route = True
    elif protocol == GRAPH_PROTOCOL and set(value) == v4_fields:
        baseline, route_plan_sha256 = _manifest_header(value)
        selection = True
        require_child_route = False
    else:
        raise PreparedGraphError("prepared graph manifest is malformed")

    nodes = value["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise PreparedGraphError("prepared graph nodes are invalid")
    normalized_nodes = [
        _manifest_node(
            node,
            index=index,
            route_plan_sha256=route_plan_sha256,
            selection=selection,
            require_child_route=require_child_route,
        )
        for index, node in enumerate(nodes)
    ]
    names = [item["node"] for item in normalized_nodes]
    if names != sorted(names) or len(names) != len(set(names)):
        raise PreparedGraphError("prepared graph nodes must be sorted and unique")

    manifest: dict[str, Any] = {
        "baseline": baseline,
        "nodes": normalized_nodes,
        "protocol": protocol,
        "route_plan_sha256": route_plan_sha256,
        "workspace_mode": value["workspace_mode"],
    }
    if not selection:
        return manifest

    completed = value["completed_nodes"]
    if (
        not isinstance(completed, list)
        or any(NODE_ID.fullmatch(item) is None for item in completed if isinstance(item, str))
        or any(not isinstance(item, str) for item in completed)
        or completed != sorted(completed)
        or len(completed) != len(set(completed))
    ):
        raise PreparedGraphError("prepared graph completed nodes are malformed")
    member_mapping_value = value["member_mapping"]
    if (
        not isinstance(member_mapping_value, Mapping)
        or any(
            NODE_ID.fullmatch(member) is None
            or not isinstance(aggregate, str)
            or NODE_ID.fullmatch(aggregate) is None
            for member, aggregate in member_mapping_value.items()
            if isinstance(member, str)
        )
        or any(not isinstance(member, str) for member in member_mapping_value)
        or list(member_mapping_value) != sorted(member_mapping_value)
    ):
        raise PreparedGraphError("prepared graph member mapping is malformed")
    member_mapping = dict(member_mapping_value)
    known_names = set(names)
    expected_mapping: dict[str, str] = {}
    for node in normalized_nodes:
        if "member_nodes" not in node:
            continue
        for member in node["member_nodes"]:
            if member not in known_names or member in expected_mapping:
                raise PreparedGraphError("prepared graph aggregate members are inconsistent")
            expected_mapping[member] = node["node"]
    if member_mapping != expected_mapping:
        raise PreparedGraphError("prepared graph member mapping is inconsistent")
    manifest["completed_nodes"] = list(completed)
    manifest["member_mapping"] = member_mapping
    return manifest


def normalize_artifact(value: object) -> dict[str, Any]:
    required = {"graph_sha256", "manifest", "protocol", "snapshot"}
    if not isinstance(value, Mapping) or set(value) != required or value["protocol"] != ARTIFACT_PROTOCOL:
        raise PreparedGraphError("prepared workspace artifact is malformed")
    try:
        manifest = _manifest(value["manifest"])
        snapshot = validate_snapshot(value["snapshot"])
    except StateError as error:
        raise PreparedGraphError(f"prepared workspace snapshot is invalid: {error}") from error
    identity = value["graph_sha256"]
    if identity != graph_sha256(manifest) or manifest["baseline"] != snapshot["state_id"] or manifest["workspace_mode"] != snapshot["ignored_mode"]:
        raise PreparedGraphError("prepared workspace identity is inconsistent")
    return {"graph_sha256": identity, "manifest": manifest, "protocol": ARTIFACT_PROTOCOL, "snapshot": snapshot}


def write_artifact(repo: Path, output: Path, *, manifest: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    artifact = normalize_artifact(
        {
            "graph_sha256": graph_sha256(manifest),
            "manifest": dict(manifest),
            "protocol": ARTIFACT_PROTOCOL,
            "snapshot": dict(snapshot),
        }
    )
    write_snapshot(repository_root(Path(repo)), output, json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return artifact


def load_artifact(path: Path) -> dict[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise PreparedGraphError("prepared workspace artifact is unavailable")
    try:
        if candidate.stat().st_size > MAX_ARTIFACT_BYTES:
            raise PreparedGraphError("prepared workspace artifact exceeds the size limit")
        return normalize_artifact(json.loads(candidate.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        if isinstance(error, PreparedGraphError):
            raise
        raise PreparedGraphError("prepared workspace artifact is unreadable") from error


def graph_scopes(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for node in manifest["nodes"]:
        for scope in node["scopes"]:
            unique[(scope["kind"], scope["path"])] = dict(scope)
    return [unique[key] for key in sorted(unique)]


def dispatch_workspace_claim(*, ledger_root: Path, session_id: str, capsule: Mapping[str, Any], repo: Path) -> dict[str, Any]:
    identity = capsule.get("graph_sha256")
    if not isinstance(identity, str):
        raise PreparedGraphError("dispatch lacks a prepared graph identity")
    path = artifact_path(ledger_root, session_id, identity)
    artifact = load_artifact(path)
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    matches = [node for node in manifest["nodes"] if node["node"] == capsule.get("node")]
    if len(matches) != 1:
        raise PreparedGraphError("dispatch node is absent from prepared graph")
    node = matches[0]
    route = capsule.get("route")
    route_match = [binding for binding in node["route_bindings"] if binding == route] if isinstance(route, Mapping) else []
    decision = node["decision"]
    if (
        artifact["graph_sha256"] != identity
        or snapshot["state_id"] != capsule.get("baseline")
        or len(route_match) != 1
        or node["contract"] != capsule.get("contract")
        or node["scopes"] != capsule.get("scopes")
        or decision.get("role") != capsule.get("role")
        or decision.get("assurance") != capsule.get("assurance")
        or decision.get("acceptance") != capsule.get("acceptance")
        or decision.get("acceptance_ids") != capsule.get("acceptance_ids")
    ):
        raise PreparedGraphError("dispatch does not match its prepared graph")
    root = repository_root(Path(repo))
    if Path(str(snapshot["repo_root"])).resolve() != root.resolve():
        raise PreparedGraphError("prepared workspace repository does not match dispatch")
    return {
        "baseline": snapshot["state_id"],
        "baseline_path": str(path),
        "graph_scopes": graph_scopes(manifest),
        "graph_sha256": identity,
        "route_constraints": dict(route_match[0]["constraints"]),
        "scopes": [dict(scope) for scope in node["scopes"]],
        "workspace_mode": snapshot["ignored_mode"],
    }


def verify_artifact_workspace(path: Path, *, repo: Path, baseline: str, graph_sha256_value: str, graph_scopes_value: object, workspace_mode: str) -> dict[str, Any]:
    artifact = load_artifact(path)
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    scopes = graph_scopes(manifest)
    if artifact["graph_sha256"] != graph_sha256_value or snapshot["state_id"] != baseline or snapshot["ignored_mode"] != workspace_mode or scopes != graph_scopes_value:
        raise PreparedGraphError("ledger workspace identity does not match artifact")
    root = repository_root(Path(repo))
    control_roots = repository_control_roots(root)
    index_records = repository_index_records(root)
    gitlinks = repository_gitlinks(root, index_records)
    spellings = repository_path_spelling_map(index_records)
    directory_spellings: dict[str, frozenset[str]] = {}
    allowed_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for scope in scopes:
        normalized = normalize_allow(
            root,
            f"{scope['kind']}:{scope['path']}",
            protected_roots=control_roots,
            gitlinks=gitlinks,
            tracked_spellings=spellings,
            directory_spellings=directory_spellings,
        )
        allowed_by_key[(normalized["path"], normalized["kind"])] = normalized
    allowed = [allowed_by_key[key] for key in sorted(allowed_by_key)]
    _code, result, _current = verify(
        root,
        snapshot,
        allowed,
        control_roots=control_roots,
        index_records=index_records,
        scope_entries=True,
    )
    return result


def verify_pre_spawn_workspace(
    artifact: Path,
    *,
    repo: Path,
    baseline: str,
    graph_sha256_value: str,
    graph_scopes_value: object,
    workspace_mode: str,
    active_sibling_scopes: object,
    pending_candidate_scopes: object,
) -> dict[str, Any]:
    """Check that a queued spawn can safely inherit a prepared workspace.

    The prepared graph remains the read boundary, while only scopes leased to
    active siblings may carry a baseline-relative delta.  The candidate's
    pending scopes consequently remain fail-closed until its spawn is active.
    """

    prepared = load_artifact(artifact)
    manifest = prepared["manifest"]
    snapshot = prepared["snapshot"]
    scopes = graph_scopes(manifest)
    if (
        prepared["graph_sha256"] != graph_sha256_value
        or snapshot["state_id"] != baseline
        or snapshot["ignored_mode"] != workspace_mode
        or scopes != graph_scopes_value
    ):
        raise PreparedGraphError("ledger workspace identity does not match artifact")

    active = _scopes(active_sibling_scopes, "active sibling scopes")
    pending = _scopes(pending_candidate_scopes, "pending candidate scopes")
    graph_keys = {(scope["kind"], scope["path"]) for scope in scopes}
    if any((scope["kind"], scope["path"]) not in graph_keys for scope in active):
        raise PreparedGraphError("active sibling scopes are outside the prepared graph")
    if any((scope["kind"], scope["path"]) not in graph_keys for scope in pending):
        raise PreparedGraphError("pending candidate scopes are outside the prepared graph")
    for active_scope in active:
        for pending_scope in pending:
            if repository_scopes_overlap(active_scope, pending_scope):
                raise PreparedGraphError(
                    "active sibling scopes overlap pending candidate scopes"
                )

    try:
        root = repository_root(Path(repo))
        control_roots = repository_control_roots(root)
        index_records = repository_index_records(root)
        gitlinks = repository_gitlinks(root, index_records)
        spellings = repository_path_spelling_map(index_records)
        directory_spellings: dict[str, frozenset[str]] = {}

        def normalize(scopes_to_normalize: list[dict[str, str]]) -> list[dict[str, str]]:
            return [
                normalize_allow(
                    root,
                    f"{scope['kind']}:{scope['path']}",
                    protected_roots=control_roots,
                    gitlinks=gitlinks,
                    tracked_spellings=spellings,
                    directory_spellings=directory_spellings,
                )
                for scope in scopes_to_normalize
            ]

        graph_allowed = normalize(scopes)
        allowed_active = normalize(active)
        pending_scopes = normalize(pending)
        _code, result, _current = verify(
            root,
            snapshot,
            allowed_active,
            control_roots=control_roots,
            index_records=index_records,
            scope_entries=True,
            entry_scopes=graph_allowed,
        )
    except StateError as error:
        raise PreparedGraphError(f"pre-spawn workspace verification is invalid: {error}") from error

    return {
        "schema": "cco.pre-spawn-workspace-verification.v1",
        "baseline_state": result["baseline_state"],
        "current_state": result["current_state"],
        "allowed_active_scopes": allowed_active,
        "pending_scopes": pending_scopes,
        "changed_paths": result["changed_paths"],
        "violations": result["violations"],
        "verdict": result["verdict"],
    }

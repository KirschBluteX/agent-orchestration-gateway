#!/usr/bin/env python3
"""Persist and verify one compact workspace artifact for a prepared CCO graph."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from protocol_hash import ProtocolHashError, canonical_bytes, require_repository_scope
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


ARTIFACT_PROTOCOL = "cco.prepared-workspace.v1"
GRAPH_PROTOCOL = "cco.graph.v1"
GRAPH_DOMAIN = b"cco.graph.v1\0"
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
ARTIFACT_FILENAME = re.compile(r"^.+-[0-9a-f]{64}\.json$")


class PreparedGraphError(ValueError):
    """A prepared graph artifact is missing, malformed, or state-inconsistent."""


def graph_sha256(manifest: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        GRAPH_DOMAIN + canonical_bytes(dict(manifest))
    ).hexdigest()


def artifact_path(ledger_root: Path, session_id: str, identity: str) -> Path:
    if SESSION_ID.fullmatch(session_id) is None:
        raise PreparedGraphError("prepared workspace session identity is invalid")
    if SHA256.fullmatch(identity) is None:
        raise PreparedGraphError("prepared workspace graph identity is invalid")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    return root.parent / "workspace" / f"{session_id}-{identity[7:]}.json"


def cleanup_session_artifacts(ledger_root: Path, session_id: str) -> list[Path]:
    """Remove only prepared workspace artifacts owned by one completed session."""

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


def cleanup_stale_artifacts(
    ledger_root: Path,
    *,
    keep_session_id: str,
    max_age_seconds: float,
) -> list[Path]:
    """Remove abandoned graph artifacts at a cold lifecycle boundary."""

    if SESSION_ID.fullmatch(keep_session_id) is None or max_age_seconds < 60:
        raise PreparedGraphError("prepared workspace cleanup bounds are invalid")
    root = Path(os.path.abspath(Path(ledger_root).expanduser())).resolve()
    workspace = root.parent / "workspace"
    if not workspace.is_dir():
        return []
    removed: list[Path] = []
    now = time.time()
    for candidate in workspace.iterdir():
        if candidate.name.startswith(f"{keep_session_id}-"):
            continue
        if not (
            ARTIFACT_FILENAME.fullmatch(candidate.name)
            or candidate.name.startswith(".cco-state-")
        ):
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
        normalized = [
            require_repository_scope(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    except ProtocolHashError as error:
        raise PreparedGraphError(str(error)) from error
    if normalized != sorted(
        normalized, key=lambda item: (item["kind"], item["path"])
    ) or len({(item["kind"], item["path"]) for item in normalized}) != len(
        normalized
    ):
        raise PreparedGraphError(f"{label} must be sorted and duplicate-free")
    return normalized


def _route_bindings(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise PreparedGraphError(f"{label} must be a non-empty bounded list")
    normalized: list[dict[str, Any]] = []
    for index, binding in enumerate(value):
        if not isinstance(binding, dict) or set(binding) != {
            "plan_sha256",
            "rank",
            "selected",
        }:
            raise PreparedGraphError(f"{label}[{index}] is malformed")
        selected = binding.get("selected")
        if (
            SHA256.fullmatch(str(binding.get("plan_sha256"))) is None
            or type(binding.get("rank")) is not int
            or not isinstance(selected, dict)
            or set(selected) != {"effort", "model"}
            or MODEL.fullmatch(str(selected.get("model"))) is None
            or EFFORT.fullmatch(str(selected.get("effort"))) is None
        ):
            raise PreparedGraphError(f"{label}[{index}] is invalid")
        normalized.append(
            {
                "plan_sha256": binding["plan_sha256"],
                "rank": binding["rank"],
                "selected": {
                    "effort": selected["effort"],
                    "model": selected["model"],
                },
            }
        )
    ranks = [binding["rank"] for binding in normalized]
    if ranks != list(range(ranks[0], ranks[0] + len(ranks))) or len(
        {binding["plan_sha256"] for binding in normalized}
    ) != len(normalized):
        raise PreparedGraphError(f"{label} is not a canonical fallback chain")
    return normalized


def _manifest(value: object) -> dict[str, Any]:
    fields = {
        "baseline",
        "nodes",
        "protocol",
        "route_plan_sha256",
        "workspace_mode",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise PreparedGraphError("prepared graph manifest is malformed")
    if value.get("protocol") != GRAPH_PROTOCOL:
        raise PreparedGraphError("prepared graph manifest protocol is invalid")
    if SHA256.fullmatch(str(value.get("baseline"))) is None or SHA256.fullmatch(
        str(value.get("route_plan_sha256"))
    ) is None:
        raise PreparedGraphError("prepared graph manifest identity is invalid")
    if value.get("workspace_mode") not in {"light", "strict"}:
        raise PreparedGraphError("prepared graph workspace mode is invalid")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PreparedGraphError("prepared graph manifest nodes are invalid")
    normalized_nodes: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or set(node) != {
            "contract",
            "decision",
            "node",
            "route_bindings",
            "scopes",
        }:
            raise PreparedGraphError(f"prepared graph node {index} is malformed")
        name = node.get("node")
        contract = node.get("contract")
        decision = node.get("decision")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(contract, dict)
            or contract.get("node") != name
            or not isinstance(decision, dict)
        ):
            raise PreparedGraphError(f"prepared graph node {index} is inconsistent")
        normalized_nodes.append(
            {
                "contract": dict(contract),
                "decision": dict(decision),
                "node": name,
                "route_bindings": _route_bindings(
                    node.get("route_bindings"),
                    f"prepared graph node {index}.route_bindings",
                ),
                "scopes": _scopes(node.get("scopes"), f"prepared graph node {index}.scopes"),
            }
        )
    if [node["node"] for node in normalized_nodes] != sorted(
        node["node"] for node in normalized_nodes
    ) or len({node["node"] for node in normalized_nodes}) != len(normalized_nodes):
        raise PreparedGraphError("prepared graph nodes must be sorted and unique")
    if any(
        node["route_bindings"][0]["plan_sha256"]
        != value["route_plan_sha256"]
        for node in normalized_nodes
    ):
        raise PreparedGraphError("prepared graph route base identity is inconsistent")
    return {
        "baseline": value["baseline"],
        "nodes": normalized_nodes,
        "protocol": GRAPH_PROTOCOL,
        "route_plan_sha256": value["route_plan_sha256"],
        "workspace_mode": value["workspace_mode"],
    }


def normalize_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "graph_sha256",
        "manifest",
        "protocol",
        "snapshot",
    }:
        raise PreparedGraphError("prepared workspace artifact is malformed")
    if value.get("protocol") != ARTIFACT_PROTOCOL:
        raise PreparedGraphError("prepared workspace artifact protocol is invalid")
    try:
        manifest = _manifest(value.get("manifest"))
        snapshot = validate_snapshot(value.get("snapshot"))
    except StateError as error:
        raise PreparedGraphError(f"prepared workspace snapshot is invalid: {error}") from error
    identity = value.get("graph_sha256")
    if identity != graph_sha256(manifest):
        raise PreparedGraphError("prepared workspace graph identity does not match")
    if (
        manifest["baseline"] != snapshot["state_id"]
        or manifest["workspace_mode"] != snapshot["ignored_mode"]
    ):
        raise PreparedGraphError("prepared workspace baseline identity is inconsistent")
    return {
        "graph_sha256": identity,
        "manifest": manifest,
        "protocol": ARTIFACT_PROTOCOL,
        "snapshot": snapshot,
    }


def write_artifact(
    repo: Path,
    output: Path,
    *,
    manifest: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = normalize_artifact(
        {
            "graph_sha256": graph_sha256(manifest),
            "manifest": dict(manifest),
            "protocol": ARTIFACT_PROTOCOL,
            "snapshot": dict(snapshot),
        }
    )
    write_snapshot(
        repository_root(Path(repo)),
        output,
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )
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


def dispatch_workspace_claim(
    *,
    ledger_root: Path,
    session_id: str,
    capsule: Mapping[str, Any],
    repo: Path,
) -> dict[str, Any]:
    identity = capsule.get("graph_sha256")
    if not isinstance(identity, str):
        raise PreparedGraphError("dispatch lacks a prepared graph identity")
    path = artifact_path(ledger_root, session_id, identity)
    artifact = load_artifact(path)
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    matching = [node for node in manifest["nodes"] if node["node"] == capsule.get("node")]
    if len(matching) != 1:
        raise PreparedGraphError("dispatch node is absent from prepared graph")
    node = matching[0]
    route = capsule.get("route")
    if (
        artifact["graph_sha256"] != identity
        or snapshot["state_id"] != capsule.get("baseline")
        or not isinstance(route, Mapping)
        or dict(route) not in node["route_bindings"]
        or node["contract"] != capsule.get("contract")
        or node["decision"] != capsule.get("decision")
        or node["scopes"] != capsule.get("scopes")
    ):
        raise PreparedGraphError("dispatch does not match its prepared graph")
    root = repository_root(Path(repo))
    try:
        snapshot_root = Path(str(snapshot["repo_root"])).resolve()
    except OSError as error:
        raise PreparedGraphError("prepared workspace repository is invalid") from error
    if snapshot_root != root.resolve():
        raise PreparedGraphError("prepared workspace repository does not match dispatch")
    return {
        "baseline": snapshot["state_id"],
        "baseline_path": str(path),
        "graph_scopes": graph_scopes(manifest),
        "graph_sha256": identity,
        "scopes": [dict(scope) for scope in node["scopes"]],
        "workspace_mode": snapshot["ignored_mode"],
    }


def verify_artifact_workspace(
    path: Path,
    *,
    repo: Path,
    baseline: str,
    graph_sha256_value: str,
    graph_scopes_value: object,
    workspace_mode: str,
) -> dict[str, Any]:
    artifact = load_artifact(path)
    manifest = artifact["manifest"]
    snapshot = artifact["snapshot"]
    scopes = graph_scopes(manifest)
    if (
        artifact["graph_sha256"] != graph_sha256_value
        or snapshot["state_id"] != baseline
        or snapshot["ignored_mode"] != workspace_mode
        or scopes != graph_scopes_value
    ):
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
    )
    return result

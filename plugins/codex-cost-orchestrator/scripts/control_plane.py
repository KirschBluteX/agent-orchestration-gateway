#!/usr/bin/env python3
"""Compact cco.v9 plan, wave, routing, and lifecycle control plane."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping
import unicodedata

from protocol_hash import (
    ProtocolHashError,
    canonical_bytes,
    parse_canonical_json_object,
    parse_safe_integer,
    reject_constant,
    reject_float,
    repository_scopes_overlap,
    require_repository_path,
)
from delegation_compiler import (
    DELEGATE,
    DelegationCompilerError,
    compile_delegation_request,
    derive_assurance,
    normalize_closed_plan,
)
from host_paths import HostPathError, host_path
from operation_deadline import (
    OperationDeadlineExceeded,
    checkpoint,
    deadline_after,
    remaining_seconds,
)
from routing_catalog import (
    RoutingCatalogError,
    load_native_catalog,
    load_route_policy,
    resolve_route_plan,
)
from state_lock import StateLockBusy, acquire
from workspace_guard import (
    WorkspaceGuardError,
    WorkspaceGuardUnavailable,
    capture as capture_workspace,
    discover_workspace,
    normalize_scope_groups,
    validate_baseline as validate_workspace_baseline,
    verify_state as verify_workspace,
)
from writer_isolation import (
    COOPERATIVE,
    MAX_GROUP_SIZE as MAX_COOPERATIVE_WRITERS,
    WriterIsolationError,
    WriterIsolationUnavailable,
    apply_journal as apply_isolation_journal,
    cleanup_isolates,
    cleanup_journal as cleanup_isolation_journal,
    cleanup_preparing_isolates,
    cleanup_unused_isolate_batches,
    cleanup_unused_journal_batches,
    preparing_isolate_roots,
    prepare_isolates,
    rollback_journal as rollback_isolation_journal,
    scoped_content_identity,
    stage_apply_journal,
    validate_journal as validate_isolation_journal,
    validate_record as validate_isolation_record,
    verify_canonical as verify_isolation_canonical,
    verify_isolate,
)


PROTOCOL = "cco.v9"
PLAN_PROTOCOL = "cco.plan.v1"
WAVE_PROTOCOL = "cco.wave.v3"
BATCH_PROTOCOL = "cco.wave-batch.v2"
LIFECYCLE_PROTOCOL = "cco.lifecycle.v2"
# Receipts are the sole durable record for one-shot lifecycle observations.
PENDING_EVENT_PROTOCOL = "cco.receipt.v2"
TASK_HEADER = "CCO_TASK cco.v9"
CONTINUE_HEADER = "CCO_CONTINUE cco.v9"
RESULT_HEADER = "CCO_RESULT cco.v9"
READ_ROLE = "cost_orchestrator_read_leaf"
WRITE_ROLE = "cost_orchestrator_write_leaf"
ROLES = frozenset({"explorer", "worker", "reviewer"})
LOGICAL_STATES = frozenset(
    {
        "waiting",
        "ready",
        "starting",
        "running",
        "ready_to_apply",
        "paused",
        "retired",
        "fenced",
    }
)
DISPATCH_STATES = frozenset(
    {
        "starting",
        "running",
        "ready_to_apply",
        "paused",
        "retired",
        "fenced",
        "rejected",
    }
)
ACTIVE_STATES = frozenset({"running", "ready_to_apply", "paused"})
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
TASK_PATH_RE = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HOST_OPAQUE_MESSAGE_RE = re.compile(r"gAAAA[A-Za-z0-9_-]{80,}={0,2}")
FAILURE_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
MAX_INPUT_BYTES = 1024 * 1024
MAX_TOOL_USE_ID_BYTES = 4_096
# A plan admits at most 128 nodes plus one compiler-injected reviewer.  Native
# retries and bounded continuations can consume several attempt identities per
# dispatch, so retain a fixed plan-lifetime replay margin beyond that graph.
MAX_TOMBSTONES = 1_024
MAX_TRANSIENT_RETRIES = 3
NATIVE_CLAIM_TTL_MILLISECONDS = 120_000
PREFLIGHT_VERIFICATION_SECONDS = 14.0
PREFLIGHT_ROLLBACK_RESERVE_SECONDS = 4.0
NATIVE_FAILURE_KINDS = frozenset(
    {
        "network",
        "other",
        "owner_unavailable",
        "rate_limit",
        "route_rejected",
        "service",
        "timeout",
    }
)
STATE_FILE_RE = re.compile(r"^(?P<workspace>[0-9a-f]{64})--(?P<session>[0-9a-f]{64})\.json$")
RECOVERY_FILE_RE = re.compile(r"^\.cco-recovery-[A-Za-z0-9_-]+\.json$")
RECOVERY_STAGING_FILE_RE = re.compile(
    r"^\.cco-staging-[A-Za-z0-9_-]+\.pending$"
)
PENDING_EVENT_FILE_RE = re.compile(
    r"^\.cco-pending-s(?P<session>[0-9a-f]{64})-(?P<event>[0-9a-f]{64})\.event$"
)
STATE_ROOT_SENTINEL = ".cco-state-root-v1"
STATE_ROOT_SENTINEL_BYTES = b"cco.state-root.v1\n"
# Capacity reservation and state publication share this root lock.
# Keep its persisted identity stable for the current protocol.
STATE_ROOT_LOCK = "state-root-capacity"
# Serializes only isolate/journal filesystem publication and reclamation.  It is
# not a second lifecycle ledger: liveness remains derived from lifecycle state.
ISOLATION_NAMESPACE_LOCK = "isolation-namespace"
ISOLATION_NAMESPACE_WAIT_SECONDS = 1.0
MAX_STATE_FILE_BYTES = 32 * 1024 * 1024
MAX_STATE_FILES = 4_096
MAX_PENDING_EVENT_FILES = 128
MAX_PENDING_EVENT_BYTES = MAX_INPUT_BYTES + 128 * 1024
MAX_RESULT_OBSERVATION_BYTES = MAX_INPUT_BYTES
MAX_COOPERATIVE_JOURNAL_LIFECYCLE_BYTES = 16 * 1024 * 1024
# Orphan reclamation is optional, so never let its liveness scan consume the
# lifecycle file limit or a host Hook deadline.  A skipped scan leaks only
# already-unowned temporary files and can be retried after old states are cleaned.
MAX_ISOLATION_LIVENESS_SCAN_BYTES = 32 * 1024 * 1024
RECEIPT_PHASES = frozenset(
    {
        "reserved",
        "native_observed",
        "awaiting_result",
        "result_observed",
        "observed",
        "acknowledged",
    }
)
STATE_READ_CHUNK_BYTES = 1024 * 1024
BREAKING_UPGRADE_MESSAGE = (
    "unsupported predecessor CCO artifact; clean up the old CCO state and "
    "artifacts, then start a new task (breaking upgrade)"
)
OPAQUE_MESSAGE_POLICY_ENV = "CCO_OPAQUE_MESSAGE_POLICY"
OPAQUE_MESSAGE_POLICIES = frozenset({"strict", "trusted_host"})


class ControlPlaneError(RuntimeError):
    """A cco.v9 contract or lifecycle transition is invalid."""


class ControlPlaneUnavailable(ControlPlaneError):
    """CCO state infrastructure is temporarily unavailable; do not fence work."""


class _AtomicWriteUncertain(ControlPlaneUnavailable):
    """A replacement was published, but its directory sync was not confirmed.

    Callers must retain the in-memory revision and let receipt/state replay
    resolve the outcome.  Reverting local bookkeeping after ``os.replace``
    would be unsafe because the replacement may already be visible.
    """


def _text(value: object, label: str, *, limit: int = 8_192) -> str:
    if not isinstance(value, str):
        raise ControlPlaneError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized.encode("utf-8")) > limit:
        raise ControlPlaneError(f"{label} is empty or too large")
    return normalized


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_bytes(dict(value))).hexdigest()


def host_opaque_message(value: object) -> bool:
    """Recognize the whole-message ciphertext emitted by the current host."""

    return (
        isinstance(value, str)
        and HOST_OPAQUE_MESSAGE_RE.fullmatch(value.strip()) is not None
    )


def _opaque_message_policy() -> str:
    policy = os.environ.get(OPAQUE_MESSAGE_POLICY_ENV, "trusted_host").strip()
    if policy not in OPAQUE_MESSAGE_POLICIES:
        raise ControlPlaneError(
            f"{OPAQUE_MESSAGE_POLICY_ENV} must be strict or trusted_host"
        )
    return policy


def _state_root() -> Path:
    configured = os.environ.get("CCO_STATE_DIR")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "v9"
    )
    return Path(os.path.abspath(root))


def _workspace_key(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ControlPlaneError("lifecycle workspace root is invalid")
    try:
        ordinary = host_path(os.fspath(value))
    except (HostPathError, OSError, TypeError) as error:
        raise ControlPlaneError("lifecycle workspace root is invalid") from error
    return os.path.normcase(os.path.realpath(os.path.abspath(ordinary)))


def _workspace_lock_identity(value: object) -> str:
    return f"workspace-{_workspace_digest(value)}"


def _workspace_digest(value: object) -> str:
    return hashlib.sha256(_workspace_key(value).encode("utf-8")).hexdigest()


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lifecycle_state_path(root: Path, workspace: object, session_id: str) -> Path:
    return root / f"{_workspace_digest(workspace)}--{_session_digest(session_id)}.json"


def _pending_event_path(root: Path, session_id: str, event_id: str) -> Path:
    if SHA256_RE.fullmatch(event_id) is None:
        raise ControlPlaneError("pending event identity is invalid")
    return root / (
        f".cco-pending-s{_session_digest(session_id)}-{event_id[7:]}.event"
    )


def _preflight_verification_budget() -> float:
    remaining = remaining_seconds(reserve=PREFLIGHT_ROLLBACK_RESERVE_SECONDS)
    return (
        PREFLIGHT_VERIFICATION_SECONDS
        if remaining is None
        else min(PREFLIGHT_VERIFICATION_SECONDS, remaining)
    )


def _bounded_lock_timeout(limit: float) -> float:
    remaining = remaining_seconds()
    return limit if remaining is None else min(limit, remaining)


def _isolation_lock_timeout(limit: float) -> float:
    """Fail fast rather than hold the global state lock behind file cleanup."""

    return _bounded_lock_timeout(min(limit, ISOLATION_NAMESPACE_WAIT_SECONDS))


def _sync_directory(path: Path) -> None:
    """Persist a completed POSIX namespace transition before reporting success.

    Python cannot open a directory for ``fsync`` on Windows; the Windows path
    instead uses ``MoveFileExW(MOVEFILE_WRITE_THROUGH)`` in
    :func:`_replace_atomically`.  POSIX hosts need the parent sync in addition
    to the staged-file sync for crash-durable publication.
    """

    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle state directory cannot be synchronized"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                raise ControlPlaneUnavailable(
                    "lifecycle state directory cannot be closed"
                ) from error


def _replace_atomically(source: Path, target: Path) -> None:
    """Replace one same-directory staged file with host-appropriate durability."""

    if os.name != "nt":
        os.replace(source, target)
        return
    # ``os.replace`` maps to an atomic rename but does not request Windows'
    # write-through completion.  The staging file is deliberately in the same
    # directory, so ``MoveFileExW`` retains the replacement semantics while
    # making the namespace transition durable before it returns.
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    replace_existing = 0x00000001
    write_through = 0x00000008
    if not move_file(str(source), str(target), replace_existing | write_through):
        code = ctypes.get_last_error()
        raise OSError(code, "MoveFileExW lifecycle replacement failed", str(target))


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    # Workspace adapters legitimately contain platform inode/device integers above
    # JavaScript's safe range.  Artifact identities bind their state_id, while the
    # persisted snapshot itself uses deterministic ordinary JSON without re-hashing
    # those host-native integers through the wire-protocol canonicalizer.
    serialized = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    staged: Path | None = None
    replaced = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".cco-v9-",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            staged = Path(handle.name)
        _replace_atomically(staged, path)
        staged = None
        replaced = True
        try:
            _sync_directory(path.parent)
        except ControlPlaneUnavailable as error:
            # The file data is fsync'd and the replacement completed, but a
            # caller cannot safely assume either rollback or durable success
            # until a later replay observes the namespace.
            raise _AtomicWriteUncertain(
                "lifecycle replacement directory sync is uncertain"
            ) from error
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except _AtomicWriteUncertain:
        raise
    except OSError as error:
        if replaced:
            raise _AtomicWriteUncertain(
                "lifecycle replacement durability is uncertain"
            ) from error
        raise ControlPlaneUnavailable("lifecycle atomic write is unavailable") from error
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                # This is only an unlinked staging file: never hide the
                # authoritative publication outcome behind cleanup failure.
                pass


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        if _load_object(path, "immutable CCO artifact") != dict(value):
            raise ControlPlaneError("immutable CCO artifact identity collision")
        return
    _atomic_write(path, value)


def _read_bounded_bytes(
    path: Path,
    label: str,
    *,
    limit: int = MAX_STATE_FILE_BYTES,
) -> bytes:
    checkpoint()
    raw = bytearray()
    try:
        with path.open("rb") as handle:
            while True:
                checkpoint()
                chunk = handle.read(min(STATE_READ_CHUNK_BYTES, limit - len(raw) + 1))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > limit:
                    raise ControlPlaneError(f"{label} is too large")
    except OSError as error:
        raise ControlPlaneUnavailable(f"{label} is unavailable") from error
    return bytes(raw)


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    checkpoint()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ControlPlaneError,
        ProtocolHashError,
    ) as error:
        raise ControlPlaneError(f"{label} is not valid JSON") from error
    checkpoint()
    if not isinstance(value, dict):
        raise ControlPlaneError(f"{label} is malformed")
    return value


def _load_object(path: Path, label: str) -> dict[str, Any]:
    return _decode_object(_read_bounded_bytes(path, label), label)


def _state_json_paths(root: Path) -> list[Path]:
    """Return a deterministic, memory-bounded snapshot of lifecycle files."""

    paths: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                checkpoint()
                if not entry.name.endswith(".json") and (
                    RECOVERY_STAGING_FILE_RE.fullmatch(entry.name) is None
                ):
                    continue
                if STATE_FILE_RE.fullmatch(entry.name) is None:
                    raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
                if len(paths) >= MAX_STATE_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle state directory exceeds the "
                        f"{MAX_STATE_FILES} file limit"
                    )
                paths.append(Path(entry.path))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle state directory is unavailable"
        ) from error
    checkpoint()
    paths.sort(key=lambda item: item.name)
    return paths


def _state_capacity_used(paths: list[Path]) -> int:
    return len(paths)


def _session_state_paths(root: Path, session_id: str) -> tuple[list[Path], bool]:
    """Find current indexed state and reject a predecessor before decoding it."""

    suffix = f"--{_session_digest(session_id)}.json"
    predecessor_name = f"{session_id}.json"
    matches: list[Path] = []
    predecessor = False
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                checkpoint()
                if entry.name == predecessor_name:
                    predecessor = True
                elif entry.name.endswith(suffix):
                    if STATE_FILE_RE.fullmatch(entry.name) is None:
                        predecessor = True
                    else:
                        matches.append(Path(entry.path))
                elif (
                    RECOVERY_FILE_RE.fullmatch(entry.name) is not None
                    or RECOVERY_STAGING_FILE_RE.fullmatch(entry.name) is not None
                ):
                    predecessor = True
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle state directory is unavailable"
        ) from error
    matches.sort(key=lambda item: item.name)
    return matches, predecessor


def _pending_event_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                checkpoint()
                if PENDING_EVENT_FILE_RE.fullmatch(entry.name) is None:
                    continue
                if len(paths) >= MAX_PENDING_EVENT_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle state directory exceeds the "
                        f"{MAX_PENDING_EVENT_FILES} pending event limit"
                    )
                paths.append(Path(entry.path))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ControlPlaneUnavailable(
            "lifecycle pending event directory is unavailable"
        ) from error
    paths.sort(key=lambda item: item.name)
    return paths


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlPlaneError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _normalize_pin(value: object) -> dict[str, str | None]:
    if value is None:
        return {"fixed_effort": None, "fixed_model": None, "source": "automatic"}
    if not isinstance(value, Mapping) or set(value) - {"model", "effort"}:
        raise ControlPlaneError("route pin may contain only model and effort")
    model = value.get("model")
    effort = value.get("effort")
    if model is not None:
        model = _text(model, "route pin model", limit=128)
    if effort is not None:
        effort = _text(effort, "route pin effort", limit=32)
    if model is None and effort is None:
        raise ControlPlaneError("route pin must select a model or effort")
    return {"fixed_effort": effort, "fixed_model": model, "source": "user"}


def _normalize_plan(
    value: object,
    workspace_root: Path,
    workspace_backend: str,
) -> dict[str, Any]:
    try:
        compiled = normalize_closed_plan(value)
    except DelegationCompilerError as error:
        raise ControlPlaneError(str(error)) from error
    nodes = [dict(item) for item in compiled["nodes"]]
    scope_groups = [list(item["scopes"]) for item in nodes]
    normalized_scope_groups = normalize_scope_groups(
        workspace_root,
        scope_groups,
        backend=workspace_backend,
    )
    for node, scopes in zip(nodes, normalized_scope_groups, strict=True):
        node["scopes"] = scopes
        node["pin"] = _normalize_pin(node["pin"])
        node["assurance"] = derive_assurance(node)
    return {
        "acceptance": compiled["acceptance"],
        "accept_risk": compiled["accept_risk"],
        "goal": compiled["goal"],
        "nodes": nodes,
        "writer_isolation": compiled.get("writer_isolation", "serial"),
    }


def _node_map(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in plan["nodes"]}


def _descendant_counts(plan: Mapping[str, Any]) -> dict[str, int]:
    direct: dict[str, set[str]] = {item["id"]: set() for item in plan["nodes"]}
    for item in plan["nodes"]:
        for dependency in item["depends_on"]:
            direct[dependency].add(item["id"])
    result: dict[str, int] = {}
    for node in direct:
        seen: set[str] = set()
        stack = list(direct[node])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(direct[current])
        result[node] = len(seen)
    return result


def _scopes_overlap(left: list[dict[str, str]], right: list[dict[str, str]]) -> bool:
    return any(repository_scopes_overlap(a, b) for a in left for b in right)


def _units_conflict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["role"] == right["role"] == "worker":
        return True
    if left["role"] != "worker" and right["role"] != "worker":
        return False
    return _scopes_overlap(left["scopes"], right["scopes"])


def _select_units(units: list[dict[str, Any]], capacity: int) -> list[dict[str, Any]]:
    if capacity < 1:
        return []
    readers = [item for item in units if item["role"] != "worker"]
    writers = [item for item in units if item["role"] == "worker"]

    def ranked(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (-item["downstream_count"], item["id"]),
        )

    candidates = [ranked(readers)[:capacity]]
    for writer in writers:
        compatible = [
            reader for reader in readers if not _units_conflict(writer, reader)
        ]
        candidates.append([writer, *ranked(compatible)[: max(0, capacity - 1)]])

    def better(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
        left_score = sum(item["downstream_count"] for item in left)
        right_score = sum(item["downstream_count"] for item in right)
        if (len(left), left_score) != (len(right), right_score):
            return (len(left), left_score) > (len(right), right_score)
        return tuple(sorted(item["id"] for item in left)) < tuple(
            sorted(item["id"] for item in right)
        )

    best: list[dict[str, Any]] = []
    for candidate in candidates:
        if better(candidate, best):
            best = candidate
    return sorted(best, key=lambda item: item["id"])


def _logical_units(
    ready: list[dict[str, Any]],
    routes: Mapping[str, Mapping[str, Any]],
    *,
    downstream: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Make one dispatch candidate for each logical node."""

    return [
        {
            "assurance": node["assurance"],
            "context_turns": node["context_turns"],
            "downstream_count": downstream[node["id"]],
            "id": node["id"],
            "members": [node["id"]],
            "role": node["role"],
            "route": deepcopy(routes[node["id"]]),
            "scopes": deepcopy(node["scopes"]),
        }
        for node in sorted(ready, key=lambda item: item["id"])
    ]


def _model_label(model: str) -> str:
    lowered = model.casefold()
    for label in ("luna", "terra", "sol"):
        if label in lowered:
            return label
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")[-16:] or "model"


def _task_name(
    unit: Mapping[str, Any],
    route: Mapping[str, str],
    generation: int,
    dispatch_id: str,
) -> str:
    role = unit["role"]
    prefix = {"explorer": "explorer", "worker": "worker", "reviewer": "reviewer"}[role]
    base = re.sub(r"[^a-z0-9_]+", "_", str(unit["id"]).casefold()).strip("_")[:32]
    suffix = dispatch_id.removeprefix("sha256:")[:10]
    return (
        f"{prefix}_{base}_{suffix}_{_model_label(route['model'])}_"
        f"{route['effort']}_g{generation:02d}"
    )


def _render_task(
    plan: Mapping[str, Any],
    unit: Mapping[str, Any],
    dispatch_id: str,
    *,
    cursor: int,
    dependency_evidence: Mapping[str, Any],
) -> str:
    nodes = _node_map(plan)
    members = []
    acceptance: dict[str, str] = {}
    for member_id in unit["members"]:
        node = nodes[member_id]
        members.append(
            {
                "acceptance": node["acceptance"],
                "depends_on": node["depends_on"],
                "id": member_id,
                "objective": node["objective"],
                "review_of": node["review_of"],
                "scopes": node["scopes"],
            }
        )
        for acceptance_id in node["acceptance"]:
            acceptance[acceptance_id] = plan["acceptance"][acceptance_id]
    isolation = unit.get("isolation")
    task_workspace = plan["workspace_root"]
    if isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE:
        record = isolation.get("record")
        if not isinstance(record, Mapping) or not isinstance(record.get("isolate_root"), str):
            raise ControlPlaneError("cooperative task has no isolate root")
        task_workspace = record["isolate_root"]
    body = {
        "acceptance": {key: acceptance[key] for key in sorted(acceptance)},
        "assurance": unit["assurance"],
        "cursor": cursor,
        "dependency_evidence": dict(dependency_evidence),
        "dispatch_id": dispatch_id,
        "members": members,
        "protocol": PROTOCOL,
        "result_fields": [
            "blockers",
            "changed_paths",
            "cursor",
            "deviations",
            "dispatch_id",
            "evidence",
            "failure_signature",
            "outcome",
            "status",
            "summary",
        ],
        "result_mode": "cumulative_from_wave_baseline",
        "role": unit["role"],
        "scopes": unit["scopes"],
        "workspace_root": task_workspace,
    }
    if isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE:
        body["writer_isolation"] = {
            "canonical_workspace_root": plan["workspace_root"],
            "mode": COOPERATIVE,
            "notice": (
                "This is cooperative writer isolation, not a sandbox. Work only in "
                "workspace_root, which is the CCO-owned isolate. Do not write the "
                "canonical workspace."
            ),
        }
    return TASK_HEADER + "\n" + canonical_bytes(body).decode("utf-8")


def parse_task_message(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(TASK_HEADER + "\n"):
        raise ControlPlaneError("spawn does not contain a cco.v9 task")
    try:
        body = parse_canonical_json_object(value.split("\n", 1)[1], "cco.v9 task")
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if body.get("protocol") != PROTOCOL or SHA256_RE.fullmatch(str(body.get("dispatch_id"))) is None:
        raise ControlPlaneError("cco.v9 task identity is invalid")
    return body


def _render_continue(dispatch: Mapping[str, Any], evidence_delta: object, cursor: int) -> str:
    body = {
        "cursor": cursor,
        "dispatch_id": dispatch["dispatch_id"],
        "evidence_delta": evidence_delta,
        "protocol": PROTOCOL,
        "result_mode": "cumulative_from_wave_baseline",
        "workspace_root": dispatch.get("task_workspace_root", dispatch["workspace_root"]),
    }
    return CONTINUE_HEADER + "\n" + canonical_bytes(body).decode("utf-8")


def parse_continue_message(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(CONTINUE_HEADER + "\n"):
        raise ControlPlaneError("continuation does not contain a cco.v9 contract")
    try:
        body = parse_canonical_json_object(value.split("\n", 1)[1], "cco.v9 continuation")
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if body.get("protocol") != PROTOCOL or SHA256_RE.fullmatch(str(body.get("dispatch_id"))) is None:
        raise ControlPlaneError("cco.v9 continuation identity is invalid")
    return body


def parse_result(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(RESULT_HEADER + "\n"):
        raise ControlPlaneError("child did not return a cco.v9 result")
    raw = value.split("\n", 1)[1].strip()
    try:
        result = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, ControlPlaneError) as error:
        raise ControlPlaneError("cco.v9 result is not JSON") from error
    required = {
        "blockers",
        "changed_paths",
        "cursor",
        "deviations",
        "dispatch_id",
        "evidence",
        "failure_signature",
        "outcome",
        "status",
        "summary",
    }
    if not isinstance(result, Mapping) or set(result) != required:
        raise ControlPlaneError("cco.v9 result fields are malformed")
    if SHA256_RE.fullmatch(str(result["dispatch_id"])) is None:
        raise ControlPlaneError("cco.v9 result dispatch identity is invalid")
    cursor = result["cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ControlPlaneError("cco.v9 result cursor is invalid")
    status = result["status"]
    outcome = result["outcome"]
    if status not in {"complete", "partial", "blocked"} or outcome not in {
        "retire",
        "pause",
        "accept",
    }:
        raise ControlPlaneError("cco.v9 result status or outcome is invalid")
    blockers = result["blockers"]
    deviations = result["deviations"]
    if not isinstance(blockers, list) or not isinstance(deviations, list):
        raise ControlPlaneError("cco.v9 result blockers or deviations are invalid")
    normalized_blockers = sorted({_text(item, "result blocker", limit=2_048) for item in blockers})
    normalized_deviations = sorted({_text(item, "result deviation", limit=2_048) for item in deviations})
    if len(normalized_blockers) != len(blockers) or len(normalized_deviations) != len(deviations):
        raise ControlPlaneError("cco.v9 result lists contain duplicates")
    if status == "blocked" and not normalized_blockers:
        raise ControlPlaneError("blocked result must name a blocker")
    if status != "complete" and outcome != "pause":
        raise ControlPlaneError("incomplete result must pause for an explicit decision")
    if status == "complete" and (normalized_blockers or normalized_deviations):
        raise ControlPlaneError("complete result cannot contain blockers or deviations")
    paths_value = result["changed_paths"]
    if not isinstance(paths_value, list):
        raise ControlPlaneError("cco.v9 changed_paths must be a list")
    try:
        changed_paths = sorted(
            {require_repository_path(item, "result changed path") for item in paths_value}
        )
    except ProtocolHashError as error:
        raise ControlPlaneError(str(error)) from error
    if len(changed_paths) != len(paths_value):
        raise ControlPlaneError("cco.v9 changed_paths contains duplicates")
    evidence_value = result["evidence"]
    if not isinstance(evidence_value, Mapping):
        raise ControlPlaneError("cco.v9 evidence must be an object")
    evidence: dict[str, str] = {}
    for raw_id, raw_evidence in evidence_value.items():
        evidence_id = _text(raw_id, "result evidence ID", limit=32)
        if evidence_id in evidence:
            raise ControlPlaneError(
                f"result evidence IDs collide after normalization: {evidence_id}"
            )
        evidence[evidence_id] = _text(
            raw_evidence,
            "result evidence",
            limit=8_192,
        )
    failure = result["failure_signature"]
    if failure is not None:
        failure = _text(failure, "result failure signature", limit=256)
        if FAILURE_RE.fullmatch(failure) is None:
            raise ControlPlaneError("result failure signature is not canonical")
    if (status != "complete" or normalized_deviations or normalized_blockers) and failure is None:
        raise ControlPlaneError("non-success result requires a failure signature")
    if status == "complete" and not normalized_deviations and not normalized_blockers and failure is not None:
        raise ControlPlaneError("successful result cannot carry a failure signature")
    return {
        "blockers": normalized_blockers,
        "changed_paths": changed_paths,
        "cursor": cursor,
        "deviations": normalized_deviations,
        "dispatch_id": result["dispatch_id"],
        "evidence": {key: evidence[key] for key in sorted(evidence)},
        "failure_signature": failure,
        "outcome": outcome,
        "status": status,
        "summary": _text(result["summary"], "result summary", limit=4_096),
    }


def _native_response_failed(value: object) -> bool:
    """Recognize only an explicit failure marker; never infer a failure kind."""

    if not isinstance(value, Mapping):
        return False
    error = value.get("error")
    return (
        value.get("isError") is True
        or value.get("is_error") is True
        or value.get("success") is False
        or value.get("ok") is False
        or str(value.get("status", "")).casefold() in {"error", "failed", "failure"}
        or (error is not None and error is not False and error != "")
    )


def _tool_action(
    action: str,
    tool_name: str | None,
    tool_input: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "action": action,
        "tool_input": deepcopy(dict(tool_input)) if tool_input is not None else None,
        "tool_name": tool_name,
    }


def _task_paths(value: object, *, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if TASK_PATH_RE.fullmatch(value) is not None and (
            key in {"task_name", "task_path", "agent_path", "target"} or value.startswith("/root/")
        ):
            found.add(value)
    elif isinstance(value, Mapping):
        for child_key, child in value.items():
            found.update(_task_paths(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            found.update(_task_paths(child, key=key))
    return found


def _owner_matches_task(owner: object, task_name: object) -> bool:
    return (
        isinstance(owner, str)
        and isinstance(task_name, str)
        and TASK_PATH_RE.fullmatch(owner) is not None
        and owner.endswith("/" + task_name)
    )


def _interrupt_target_valid(target: object) -> bool:
    if not isinstance(target, str) or not target:
        return False
    try:
        return len(target.encode("utf-8")) <= 4_096
    except UnicodeEncodeError:
        return False


def _tool_use_id_valid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_TOOL_USE_ID_BYTES
    except UnicodeEncodeError:
        return False


def _interrupt_target_matches_dispatch(
    target: object,
    dispatch: Mapping[str, Any],
) -> bool:
    """Accept only immutable, exact aliases for one interruptable dispatch.

    A task name is intentionally retained as the ownerless-spawn alias.  Once
    a native spawn reports its canonical owner, that exact path is also safe.
    Do not infer an alias from a path suffix: an unrelated canonical path can
    share a task-name basename and must never acquire this dispatch's lease.
    """

    if not _interrupt_target_valid(target):
        return False
    return target in (dispatch.get("owner"), dispatch.get("task_name"))


def _cooperative_group_size_valid(size: object) -> bool:
    """Keep one explicit, writer-isolation-owned bound for cooperative waves."""

    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 2 <= size <= MAX_COOPERATIVE_WRITERS
    )


def _cooperative_units_disjoint(units: Iterable[Mapping[str, Any]]) -> bool:
    """A cooperative batch can apply only pairwise-disjoint writer deltas."""

    collected = list(units)
    return all(
        not _scopes_overlap(
            list(left.get("scopes", [])),
            list(right.get("scopes", [])),
        )
        for index, left in enumerate(collected)
        for right in collected[index + 1 :]
    )


def _sibling_writer_scopes(
    state: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> list[dict[str, str]]:
    return [
        scope
        for item in state["dispatches"].values()
        if item["wave_id"] == dispatch["wave_id"]
        and item["role"] == "worker"
        and item["dispatch_id"] != dispatch["dispatch_id"]
        and item["state"] in {"starting", "running", "paused", "retired"}
        for scope in item["scopes"]
    ]


def _is_cooperative_dispatch(dispatch: Mapping[str, Any]) -> bool:
    isolation = dispatch.get("isolation")
    return isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE


def _cooperative_batch_id(
    plan_id: str,
    sequence: int,
    units: list[Mapping[str, Any]],
) -> str:
    """Name a bounded pre-wave isolate batch without relying on mutable state."""

    return _digest(
        b"cco.cooperative-batch.v1\0",
        {
            "members": [item["members"] for item in units],
            "plan_id": plan_id,
            "sequence": sequence,
        },
    )


def _cooperative_union_scopes(units: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Build the one canonical baseline scope for a cooperative writer group."""

    scopes: dict[tuple[str, str], dict[str, str]] = {}
    for unit in units:
        raw_scopes = unit.get("scopes")
        if not isinstance(raw_scopes, list):
            raise ControlPlaneError("cooperative writer scopes are invalid")
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping) or set(raw_scope) != {"kind", "path"}:
                raise ControlPlaneError("cooperative writer scopes are invalid")
            kind = raw_scope.get("kind")
            if kind not in {"exact", "prefix"}:
                raise ControlPlaneError("cooperative writer scopes are invalid")
            try:
                path = require_repository_path(raw_scope.get("path"), "cooperative scope")
            except ProtocolHashError as error:
                raise ControlPlaneError(str(error)) from error
            scopes[(str(kind), path)] = {"kind": str(kind), "path": path}
    if not scopes:
        raise ControlPlaneError("cooperative writer scopes are invalid")
    return [scopes[key] for key in sorted(scopes)]


def _cooperative_snapshot_digest_fields(
    canonical_baseline: Mapping[str, Any],
    isolate_snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind immutable snapshot identities into the wave digest, not state."""

    canonical_id = canonical_baseline.get("state_id")
    if not isinstance(canonical_id, str) or SHA256_RE.fullmatch(canonical_id) is None:
        raise ControlPlaneError("cooperative canonical snapshot identity is invalid")
    isolate_ids: dict[str, str] = {}
    for unit_id, snapshot in isolate_snapshots.items():
        state_id = snapshot.get("state_id")
        if (
            not isinstance(unit_id, str)
            or not isinstance(state_id, str)
            or SHA256_RE.fullmatch(state_id) is None
        ):
            raise ControlPlaneError("cooperative isolate snapshot identity is invalid")
        isolate_ids[unit_id] = state_id
    return {
        "canonical_snapshot_id": canonical_id,
        "isolate_snapshot_ids": {
            unit_id: isolate_ids[unit_id] for unit_id in sorted(isolate_ids)
        },
    }


def _validate_cooperative_preparing(
    value: object,
    *,
    plan_id: str,
) -> dict[str, Any]:
    """Validate the short-lived cross-task writer reservation."""

    required = {"batch_id", "members", "plan_id"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ControlPlaneError("cooperative preparing reservation is invalid")
    batch_id = value.get("batch_id")
    if not isinstance(batch_id, str) or SHA256_RE.fullmatch(batch_id) is None:
        raise ControlPlaneError("cooperative preparing reservation is invalid")
    if value.get("plan_id") != plan_id:
        raise ControlPlaneError("cooperative preparing reservation plan is invalid")
    members = value.get("members")
    if not isinstance(members, list) or not _cooperative_group_size_valid(len(members)):
        raise ControlPlaneError("cooperative preparing reservation members are invalid")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for member in members:
        if not isinstance(member, Mapping) or set(member) != {"id", "scopes"}:
            raise ControlPlaneError("cooperative preparing reservation members are invalid")
        member_id = member.get("id")
        if not isinstance(member_id, str) or not member_id or member_id in ids:
            raise ControlPlaneError("cooperative preparing reservation members are invalid")
        scopes = member.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            raise ControlPlaneError("cooperative preparing reservation scopes are invalid")
        normalized_scopes: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_scope in scopes:
            if not isinstance(raw_scope, Mapping) or set(raw_scope) != {"kind", "path"}:
                raise ControlPlaneError("cooperative preparing reservation scopes are invalid")
            kind = raw_scope.get("kind")
            if kind not in {"exact", "prefix"}:
                raise ControlPlaneError("cooperative preparing reservation scopes are invalid")
            try:
                path = require_repository_path(
                    raw_scope.get("path"), "cooperative preparing scope"
                )
            except ProtocolHashError as error:
                raise ControlPlaneError(str(error)) from error
            key = (str(kind), path)
            if key in seen:
                raise ControlPlaneError("cooperative preparing reservation scopes are invalid")
            seen.add(key)
            normalized_scopes.append({"kind": str(kind), "path": path})
        ids.add(member_id)
        normalized.append(
            {
                "id": member_id,
                "scopes": sorted(normalized_scopes, key=lambda item: (item["kind"], item["path"])),
            }
        )
    normalized = sorted(normalized, key=lambda item: item["id"])
    if not _cooperative_units_disjoint(normalized):
        raise ControlPlaneError("cooperative preparing reservation scopes overlap")
    return {
        "batch_id": batch_id,
        "members": normalized,
        "plan_id": plan_id,
    }


def _now_milliseconds() -> int:
    return int(time.time() * 1000)


def _native_claim_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    deadline = dispatch.get("claim_expires_at")
    return (
        dispatch.get("state") == "starting"
        and (
            isinstance(dispatch.get("tool_use_id"), str)
            or (
                isinstance(deadline, int)
                and not isinstance(deadline, bool)
                and deadline > (_now_milliseconds() if now is None else now)
            )
        )
    )


def _native_settlement_overdue(
    dispatch: Mapping[str, Any], *, now: int | None = None
) -> bool:
    deadline = dispatch.get("claim_expires_at")
    return (
        dispatch.get("state") == "starting"
        and isinstance(dispatch.get("tool_use_id"), str)
        and isinstance(deadline, int)
        and not isinstance(deadline, bool)
        and deadline <= (_now_milliseconds() if now is None else now)
    )


def _writer_lease_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    if dispatch.get("role") != "worker":
        return False
    if dispatch.get("state") in {"running", "ready_to_apply", "paused"}:
        return True
    if dispatch.get("state") == "starting" and dispatch.get("tool_kind") == "continuation":
        return True
    return _native_claim_active(dispatch, now=now)


def _reader_active(dispatch: Mapping[str, Any], *, now: int | None = None) -> bool:
    return dispatch.get("role") != "worker" and (
        dispatch.get("state") == "running" or _native_claim_active(dispatch, now=now)
    )


class ControlPlane:
    """One deep interface for cco.v9 plan, wave, and lifecycle behavior."""

    def __init__(
        self,
        session_id: str,
        *,
        root: Path | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        if SESSION_RE.fullmatch(session_id) is None:
            raise ControlPlaneError("session identity is invalid")
        if lock_timeout <= 0:
            raise ControlPlaneError("lock timeout must be positive")
        self.session_id = session_id
        self._uses_default_root = root is None and not os.environ.get("CCO_STATE_DIR")
        self.root = Path(os.path.abspath((root or _state_root()).expanduser()))
        self._state_path: Path | None = None
        self.lock_timeout = float(lock_timeout)

    @property
    def _state_root_sentinel(self) -> Path:
        return self.root / STATE_ROOT_SENTINEL

    def _state_root_is_marked(self) -> bool:
        marker = self._state_root_sentinel
        if not marker.exists():
            return False
        if _read_bounded_bytes(marker, "CCO state-root sentinel", limit=128) != (
            STATE_ROOT_SENTINEL_BYTES
        ):
            raise ControlPlaneError("CCO state-root sentinel is invalid")
        return True

    def _mark_state_root_if_safe(self) -> None:
        """Mark only a dedicated or empty state root as CCO-owned."""

        self.root.mkdir(parents=True, exist_ok=True)
        if self._state_root_is_marked():
            return
        json_files = _state_json_paths(self.root)
        if json_files and not self._uses_default_root:
            for path in json_files:
                try:
                    state = self._validate_lifecycle_state(
                        _load_object(path, "current cco.v9 lifecycle state")
                    )
                except ControlPlaneUnavailable:
                    raise
                except ControlPlaneError:
                    return
                indexed = STATE_FILE_RE.fullmatch(path.name)
                if indexed is not None and (
                    indexed.group("workspace")
                    != _workspace_digest(state["workspace_root"])
                    or indexed.group("session")
                    != _session_digest(state["session_id"])
                ):
                    return
        try:
            descriptor = os.open(
                self._state_root_sentinel,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if not self._state_root_is_marked():
                raise ControlPlaneError("CCO state-root sentinel is invalid")
            return
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(STATE_ROOT_SENTINEL_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(self.root)
        except Exception:
            try:
                self._state_root_sentinel.unlink(missing_ok=True)
                _sync_directory(self.root)
            except (ControlPlaneUnavailable, OSError):
                pass
            raise

    @staticmethod
    def _validate_pending_event(
        value: Mapping[str, Any],
        *,
        expected_session: str | None = None,
    ) -> dict[str, Any]:
        """Validate one durable lifecycle receipt.

        A receipt keeps its identity while its phase advances.  In particular,
        acknowledgement is part of the receipt rather than an evictable second
        ledger in the lifecycle state.  Every receipt is emitted by the current
        protocol; older receipts are rejected rather than partially upgraded.
        """

        event = dict(value)
        if event.get("protocol") != PENDING_EVENT_PROTOCOL:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        event_id = event.get("event_id")
        session = event.get("session_id")
        kind = event.get("kind")
        if not isinstance(session, str) or SESSION_RE.fullmatch(session) is None:
            raise ControlPlaneError("pending event session is invalid")
        if expected_session is not None and session != expected_session:
            raise ControlPlaneError("pending event session does not match")
        phase = event.get("phase")
        if phase not in RECEIPT_PHASES:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)

        def require_serialized_bound() -> None:
            """Reject a receipt that its bounded reader could never recover."""

            try:
                serialized = (
                    json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
            except (TypeError, ValueError, UnicodeEncodeError) as error:
                raise ControlPlaneError("lifecycle receipt is not serializable") from error
            if len(serialized) > MAX_PENDING_EVENT_BYTES:
                raise ControlPlaneError("lifecycle receipt is too large")

        if kind == "native_attempt":
            required = {
                "cursor",
                "dispatch_id",
                "event_id",
                "generation",
                "kind",
                "observation",
                "owner",
                "phase",
                "plan_id",
                "protocol",
                "result_sha256",
                "session_id",
                "tool_input_sha256",
                "tool_kind",
                "tool_use_id",
                "workspace_root",
            }
            if (
                set(event) != required
                or not isinstance(event.get("plan_id"), str)
                or SHA256_RE.fullmatch(str(event["plan_id"])) is None
                or not isinstance(event.get("dispatch_id"), str)
                or SHA256_RE.fullmatch(str(event["dispatch_id"])) is None
                or isinstance(event.get("generation"), bool)
                or not isinstance(event.get("generation"), int)
                or event["generation"] < 1
                or isinstance(event.get("cursor"), bool)
                or not isinstance(event.get("cursor"), int)
                or event["cursor"] < 0
                or event.get("tool_kind") not in {"spawn", "reuse", "continuation"}
                or not isinstance(event.get("tool_use_id"), str)
                or not event["tool_use_id"]
                or not isinstance(event.get("tool_input_sha256"), str)
                or SHA256_RE.fullmatch(event["tool_input_sha256"]) is None
                or not isinstance(event.get("workspace_root"), str)
                or (
                    event.get("owner") is not None
                    and (
                        not isinstance(event.get("owner"), str)
                        or TASK_PATH_RE.fullmatch(event["owner"]) is None
                    )
                )
                or (
                    event.get("result_sha256") is not None
                    and (
                        not isinstance(event.get("result_sha256"), str)
                        or SHA256_RE.fullmatch(event["result_sha256"]) is None
                    )
                )
            ):
                raise ControlPlaneError("native attempt receipt is invalid")
            observation = event.get("observation")
            if phase == "reserved" or (
                phase == "acknowledged" and observation is None
            ):
                if observation is not None or event.get("owner") is not None or event.get("result_sha256") is not None:
                    raise ControlPlaneError("reserved native receipt has an observation")
            elif phase in {"native_observed", "awaiting_result"} or (
                phase == "acknowledged"
                and isinstance(observation, Mapping)
                and observation.get("kind") == "native_success"
            ):
                if (
                    not isinstance(observation, Mapping)
                    or set(observation) != {"kind", "owners"}
                    or observation.get("kind") != "native_success"
                    or not isinstance(observation.get("owners"), list)
                    or len(observation["owners"]) > 2
                    or len(set(observation["owners"])) != len(observation["owners"])
                    or any(
                        not isinstance(owner, str)
                        or TASK_PATH_RE.fullmatch(owner) is None
                        for owner in observation["owners"]
                    )
                    or event.get("owner")
                    != (
                        observation["owners"][0]
                        if len(observation["owners"]) == 1
                        else None
                    )
                    or event.get("result_sha256") is not None
                ):
                    raise ControlPlaneError("native receipt observation is invalid")
            elif phase in {"result_observed", "acknowledged"}:
                if not isinstance(observation, Mapping):
                    raise ControlPlaneError("result receipt observation is invalid")
                observation_kind = observation.get("kind")
                if observation_kind == "valid_result":
                    result = observation.get("result")
                    if (
                        set(observation) != {"kind", "result", "result_sha256"}
                        or not isinstance(result, Mapping)
                        or not isinstance(observation.get("result_sha256"), str)
                        or observation["result_sha256"] != event.get("result_sha256")
                        or SHA256_RE.fullmatch(observation["result_sha256"]) is None
                    ):
                        raise ControlPlaneError("valid result receipt observation is invalid")
                    try:
                        serialized = canonical_bytes(dict(result))
                        parse_result(
                            RESULT_HEADER + "\n" + serialized.decode("utf-8")
                        )
                    except (ProtocolHashError, UnicodeDecodeError, ControlPlaneError) as error:
                        raise ControlPlaneError("valid result receipt observation is invalid") from error
                    if len(serialized) > MAX_RESULT_OBSERVATION_BYTES:
                        raise ControlPlaneError("valid result receipt observation is too large")
                elif observation_kind == "invalid_result":
                    if (
                        set(observation) != {"failure_signature", "kind", "result_sha256"}
                        or observation.get("failure_signature") != "invalid_result"
                        or not isinstance(observation.get("result_sha256"), str)
                        or observation["result_sha256"] != event.get("result_sha256")
                        or SHA256_RE.fullmatch(observation["result_sha256"]) is None
                    ):
                        raise ControlPlaneError("invalid result receipt observation is invalid")
                else:
                    raise ControlPlaneError("result receipt observation is invalid")
            else:
                raise ControlPlaneError("native attempt receipt phase is invalid")
            identity = {
                key: event[key]
                for key in (
                    "cursor",
                    "dispatch_id",
                    "generation",
                    "kind",
                    "plan_id",
                    "protocol",
                    "session_id",
                    "tool_input_sha256",
                    "tool_kind",
                    "tool_use_id",
                    "workspace_root",
                )
            }
            expected_id = _digest(b"cco.receipt.v2\0", identity)
            if not isinstance(event_id, str) or event_id != expected_id:
                raise ControlPlaneError("native attempt receipt identity is invalid")
            require_serialized_bound()
            return event

        if kind == "interrupt_attempt":
            required = {
                "dispatch_id",
                "event_id",
                "generation",
                "kind",
                "owner",
                "phase",
                "plan_id",
                "previous_status",
                "protocol",
                "session_id",
                "tool_use_id",
                "workspace_root",
            }
            if (
                set(event) != required
                or not isinstance(event.get("plan_id"), str)
                or SHA256_RE.fullmatch(str(event["plan_id"])) is None
                or not isinstance(event.get("dispatch_id"), str)
                or SHA256_RE.fullmatch(str(event["dispatch_id"])) is None
                or isinstance(event.get("generation"), bool)
                or not isinstance(event.get("generation"), int)
                or event["generation"] < 1
                or not _interrupt_target_valid(event.get("owner"))
                or not isinstance(event.get("tool_use_id"), str)
                or not event["tool_use_id"]
                or not isinstance(event.get("workspace_root"), str)
            ):
                raise ControlPlaneError("interrupt attempt receipt is invalid")
            previous_status = event.get("previous_status")
            if phase == "reserved" or (
                phase == "acknowledged" and previous_status is None
            ):
                if previous_status is not None:
                    raise ControlPlaneError("reserved interrupt receipt has an observation")
            elif phase == "observed" or (
                phase == "acknowledged" and isinstance(previous_status, str)
            ):
                if not isinstance(previous_status, str) or not previous_status:
                    raise ControlPlaneError("interrupt receipt observation is invalid")
            else:
                raise ControlPlaneError("interrupt receipt phase is invalid")
            identity = {
                key: event[key]
                for key in (
                    "dispatch_id",
                    "generation",
                    "kind",
                    "owner",
                    "plan_id",
                    "protocol",
                    "session_id",
                    "tool_use_id",
                    "workspace_root",
                )
            }
            expected_id = _digest(b"cco.receipt.v2\0", identity)
            if not isinstance(event_id, str) or event_id != expected_id:
                raise ControlPlaneError("interrupt attempt receipt identity is invalid")
            require_serialized_bound()
            return event

        if kind == "session_restart":
            base_fields = {
                "event_id",
                "kind",
                "occurrence",
                "phase",
                "protocol",
                "session_id",
                "source",
            }
            bound_fields = base_fields | {"plan_id", "epoch"}
            if (set(event) != base_fields and set(event) != bound_fields) or (
                event.get("source") not in {"resume", "clear"}
                or not isinstance(event.get("occurrence"), str)
                or re.fullmatch(r"[0-9a-f]{32}", event["occurrence"]) is None
            ):
                raise ControlPlaneError("pending restart event is invalid")
            if set(event) == bound_fields and (
                not isinstance(event.get("plan_id"), str)
                or SHA256_RE.fullmatch(event["plan_id"]) is None
                or isinstance(event.get("epoch"), bool)
                or not isinstance(event.get("epoch"), int)
                or event["epoch"] < 1
            ):
                raise ControlPlaneError("pending restart receipt binding is invalid")
        else:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        if phase not in {"observed", "acknowledged"}:
            raise ControlPlaneError("lifecycle receipt phase is invalid")
        unsigned = {
            key: item
            for key, item in event.items()
            if key not in {"event_id", "phase"}
        }
        expected_id = _digest(b"cco.receipt.v2\0", unsigned)
        if not isinstance(event_id, str) or event_id != expected_id:
            raise ControlPlaneError("pending event identity is invalid")
        require_serialized_bound()
        return event

    def _pending_event(self, kind: str, **fields: Any) -> dict[str, Any]:
        if kind != "session_restart":
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        unsigned = {
            "kind": kind,
            "protocol": PENDING_EVENT_PROTOCOL,
            "session_id": self.session_id,
            **fields,
        }
        event = {
            **unsigned,
            "phase": "observed",
            "event_id": _digest(b"cco.receipt.v2\0", unsigned),
        }
        return self._validate_pending_event(event, expected_session=self.session_id)

    @staticmethod
    def _normalize_result_observation(raw_result: object) -> dict[str, Any]:
        """Store a bounded normalized result, never arbitrary child output."""

        if isinstance(raw_result, str):
            raw_bytes = raw_result.encode("utf-8", errors="surrogatepass")
        else:
            raw_bytes = type(raw_result).__qualname__.encode("utf-8")
        result_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        if not isinstance(raw_result, str) or len(raw_bytes) > MAX_INPUT_BYTES:
            return {
                "failure_signature": "invalid_result",
                "kind": "invalid_result",
                "result_sha256": result_sha256,
            }
        try:
            result = parse_result(raw_result)
            serialized = canonical_bytes(result)
        except (ControlPlaneError, ProtocolHashError):
            return {
                "failure_signature": "invalid_result",
                "kind": "invalid_result",
                "result_sha256": result_sha256,
            }
        if len(serialized) > MAX_RESULT_OBSERVATION_BYTES:
            return {
                "failure_signature": "invalid_result",
                "kind": "invalid_result",
                "result_sha256": result_sha256,
            }
        return {
            "kind": "valid_result",
            "result": result,
            "result_sha256": result_sha256,
        }

    def _native_attempt_receipt(
        self,
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        tool_use_id: str,
        tool_input: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        native = dispatch.get("native")
        if not isinstance(native, Mapping):
            raise ControlPlaneError("native attempt has no prepared input")
        observed_input = native if tool_input is None else tool_input
        identity = {
            "cursor": dispatch.get("pending_cursor")
            if dispatch.get("tool_kind") == "continuation"
            and dispatch.get("pending_cursor") is not None
            else dispatch.get("cursor"),
            "dispatch_id": dispatch.get("dispatch_id"),
            "generation": dispatch.get("generation"),
            "kind": "native_attempt",
            "plan_id": state.get("plan_id"),
            "protocol": PENDING_EVENT_PROTOCOL,
            "session_id": self.session_id,
            # This is the exact input observed by PreToolUse.  In plaintext
            # mode it equals ``native``; in trusted-host opaque mode its
            # message is ciphertext while the prepared input remains in the
            # lifecycle state for visible-envelope validation.
            "tool_input_sha256": _digest(
                b"cco.native-input.v1\0", dict(observed_input)
            ),
            "tool_kind": dispatch.get("tool_kind"),
            "tool_use_id": tool_use_id,
            "workspace_root": state.get("workspace_root"),
        }
        receipt = {
            **identity,
            "event_id": _digest(b"cco.receipt.v2\0", identity),
            "observation": None,
            "owner": None,
            "phase": "reserved",
            "result_sha256": None,
        }
        return self._validate_pending_event(receipt, expected_session=self.session_id)

    def _reserve_native_attempt_receipt(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
        tool_use_id: str,
        tool_input: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve the native and result slot before the host makes a call."""

        receipt = self._native_attempt_receipt(
            state,
            dispatch,
            tool_use_id,
            tool_input,
        )
        for tombstone in state.get("tombstones", []):
            if (
                tombstone.get("tool_use_id") == receipt["tool_use_id"]
                or tombstone.get("tool_input_sha256")
                == receipt["tool_input_sha256"]
            ):
                raise ControlPlaneError(
                    "native admission reuses a completed tool call or input"
                )
        # A host can retry PreToolUse for the same native call.  Treat only
        # an identical observed input as idempotent; never replace a live
        # receipt with a different ciphertext for the same tool_use_id.
        linked_receipt_id = dispatch.get("receipt_id")
        if isinstance(linked_receipt_id, str):
            linked = self._native_attempt_for_dispatch(dispatch)
            if linked is None:
                raise ControlPlaneUnavailable(
                    "native attempt receipt is linked but unavailable"
                )
            if linked != receipt:
                raise ControlPlaneError(
                    "native admission input changed for the existing tool call"
                )
            return linked
        anchored = sum(
            1
            for item in state.get("tombstones", [])
            if item.get("tool_use_id") is not None
            or item.get("tool_input_sha256") is not None
        )
        reserved = sum(
            1
            for item in state.get("dispatches", {}).values()
            if isinstance(item, Mapping)
            and isinstance(item.get("receipt_id"), str)
        )
        if anchored + reserved >= MAX_TOMBSTONES:
            raise ControlPlaneUnavailable(
                "lifecycle replay-anchor capacity is exhausted before native admission"
            )
        path = _pending_event_path(self.root, self.session_id, receipt["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if path.exists():
                current = self._read_pending_event(path)
                if current != receipt:
                    raise ControlPlaneError("native attempt receipt identity collision")
            else:
                receipts = _pending_event_paths(self.root)
                if len(receipts) >= MAX_PENDING_EVENT_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle receipt capacity is exhausted before native admission"
                    )
                _atomic_write(path, receipt)
        dispatch["receipt_id"] = receipt["event_id"]
        return receipt

    def _native_attempt_for_dispatch(
        self,
        dispatch: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        receipt_id = dispatch.get("receipt_id")
        if not isinstance(receipt_id, str):
            return None
        path = _pending_event_path(self.root, self.session_id, receipt_id)
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                return None
            receipt = self._read_pending_event(path)
        if receipt.get("kind") != "native_attempt":
            raise ControlPlaneError("dispatch receipt is not a native attempt")
        return receipt

    @staticmethod
    def _native_receipt_matches_dispatch(
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> bool:
        expected_cursor = (
            dispatch.get("pending_cursor")
            if dispatch.get("state") == "starting"
            and dispatch.get("tool_kind") == "continuation"
            else dispatch.get("cursor")
        )
        return (
            receipt.get("kind") == "native_attempt"
            and receipt.get("plan_id") == state.get("plan_id")
            and receipt.get("workspace_root") == state.get("workspace_root")
            and receipt.get("generation") == dispatch.get("generation")
            and receipt.get("dispatch_id") == dispatch.get("dispatch_id")
            and receipt.get("cursor") == expected_cursor
            and receipt.get("tool_kind") == dispatch.get("tool_kind")
        )

    @classmethod
    def _native_receipt_is_current(
        cls,
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> bool:
        return (
            dispatch.get("receipt_id") == receipt.get("event_id")
            and cls._native_receipt_matches_dispatch(state, dispatch, receipt)
        )

    @staticmethod
    def _attempt_receipt_belongs_to_terminal_dispatch(
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> bool:
        """Match every retry of one terminal dispatch, not only its live slot."""

        if (
            receipt.get("plan_id") != state.get("plan_id")
            or receipt.get("workspace_root") != state.get("workspace_root")
            or receipt.get("generation") != dispatch.get("generation")
            or receipt.get("dispatch_id") != dispatch.get("dispatch_id")
        ):
            return False
        if receipt.get("kind") == "native_attempt":
            return True
        return (
            receipt.get("kind") == "interrupt_attempt"
            and _interrupt_target_matches_dispatch(receipt.get("owner"), dispatch)
        )

    @staticmethod
    def _interrupt_receipt_is_current(
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> bool:
        return (
            receipt.get("kind") == "interrupt_attempt"
            and receipt.get("plan_id") == state.get("plan_id")
            and receipt.get("workspace_root") == state.get("workspace_root")
            and receipt.get("generation") == dispatch.get("generation")
            and receipt.get("dispatch_id") == dispatch.get("dispatch_id")
            and receipt.get("event_id") == dispatch.get("interrupt_receipt_id")
            and receipt.get("tool_use_id") == dispatch.get("interrupt_tool_use_id")
            and _interrupt_target_matches_dispatch(receipt.get("owner"), dispatch)
        )

    def _find_native_attempt_receipt(
        self,
        *,
        dispatch_id: str | None = None,
        tool_use_id: str | None = None,
        tool_input_sha256: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any] | None:
        """Find one still-durable attempt; late deleted attempts are inert."""

        matches: list[dict[str, Any]] = []
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if receipt.get("kind") != "native_attempt":
                    continue
                if dispatch_id is not None and receipt.get("dispatch_id") != dispatch_id:
                    continue
                if tool_use_id is not None and receipt.get("tool_use_id") != tool_use_id:
                    continue
                if (
                    tool_input_sha256 is not None
                    and receipt.get("tool_input_sha256") != tool_input_sha256
                ):
                    continue
                if owner is not None and receipt.get("owner") not in {None, owner}:
                    continue
                matches.append(receipt)
        if len(matches) > 1:
            raise ControlPlaneError("native lifecycle receipt is ambiguous")
        return matches[0] if matches else None

    def _clear_acknowledged_native_attempts(self, owner: str) -> None:
        """Finish only terminal receipts; never choose a live attempt by owner."""

        acknowledged: list[dict[str, Any]] = []
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if (
                    receipt.get("kind") == "native_attempt"
                    and receipt.get("owner") == owner
                    and receipt.get("phase") == "acknowledged"
                ):
                    acknowledged.append(receipt)
        for receipt in acknowledged:
            self._clear_pending_event(receipt)

    def _write_native_attempt_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = self._validate_pending_event(
            receipt,
            expected_session=self.session_id,
        )
        if normalized.get("kind") != "native_attempt":
            raise ControlPlaneError("receipt is not a native attempt")
        path = _pending_event_path(self.root, self.session_id, normalized["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                raise ControlPlaneUnavailable("native lifecycle receipt disappeared")
            current = self._read_pending_event(path)
            if current.get("event_id") != normalized.get("event_id"):
                raise ControlPlaneUnavailable("native lifecycle receipt changed")
            # A receipt is an append-only attempt state machine.  A duplicate
            # may observe the already-recorded value, but cannot replace it.
            if current == normalized or current.get("phase") == "acknowledged":
                return current
            current_phase = current.get("phase")
            proposed_phase = normalized.get("phase")
            if proposed_phase == "acknowledged":
                expected = dict(current)
                expected["phase"] = "acknowledged"
                if normalized != expected:
                    return current
                _atomic_write(path, normalized)
                return normalized
            if current_phase == "reserved" and proposed_phase in {
                "native_observed",
                "result_observed",
            }:
                _atomic_write(path, normalized)
                return normalized
            if current_phase == "native_observed" and proposed_phase in {
                "awaiting_result",
                "result_observed",
            }:
                if proposed_phase == "awaiting_result":
                    expected = dict(current)
                    expected["phase"] = "awaiting_result"
                    if normalized != expected:
                        return current
                elif (
                    current.get("owner") is not None
                    and current.get("owner") != normalized.get("owner")
                ):
                    return current
                _atomic_write(path, normalized)
                return normalized
            if current_phase == "awaiting_result" and proposed_phase == "result_observed":
                if (
                    current.get("owner") is not None
                    and current.get("owner") != normalized.get("owner")
                ):
                    return current
                _atomic_write(path, normalized)
                return normalized
            return current

    def _ack_native_attempt_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        acknowledged = dict(receipt)
        acknowledged["phase"] = "acknowledged"
        current = self._write_native_attempt_receipt(acknowledged)
        if current.get("phase") == "acknowledged":
            return current
        acknowledged = dict(current)
        acknowledged["phase"] = "acknowledged"
        return self._write_native_attempt_receipt(acknowledged)

    def _finalize_native_attempt_receipt(self, receipt: Mapping[str, Any]) -> None:
        try:
            acknowledged = self._ack_native_attempt_receipt(receipt)
        except ControlPlaneUnavailable:
            path = _pending_event_path(self.root, self.session_id, str(receipt["event_id"]))
            with acquire(
                self.root,
                STATE_ROOT_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                if not path.exists():
                    return
            raise
        self._clear_pending_event(acknowledged)

    def _discard_reserved_native_attempt_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> None:
        """Release a reservation only when the host call was never admitted."""

        normalized = self._validate_pending_event(
            receipt,
            expected_session=self.session_id,
        )
        if normalized.get("kind") != "native_attempt" or normalized.get("phase") != "reserved":
            return
        path = _pending_event_path(self.root, self.session_id, normalized["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                return
            current = self._read_pending_event(path)
            if current == normalized:
                path.unlink(missing_ok=True)
                _sync_directory(path.parent)

    def _interrupt_attempt_receipt(
        self,
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        owner: str,
        tool_use_id: str,
    ) -> dict[str, Any]:
        identity = {
            "dispatch_id": dispatch.get("dispatch_id"),
            "generation": dispatch.get("generation"),
            "kind": "interrupt_attempt",
            "owner": owner,
            "plan_id": state.get("plan_id"),
            "protocol": PENDING_EVENT_PROTOCOL,
            "session_id": self.session_id,
            "tool_use_id": tool_use_id,
            "workspace_root": state.get("workspace_root"),
        }
        receipt = {
            **identity,
            "event_id": _digest(b"cco.receipt.v2\0", identity),
            "phase": "reserved",
            "previous_status": None,
        }
        return self._validate_pending_event(receipt, expected_session=self.session_id)

    def _reserve_interrupt_attempt_receipt(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
        owner: str,
        tool_use_id: str,
    ) -> dict[str, Any]:
        receipt = self._interrupt_attempt_receipt(state, dispatch, owner, tool_use_id)
        path = _pending_event_path(self.root, self.session_id, receipt["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if path.exists():
                current = self._read_pending_event(path)
                if current != receipt:
                    raise ControlPlaneError("interrupt receipt identity collision")
            else:
                receipts = _pending_event_paths(self.root)
                if len(receipts) >= MAX_PENDING_EVENT_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle receipt capacity is exhausted before native admission"
                    )
                _atomic_write(path, receipt)
        dispatch["interrupt_receipt_id"] = receipt["event_id"]
        return receipt

    def _interrupt_attempt_for_dispatch(
        self,
        dispatch: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        receipt_id = dispatch.get("interrupt_receipt_id")
        if not isinstance(receipt_id, str):
            return None
        path = _pending_event_path(self.root, self.session_id, receipt_id)
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                return None
            receipt = self._read_pending_event(path)
        if receipt.get("kind") != "interrupt_attempt":
            raise ControlPlaneError("dispatch receipt is not an interrupt attempt")
        return receipt

    def _find_interrupt_attempt_receipt(
        self,
        owner: str,
        tool_use_id: str,
    ) -> dict[str, Any] | None:
        matches: list[dict[str, Any]] = []
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if (
                    receipt.get("kind") == "interrupt_attempt"
                    and receipt.get("owner") == owner
                    and receipt.get("tool_use_id") == tool_use_id
                ):
                    matches.append(receipt)
        if len(matches) > 1:
            raise ControlPlaneError("interrupt lifecycle receipt is ambiguous")
        return matches[0] if matches else None

    def _write_interrupt_attempt_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = self._validate_pending_event(
            receipt,
            expected_session=self.session_id,
        )
        if normalized.get("kind") != "interrupt_attempt":
            raise ControlPlaneError("receipt is not an interrupt attempt")
        path = _pending_event_path(self.root, self.session_id, normalized["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                raise ControlPlaneUnavailable("interrupt lifecycle receipt disappeared")
            current = self._read_pending_event(path)
            if current.get("event_id") != normalized.get("event_id"):
                raise ControlPlaneUnavailable("interrupt lifecycle receipt changed")
            if current == normalized or current.get("phase") == "acknowledged":
                return current
            if normalized.get("phase") == "acknowledged":
                expected = dict(current)
                expected["phase"] = "acknowledged"
                if normalized != expected:
                    return current
                _atomic_write(path, normalized)
                return normalized
            if (
                current.get("phase") == "reserved"
                and normalized.get("phase") == "observed"
            ):
                _atomic_write(path, normalized)
                return normalized
            return current

    def _finalize_interrupt_attempt_receipt(self, receipt: Mapping[str, Any]) -> None:
        acknowledged = dict(receipt)
        acknowledged["phase"] = "acknowledged"
        try:
            acknowledged = self._write_interrupt_attempt_receipt(acknowledged)
        except ControlPlaneUnavailable:
            path = _pending_event_path(self.root, self.session_id, str(receipt["event_id"]))
            with acquire(
                self.root,
                STATE_ROOT_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                if not path.exists():
                    return
            raise
        if acknowledged.get("phase") != "acknowledged":
            acknowledged = dict(acknowledged)
            acknowledged["phase"] = "acknowledged"
            acknowledged = self._write_interrupt_attempt_receipt(acknowledged)
        self._clear_pending_event(acknowledged)

    def _discard_reserved_interrupt_attempt_receipt(
        self,
        receipt: Mapping[str, Any],
    ) -> None:
        normalized = self._validate_pending_event(
            receipt,
            expected_session=self.session_id,
        )
        if normalized.get("kind") != "interrupt_attempt" or normalized.get("phase") != "reserved":
            return
        path = _pending_event_path(self.root, self.session_id, normalized["event_id"])
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if path.exists() and self._read_pending_event(path) == normalized:
                path.unlink(missing_ok=True)
                _sync_directory(path.parent)

    @staticmethod
    def _validate_native_attempt_observation(
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        event = dict(value)
        owners = event.get("owners")
        if (
            set(event)
            != {
                "dispatch_id",
                "kind",
                "owners",
                "tool_input_sha256",
                "tool_use_id",
            }
            or event.get("kind") != "native_attempt_observation"
            or not isinstance(event.get("dispatch_id"), str)
            or SHA256_RE.fullmatch(event["dispatch_id"]) is None
            or not isinstance(event.get("tool_input_sha256"), str)
            or SHA256_RE.fullmatch(event["tool_input_sha256"]) is None
            or not isinstance(event.get("tool_use_id"), str)
            or not event["tool_use_id"]
            or not isinstance(owners, list)
            or len(owners) > 2
            or len(set(owners)) != len(owners)
            or any(
                not isinstance(owner, str) or TASK_PATH_RE.fullmatch(owner) is None
                for owner in owners
            )
        ):
            raise ControlPlaneError("native attempt observation is invalid")
        return event

    @staticmethod
    def _validate_interrupt_attempt_observation(
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        event = dict(value)
        if (
            set(event)
            != {"kind", "previous_status", "target", "tool_use_id"}
            or event.get("kind") != "interrupt_attempt_observation"
            or not _interrupt_target_valid(event.get("target"))
            or not isinstance(event.get("previous_status"), str)
            or not event["previous_status"]
            or not isinstance(event.get("tool_use_id"), str)
            or not event["tool_use_id"]
        ):
            raise ControlPlaneError("interrupt attempt observation is invalid")
        return event

    def _postflight_observation(
        self,
        payload: Mapping[str, Any],
        *,
        opaque_message: bool = False,
    ) -> dict[str, Any]:
        tool_use_id = payload.get("tool_use_id")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("native tool result has no call identity")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("native tool result has no input identity")
        message = tool_input.get("message")
        if opaque_message:
            if not host_opaque_message(message):
                raise ControlPlaneError("opaque postflight message is invalid")
            input_sha256 = _digest(b"cco.native-input.v1\0", dict(tool_input))
            receipt = self._find_native_attempt_receipt(
                tool_use_id=tool_use_id,
                tool_input_sha256=input_sha256,
            )
            if receipt is None:
                raise ControlPlaneError(
                    "opaque native result has no matching preflight receipt"
                )
            dispatch_id = str(receipt["dispatch_id"])
        elif isinstance(message, str) and message.startswith(TASK_HEADER + "\n"):
            dispatch_id = parse_task_message(message)["dispatch_id"]
            input_sha256 = _digest(b"cco.native-input.v1\0", dict(tool_input))
        elif isinstance(message, str) and message.startswith(CONTINUE_HEADER + "\n"):
            dispatch_id = parse_continue_message(message)["dispatch_id"]
            input_sha256 = _digest(b"cco.native-input.v1\0", dict(tool_input))
        else:
            target = tool_input.get("target")
            response = payload.get("tool_response")
            previous_status = (
                response.get("previous_status")
                if isinstance(response, Mapping)
                else None
            )
            if not isinstance(target, str):
                raise ControlPlaneError("native tool result is not CCO-owned")
            if not isinstance(previous_status, str) or not previous_status:
                previous_status = "unknown"
            return self._validate_interrupt_attempt_observation(
                {
                    "kind": "interrupt_attempt_observation",
                    "previous_status": previous_status,
                    "target": target,
                    "tool_use_id": tool_use_id,
                }
            )
        response = payload.get("tool_response")
        if _native_response_failed(response):
            raise ControlPlaneError(
                "failure-side PostToolUse is not a settlement event; use native-failure"
            )
        owners = sorted(_task_paths(response))[:2]
        return self._validate_native_attempt_observation(
            {
                "dispatch_id": dispatch_id,
                "kind": "native_attempt_observation",
                "owners": owners,
                "tool_input_sha256": input_sha256,
                "tool_use_id": tool_use_id,
            }
        )

    def _observe_native_attempt(self, event: Mapping[str, Any]) -> bool:
        """Settle PostToolUse only through the preflight-reserved attempt slot."""

        normalized = self._validate_native_attempt_observation(event)
        if normalized.get("kind") != "native_attempt_observation":
            raise ControlPlaneError("native observation is not a native success")
        receipt = self._find_native_attempt_receipt(
            tool_use_id=str(normalized["tool_use_id"]),
            tool_input_sha256=str(normalized["tool_input_sha256"]),
        )
        if receipt is None:
            # A superseded continuation, cleaned-up plan, or replayed PostTool
            # cannot mutate the current dispatch.  It is intentionally inert.
            return False
        if receipt.get("dispatch_id") != normalized.get("dispatch_id"):
            raise ControlPlaneError("native observation dispatch is not receipt-bound")
        if receipt.get("phase") == "acknowledged":
            self._clear_pending_event(receipt)
            return True
        if receipt.get("phase") in {"awaiting_result", "result_observed"}:
            return True
        if receipt.get("phase") not in {"reserved", "native_observed"}:
            raise ControlPlaneError("native receipt is not ready for observation")

        observed = dict(receipt)
        owners = normalized["owners"]
        observed["observation"] = {"kind": "native_success", "owners": owners}
        observed["owner"] = owners[0] if len(owners) == 1 else None
        observed["phase"] = "native_observed"
        observed = self._write_native_attempt_receipt(observed)
        if observed.get("phase") == "acknowledged":
            self._clear_pending_event(observed)
            return True
        if observed.get("phase") in {"awaiting_result", "result_observed"}:
            # A concurrent result or duplicate postflight already advanced the
            # immutable attempt.  Do not replay a different owner list.
            return True
        if observed.get("phase") != "native_observed":
            raise ControlPlaneError("native receipt observation is not active")
        recorded_observation = observed.get("observation")
        if not isinstance(recorded_observation, Mapping):
            raise ControlPlaneError("native receipt observation is invalid")
        settlement_event = dict(normalized)
        settlement_event["owners"] = list(recorded_observation["owners"])
        settled = self._settle_native_success_event(settlement_event, receipt=observed)
        if not settled:
            # An earlier plan or an unlinked reservation cannot authorize this
            # postflight.  Its identity proof prevents it from mutating a
            # replacement plan or a dispatch whose state claim never landed.
            self._finalize_native_attempt_receipt(observed)
            return False

        terminal = False
        if self.state_path.exists():
            with self._coordinated_state() as state:
                dispatch = state["dispatches"].get(receipt["dispatch_id"])
                terminal = not isinstance(dispatch, Mapping) or dispatch.get("state") in {
                    "fenced",
                    "rejected",
                    "retired",
                }
        if terminal:
            self._finalize_native_attempt_receipt(observed)
            return True
        awaiting = dict(observed)
        awaiting["phase"] = "awaiting_result"
        self._write_native_attempt_receipt(awaiting)
        return True

    def _stage_pending_event(self, event: Mapping[str, Any]) -> Path:
        """Publish an observation or reservation without replacing its receipt."""

        normalized = self._validate_pending_event(
            event,
            expected_session=self.session_id,
        )
        path = _pending_event_path(
            self.root,
            self.session_id,
            normalized["event_id"],
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            existing = _pending_event_paths(self.root)
            if not path.exists() and len(existing) >= MAX_PENDING_EVENT_FILES:
                raise ControlPlaneUnavailable(
                    "lifecycle pending event capacity is exhausted"
                )
            if path.exists():
                current = self._validate_pending_event(
                    _decode_object(
                        _read_bounded_bytes(
                            path,
                            "pending lifecycle event",
                            limit=MAX_PENDING_EVENT_BYTES,
                        ),
                        "pending lifecycle event",
                    ),
                    expected_session=self.session_id,
                )
                if current != normalized:
                    if current.get("event_id") != normalized.get("event_id"):
                        raise ControlPlaneError(
                            "pending lifecycle event identity collision"
                        )
                    # A late duplicate must not turn a durable settlement
                    # acknowledgement back into an observed event.  The
                    # receipt is authoritative until its final deletion.
                    if current.get("phase") != "acknowledged":
                        _atomic_write(path, normalized)
            else:
                _atomic_write(path, normalized)
        return path

    def _read_pending_event(self, path: Path) -> dict[str, Any]:
        return self._validate_pending_event(
            _decode_object(
                _read_bounded_bytes(
                    path,
                    "pending lifecycle event",
                    limit=MAX_PENDING_EVENT_BYTES,
                ),
                "pending lifecycle event",
            ),
            expected_session=self.session_id,
        )

    def _ack_pending_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Durably acknowledge a settled receipt before attempting deletion."""

        normalized = self._validate_pending_event(
            event,
            expected_session=self.session_id,
        )
        path = _pending_event_path(
            self.root,
            self.session_id,
            normalized["event_id"],
        )
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                return normalized
            current = self._read_pending_event(path)
            if current.get("event_id") != normalized.get("event_id"):
                raise ControlPlaneUnavailable(
                    "pending lifecycle event changed before acknowledgement"
                )
            if current.get("phase") == "acknowledged":
                return current
            acknowledged = dict(current)
            acknowledged["phase"] = "acknowledged"
            acknowledged = self._validate_pending_event(
                acknowledged,
                expected_session=self.session_id,
            )
            _atomic_write(path, acknowledged)
            return acknowledged

    def _clear_pending_event(self, event: Mapping[str, Any]) -> None:
        normalized = self._validate_pending_event(
            event,
            expected_session=self.session_id,
        )
        path = _pending_event_path(
            self.root,
            self.session_id,
            normalized["event_id"],
        )
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            if not path.exists():
                return
            current = self._read_pending_event(path)
            if current.get("event_id") != normalized.get("event_id"):
                raise ControlPlaneUnavailable(
                    "pending lifecycle event changed before finalization"
                )
            if current.get("phase") != "acknowledged":
                raise ControlPlaneUnavailable(
                    "pending lifecycle event is not acknowledged"
                )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ControlPlaneUnavailable(
                    "pending lifecycle event finalization failed"
                ) from error
            else:
                _sync_directory(path.parent)

    def _pending_restart_receipt(self) -> dict[str, Any] | None:
        """Return the oldest restart transaction awaiting this session's recovery."""

        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if receipt.get("kind") == "session_restart":
                    return receipt
        return None

    def _stage_restart_receipt(self, source: str) -> dict[str, Any]:
        """Publish a restart transaction before it can fence lifecycle state."""

        try:
            if self.state_path.exists():
                with self._coordinated_state() as state:
                    event = self._pending_event(
                        "session_restart",
                        occurrence=os.urandom(16).hex(),
                        source=source,
                        epoch=state["epoch"],
                        plan_id=state["plan_id"],
                    )
                    # The receipt is the replay anchor for the state write and
                    # later native-receipt finalization.  It must be durable
                    # before this restart can make a lifecycle decision.
                    self._stage_pending_event(event)
                    return event
        except _AtomicWriteUncertain:
            # A bound restart receipt can already be visible.  Let the next
            # replay discover that exact transaction rather than fabricating a
            # second, unbound restart decision.
            raise
        except ControlPlaneUnavailable:
            # A host restart can arrive before a current lifecycle is readable.
            # Keep its bounded receipt unbound until normal recovery can either
            # settle it or prove that no lifecycle exists.
            pass
        event = self._pending_event(
            "session_restart",
            occurrence=os.urandom(16).hex(),
            source=source,
        )
        self._stage_pending_event(event)
        return event

    def process_restart_event(self, source: str) -> int:
        """Fence active work through one durable, replayable restart receipt."""

        if source not in {"resume", "clear"}:
            raise ControlPlaneError("pending restart event is invalid")
        # A prior restart may have committed its lifecycle fence but crashed
        # while acknowledging/deleting an attempt receipt.  Resume that exact
        # transaction rather than inventing another epoch or treating native
        # awaiting/result observations as a competing restart.
        event = self._pending_restart_receipt()
        if event is None:
            event = self._stage_restart_receipt(source)
        return int(self._process_pending_event(event) or 0)

    def process_postflight_event(
        self,
        payload: Mapping[str, Any],
        *,
        opaque_message: bool = False,
    ) -> bool:
        event = self._postflight_observation(
            payload,
            opaque_message=opaque_message,
        )
        if event["kind"] == "native_attempt_observation":
            return self._observe_native_attempt(event)
        if event["kind"] == "interrupt_attempt_observation":
            return self._observe_interrupt_attempt(event)
        raise ControlPlaneError("postflight observation is not CCO-owned")

    def process_result_event(self, owner: str, raw_result: object) -> dict[str, Any]:
        """Durably bind one child result to its current native attempt.

        Result text has no host call identifier, so dispatch/cursor are parsed
        before lifecycle selection.  A known terminal dispatch is a delayed
        prior-generation observation and is inert.  Every other malformed,
        wrong-dispatch, or wrong-cursor result from an owner with a live attempt
        fences that exact attempt (and an isolate peer batch) instead of leaving
        its lease live.
        """

        observation = self._normalize_result_observation(raw_result)

        def ignored() -> dict[str, Any]:
            self._clear_acknowledged_native_attempts(owner)
            return {
                "dispatch_id": None,
                "members": [],
                "replayed": True,
                "state": "ignored",
                "verification": None,
            }

        if not self.state_path.exists():
            return ignored()

        supplied_dispatch_id: str | None = None
        supplied_cursor: int | None = None
        if observation["kind"] == "valid_result":
            result = observation.get("result")
            if not isinstance(result, Mapping):
                return ignored()
            supplied_dispatch_id = result.get("dispatch_id")
            supplied_cursor = result.get("cursor")
            if (
                not isinstance(supplied_dispatch_id, str)
                or isinstance(supplied_cursor, bool)
                or not isinstance(supplied_cursor, int)
            ):
                return ignored()

        ambiguous_receipts: list[tuple[str, dict[str, Any]]] = []
        observed: dict[str, Any] | None = None
        old_terminal_result = False
        with self._coordinated_state() as state:
            target = (
                state["dispatches"].get(supplied_dispatch_id)
                if supplied_dispatch_id is not None
                else None
            )
            active: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            for dispatch in state["dispatches"].values():
                if (
                    not isinstance(dispatch, dict)
                    or dispatch.get("state") not in {"starting", "running"}
                    or (
                        dispatch.get("owner") != owner
                        and not (
                            dispatch.get("owner") is None
                            and _owner_matches_task(owner, dispatch.get("task_name"))
                        )
                    )
                ):
                    continue
                receipt = self._native_attempt_for_dispatch(dispatch)
                if receipt is not None and not self._native_receipt_is_current(
                    state, dispatch, receipt
                ):
                    receipt = None
                if receipt is not None and receipt.get("owner") not in {None, owner}:
                    receipt = None
                active.append((dispatch, receipt))

            if len(active) > 1:
                for dispatch, _receipt in active:
                    ambiguous_receipts.extend(
                        self._fence_cooperative_members_locked(
                            state, dispatch, "ambiguous_active_owner_result"
                        )
                    )
                self._write_state(state)
            elif not active:
                old_terminal_result = (
                    isinstance(target, Mapping)
                    and target.get("state") in {"paused", "retired", "fenced", "rejected"}
                    and target.get("owner") == owner
                )
            else:
                dispatch, receipt = active[0]
                # A first durable observation wins.  This preserves an exact
                # concurrent result even if a later duplicate contains a
                # different result payload.
                if (
                    supplied_dispatch_id is not None
                    and isinstance(target, Mapping)
                    and target.get("state") in {"paused", "retired", "fenced", "rejected"}
                    and target.get("owner") == owner
                ):
                    old_terminal_result = True
                elif receipt is not None and receipt.get("phase") == "result_observed":
                    observed = receipt
                elif receipt is None or receipt.get("phase") == "acknowledged":
                    ambiguous_receipts.extend(
                        self._fence_cooperative_members_locked(
                            state, dispatch, "result_receipt_lost"
                        )
                    )
                    self._write_state(state)
                elif receipt.get("phase") not in {
                    "reserved",
                    "native_observed",
                    "awaiting_result",
                }:
                    ambiguous_receipts.extend(
                        self._fence_cooperative_members_locked(
                            state, dispatch, "result_receipt_invalid"
                        )
                    )
                    self._write_state(state)
                else:
                    observed = dict(receipt)
                    observed["owner"] = owner
                    observed["observation"] = observation
                    observed["result_sha256"] = observation["result_sha256"]
                    observed["phase"] = "result_observed"
                    observed = self._write_native_attempt_receipt(observed)

        if ambiguous_receipts:
            deduplicated = {
                receipt["event_id"]: (kind, receipt)
                for kind, receipt in ambiguous_receipts
            }
            self._finalize_detached_attempt_receipts(list(deduplicated.values()))
            return {
                "dispatch_id": None,
                "members": [],
                "state": "fenced",
                "verification": None,
            }
        if old_terminal_result or observed is None:
            return ignored()

        stored_observation = observed.get("observation")
        if not isinstance(stored_observation, Mapping):
            return self._fence_result_receipt(owner, observed, "invalid_result")
        if stored_observation.get("kind") == "invalid_result":
            return self._fence_result_receipt(owner, observed, "invalid_result")
        stored_result = stored_observation.get("result")
        if not isinstance(stored_result, Mapping):
            return self._fence_result_receipt(owner, observed, "invalid_result")
        expected_cursor = observed.get("cursor")
        if (
            stored_result.get("dispatch_id") != observed.get("dispatch_id")
            or stored_result.get("cursor") != expected_cursor
        ):
            reason = (
                "result_dispatch_mismatch"
                if stored_result.get("dispatch_id") != observed.get("dispatch_id")
                else "result_cursor_mismatch"
            )
            return self._fence_result_receipt(owner, observed, reason)
        try:
            settled = self.record_result(
                owner,
                RESULT_HEADER
                + "\n"
                + canonical_bytes(dict(stored_result)).decode("utf-8"),
            )
        except ControlPlaneUnavailable:
            # The normalized observation remains durable and is replayable.
            raise
        except ControlPlaneError:
            return self._fence_result_receipt(owner, observed, "invalid_result")
        return settled

    def _release_settled_result_receipt(self, receipt: Mapping[str, Any]) -> None:
        """Persist the replay anchor before deleting an acknowledged receipt."""

        dispatch_id = str(receipt["dispatch_id"])
        interrupt_receipt: dict[str, Any] | None = None
        if self.state_path.exists():
            with self._coordinated_state() as state:
                dispatch = state["dispatches"].get(dispatch_id)
                if isinstance(dispatch, dict) and dispatch.get("state") in {
                    "paused",
                    "ready_to_apply",
                    "retired",
                    "fenced",
                    "rejected",
                }:
                    changed = False
                    if dispatch.get("receipt_id") == receipt.get("event_id"):
                        self._append_tombstone(
                            state,
                            dispatch,
                            "native_attempt_consumed",
                            receipt=receipt,
                        )
                        dispatch["receipt_id"] = None
                        changed = True
                    if dispatch.get("state") in {"retired", "fenced", "rejected"} and (
                        dispatch.get("interrupt_receipt_id") is not None
                    ):
                        interrupt_receipt = self._seal_terminal_interrupt_attempt(
                            dispatch
                        )
                        changed = True
                    if changed:
                        self._write_state(state)
        self._finalize_native_attempt_receipt(receipt)
        if interrupt_receipt is not None:
            self._finalize_interrupt_attempt_receipt(interrupt_receipt)

    def _seal_terminal_interrupt_attempt(
        self,
        dispatch: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Release a terminal interrupt slot without ever reusing its owner."""

        receipt = self._interrupt_attempt_for_dispatch(dispatch)
        dispatch["interrupt_receipt_id"] = None
        dispatch["interrupt_tool_use_id"] = None
        dispatch["interrupt_claim_expires_at"] = None
        dispatch["interrupt_unresolved"] = True
        return receipt

    def _fence_result_receipt(
        self,
        owner: str,
        receipt: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Fence deterministic bad output in the same lifecycle settlement."""

        result: dict[str, Any]
        receipts: list[tuple[str, dict[str, Any]]] = []
        if not self.state_path.exists():
            self._finalize_native_attempt_receipt(receipt)
            return {
                "dispatch_id": receipt["dispatch_id"],
                "members": [],
                "state": "ignored",
                "verification": None,
            }
        with self._coordinated_state() as state:
            dispatch = state["dispatches"].get(receipt["dispatch_id"])
            if not isinstance(dispatch, dict) or not self._native_receipt_is_current(
                state,
                dispatch,
                receipt,
            ):
                result = {
                    "dispatch_id": receipt["dispatch_id"],
                    "members": [],
                    "state": "ignored",
                    "verification": None,
                }
            elif dispatch.get("state") in {
                "paused",
                "ready_to_apply",
                "fenced",
                "rejected",
                "retired",
            }:
                receipts = self._detach_terminal_attempt_receipts_locked(
                    state, [dispatch]
                )
                self._write_state(state)
                result = {
                    "dispatch_id": dispatch["dispatch_id"],
                    "members": dispatch["members"],
                    "state": dispatch["state"],
                    "verification": None,
                }
            else:
                if dispatch.get("owner") is None:
                    if not _owner_matches_task(owner, dispatch.get("task_name")):
                        raise ControlPlaneError("result owner does not match its native receipt")
                    dispatch["owner"] = owner
                elif dispatch.get("owner") != owner:
                    raise ControlPlaneError("result owner does not match its native receipt")
                receipts = self._fence_cooperative_members_locked(state, dispatch, reason)
                self._write_state(state)
                result = {
                    "dispatch_id": dispatch["dispatch_id"],
                    "members": dispatch["members"],
                    "state": "fenced",
                    "verification": None,
                }
        if not receipts:
            receipts = [("native", dict(receipt))]
        deduplicated = {
            item["event_id"]: (kind, item) for kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))
        return result

    def _replay_native_attempt_receipt(self, receipt: Mapping[str, Any]) -> Any:
        """Resume only a durable observation; reservations keep their slot."""

        phase = receipt.get("phase")
        if phase == "acknowledged":
            self._clear_pending_event(receipt)
            return None
        if phase not in {"native_observed", "result_observed"}:
            return None
        current = False
        if self.state_path.exists():
            with self._coordinated_state() as state:
                dispatch = state["dispatches"].get(receipt.get("dispatch_id"))
                current = isinstance(dispatch, Mapping) and self._native_receipt_is_current(
                    state,
                    dispatch,
                    receipt,
                )
        if not current:
            self._finalize_native_attempt_receipt(receipt)
            return None
        if phase == "native_observed":
            observation = receipt.get("observation")
            if not isinstance(observation, Mapping):
                raise ControlPlaneError("native receipt observation is invalid")
            event = {
                "dispatch_id": receipt["dispatch_id"],
                "kind": "native_attempt_observation",
                "owners": list(observation["owners"]),
                "tool_input_sha256": receipt["tool_input_sha256"],
                "tool_use_id": receipt["tool_use_id"],
            }
            return self._observe_native_attempt(event)
        owner = receipt.get("owner")
        observation = receipt.get("observation")
        if not isinstance(owner, str) or not isinstance(observation, Mapping):
            raise ControlPlaneError("result receipt owner is invalid")
        if observation.get("kind") == "invalid_result":
            return self._fence_result_receipt(owner, receipt, "invalid_result")
        result = observation.get("result")
        if not isinstance(result, Mapping):
            return self._fence_result_receipt(owner, receipt, "invalid_result")
        try:
            return self.record_result(
                owner,
                RESULT_HEADER + "\n" + canonical_bytes(dict(result)).decode("utf-8"),
            )
        except ControlPlaneUnavailable:
            raise
        except ControlPlaneError:
            return self._fence_result_receipt(owner, receipt, "invalid_result")

    def _replay_interrupt_attempt_receipt(self, receipt: Mapping[str, Any]) -> Any:
        if receipt.get("phase") == "acknowledged":
            self._clear_pending_event(receipt)
            return None
        if receipt.get("phase") != "observed":
            return None
        return self._observe_interrupt_attempt(
            {
                "kind": "interrupt_attempt_observation",
                "target": receipt["owner"],
                "previous_status": receipt["previous_status"],
                "tool_use_id": receipt["tool_use_id"],
            }
        )

    def _process_pending_event(self, event: Mapping[str, Any]) -> Any:
        """Durably observe, settle, acknowledge, and finalize one receipt."""

        path = self._stage_pending_event(event)
        current = self._read_pending_event(path)
        if current["phase"] == "acknowledged":
            self._clear_pending_event(current)
            return None
        settled = self._settle_pending_event(current)
        acknowledged = self._ack_pending_event(current)
        self._clear_pending_event(acknowledged)
        return settled

    def replay_pending_events(self, *, all_sessions: bool = False) -> int:
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            paths = _pending_event_paths(self.root)
        if not all_sessions:
            prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
            paths = [path for path in paths if path.name.startswith(prefix)]
        events: list[tuple[Path, dict[str, Any]]] = []
        for path in paths:
            event = self._validate_pending_event(
                _decode_object(
                    _read_bounded_bytes(
                        path,
                        "pending lifecycle event",
                        limit=MAX_PENDING_EVENT_BYTES,
                    ),
                    "pending lifecycle event",
                )
            )
            match = PENDING_EVENT_FILE_RE.fullmatch(path.name)
            if match is None or (
                match.group("session") != _session_digest(event["session_id"])
                or match.group("event") != event["event_id"][7:]
            ):
                raise ControlPlaneError(
                    "pending lifecycle event filename does not match its payload"
                )
            events.append((path, event))
        priority = {
            # A restart receipt is a durable host decision to fence the
            # current lease.  It must win over a result that happened to be
            # observed just before the process crashed, otherwise replay can
            # retire the lease the restart was meant to fence.
            "session_restart": 0,
            "result_observed": 1,
            "native_attempt": 2,
            "interrupt_attempt": 3,
        }
        events.sort(
            key=lambda item: (
                priority[
                    "result_observed"
                    if item[1]["kind"] == "native_attempt"
                    and item[1].get("phase") == "result_observed"
                    else item[1]["kind"]
                ],
                item[0].name,
            )
        )
        replayed = 0
        for _path, event in events:
            control = ControlPlane(
                event["session_id"],
                root=self.root,
                lock_timeout=self.lock_timeout,
            )
            if event["kind"] in {"native_attempt", "interrupt_attempt"} and event.get(
                "phase"
            ) == "reserved":
                control._reconcile_attempt_reservations()
            elif event["kind"] == "native_attempt":
                control._replay_native_attempt_receipt(event)
            elif event["kind"] == "interrupt_attempt":
                control._replay_interrupt_attempt_receipt(event)
            elif event["phase"] != "acknowledged":
                control._settle_pending_event(event)
                event = control._ack_pending_event(event)
            if event["kind"] not in {"native_attempt", "interrupt_attempt"}:
                control._clear_pending_event(event)
            replayed += 1
        return replayed

    def _settle_pending_event(self, event: Mapping[str, Any]) -> Any:
        normalized = self._validate_pending_event(
            event,
            expected_session=self.session_id,
        )
        if normalized["kind"] == "session_restart":
            return self._settle_restart_event(normalized)
        raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)

    def _resolve_state_path(self) -> Path:
        if self._state_path is not None and self._state_path.exists():
            return self._state_path
        self._state_path = None
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            matches, predecessor = _session_state_paths(self.root, self.session_id)
        if predecessor:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        if len(matches) > 1:
            raise ControlPlaneError("current task has multiple lifecycle state files")
        self._state_path = (
            matches[0]
            if matches
            else self.root / f".cco-uninitialized-s{_session_digest(self.session_id)}.json"
        )
        return self._state_path

    @property
    def state_path(self) -> Path:
        """Resolve this task's state without parsing another task's recovery."""

        return self._resolve_state_path()

    @staticmethod
    def _validate_lifecycle_state(
        state: Mapping[str, Any],
        *,
        expected_session: str | None = None,
    ) -> dict[str, Any]:
        normalized = dict(state)
        if normalized.get("protocol") != LIFECYCLE_PROTOCOL:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        if {"lineage_id", "parent_state_sha256"} & set(normalized):
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        raw_dispatches = normalized.get("dispatches")
        logical_value = normalized.get("logical")
        if (
            (isinstance(raw_dispatches, Mapping) and any(
                isinstance(dispatch, Mapping)
                and dispatch.get("state") == "interrupting"
                for dispatch in raw_dispatches.values()
            ))
            or (
                isinstance(logical_value, Mapping)
                and any(
                    isinstance(item, Mapping) and item.get("state") == "interrupting"
                    for item in logical_value.values()
                )
            )
        ):
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        session = normalized.get("session_id")
        if not isinstance(session, str) or SESSION_RE.fullmatch(session) is None:
            raise ControlPlaneError("lifecycle state session is invalid")
        if expected_session is not None and session != expected_session:
            raise ControlPlaneError("lifecycle state session does not match")
        if not isinstance(normalized.get("workspace_root"), str):
            raise ControlPlaneError("lifecycle workspace root is invalid")
        plan_id = normalized.get("plan_id")
        revision = normalized.get("revision")
        if not isinstance(plan_id, str) or SHA256_RE.fullmatch(plan_id) is None:
            raise ControlPlaneError("lifecycle plan identity is invalid")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ControlPlaneError("lifecycle revision is invalid")
        dispatches = normalized.get("dispatches")
        if not isinstance(dispatches, Mapping):
            raise ControlPlaneError("lifecycle dispatch collection is invalid")
        for dispatch_id, dispatch in dispatches.items():
            if (
                not isinstance(dispatch_id, str)
                or SHA256_RE.fullmatch(dispatch_id) is None
                or not isinstance(dispatch, Mapping)
                or dispatch.get("dispatch_id") != dispatch_id
                or dispatch.get("state") not in DISPATCH_STATES
                or dispatch.get("role") not in ROLES
            ):
                raise ControlPlaneError("lifecycle dispatch record is invalid")
            if isinstance(dispatch, dict):
                required_current_fields = {
                    "context_turns",
                    "fallback_from_owner",
                    "interrupt_unresolved",
                    "isolation",
                    "last_transient_failure",
                    "pending_cursor",
                    "receipt_id",
                    "reused_from",
                    "interrupt_receipt_id",
                    "interrupt_tool_use_id",
                    "interrupt_claim_expires_at",
                }
                if not required_current_fields <= set(dispatch):
                    raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
                receipt_id = dispatch["receipt_id"]
                if receipt_id is not None and (
                    not isinstance(receipt_id, str)
                    or SHA256_RE.fullmatch(receipt_id) is None
                ):
                    raise ControlPlaneError("lifecycle dispatch receipt identity is invalid")
                interrupt_receipt_id = dispatch["interrupt_receipt_id"]
                if interrupt_receipt_id is not None and (
                    not isinstance(interrupt_receipt_id, str)
                    or SHA256_RE.fullmatch(interrupt_receipt_id) is None
                ):
                    raise ControlPlaneError(
                        "lifecycle interrupt receipt identity is invalid"
                    )
                interrupt_tool_use_id = dispatch["interrupt_tool_use_id"]
                if interrupt_tool_use_id is not None and (
                    not isinstance(interrupt_tool_use_id, str)
                    or not interrupt_tool_use_id
                ):
                    raise ControlPlaneError(
                        "lifecycle interrupt call identity is invalid"
                    )
                interrupt_expires_at = dispatch["interrupt_claim_expires_at"]
                if interrupt_expires_at is not None and (
                    isinstance(interrupt_expires_at, bool)
                    or not isinstance(interrupt_expires_at, int)
                    or interrupt_expires_at < 0
                ):
                    raise ControlPlaneError(
                        "lifecycle interrupt claim expiry is invalid"
                    )
                if interrupt_receipt_id is None and (
                    interrupt_tool_use_id is not None
                    or interrupt_expires_at is not None
                ):
                    raise ControlPlaneError(
                        "lifecycle interrupt reservation is incomplete"
                    )
        logical = normalized.get("logical")
        if not isinstance(logical, Mapping) or any(
            not isinstance(item, Mapping) or item.get("state") not in LOGICAL_STATES
            for item in logical.values()
        ):
            raise ControlPlaneError("lifecycle logical state is invalid")
        if "settled_events" in normalized:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        tombstones = normalized.get("tombstones")
        if (
            not isinstance(tombstones, list)
            or len(tombstones) > MAX_TOMBSTONES
            or any(
                not isinstance(item, Mapping)
                or set(item)
                - {
                    "cursor",
                    "dispatch_id",
                    "owner",
                    "reason",
                    "tool_input_sha256",
                    "tool_use_id",
                }
                or not isinstance(item.get("cursor"), int)
                or isinstance(item.get("cursor"), bool)
                or item["cursor"] < 0
                or not isinstance(item.get("dispatch_id"), str)
                or SHA256_RE.fullmatch(item["dispatch_id"]) is None
                or not isinstance(item.get("reason"), str)
                or not item["reason"]
                or (
                    item.get("owner") is not None
                    and (
                        not isinstance(item.get("owner"), str)
                        or TASK_PATH_RE.fullmatch(item["owner"]) is None
                    )
                )
                or (
                    item.get("tool_input_sha256") is not None
                    and (
                        not isinstance(item.get("tool_input_sha256"), str)
                        or SHA256_RE.fullmatch(item["tool_input_sha256"]) is None
                    )
                )
                or (
                    item.get("tool_input_sha256") is not None
                    and item.get("tool_use_id") is None
                )
                or (
                    item.get("tool_use_id") is not None
                    and (
                        not _tool_use_id_valid(item.get("tool_use_id"))
                    )
                )
                for item in tombstones
            )
        ):
            raise ControlPlaneError("lifecycle tombstone collection is invalid")
        # Isolate records are derivable from cooperative dispatches. Retaining
        # a duplicate mirror risks cleanup using stale roots after recovery.
        if "cooperative_isolates" in normalized:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        cooperative_preparing = normalized.get("cooperative_preparing")
        if cooperative_preparing is not None:
            normalized["cooperative_preparing"] = _validate_cooperative_preparing(
                cooperative_preparing,
                plan_id=plan_id,
            )
        cooperative_journal = normalized.get("cooperative_journal")
        if cooperative_journal is not None:
            if not isinstance(cooperative_journal, Mapping):
                raise ControlPlaneError("cooperative lifecycle journal is invalid")
            try:
                if len(canonical_bytes(dict(cooperative_journal))) > MAX_COOPERATIVE_JOURNAL_LIFECYCLE_BYTES:
                    raise ControlPlaneError("cooperative lifecycle journal exceeds its capacity")
            except ProtocolHashError as error:
                raise ControlPlaneError("cooperative lifecycle journal is invalid") from error
        return normalized

    def _workspace_hint(self) -> str:
        state = self._validate_lifecycle_state(
            _load_object(self.state_path, "cco.v9 lifecycle state"),
            expected_session=self.session_id,
        )
        return str(state["workspace_root"])

    @contextmanager
    def _coordinated_state(
        self,
        workspace_root: object | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Load state while its workspace, session, and publication stay stable."""

        workspace = self._workspace_hint() if workspace_root is None else workspace_root
        workspace_key = _workspace_key(workspace)
        deadline = time.monotonic() + self.lock_timeout

        def remaining() -> float:
            local = max(0.0, deadline - time.monotonic())
            operation = remaining_seconds()
            return local if operation is None else min(local, operation)

        with acquire(
            self.root,
            _workspace_lock_identity(workspace),
            timeout=remaining(),
        ):
            with acquire(self.root, self.session_id, timeout=remaining()):
                with acquire(
                    self.root,
                    STATE_ROOT_LOCK,
                    timeout=remaining(),
                ):
                    # Serialize the final path rescan and state load with recovery
                    # publication, then release the root-wide lock before any
                    # workspace-local computation or persistence.
                    self._state_path = None
                    state = self._read_state(expected_workspace=workspace_key)
                yield state

    def _reconcile_expired_claims(
        self,
        state: dict[str, Any],
        *,
        now: int | None = None,
    ) -> tuple[bool, list[tuple[str, dict[str, Any]]]]:
        """Unlink only expired pre-admission native reservations."""

        changed = False
        released: list[tuple[str, dict[str, Any]]] = []
        current = _now_milliseconds() if now is None else now
        for dispatch in state["dispatches"].values():
            if dispatch.get("state") == "starting" and not _native_claim_active(
                dispatch, now=current
            ):
                receipt = self._native_attempt_for_dispatch(dispatch)
                if (
                    receipt is not None
                    and receipt.get("phase") == "reserved"
                ):
                    released.append(("native", receipt))
                dispatch["tool_use_id"] = None
                dispatch["claim_expires_at"] = None
                dispatch["receipt_id"] = None
                if dispatch.get("tool_kind") == "continuation":
                    dispatch["state"] = "paused"
                    for member in dispatch["members"]:
                        state["logical"][member]["state"] = "paused"
                changed = True
            # Expiry cannot prove whether the host executed an interrupt.
            # Detach the stale reservation, but fence owner reuse until an
            # explicit restart/result establishes a safe terminal outcome.
            interrupt_deadline = dispatch.get("interrupt_claim_expires_at")
            if (
                dispatch.get("state") in ACTIVE_STATES
                and isinstance(dispatch.get("interrupt_receipt_id"), str)
                and isinstance(dispatch.get("interrupt_tool_use_id"), str)
                and isinstance(interrupt_deadline, int)
                and not isinstance(interrupt_deadline, bool)
                and interrupt_deadline <= current
            ):
                interrupt_receipt = self._interrupt_attempt_for_dispatch(dispatch)
                dispatch["interrupt_receipt_id"] = None
                dispatch["interrupt_tool_use_id"] = None
                dispatch["interrupt_claim_expires_at"] = None
                dispatch["interrupt_unresolved"] = True
                if interrupt_receipt is not None:
                    released.append(("finalize_interrupt", interrupt_receipt))
                changed = True
            if (
                dispatch.get("state") in {"retired", "fenced", "rejected"}
                and dispatch.get("interrupt_receipt_id") is not None
            ):
                interrupt_receipt = self._seal_terminal_interrupt_attempt(dispatch)
                if interrupt_receipt is not None:
                    released.append(("finalize_interrupt", interrupt_receipt))
                changed = True

        return changed, released

    def _discard_reserved_attempt_receipts(
        self,
        receipts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for kind, receipt in receipts:
            if kind == "native":
                self._discard_reserved_native_attempt_receipt(receipt)
            elif kind == "interrupt":
                self._discard_reserved_interrupt_attempt_receipt(receipt)
            else:
                self._finalize_interrupt_attempt_receipt(receipt)

    def _discard_unlinked_reserved_attempt_receipts(
        self,
        state: Mapping[str, Any],
    ) -> int:
        """Release reservations that crashed before they were state-linked."""

        linked = {
            receipt_id
            for dispatch in state.get("dispatches", {}).values()
            if isinstance(dispatch, Mapping)
            for receipt_id in (
                dispatch.get("receipt_id"),
                dispatch.get("interrupt_receipt_id"),
            )
            if isinstance(receipt_id, str)
        }
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        removed = 0
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if (
                    receipt.get("kind") not in {"native_attempt", "interrupt_attempt"}
                    or receipt.get("phase") != "reserved"
                    or receipt.get("event_id") in linked
                ):
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise ControlPlaneUnavailable(
                        "orphaned lifecycle receipt cleanup failed"
                    ) from error
                _sync_directory(path.parent)
                removed += 1
        return removed

    @staticmethod
    def _begin_native_claim(
        state: dict[str, Any],
        dispatch: dict[str, Any],
        tool_use_id: str,
    ) -> None:
        dispatch["state"] = "starting"
        dispatch["tool_use_id"] = tool_use_id
        dispatch["claim_expires_at"] = (
            _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
        )
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "starting"

    def _rollback_native_claim(self, dispatch_id: str, tool_use_id: str) -> None:
        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, dispatch_id)
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_use_id") != tool_use_id
            ):
                return
            receipt = self._native_attempt_for_dispatch(dispatch)
            dispatch["tool_use_id"] = None
            dispatch["receipt_id"] = None
            if dispatch.get("tool_kind") == "continuation":
                dispatch["state"] = "paused"
                dispatch["claim_expires_at"] = None
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "paused"
            else:
                dispatch["claim_expires_at"] = (
                    _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                )
            self._write_state(state)
            if receipt is not None:
                self._discard_reserved_native_attempt_receipt(receipt)

    def _discard_stale_unstarted_wave(self, dispatch_id: str, tool_use_id: str) -> bool:
        """Discard a baseline that never reached a native child and can be recaptured."""

        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, dispatch_id)
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_kind") not in {"spawn", "reuse"}
                or dispatch.get("tool_use_id") != tool_use_id
            ):
                return False
            wave_id = dispatch["wave_id"]
            wave_records = [
                item
                for item in state["dispatches"].values()
                if item.get("wave_id") == wave_id
            ]
            rebuildable = bool(wave_records) and all(
                (item.get("state") == "rejected" and item.get("owner") is None)
                or (
                    item.get("state") == "starting"
                    and item.get("tool_kind") in {"spawn", "reuse"}
                    and (
                        item.get("tool_use_id") is None
                        or item.get("dispatch_id") == dispatch_id
                    )
                    and (
                        item.get("owner") is None
                        if item.get("tool_kind") == "spawn"
                        else isinstance(item.get("owner"), str)
                        and isinstance(item.get("reused_from"), str)
                    )
                )
                for item in wave_records
            )
            if not rebuildable:
                self._fence_members(state, dispatch, "workspace_baseline_stale")
                self._settle_wave(state)
                self._write_state(state)
                return False
            plan = self._read_plan(state)
            abandoned_receipts: list[dict[str, Any]] = []
            for item in wave_records:
                if item.get("state") == "rejected":
                    continue
                receipt = self._native_attempt_for_dispatch(item)
                if receipt is not None:
                    abandoned_receipts.append(receipt)
                self._append_tombstone(state, item, "workspace_baseline_recaptured")
                for member in item["members"]:
                    logical = state["logical"][member]
                    logical["dispatch_id"] = None
                    logical["result"] = None
                    logical["state"] = "waiting"
                del state["dispatches"][item["dispatch_id"]]
            state["active_wave_id"] = None
            self._refresh_ready(state, plan)
            self._write_state(state)
            for receipt in abandoned_receipts:
                self._discard_reserved_native_attempt_receipt(receipt)
            return True

    def _workspace_state_candidates(
        self,
        workspace_root: object,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Load only current indexed state for the canonical workspace."""

        workspace_digest = _workspace_digest(workspace_root)
        snapshot = _state_json_paths(self.root)
        indexed = [
            path
            for path in snapshot
            if STATE_FILE_RE.fullmatch(path.name).group("workspace") == workspace_digest
        ]
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in indexed:
            checkpoint()
            match = STATE_FILE_RE.fullmatch(path.name)
            assert match is not None
            raw_state = _load_object(path, "cco.v9 lifecycle state")
            state = self._validate_lifecycle_state(raw_state)
            if (
                match.group("workspace") != _workspace_digest(state["workspace_root"])
                or match.group("session") != _session_digest(state["session_id"])
            ):
                raise ControlPlaneError("indexed lifecycle filename does not match its state")
            candidates.append((path, state))
        return candidates

    def _assert_cross_task_compatible(
        self,
        workspace_root: object,
        *,
        role: str,
        scopes: list[Mapping[str, str]],
        current_dispatch: str | None = None,
        cooperative_wave_id: str | None = None,
        cooperative_preparing_batch_id: str | None = None,
    ) -> None:
        target = _workspace_key(workspace_root)
        now = _now_milliseconds()
        for _path, state in self._workspace_state_candidates(workspace_root):
            checkpoint()
            if _workspace_key(state["workspace_root"]) != target:
                continue
            preparing = state.get("cooperative_preparing")
            if preparing is not None:
                reservation = _validate_cooperative_preparing(
                    preparing,
                    plan_id=str(state["plan_id"]),
                )
                same_preparation = (
                    state["session_id"] == self.session_id
                    and reservation["batch_id"] == cooperative_preparing_batch_id
                )
                if not same_preparation:
                    raise ControlPlaneError(
                        "workspace cooperative preparation is already reserved by "
                        f"{state['session_id']}:{reservation['batch_id']}"
                    )
            for dispatch in state["dispatches"].values():
                if state["session_id"] == self.session_id and dispatch.get(
                    "dispatch_id"
                ) == current_dispatch:
                    continue
                if (
                    cooperative_wave_id is not None
                    and state["session_id"] == self.session_id
                    and dispatch.get("wave_id") == cooperative_wave_id
                    and _is_cooperative_dispatch(dispatch)
                ):
                    continue
                # A cooperative group holds one workspace-wide writer lease for
                # its whole lifecycle, including ready-to-apply. Do not reduce
                # that lease to ordinary overlap checks for another task.
                if _is_cooperative_dispatch(dispatch) and dispatch.get("state") not in {
                    "retired",
                    "fenced",
                    "rejected",
                }:
                    raise ControlPlaneError(
                        "workspace cooperative writer batch is already active: "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )
                if role == "worker" and _writer_lease_active(dispatch, now=now):
                    raise ControlPlaneError(
                        "workspace writer lease is already held by "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )
                if (
                    role == "worker"
                    and _reader_active(dispatch, now=now)
                    and _scopes_overlap(scopes, dispatch["scopes"])
                ):
                    raise ControlPlaneError(
                        "workspace has an overlapping reader held by "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )
                if (
                    role != "worker"
                    and _writer_lease_active(dispatch, now=now)
                    and _scopes_overlap(scopes, dispatch["scopes"])
                ):
                    raise ControlPlaneError(
                        "workspace writer overlaps this reader: "
                        f"{state['session_id']}:{dispatch['dispatch_id']}"
                    )

    def _has_live_cross_task_work(self, workspace_root: object) -> bool:
        """Cooperative batches never join a workspace already owned by a task."""

        target = _workspace_key(workspace_root)
        now = _now_milliseconds()
        for _path, state in self._workspace_state_candidates(workspace_root):
            if state["session_id"] == self.session_id or _workspace_key(
                state["workspace_root"]
            ) != target:
                continue
            if state.get("cooperative_preparing") is not None:
                return True
            if any(
                _is_cooperative_dispatch(dispatch)
                and dispatch.get("state") not in {"retired", "fenced", "rejected"}
                or _writer_lease_active(dispatch, now=now)
                or _reader_active(dispatch, now=now)
                for dispatch in state["dispatches"].values()
            ):
                return True
        return False

    def _artifact_path(self, kind: str, identity: str) -> Path:
        if kind not in {"plan", "wave"} or SHA256_RE.fullmatch(identity) is None:
            raise ControlPlaneError("artifact identity is invalid")
        return self.root / "artifacts" / f"{self.session_id}-{kind}-{identity[7:]}.json"

    def _owned_artifact_paths(self, kind: str) -> list[Path]:
        if kind not in {"plan", "wave"}:
            raise ControlPlaneError("artifact kind is invalid")
        artifacts = self.root / "artifacts"
        if not artifacts.is_dir():
            return []
        prefix = f"{self.session_id}-{kind}-"
        owned: list[Path] = []
        try:
            with os.scandir(artifacts) as entries:
                for entry in entries:
                    checkpoint()
                    if not entry.name.startswith(prefix) or not entry.name.endswith(".json"):
                        continue
                    identity = entry.name[len(prefix) : -5]
                    if len(identity) == 64 and all(
                        character in "0123456789abcdef" for character in identity
                    ):
                        owned.append(Path(entry.path))
        except OSError as error:
            raise ControlPlaneUnavailable("artifact directory is unavailable") from error
        owned.sort(key=lambda item: item.name)
        return owned

    def _read_state(
        self,
        *,
        expected_workspace: object | None = None,
    ) -> dict[str, Any]:
        source = self._resolve_state_path()
        raw_state = _load_object(source, "cco.v9 lifecycle state")
        state = self._validate_lifecycle_state(
            raw_state,
            expected_session=self.session_id,
        )
        if expected_workspace is not None and _workspace_key(
            state["workspace_root"]
        ) != _workspace_key(expected_workspace):
            raise ControlPlaneError("lifecycle workspace changed during coordination")
        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        if STATE_FILE_RE.fullmatch(source.name) is not None and source != canonical:
            raise ControlPlaneError("indexed lifecycle filename does not match its state")
        if source != canonical:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        if state != raw_state:
            self._write_state(state)
        return state

    def _cleanup_superseded_artifacts_best_effort(
        self,
        state: Mapping[str, Any],
    ) -> None:
        """Remove disposable artifacts without weakening a committed state write.

        Lifecycle state is the only authoritative publication.  Immutable plan
        and wave artifacts are cache-like conveniences: a cleanup failure after
        ``os.replace`` must never make callers believe that the state write
        failed and roll back a receipt that is now durably linked by state.
        """

        try:
            artifacts = self.root / "artifacts"
            if not artifacts.is_dir():
                return
            active_wave = state.get("active_wave_id")
            keep_wave = (
                self._artifact_path("wave", active_wave)
                if isinstance(active_wave, str)
                else None
            )
            for path in self._owned_artifact_paths("wave"):
                if keep_wave is None or path != keep_wave:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            keep_plan = Path(str(state["plan_path"]))
            for path in self._owned_artifact_paths("plan"):
                if path != keep_plan:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            # This runs strictly after the authoritative atomic replacement.
            # Artifact cleanup has no durable accounting role and is retried by
            # later normal writes or explicit cleanup.
            return

    def _write_state(self, state: dict[str, Any]) -> None:
        """Atomically publish authoritative state, then clean artifacts best-effort."""

        self._mark_state_root_if_safe()
        canonical = _lifecycle_state_path(
            self.root,
            state["workspace_root"],
            self.session_id,
        )
        target = canonical
        if target.exists():
            current_raw = _read_bounded_bytes(target, "current cco.v9 lifecycle state")
            current = self._validate_lifecycle_state(
                _decode_object(current_raw, "current cco.v9 lifecycle state"),
                expected_session=self.session_id,
            )
            if current["revision"] != state.get("revision"):
                raise ControlPlaneError("lifecycle state changed before persistence")
        previous_revision = state["revision"]
        state["revision"] = int(previous_revision) + 1
        try:
            _atomic_write(target, state)
        except _AtomicWriteUncertain:
            # ``os.replace`` completed, so this process must not mutate its
            # in-memory state back to the old revision.  A duplicate Hook or
            # restart will read the authoritative file/receipt and settle it.
            self._state_path = target
            raise
        except Exception:
            state["revision"] = previous_revision
            raise
        self._state_path = target
        self._cleanup_superseded_artifacts_best_effort(state)

    def _read_plan(self, state: Mapping[str, Any]) -> dict[str, Any]:
        plan = _load_object(Path(state["plan_path"]), "cco.v9 plan artifact")
        if plan.get("protocol") != PLAN_PROTOCOL or plan.get("plan_id") != state.get("plan_id"):
            raise ControlPlaneError("plan artifact identity is invalid")
        unsigned = {key: value for key, value in plan.items() if key != "plan_id"}
        if _digest(b"cco.plan.v1\0", unsigned) != plan["plan_id"]:
            raise ControlPlaneError("plan artifact digest is invalid")
        return plan

    def _read_wave(self, state: Mapping[str, Any]) -> dict[str, Any]:
        wave_id = state.get("active_wave_id")
        if not isinstance(wave_id, str):
            raise ControlPlaneError("there is no active wave")
        wave = _load_object(self._artifact_path("wave", wave_id), "cco.v9 wave artifact")
        protocol = wave.get("protocol")
        if protocol != WAVE_PROTOCOL:
            raise ControlPlaneError(BREAKING_UPGRADE_MESSAGE)
        if wave.get("wave_id") != wave_id:
            raise ControlPlaneError("wave artifact identity is invalid")
        units = wave.get("units")
        if not isinstance(units, list) or not units:
            raise ControlPlaneError("wave artifact has no logical units")
        identity = {
            key: wave.get(key)
            for key in ("plan_id", "protocol", "sequence", "units")
        }
        if wave.get("plan_id") != state.get("plan_id"):
            raise ControlPlaneError("wave artifact digest is invalid")
        cooperative_units = [
            unit
            for unit in units
            if isinstance(unit, Mapping)
            and isinstance(unit.get("isolation"), Mapping)
            and unit["isolation"].get("mode") == COOPERATIVE
        ]
        if cooperative_units:
            canonical_baseline = wave.get("canonical_baseline")
            isolate_snapshots = wave.get("isolate_snapshots")
            if (
                "baselines" in wave
                or "isolate_baselines" in wave
                or not isinstance(canonical_baseline, Mapping)
                or not isinstance(isolate_snapshots, Mapping)
                or not _cooperative_group_size_valid(len(cooperative_units))
                or set(isolate_snapshots) != {str(unit.get("id")) for unit in cooperative_units}
                or not _cooperative_units_disjoint(cooperative_units)
            ):
                raise ControlPlaneError("cooperative wave canonical baseline is invalid")
            try:
                union_scopes = _cooperative_union_scopes(cooperative_units)
            except ControlPlaneError:
                raise
            try:
                canonical_snapshot = validate_workspace_baseline(canonical_baseline)
            except WorkspaceGuardError as error:
                raise ControlPlaneError(
                    "cooperative wave canonical baseline is invalid: " + str(error)
                ) from error
            if (
                canonical_snapshot["scopes"] != union_scopes
                or canonical_snapshot["writable"] is not True
            ):
                raise ControlPlaneError(
                    "cooperative wave canonical baseline does not cover its union scopes"
                )
            bound_isolate_snapshots: dict[str, Mapping[str, Any]] = {}
            for unit in cooperative_units:
                isolation = unit["isolation"]
                record = isolation.get("record")
                try:
                    bound = validate_isolation_record(record, self.root)
                except WriterIsolationError as error:
                    raise ControlPlaneError(str(error)) from error
                source = isolate_snapshots.get(str(unit["id"]))
                try:
                    source_baseline = validate_workspace_baseline(source)
                except WorkspaceGuardError as error:
                    raise ControlPlaneError(
                        "cooperative wave isolate snapshot is invalid: " + str(error)
                    ) from error
                if (
                    unit.get("baseline_id") != canonical_snapshot.get("state_id")
                    or bound["scopes"] != unit.get("scopes")
                    or _workspace_key(canonical_snapshot["root"])
                    != _workspace_key(bound["recovery"]["canonical_root"])
                    or _workspace_key(source_baseline["root"])
                    != _workspace_key(bound["isolate_root"])
                    or source_baseline["scopes"] != bound["scopes"]
                    or source_baseline["writable"] is not True
                ):
                    raise ControlPlaneError(
                        "cooperative wave isolate record does not match its baseline"
                    )
                bound_isolate_snapshots[str(unit["id"])] = source_baseline
            cooperative_identity = {
                **identity,
                **_cooperative_snapshot_digest_fields(
                    canonical_snapshot,
                    bound_isolate_snapshots,
                ),
            }
            if _digest(b"cco.wave.v3\0", cooperative_identity) != wave_id:
                raise ControlPlaneError("wave artifact digest is invalid")
        else:
            if (
                "canonical_baseline" in wave
                or "isolate_baselines" in wave
                or "isolate_snapshots" in wave
            ):
                raise ControlPlaneError("serial wave has unexpected isolate baselines")
            baselines = wave.get("baselines")
            if not isinstance(baselines, Mapping):
                raise ControlPlaneError("wave logical baselines are invalid")
            expected_ids = {
                unit.get("id")
                for unit in units
                if isinstance(unit, Mapping) and isinstance(unit.get("id"), str)
            }
            if set(baselines) != expected_ids:
                raise ControlPlaneError("wave logical baselines do not match its units")
            for unit in units:
                unit_id = unit.get("id") if isinstance(unit, Mapping) else None
                baseline = baselines.get(unit_id) if isinstance(unit_id, str) else None
                if (
                    not isinstance(unit, Mapping)
                    or not isinstance(unit.get("id"), str)
                    or not isinstance(unit.get("scopes"), list)
                    or not isinstance(baseline, Mapping)
                    or baseline.get("state_id") != unit.get("baseline_id")
                    or baseline.get("scopes") != unit["scopes"]
                ):
                    raise ControlPlaneError(
                        "logical unit baseline does not match its dispatch scope"
                    )
            if _digest(b"cco.wave.v3\0", identity) != wave_id:
                raise ControlPlaneError("wave artifact digest is invalid")
        return wave

    def create_plan(
        self,
        repo: Path,
        compiled_plan: object,
        *,
        resume_identical: bool = False,
    ) -> dict[str, Any]:
        backend, workspace = discover_workspace(repo)
        normalized = _normalize_plan(compiled_plan, workspace, backend)
        unsigned = {
            **normalized,
            "protocol": PLAN_PROTOCOL,
            "workspace_backend": backend,
            "workspace_root": str(workspace),
        }
        plan_id = _digest(b"cco.plan.v1\0", unsigned)
        plan = {**unsigned, "plan_id": plan_id}
        plan_path = self._artifact_path("plan", plan_id)
        if self.state_path.exists():
            with self._coordinated_state() as state:
                if (
                    resume_identical
                    and state.get("plan_id") == plan_id
                    and state.get("active_wave_id") is None
                    and state.get("wave_sequence") == 0
                    and not state.get("dispatches")
                    and all(
                        item.get("state") in {"waiting", "ready"}
                        for item in state["logical"].values()
                    )
                    and self._read_plan(state) == plan
                ):
                    return {
                        "plan_id": plan_id,
                        "protocol": PLAN_PROTOCOL,
                        "ready": sorted(
                            node
                            for node, item in state["logical"].items()
                            if item["state"] == "ready"
                        ),
                        "workspace_root": str(workspace),
                    }
                raise ControlPlaneError(
                    "the current task already has CCO lifecycle proof; "
                    "run explicit cleanup first"
                )
        with (
            acquire(
                self.root,
                _workspace_lock_identity(workspace),
                timeout=self.lock_timeout,
            ),
            acquire(self.root, self.session_id, timeout=self.lock_timeout),
        ):
            if self.state_path.exists():
                raise ControlPlaneError(
                    "the current task already has CCO lifecycle proof; run explicit cleanup first"
                )
            with acquire(
                self.root,
                STATE_ROOT_LOCK,
                timeout=_bounded_lock_timeout(self.lock_timeout),
            ):
                prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
                if any(
                    path.name.startswith(prefix)
                    for path in _pending_event_paths(self.root)
                ):
                    raise ControlPlaneError(
                        "CCO has an unsettled native lifecycle receipt; finish restart "
                        "recovery before starting a new task."
                    )
                if _state_capacity_used(_state_json_paths(self.root)) >= MAX_STATE_FILES:
                    raise ControlPlaneUnavailable(
                        "lifecycle state directory exceeds the "
                        f"{MAX_STATE_FILES} file limit"
                    )
                _write_immutable(plan_path, plan)
                logical = {
                    item["id"]: {
                        "assurance": item["assurance"],
                        "dispatch_id": None,
                        "generation": 1,
                        "result": None,
                        "state": "waiting",
                    }
                    for item in plan["nodes"]
                }
                state = {
                    "active_wave_id": None,
                    "dispatches": {},
                    "epoch": 1,
                    "logical": logical,
                    "plan_id": plan_id,
                    "plan_path": str(plan_path),
                    "protocol": LIFECYCLE_PROTOCOL,
                    "revision": 0,
                    "session_id": self.session_id,
                    "tombstones": [],
                    "wave_sequence": 0,
                    "workspace_root": str(workspace),
                }
                self._refresh_ready(state, plan)
                try:
                    self._write_state(state)
                except Exception:
                    if not self.state_path.exists():
                        plan_path.unlink(missing_ok=True)
                    raise
        return {
            "plan_id": plan_id,
            "protocol": PLAN_PROTOCOL,
            "ready": sorted(node for node, item in logical.items() if item["state"] == "ready"),
            "workspace_root": str(workspace),
        }

    @staticmethod
    def _logical_satisfied(
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        node_id: str,
    ) -> bool:
        logical = state["logical"][node_id]
        if logical["state"] != "retired":
            return False
        node = _node_map(plan)[node_id]
        return (
            node["role"] != "reviewer"
            or (logical.get("result") or {}).get("outcome") == "accept"
        )

    @classmethod
    def _refresh_ready(cls, state: dict[str, Any], plan: Mapping[str, Any]) -> None:
        nodes = _node_map(plan)
        changed = True
        while changed:
            changed = False
            for node_id, logical in state["logical"].items():
                if logical["state"] != "waiting":
                    continue
                if all(
                    cls._logical_satisfied(state, plan, dependency)
                    for dependency in nodes[node_id]["depends_on"]
                ):
                    logical["state"] = "ready"
                    changed = True

    @staticmethod
    def _overall_state(state: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
        logical = state["logical"]
        states = [item["state"] for item in logical.values()]
        if any(
            _native_claim_active(dispatch)
            or dispatch.get("state") in ACTIVE_STATES
            for dispatch in state["dispatches"].values()
        ):
            return "active"
        if any(item in {"ready", "starting"} for item in states):
            return "ready"
        if any(item in {"fenced", "waiting"} for item in states):
            return "blocked"
        if all(item == "retired" for item in states):
            nodes = _node_map(plan)
            rejected_review = any(
                nodes[node_id]["role"] == "reviewer"
                and (item.get("result") or {}).get("outcome") != "accept"
                for node_id, item in logical.items()
            )
            return "blocked" if rejected_review else "complete"
        return "ready"

    def _routes(
        self,
        plan: Mapping[str, Any],
        nodes: list[dict[str, Any]],
        native_catalog: object,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        requests = [
            {
                "assurance": node["assurance"],
                "constraints": node["pin"],
                "node": node["id"],
                "role": node["role"],
            }
            for node in nodes
        ]
        policy = load_route_policy(Path(plan["workspace_root"]))["policy"]
        errors: dict[str, str] = {}
        try:
            route_plan = resolve_route_plan(requests, native_catalog, policy=policy)
            return {item["node"]: item for item in route_plan["routes"]}, errors
        except RoutingCatalogError:
            routes: dict[str, dict[str, Any]] = {}
            for request in requests:
                try:
                    resolved = resolve_route_plan([request], native_catalog, policy=policy)
                    routes[request["node"]] = resolved["routes"][0]
                except RoutingCatalogError as error:
                    errors[request["node"]] = str(error)
            return routes, errors

    def _cooperative_writer_units(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        units: list[dict[str, Any]],
        *,
        capacity: int,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Select one bounded, maximal compatible set of ready writers.

        Cooperative isolation is intentionally still narrow: each member is a
        fresh standalone writer and the group is bounded by both native
        capacity and the writer-isolation module's explicit isolate limit.
        Unlike the original two-writer experiment, the plan may have more
        writers and a wave takes every compatible ready writer it can fit.
        """

        if plan.get("writer_isolation", "serial") != COOPERATIVE:
            return None, None
        limit = min(capacity, MAX_COOPERATIVE_WRITERS)
        if limit < 2:
            return None, "cooperative_capacity_below_two"
        # Retain a fenced/recovery candidate as lifecycle evidence instead of
        # overwriting its owned roots with a later experimental batch.
        if state.get("cooperative_preparing") is not None:
            return None, "cooperative_preparation_already_reserved"
        if self._cooperative_isolate_records(state):
            return None, "cooperative_isolates_still_owned"
        if self._has_live_cross_task_work(plan["workspace_root"]):
            return None, "cooperative_cross_task_work_is_active"

        candidates = [
            item
            for item in units
            if item.get("role") == "worker"
            and isinstance(item.get("members"), list)
            and len(item["members"]) == 1
            and item.get("context_turns") == 0
        ]
        if len(candidates) < 2:
            return None, "cooperative_requires_two_ready_standalone_writers"
        candidates.sort(key=lambda item: (-int(item["downstream_count"]), str(item["id"])))

        # The isolate limit is a small explicit constant.  This bounded DFS
        # finds a largest disjoint group without introducing a second scheduler
        # or arbitrarily privileging a conflicting high-priority writer.
        def choose(target: int) -> list[dict[str, Any]] | None:
            def visit(start: int, selected: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
                if len(selected) == target:
                    return selected
                if len(selected) + len(candidates) - start < target:
                    return None
                for index in range(start, len(candidates)):
                    candidate = candidates[index]
                    if all(
                        not _scopes_overlap(candidate["scopes"], prior["scopes"])
                        for prior in selected
                    ):
                        found = visit(index + 1, [*selected, candidate])
                        if found is not None:
                            return found
                return None

            return visit(0, [])

        for target in range(min(limit, len(candidates)), 1, -1):
            selected = choose(target)
            if selected is not None:
                return sorted(selected, key=lambda item: str(item["id"])), None
        return None, "cooperative_ready_writer_scopes_overlap"

    def _dependency_evidence(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        members: list[str],
    ) -> dict[str, Any]:
        nodes = _node_map(plan)
        dependencies = sorted({item for member in members for item in nodes[member]["depends_on"]})
        return {
            item: state["logical"][item]["result"]
            for item in dependencies
            if state["logical"][item]["result"] is not None
        }

    @staticmethod
    def _selected_dispatch_route(dispatch: Mapping[str, Any]) -> Mapping[str, Any] | None:
        candidates = dispatch.get("route_candidates")
        cursor = dispatch.get("route_cursor")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not isinstance(candidates, list)
            or not 0 <= cursor < len(candidates)
            or not isinstance(candidates[cursor], Mapping)
        ):
            return None
        return candidates[cursor]

    @classmethod
    def _source_matches_reuse(
        cls,
        source: Mapping[str, Any],
        *,
        dependency_dispatches: set[str],
        dependency_member: str,
        role: str,
        assurance: str,
        route: Mapping[str, Any],
        scopes: list[dict[str, str]],
    ) -> bool:
        result = source.get("result")
        selected = cls._selected_dispatch_route(source)
        owner = source.get("owner")
        return (
            len(dependency_dispatches) == 1
            and source.get("dispatch_id") in dependency_dispatches
            and source.get("state") == "retired"
            and source.get("role") == role
            and role in {"explorer", "worker"}
            and source.get("assurance") == assurance
            and source.get("context_turns") == 0
            and source.get("generation") == 1
            and isinstance(source.get("members"), list)
            and source["members"] == [dependency_member]
            and isinstance(owner, str)
            and TASK_PATH_RE.fullmatch(owner) is not None
            and source.get("transient_retries") == 0
            and source.get("last_transient_failure") is None
            and source.get("route_cursor") == 0
            and source.get("fallback_from_owner") is None
            and source.get("receipt_id") is None
            and source.get("tool_use_id") is None
            and source.get("claim_expires_at") is None
            and source.get("pending_cursor") is None
            and source.get("interrupt_receipt_id") is None
            and source.get("interrupt_tool_use_id") is None
            and source.get("interrupt_claim_expires_at") is None
            and source.get("interrupt_unresolved") is False
            and source.get("isolation") is None
            and isinstance(result, Mapping)
            and result.get("status") == "complete"
            and result.get("outcome") == "retire"
            and result.get("blockers") == []
            and result.get("deviations") == []
            and result.get("failure_signature") is None
            and isinstance(selected, Mapping)
            and dict(selected) == dict(route)
            and isinstance(source.get("scopes"), list)
            and source["scopes"] == scopes
        )

    @classmethod
    def _reuse_candidate(
        cls,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        unit: Mapping[str, Any],
        *,
        route_cursor: int,
        reserved_owners: set[str],
    ) -> Mapping[str, Any] | None:
        if (
            unit.get("role") == "reviewer"
            or unit.get("context_turns") != 0
            or not isinstance(unit.get("members"), list)
            or len(unit["members"]) != 1
        ):
            return None
        member = unit["members"][0]
        node = _node_map(plan)[member]
        if len(node["depends_on"]) != 1:
            return None
        dependency_dispatches = {
            state["logical"][dependency].get("dispatch_id")
            for dependency in node["depends_on"]
            if isinstance(state["logical"][dependency].get("dispatch_id"), str)
        }
        route = unit["route"]["candidates"][route_cursor]
        candidates: list[Mapping[str, Any]] = []
        for dispatch_id in sorted(dependency_dispatches):
            source = state["dispatches"].get(dispatch_id)
            if not isinstance(source, Mapping) or not cls._source_matches_reuse(
                source,
                dependency_dispatches=dependency_dispatches,
                dependency_member=node["depends_on"][0],
                role=unit["role"],
                assurance=unit["assurance"],
                route=route,
                scopes=unit["scopes"],
            ):
                continue
            owner = source["owner"]
            if owner in reserved_owners or any(
                other.get("dispatch_id") != source.get("dispatch_id")
                and other.get("owner") == owner
                and other.get("state") in {"starting", "running", "paused"}
                for other in state["dispatches"].values()
            ):
                continue
            candidates.append(source)
        return candidates[0] if len(candidates) == 1 else None

    def _dispatch_record(
        self,
        state: Mapping[str, Any],
        plan: Mapping[str, Any],
        wave_id: str,
        unit: Mapping[str, Any],
        *,
        route_cursor: int,
        reused_from: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        route = unit["route"]["candidates"][route_cursor]
        isolation = unit.get("isolation")
        if isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE:
            if reused_from is not None:
                raise ControlPlaneError("cooperative writer isolation cannot reuse an owner")
            record = isolation.get("record")
            if not isinstance(record, Mapping) or not isinstance(record.get("isolate_root"), str):
                raise ControlPlaneError("cooperative writer isolation has no isolate record")
        baseline_id = unit.get("baseline_id")
        if not isinstance(baseline_id, str) or SHA256_RE.fullmatch(baseline_id) is None:
            raise ControlPlaneError("logical unit baseline identity is invalid")
        generation = max(state["logical"][item]["generation"] for item in unit["members"])
        source_id = reused_from.get("dispatch_id") if reused_from is not None else None
        identity = {
            "baseline_id": baseline_id,
            "cursor": 0,
            "generation": generation,
            "members": unit["members"],
            "reused_from": source_id,
            "route": route,
            "route_cursor": route_cursor,
            "isolation": isolation,
            "wave_id": wave_id,
        }
        dispatch_id = _digest(b"cco.dispatch.v2\0", identity)
        task_name = _task_name(unit, route, generation, dispatch_id)
        message = _render_task(
            plan,
            unit,
            dispatch_id,
            cursor=0,
            dependency_evidence=self._dependency_evidence(state, plan, unit["members"]),
        )
        if reused_from is None:
            native = {
                "agent_type": WRITE_ROLE if unit["role"] == "worker" else READ_ROLE,
                "fork_turns": "none"
                if unit["context_turns"] == 0
                else str(unit["context_turns"]),
                "message": message,
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": task_name,
            }
            owner = None
            tool_kind = "spawn"
        else:
            native = {"message": message, "target": reused_from["owner"]}
            owner = reused_from["owner"]
            tool_kind = "reuse"
        return {
            "assurance": unit["assurance"],
            "baseline_id": baseline_id,
            "claim_expires_at": (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            ),
            "cursor": 0,
            "context_turns": unit["context_turns"],
            "dispatch_id": dispatch_id,
            "fallback_from_owner": None,
            "generation": generation,
            "interrupt_claim_expires_at": None,
            "interrupt_receipt_id": None,
            "interrupt_tool_use_id": None,
            "interrupt_unresolved": False,
            "isolation": deepcopy(isolation) if isinstance(isolation, Mapping) else None,
            "last_transient_failure": None,
            "members": list(unit["members"]),
            "native": native,
            "owner": owner,
            "pending_cursor": None,
            "receipt_id": None,
            "transient_retries": 0,
            "role": unit["role"],
            "route_candidates": deepcopy(unit["route"]["candidates"]),
            "route_cursor": route_cursor,
            "reused_from": source_id,
            "scopes": deepcopy(unit["scopes"]),
            "state": "starting",
            "task_name": task_name,
            "task_workspace_root": (
                isolation["record"]["isolate_root"]
                if isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE
                else plan["workspace_root"]
            ),
            "tool_kind": tool_kind,
            "tool_use_id": None,
            "unit_id": unit["id"],
            "wave_id": wave_id,
            "workspace_root": plan["workspace_root"],
        }

    @staticmethod
    def _available_route_cursor(
        state: Mapping[str, Any],
        unit: Mapping[str, Any],
    ) -> int | None:
        generation = max(
            state["logical"][member]["generation"] for member in unit["members"]
        )
        rejected: set[tuple[str, str]] = set()
        for dispatch in state["dispatches"].values():
            if (
                dispatch.get("state") != "rejected"
                or dispatch.get("generation") != generation
                or dispatch.get("members") != unit.get("members")
            ):
                continue
            cursor = dispatch.get("route_cursor")
            candidates = dispatch.get("route_candidates")
            if (
                isinstance(cursor, bool)
                or not isinstance(cursor, int)
                or not isinstance(candidates, list)
                or not 0 <= cursor < len(candidates)
                or not isinstance(candidates[cursor], Mapping)
            ):
                raise ControlPlaneError("rejected route history is invalid")
            route = candidates[cursor]
            rejected.add((str(route.get("model")), str(route.get("effort"))))
        for cursor, route in enumerate(unit["route"]["candidates"]):
            if (str(route.get("model")), str(route.get("effort"))) not in rejected:
                return cursor
        return None

    @staticmethod
    def _public_batch(state: Mapping[str, Any], dispatches: list[Mapping[str, Any]]) -> dict[str, Any]:
        actions = []
        for dispatch in dispatches:
            if dispatch.get("tool_kind") == "spawn":
                actions.append(
                    _tool_action("spawn_new_owner", "spawn_agent", dispatch["native"])
                )
            elif dispatch.get("tool_kind") == "reuse":
                actions.append(
                    _tool_action("reuse_owner", "followup_task", dispatch["native"])
                )
            else:
                raise ControlPlaneError("wave contains an unsupported native action")
        return {
            "dispatches": actions,
            "plan_id": state["plan_id"],
            "protocol": BATCH_PROTOCOL,
            "state": "dispatch" if dispatches else "waiting",
            "wave_id": state.get("active_wave_id"),
        }

    def next_wave(
        self,
        *,
        capacity: int,
        native_catalog: object | None = None,
    ) -> dict[str, Any]:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ControlPlaneError("native capacity must be a positive integer")
        catalog = load_native_catalog() if native_catalog is None else native_catalog
        with self._coordinated_state() as state:
            self._discard_unlinked_reserved_attempt_receipts(state)
            reconciled, expired_receipts = self._reconcile_expired_claims(state)
            if reconciled:
                self._write_state(state)
                self._discard_reserved_attempt_receipts(expired_receipts)
            plan = self._read_plan(state)
            if state.get("cooperative_preparing") is not None:
                raise ControlPlaneUnavailable(
                    "cooperative preparation is reserved; restart before admission"
                )
            if state["active_wave_id"] is not None:
                pending = [
                    item
                    for item in state["dispatches"].values()
                    if item["wave_id"] == state["active_wave_id"]
                    and item["state"] == "starting"
                    and item["tool_use_id"] is None
                ]
                if pending:
                    for dispatch in pending:
                        self._assert_cross_task_compatible(
                            state["workspace_root"],
                            role=dispatch["role"],
                            scopes=dispatch["scopes"],
                            current_dispatch=dispatch["dispatch_id"],
                            cooperative_wave_id=(
                                dispatch["wave_id"]
                                if _is_cooperative_dispatch(dispatch)
                                else None
                            ),
                        )
                    deadline = _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                    for dispatch in pending:
                        dispatch["claim_expires_at"] = deadline
                    self._write_state(state)
                    return self._public_batch(state, sorted(pending, key=lambda item: item["task_name"]))
                active = [
                    item
                    for item in state["dispatches"].values()
                    if item["wave_id"] == state["active_wave_id"]
                    and (
                        item["state"] in ACTIVE_STATES
                        or _native_claim_active(item)
                    )
                ]
                if active:
                    return self._public_batch(state, [])
                state["active_wave_id"] = None
                self._refresh_ready(state, plan)
            nodes = _node_map(plan)
            ready = []
            for node_id, logical in state["logical"].items():
                if logical["state"] != "ready":
                    continue
                node = deepcopy(nodes[node_id])
                node["assurance"] = logical["assurance"]
                ready.append(node)
            if not ready:
                self._write_state(state)
                result = {
                    "dispatches": [],
                    "plan_id": state["plan_id"],
                    "protocol": BATCH_PROTOCOL,
                    "state": self._overall_state(state, plan),
                    "wave_id": None,
                }
                if plan.get("writer_isolation", "serial") == COOPERATIVE:
                    result["cooperative_reason"] = "cooperative_no_ready_writers"
                return result
            routes, route_errors = self._routes(plan, ready, catalog)
            for node_id, error in route_errors.items():
                state["logical"][node_id]["state"] = "fenced"
                state["logical"][node_id]["result"] = {
                    "failure_signature": "route_unavailable",
                    "summary": error,
                }
            routable = [item for item in ready if item["id"] in routes]
            if not routable:
                self._write_state(state)
                result = {
                    **self._public_batch(state, []),
                    "blocked": [
                        {"node": node, "reason": route_errors[node]}
                        for node in sorted(route_errors)
                    ],
                    "state": "blocked",
                }
                if plan.get("writer_isolation", "serial") == COOPERATIVE:
                    result["cooperative_reason"] = "cooperative_no_routable_ready_writers"
                return result
            downstream = _descendant_counts(plan)
            units = _logical_units(
                routable,
                routes,
                downstream=downstream,
            )
            # A completed cooperative wave retains its immutable dispatches
            # for audit, but its now-terminal roots and applied journal must
            # not consume the bounded isolate namespace forever.  This helper
            # detaches only a fully settled batch before reclaiming it; any
            # fenced or recovery-required evidence remains owned and yields a
            # serial fallback with an observable cooperative reason below.
            if plan.get("writer_isolation", "serial") == COOPERATIVE:
                self._cleanup_terminal_cooperative_artifacts_locked(state)
            cooperative_selected, cooperative_reason = self._cooperative_writer_units(
                state,
                plan,
                units,
                capacity=capacity,
            )
            cooperative = cooperative_selected is not None
            selected = (
                cooperative_selected
                if cooperative_selected is not None
                else _select_units(units, capacity)
            )
            route_cursors: dict[str, int] = {}
            available: list[dict[str, Any]] = []
            for unit in selected:
                cursor = self._available_route_cursor(state, unit)
                if cursor is None:
                    for member in unit["members"]:
                        state["logical"][member]["state"] = "fenced"
                        state["logical"][member]["result"] = {
                            "failure_signature": "route_exhausted",
                            "summary": "all prepared native routes were rejected",
                        }
                        route_errors[member] = "all prepared native routes were rejected"
                    continue
                route_cursors[unit["id"]] = cursor
                available.append(unit)
            selected = available
            if cooperative and not _cooperative_group_size_valid(len(selected)):
                # A route failure already fenced its logical member.  The one
                # remaining writer proceeds through the ordinary serial path.
                cooperative = False
                cooperative_reason = "cooperative_selected_writer_route_unavailable"
            if not selected:
                self._write_state(state)
                result = {
                    **self._public_batch(state, []),
                    "blocked": [
                        {"node": node, "reason": route_errors[node]}
                        for node in sorted(route_errors)
                    ],
                    "state": "blocked",
                }
                if cooperative_reason is not None:
                    result["cooperative_reason"] = cooperative_reason
                return result
            reuse_sources: dict[str, str | None] = {}
            reserved_owners: set[str] = set()
            for unit in selected:
                source = (
                    None
                    if cooperative
                    else self._reuse_candidate(
                        state,
                        plan,
                        unit,
                        route_cursor=route_cursors[unit["id"]],
                        reserved_owners=reserved_owners,
                    )
                )
                source_id = source.get("dispatch_id") if source is not None else None
                reuse_sources[unit["id"]] = source_id
                if source is not None:
                    reserved_owners.add(source["owner"])
            artifact_units = []
            for unit in selected:
                artifact_units.append(
                    {
                        "assurance": unit["assurance"],
                        "context_turns": unit["context_turns"],
                        "generation": max(
                            state["logical"][member]["generation"]
                            for member in unit["members"]
                        ),
                        "id": unit["id"],
                        "isolation": None,
                        "members": list(unit["members"]),
                        "reused_from": reuse_sources[unit["id"]],
                        "role": unit["role"],
                        "route_candidates": deepcopy(unit["route"]["candidates"]),
                        "scopes": deepcopy(unit["scopes"]),
                    }
                )
            workspace_root = plan["workspace_root"]
            plan_id = plan["plan_id"]
            cooperative_batch_id = (
                _cooperative_batch_id(
                    plan_id,
                    state["wave_sequence"] + 1,
                    selected,
                )
                if cooperative
                else None
            )
            cooperative_reservation: dict[str, Any] | None = None
            if cooperative:
                assert cooperative_batch_id is not None
                cooperative_reservation = _validate_cooperative_preparing(
                    {
                        "batch_id": cooperative_batch_id,
                        "members": [
                            {
                                "id": unit["members"][0],
                                "scopes": deepcopy(unit["scopes"]),
                            }
                            for unit in selected
                        ],
                        "plan_id": plan_id,
                    },
                    plan_id=plan_id,
                )
                # Persist this writer lease before the first isolate directory
                # exists. The coordinated-state lock order is workspace, session,
                # then state-root, so a competing task cannot slip into prepare.
                for unit in selected:
                    self._assert_cross_task_compatible(
                        workspace_root,
                        role=unit["role"],
                        scopes=unit["scopes"],
                    )
                state["cooperative_preparing"] = cooperative_reservation
            self._write_state(state)
            expected_revision = state["revision"]
            blocked = [
                {"node": node, "reason": route_errors[node]}
                for node in sorted(route_errors)
            ]

        isolate_records: list[dict[str, Any]] = []
        canonical_baseline: dict[str, Any] | None = None
        isolate_snapshots: dict[str, dict[str, Any]] = {}
        isolate_committed = False
        try:
            if cooperative:
                assert cooperative_batch_id is not None
                with acquire(
                    self.root,
                    ISOLATION_NAMESPACE_LOCK,
                    timeout=_isolation_lock_timeout(self.lock_timeout),
                ):
                    isolate_records = prepare_isolates(
                        self.root,
                        Path(workspace_root),
                        backend=plan["workspace_backend"],
                        session_id=self.session_id,
                        batch_id=cooperative_batch_id,
                        members=[
                            {"id": unit["members"][0], "scopes": unit["scopes"]}
                            for unit in selected
                        ],
                    )
                # Worktree creation changes Git administration. Capture exactly
                # one union-scope canonical baseline only after every root
                # exists, and bind every cooperative dispatch to this identity.
                canonical_baseline = capture_workspace(
                    Path(workspace_root),
                    scopes=_cooperative_union_scopes(selected),
                    writable=True,
                )
                baselines: dict[str, dict[str, Any]] = {}
            else:
                baselines = {
                    unit["id"]: capture_workspace(
                        Path(workspace_root),
                        scopes=unit["scopes"],
                        writable=unit["role"] == "worker",
                    )
                    for unit in selected
                }
            for index, (unit, artifact_unit) in enumerate(
                zip(selected, artifact_units, strict=True)
            ):
                baseline = (
                    canonical_baseline if cooperative else baselines[unit["id"]]
                )
                assert isinstance(baseline, Mapping)
                unit["baseline_id"] = baseline["state_id"]
                artifact_unit["baseline_id"] = baseline["state_id"]
                if cooperative:
                    source = capture_workspace(
                        Path(isolate_records[index]["isolate_root"]),
                        scopes=unit["scopes"],
                        writable=True,
                    )
                    if scoped_content_identity(
                        Path(workspace_root), unit["scopes"]
                    ) != scoped_content_identity(
                        Path(isolate_records[index]["isolate_root"]), unit["scopes"]
                    ):
                        raise WriterIsolationError(
                            "cooperative isolate does not match the canonical baseline"
                        )
                    isolation = {
                        "mode": COOPERATIVE,
                        "record": deepcopy(isolate_records[index]),
                    }
                    unit["isolation"] = isolation
                    artifact_unit["isolation"] = deepcopy(isolation)
                    isolate_snapshots[unit["id"]] = source
            if cooperative:
                assert canonical_baseline is not None
                verify_isolation_canonical(
                    Path(workspace_root),
                    canonical_baseline,
                    scope=canonical_baseline["scopes"],
                )

            with self._coordinated_state(workspace_root) as state:
                self._discard_unlinked_reserved_attempt_receipts(state)
                reconciled, expired_receipts = self._reconcile_expired_claims(state)
                if reconciled:
                    self._write_state(state)
                    self._discard_reserved_attempt_receipts(expired_receipts)
                    raise ControlPlaneError(
                        "lifecycle changed while preparing the wave; retry next"
                    )
                if (
                    state["revision"] != expected_revision
                    or state["active_wave_id"] is not None
                    or (
                        cooperative
                        and state.get("cooperative_preparing") != cooperative_reservation
                    )
                    or any(
                        state["logical"][member]["state"] != "ready"
                        for unit in selected
                        for member in unit["members"]
                    )
                ):
                    raise ControlPlaneError(
                        "lifecycle changed while preparing the wave; retry next"
                    )
                plan = self._read_plan(state)
                if plan["plan_id"] != plan_id or plan["workspace_root"] != workspace_root:
                    raise ControlPlaneError("plan changed while preparing the wave")
                if cooperative and self._has_live_cross_task_work(workspace_root):
                    raise ControlPlaneError(
                        "cooperative writer isolation cannot join cross-task workspace work"
                    )
                for unit in selected:
                    self._assert_cross_task_compatible(
                        workspace_root,
                        role=unit["role"],
                        scopes=unit["scopes"],
                        cooperative_preparing_batch_id=(
                            cooperative_batch_id if cooperative else None
                        ),
                    )
                state["wave_sequence"] += 1
                wave_identity = {
                    "plan_id": plan_id,
                    "protocol": WAVE_PROTOCOL,
                    "sequence": state["wave_sequence"],
                    "units": artifact_units,
                }
                digest_identity = wave_identity
                if cooperative:
                    assert canonical_baseline is not None
                    digest_identity = {
                        **wave_identity,
                        **_cooperative_snapshot_digest_fields(
                            canonical_baseline,
                            isolate_snapshots,
                        ),
                    }
                wave_id = _digest(b"cco.wave.v3\0", digest_identity)
                if cooperative:
                    assert canonical_baseline is not None
                    wave = {
                        **wave_identity,
                        "canonical_baseline": canonical_baseline,
                        "isolate_snapshots": isolate_snapshots,
                        "wave_id": wave_id,
                    }
                else:
                    wave = {**wave_identity, "baselines": baselines, "wave_id": wave_id}
                _write_immutable(self._artifact_path("wave", wave_id), wave)
                created: list[dict[str, Any]] = []
                for unit in selected:
                    dispatch = self._dispatch_record(
                        state,
                        plan,
                        wave_id,
                        unit,
                        route_cursor=route_cursors[unit["id"]],
                        reused_from=(
                            state["dispatches"][reuse_sources[unit["id"]]]
                            if reuse_sources[unit["id"]] is not None
                            else None
                        ),
                    )
                    state["dispatches"][dispatch["dispatch_id"]] = dispatch
                    for member in unit["members"]:
                        state["logical"][member]["dispatch_id"] = dispatch["dispatch_id"]
                        state["logical"][member]["state"] = "starting"
                    created.append(dispatch)
                state["active_wave_id"] = wave_id
                if cooperative:
                    state.pop("cooperative_preparing", None)
                try:
                    self._write_state(state)
                except _AtomicWriteUncertain:
                    # The final state can already reference these isolate
                    # roots even though its parent-directory durability is
                    # uncertain.  Preserve the physical roots for replay;
                    # deleting them here would turn a replayable publication
                    # into an invalid live wave.
                    isolate_committed = cooperative
                    raise
                isolate_committed = cooperative
                result = self._public_batch(state, created)
                if blocked:
                    result["blocked"] = blocked
                if cooperative_reason is not None:
                    result["cooperative_reason"] = cooperative_reason
                return result
        except (WriterIsolationError, WriterIsolationUnavailable) as error:
            raise ControlPlaneUnavailable(str(error)) from error
        finally:
            if cooperative and not isolate_committed:
                cleanup_failed = False
                try:
                    with acquire(
                        self.root,
                        ISOLATION_NAMESPACE_LOCK,
                        timeout=_isolation_lock_timeout(self.lock_timeout),
                    ):
                        if isolate_records:
                            cleanup_isolates(self.root, isolate_records)
                        # ``prepare_isolates`` can fail after creating a root but
                        # before returning its record.  The persisted reservation
                        # is the sole authoritative liveness identity in that
                        # interval, so always clean its deterministic targets too.
                        if cooperative_reservation is not None:
                            cleanup_preparing_isolates(
                                self.root,
                                canonical_root=Path(workspace_root),
                                session_id=self.session_id,
                                batch_id=cooperative_reservation["batch_id"],
                                backend=str(plan["workspace_backend"]),
                                count=len(cooperative_reservation["members"]),
                            )
                except (WriterIsolationError, WriterIsolationUnavailable):
                    cleanup_failed = True
                if not cleanup_failed:
                    try:
                        with self._coordinated_state(workspace_root) as state:
                            if (
                                state.get("cooperative_preparing")
                                == cooperative_reservation
                            ):
                                state.pop("cooperative_preparing", None)
                                self._write_state(state)
                    except (ControlPlaneError, ControlPlaneUnavailable):
                        pass

    def _find_dispatch(self, state: Mapping[str, Any], dispatch_id: str) -> dict[str, Any]:
        dispatch = state.get("dispatches", {}).get(dispatch_id)
        if not isinstance(dispatch, dict):
            raise ControlPlaneError("dispatch is unknown or expired")
        return dispatch

    @staticmethod
    def _validate_dispatch_wave(dispatch: Mapping[str, Any], wave: Mapping[str, Any]) -> None:
        matches = [
            unit
            for unit in wave["units"]
            if isinstance(unit, Mapping) and unit.get("id") == dispatch.get("unit_id")
        ]
        if len(matches) != 1:
            raise ControlPlaneError("dispatch has no unique immutable wave unit")
        unit = matches[0]
        members = dispatch.get("members")
        if (
            not isinstance(members, list)
            or len(members) != 1
            or unit.get("members") != members
        ):
            raise ControlPlaneError("dispatch must contain exactly one logical member")
        for field, dispatch_field in (
            ("assurance", "assurance"),
            ("context_turns", "context_turns"),
            ("generation", "generation"),
            ("isolation", "isolation"),
            ("members", "members"),
            ("reused_from", "reused_from"),
            ("role", "role"),
            ("route_candidates", "route_candidates"),
            ("scopes", "scopes"),
        ):
            if unit.get(field) != dispatch.get(dispatch_field):
                raise ControlPlaneError(f"dispatch {dispatch_field} does not match its wave")
        if unit.get("baseline_id") != dispatch.get("baseline_id"):
            raise ControlPlaneError("dispatch baseline does not match its wave")
        isolation = dispatch.get("isolation")
        if isinstance(isolation, Mapping) and isolation.get("mode") == COOPERATIVE:
            record = isolation.get("record")
            recovery = record.get("recovery") if isinstance(record, Mapping) else None
            if (
                not isinstance(record, Mapping)
                or dispatch.get("task_workspace_root") != record.get("isolate_root")
                or not isinstance(recovery, Mapping)
                or dispatch.get("workspace_root") != recovery.get("canonical_root")
                or dispatch.get("reused_from") is not None
            ):
                raise ControlPlaneError("cooperative dispatch isolate does not match its wave")
        elif (
            isolation is not None
            or dispatch.get("task_workspace_root", dispatch.get("workspace_root"))
            != dispatch.get("workspace_root")
        ):
            raise ControlPlaneError("serial dispatch workspace binding is invalid")
        cursor = dispatch.get("route_cursor")
        candidates = unit.get("route_candidates")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not isinstance(candidates, list)
            or not 0 <= cursor < len(candidates)
        ):
            raise ControlPlaneError("dispatch route cursor is invalid")
        selected = candidates[cursor]
        native = dispatch.get("native")
        if dispatch.get("tool_kind") == "spawn" and (
            not isinstance(native, Mapping)
            or native.get("model") != selected.get("model")
            or native.get("reasoning_effort") != selected.get("effort")
        ):
            raise ControlPlaneError("dispatch native route does not match its wave")
        if dispatch.get("tool_kind") == "reuse" and (
            not isinstance(native, Mapping)
            or native.get("target") != dispatch.get("owner")
            or native.get("message") is None
            or not isinstance(dispatch.get("reused_from"), str)
        ):
            raise ControlPlaneError("dispatch reuse input does not match its wave")

    @staticmethod
    def _dispatch_baseline(
        dispatch: Mapping[str, Any], wave: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if _is_cooperative_dispatch(dispatch):
            baseline = wave.get("canonical_baseline")
            if not isinstance(baseline, Mapping):
                raise ControlPlaneError("cooperative dispatch has no canonical baseline")
            return baseline
        matches = [
            unit
            for unit in wave["units"]
            if isinstance(unit, Mapping) and unit.get("id") == dispatch.get("unit_id")
        ]
        baselines = wave.get("baselines")
        unit_id = matches[0].get("id") if len(matches) == 1 else None
        baseline = baselines.get(unit_id) if isinstance(baselines, Mapping) else None
        if len(matches) != 1 or not isinstance(baseline, Mapping):
            raise ControlPlaneError("dispatch has no immutable logical baseline")
        return baseline

    def _verify_native_admission(
        self,
        dispatch_id: str,
        tool_use_id: str,
        claim: Callable[
            [dict[str, Any]],
            tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]],
        ],
        *,
        recapture_stale_native: bool,
        observed_tool_input: Mapping[str, Any] | None = None,
    ) -> None:
        """Run the shared two-phase admission around lock-free workspace verification."""

        with self._coordinated_state() as state:
            self._discard_unlinked_reserved_attempt_receipts(state)
            reconciled, expired_receipts = self._reconcile_expired_claims(state)
            if reconciled:
                self._write_state(state)
                self._discard_reserved_attempt_receipts(expired_receipts)
            dispatch, wave, allowed = claim(state)
            workspace_root = Path(dispatch["workspace_root"])
            cooperative = _is_cooperative_dispatch(dispatch)
            isolate_record: Mapping[str, Any] | None = None
            isolate_baseline: Mapping[str, Any] | None = None
            if cooperative:
                isolation = dispatch.get("isolation")
                assert isinstance(isolation, Mapping)
                try:
                    isolate_record = validate_isolation_record(isolation.get("record"), self.root)
                except WriterIsolationError as error:
                    raise ControlPlaneError(str(error)) from error
                snapshots = wave.get("isolate_snapshots")
                candidate = (
                    snapshots.get(dispatch["unit_id"])
                    if isinstance(snapshots, Mapping)
                    else None
                )
                if not isinstance(candidate, Mapping):
                    raise ControlPlaneError("cooperative isolate baseline is unavailable")
                isolate_baseline = deepcopy(dict(candidate))
            self._assert_cross_task_compatible(
                workspace_root,
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
                cooperative_wave_id=dispatch["wave_id"] if cooperative else None,
            )
            baseline = deepcopy(self._dispatch_baseline(dispatch, wave))
            canonical_scopes = deepcopy(baseline.get("scopes"))
            if cooperative and not isinstance(canonical_scopes, list):
                raise ControlPlaneError("cooperative canonical baseline scopes are invalid")
            owner_scopes = deepcopy(dispatch["scopes"])
            checkpoint()
            receipt = self._reserve_native_attempt_receipt(
                state,
                dispatch,
                tool_use_id,
                observed_tool_input,
            )
            try:
                self._begin_native_claim(state, dispatch, tool_use_id)
                self._write_state(state)
            except _AtomicWriteUncertain:
                # The claim and its receipt may already be published.  Leave
                # both for the durable receipt/state replay instead of
                # manufacturing an unlinked rollback.
                raise
            except Exception:
                dispatch["receipt_id"] = None
                self._discard_reserved_native_attempt_receipt(receipt)
                raise
            claim_revision = state["revision"]
        try:
            with deadline_after(_preflight_verification_budget()):
                if cooperative:
                    assert isolate_record is not None and isolate_baseline is not None
                    verify_isolation_canonical(
                        workspace_root,
                        baseline,
                        scope=canonical_scopes,
                    )
                    verify_isolation_canonical(
                        Path(isolate_record["isolate_root"]),
                        isolate_baseline,
                        scope=owner_scopes,
                    )
                else:
                    verify_workspace(
                        workspace_root,
                        baseline,
                        allowed_scopes=allowed,
                        owner_scopes=owner_scopes,
                        pre_spawn=True,
                    )
        except OperationDeadlineExceeded as error:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneUnavailable(str(error)) from error
        except WorkspaceGuardUnavailable as error:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneUnavailable(str(error)) from error
        except WriterIsolationUnavailable as error:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneUnavailable(str(error)) from error
        except WriterIsolationError as error:
            if cooperative:
                reason = (
                    "canonical_drift"
                    if str(error).startswith("canonical workspace drift:")
                    else "isolate_identity_drift"
                )
                self._fence_cooperative_batch(dispatch_id, reason)
            else:
                self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneError(str(error)) from error
        except WorkspaceGuardError as error:
            if recapture_stale_native:
                recaptured = self._discard_stale_unstarted_wave(dispatch_id, tool_use_id)
                action = (
                    "call next again"
                    if recaptured
                    else "inspect and retry the fenced node"
                )
                raise ControlPlaneError(
                    f"{error}; the stale native admission was settled; {action}"
                ) from error
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise ControlPlaneError(str(error)) from error
        try:
            with self._coordinated_state() as state:
                if state["revision"] != claim_revision:
                    raise ControlPlaneError(
                        "lifecycle changed while verifying the native admission"
                    )
                dispatch, _wave, _allowed = claim(state)
                if dispatch.get("tool_use_id") != tool_use_id:
                    raise ControlPlaneError("native admission claim is stale")
                self._assert_cross_task_compatible(
                    workspace_root,
                    role=dispatch["role"],
                    scopes=dispatch["scopes"],
                    current_dispatch=dispatch["dispatch_id"],
                    cooperative_wave_id=dispatch["wave_id"] if cooperative else None,
                )
                dispatch["claim_expires_at"] = (
                    _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                )
                checkpoint()
                self._write_state(state)
        except _AtomicWriteUncertain:
            # A post-verification renewal may already be live.  Its receipt
            # remains the replay anchor, so never clear it from a stale local
            # copy after a directory-sync uncertainty.
            raise
        except Exception:
            self._rollback_native_claim(dispatch_id, tool_use_id)
            raise

    def _resolve_opaque_native_input(
        self,
        tool_input: Mapping[str, Any],
        *,
        allowed_kinds: set[str],
    ) -> tuple[dict[str, Any], str, str]:
        """Resolve one ciphertext envelope without treating its shape as identity.

        The current host hides the prepared message but leaves the remaining
        native input visible.  Trusted-host mode therefore requires one and
        only one live reservation whose complete visible envelope matches.
        The exact ciphertext is separately bound to the preflight receipt.
        """

        if not allowed_kinds or not allowed_kinds <= {
            "continuation",
            "reuse",
            "spawn",
        }:
            raise ControlPlaneError("opaque native kind selection is invalid")
        if not host_opaque_message(tool_input.get("message")):
            raise ControlPlaneError("opaque native message is invalid")
        with self._coordinated_state() as state:
            matches = self._opaque_dispatch_candidates(
                state,
                tool_input,
                allowed_kinds,
            )
            if len(matches) != 1:
                raise ControlPlaneError(
                    "opaque native input does not match one prepared dispatch"
                )
            dispatch_id, expected, kind = matches[0]
            return dispatch_id, expected, kind

    @staticmethod
    def _opaque_dispatch_candidates(
        state: Mapping[str, Any],
        tool_input: Mapping[str, Any],
        allowed_kinds: set[str],
    ) -> list[tuple[str, dict[str, Any], str]]:
        matches: list[tuple[str, dict[str, Any], str]] = []
        for dispatch in state.get("dispatches", {}).values():
            kind = dispatch.get("tool_kind")
            lifecycle = dispatch.get("state")
            if kind not in allowed_kinds or (
                lifecycle != "starting"
                and not (kind == "continuation" and lifecycle == "paused")
            ):
                continue
            expected = dispatch.get("native")
            dispatch_id = dispatch.get("dispatch_id")
            if (
                not isinstance(dispatch_id, str)
                or not isinstance(expected, Mapping)
                or set(tool_input) != set(expected)
            ):
                continue
            if any(
                key != "message" and tool_input.get(key) != expected.get(key)
                for key in expected
            ):
                continue
            matches.append((dispatch_id, dict(expected), str(kind)))
        return matches

    def _assert_opaque_dispatch_claim(
        self,
        state: Mapping[str, Any],
        observed_tool_input: Mapping[str, Any] | None,
        *,
        dispatch_id: str,
        allowed_kinds: set[str],
    ) -> None:
        if observed_tool_input is None:
            return
        matches = self._opaque_dispatch_candidates(
            state,
            observed_tool_input,
            allowed_kinds,
        )
        if len(matches) != 1 or matches[0][0] != dispatch_id:
            raise ControlPlaneError(
                "opaque native dispatch changed before admission commit"
            )

    def preflight_spawn(
        self,
        payload: Mapping[str, Any],
        *,
        opaque_message: bool = False,
        _observed_tool_input: Mapping[str, Any] | None = None,
        _opaque_dispatch_id: str | None = None,
    ) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("spawn input is missing")
        if opaque_message:
            if _opaque_message_policy() == "strict":
                raise ControlPlaneError("strict policy rejects opaque spawn input")
            dispatch_id, expected, kind = self._resolve_opaque_native_input(
                tool_input,
                allowed_kinds={"spawn"},
            )
            if kind != "spawn":
                raise ControlPlaneError("opaque input is not a prepared spawn")
            substituted = dict(payload)
            substituted["tool_input"] = expected
            self.preflight_spawn(
                substituted,
                _observed_tool_input=tool_input,
                _opaque_dispatch_id=dispatch_id,
            )
            return
        task = parse_task_message(tool_input.get("message"))
        dispatch_id = task["dispatch_id"]
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("spawn has no native tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, dispatch_id)
            if _opaque_dispatch_id is not None and dispatch_id != _opaque_dispatch_id:
                raise ControlPlaneError("opaque spawn dispatch identity changed")
            self._assert_opaque_dispatch_claim(
                state,
                _observed_tool_input,
                dispatch_id=dispatch_id,
                allowed_kinds={"spawn"},
            )
            if dispatch["state"] != "starting" or dispatch["tool_kind"] != "spawn":
                raise ControlPlaneError("dispatch is not ready to spawn")
            expected = dispatch["native"]
            if set(tool_input) != set(expected):
                raise ControlPlaneError("spawn contains fields beyond its prepared wave")
            keys = [
                "agent_type",
                "fork_turns",
                "model",
                "reasoning_effort",
                "task_name",
            ]
            keys.append("message")
            for key in keys:
                if tool_input.get(key) != expected[key]:
                    raise ControlPlaneError(
                        f"spawn {key} does not match the prepared wave"
                    )
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("dispatch already has an in-flight spawn")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if (
                dispatch["role"] == "worker"
                and sibling_writer_scopes
                and not _is_cooperative_dispatch(dispatch)
            ):
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            return dispatch, wave, sibling_writer_scopes

        self._verify_native_admission(
            dispatch_id,
            tool_use_id,
            claim,
            recapture_stale_native=True,
            observed_tool_input=(
                tool_input
                if _observed_tool_input is None
                else _observed_tool_input
            ),
        )

    def preflight_reuse(
        self,
        payload: Mapping[str, Any],
        *,
        _observed_tool_input: Mapping[str, Any] | None = None,
        _opaque_dispatch_id: str | None = None,
    ) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("reuse input is missing")
        task = parse_task_message(tool_input.get("message"))
        dispatch_id = task["dispatch_id"]
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("reuse has no native tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, dispatch_id)
            if _opaque_dispatch_id is not None and dispatch_id != _opaque_dispatch_id:
                raise ControlPlaneError("opaque reuse dispatch identity changed")
            self._assert_opaque_dispatch_claim(
                state,
                _observed_tool_input,
                dispatch_id=dispatch_id,
                allowed_kinds={"reuse"},
            )
            if dispatch["state"] != "starting" or dispatch["tool_kind"] != "reuse":
                raise ControlPlaneError("dispatch is not ready to reuse an owner")
            if set(tool_input) != set(dispatch["native"]):
                raise ControlPlaneError("reuse contains fields beyond its prepared input")
            if (
                tool_input.get("target") != dispatch.get("owner")
                or tool_input.get("message") != dispatch["native"].get("message")
            ):
                raise ControlPlaneError("reuse does not match its prepared input")
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("dispatch already has an in-flight reuse")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            plan = self._read_plan(state)
            members = dispatch.get("members")
            if not isinstance(members, list) or len(members) != 1:
                raise ControlPlaneError("reuse requires one logical member")
            node = _node_map(plan)[members[0]]
            if len(node["depends_on"]) != 1:
                raise ControlPlaneError("reuse requires one direct predecessor")
            dependency_dispatches = {
                state["logical"][dependency].get("dispatch_id")
                for dependency in node["depends_on"]
                if isinstance(state["logical"][dependency].get("dispatch_id"), str)
            }
            source = state["dispatches"].get(dispatch.get("reused_from"))
            route = self._selected_dispatch_route(dispatch)
            if (
                not isinstance(source, Mapping)
                or not isinstance(route, Mapping)
                or not self._source_matches_reuse(
                    source,
                    dependency_dispatches=dependency_dispatches,
                    dependency_member=node["depends_on"][0],
                    role=dispatch["role"],
                    assurance=dispatch["assurance"],
                    route=route,
                    scopes=dispatch["scopes"],
                )
                or source.get("owner") != dispatch.get("owner")
                or any(
                    other.get("dispatch_id")
                    not in {dispatch.get("dispatch_id"), source.get("dispatch_id")}
                    and other.get("owner") == dispatch.get("owner")
                    and other.get("state") in {"starting", "running", "paused"}
                    for other in state["dispatches"].values()
                )
            ):
                raise ControlPlaneError("prepared owner reuse is no longer valid")
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            return dispatch, wave, sibling_writer_scopes

        self._verify_native_admission(
            dispatch_id,
            tool_use_id,
            claim,
            recapture_stale_native=True,
            observed_tool_input=(
                tool_input
                if _observed_tool_input is None
                else _observed_tool_input
            ),
        )

    def preflight_opaque_followup(self, payload: Mapping[str, Any]) -> None:
        """Bind one host-opaque follow-up to its unique prepared envelope."""

        if _opaque_message_policy() == "strict":
            raise ControlPlaneError("strict policy rejects opaque follow-up input")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("opaque follow-up input is missing")
        dispatch_id, expected, kind = self._resolve_opaque_native_input(
            tool_input,
            allowed_kinds={"continuation", "reuse"},
        )
        substituted = dict(payload)
        substituted["tool_input"] = expected
        if kind == "reuse":
            self.preflight_reuse(
                substituted,
                _observed_tool_input=tool_input,
                _opaque_dispatch_id=dispatch_id,
            )
        else:
            self.preflight_continuation(
                substituted,
                _observed_tool_input=tool_input,
                _opaque_dispatch_id=dispatch_id,
            )

    def preflight_continuation(
        self,
        payload: Mapping[str, Any],
        *,
        _observed_tool_input: Mapping[str, Any] | None = None,
        _opaque_dispatch_id: str | None = None,
    ) -> None:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ControlPlaneError("continuation input is missing")
        body = parse_continue_message(tool_input.get("message"))
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise ControlPlaneError("continuation has no tool-use identity")

        def claim(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
            dispatch = self._find_dispatch(state, body["dispatch_id"])
            if _opaque_dispatch_id is not None and body["dispatch_id"] != _opaque_dispatch_id:
                raise ControlPlaneError("opaque continuation dispatch identity changed")
            self._assert_opaque_dispatch_claim(
                state,
                _observed_tool_input,
                dispatch_id=body["dispatch_id"],
                allowed_kinds={"continuation"},
            )
            if _is_cooperative_dispatch(dispatch):
                raise ControlPlaneError(
                    "cooperative writer isolation does not permit continuation"
                )
            if (
                dispatch["state"] not in {"paused", "starting"}
                or dispatch["tool_kind"] != "continuation"
            ):
                raise ControlPlaneError("continuation is not ready")
            if (
                dispatch.get("interrupt_receipt_id") is not None
                or dispatch.get("interrupt_tool_use_id") is not None
                or dispatch.get("interrupt_unresolved") is True
            ):
                raise ControlPlaneError(
                    "continuation owner has an unresolved interrupt attempt"
                )
            if (
                set(tool_input) != set(dispatch["native"])
                or
                tool_input.get("target") != dispatch["owner"]
                or tool_input.get("message") != dispatch["native"].get("message")
            ):
                raise ControlPlaneError("continuation does not match its prepared input")
            if body.get("cursor") != dispatch["pending_cursor"]:
                raise ControlPlaneError("continuation cursor is stale")
            if (
                _native_claim_active(dispatch)
                and dispatch["tool_use_id"] is not None
                and dispatch["tool_use_id"] != tool_use_id
            ):
                raise ControlPlaneError("continuation is already in flight")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            allowed = (
                dispatch["scopes"]
                if dispatch["role"] == "worker"
                else sibling_writer_scopes
            )
            return dispatch, wave, allowed

        self._verify_native_admission(
            body["dispatch_id"],
            tool_use_id,
            claim,
            recapture_stale_native=False,
            observed_tool_input=(
                tool_input
                if _observed_tool_input is None
                else _observed_tool_input
            ),
        )

    def _append_tombstone(
        self,
        state: dict[str, Any],
        dispatch: Mapping[str, Any],
        reason: str,
        *,
        receipt: Mapping[str, Any] | None = None,
        consumed_tool_use_id: str | None = None,
    ) -> None:
        if receipt is not None and consumed_tool_use_id is not None:
            raise ControlPlaneError("tombstone has conflicting native anchors")
        tombstone: dict[str, Any] = {
            "cursor": dispatch["cursor"],
            "dispatch_id": dispatch["dispatch_id"],
            "owner": dispatch.get("owner"),
            "reason": reason,
        }
        if receipt is not None:
            if not _tool_use_id_valid(receipt.get("tool_use_id")):
                raise ControlPlaneError("tombstone native call identity is invalid")
            tombstone["tool_input_sha256"] = receipt["tool_input_sha256"]
            tombstone["tool_use_id"] = receipt["tool_use_id"]
        elif consumed_tool_use_id is not None:
            if not _tool_use_id_valid(consumed_tool_use_id):
                raise ControlPlaneError("tombstone native call identity is invalid")
            tombstone["tool_use_id"] = consumed_tool_use_id
        state["tombstones"].append(tombstone)
        # Native attempt anchors are replay proof, not ordinary historical
        # status.  Dropping one would permit a later opaque envelope or call
        # ID to reuse a consumed native admission.  Refuse a transition that
        # would evict one instead of silently weakening that invariant.
        if len(state["tombstones"]) > MAX_TOMBSTONES:
            unanchored = next(
                (
                    index
                    for index, item in enumerate(state["tombstones"])
                    if item.get("tool_use_id") is None
                    and item.get("tool_input_sha256") is None
                ),
                None,
            )
            if unanchored is not None:
                del state["tombstones"][unanchored]
            elif (
                tombstone.get("tool_use_id") is not None
                or tombstone.get("tool_input_sha256") is not None
            ):
                state["tombstones"].pop()
                raise ControlPlaneUnavailable(
                    "lifecycle tombstone replay-anchor capacity is exhausted"
                )
            else:
                # Historical terminal status is non-authoritative. Preserve
                # every existing replay anchor and make this unanchored entry
                # best-effort rather than failing an otherwise safe cleanup.
                state["tombstones"].pop()

    def _settle_wave(self, state: dict[str, Any]) -> None:
        wave_id = state.get("active_wave_id")
        if not isinstance(wave_id, str):
            return
        records = [
            item for item in state["dispatches"].values() if item["wave_id"] == wave_id
        ]
        if records and all(
            item["state"] in {"retired", "fenced", "rejected"} for item in records
        ):
            state["active_wave_id"] = None

    def _fence_members(self, state: dict[str, Any], dispatch: Mapping[str, Any], reason: str) -> None:
        if dispatch.get("state") in {"retired", "fenced", "rejected"}:
            return
        dispatch["state"] = "fenced"
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "fenced"
            state["logical"][member]["result"] = {
                "failure_signature": reason,
                "summary": reason.replace("_", " "),
            }
        self._append_tombstone(state, dispatch, reason)

    def _attempt_receipts_for_dispatch(
        self,
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return every durable current-plan attempt, including unlinked receipts.

        State drops a ready-to-apply result pointer before acknowledgement so a
        recovery retry cannot bind it as a live lease.  If acknowledgement is
        interrupted, that receipt is still durable and a later whole-batch
        terminal transition must clear it with every linked peer attempt.
        """

        matches: dict[str, tuple[str, dict[str, Any]]] = {}
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            for path in _pending_event_paths(self.root):
                if not path.name.startswith(prefix):
                    continue
                receipt = self._read_pending_event(path)
                if not self._attempt_receipt_belongs_to_terminal_dispatch(
                    state, dispatch, receipt
                ):
                    continue
                if receipt.get("kind") == "native_attempt":
                    matches[str(receipt["event_id"])] = ("native", receipt)
                else:
                    matches[str(receipt["event_id"])] = ("interrupt", receipt)
        return list(matches.values())

    def _detach_terminal_attempt_receipts_locked(
        self,
        state: Mapping[str, Any],
        dispatches: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Clear every terminal attempt pointer before its receipt is finalized.

        A cooperative batch has one lifecycle outcome.  Its durable attempt
        receipts therefore cannot remain linked to a peer that the batch has
        already fenced.  State pointers are cleared in the same state write as
        the fence; receipt acknowledgement/deletion follows that write and is
        replayable if local I/O fails.
        """

        receipts: dict[str, tuple[str, dict[str, Any]]] = {}
        for item in dispatches:
            for kind, receipt in self._attempt_receipts_for_dispatch(state, item):
                receipts[str(receipt["event_id"])] = (kind, receipt)
                if kind == "native":
                    self._append_tombstone(
                        state, item, "native_attempt_consumed", receipt=receipt
                    )
            item["receipt_id"] = None

            if item.get("interrupt_receipt_id") is not None:
                item["interrupt_unresolved"] = True
            item["interrupt_receipt_id"] = None
            item["interrupt_tool_use_id"] = None
            item["interrupt_claim_expires_at"] = None
        return list(receipts.values())

    def _finalize_detached_attempt_receipts(
        self,
        receipts: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for kind, receipt in receipts:
            if kind == "native":
                self._finalize_native_attempt_receipt(receipt)
            else:
                self._finalize_interrupt_attempt_receipt(receipt)

    def _fence_cooperative_members_locked(
        self,
        state: dict[str, Any],
        dispatch: Mapping[str, Any],
        reason: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Fence the full isolate batch; a cooperative group never succeeds partly."""

        if not _is_cooperative_dispatch(dispatch):
            self._fence_members(state, dispatch, reason)
            return self._detach_terminal_attempt_receipts_locked(state, [dispatch])
        records = self._cooperative_wave_dispatches(state, dispatch)
        for item in records:
            self._fence_members(state, item, reason)
        self._settle_wave(state)
        return self._detach_terminal_attempt_receipts_locked(state, records)

    def _fence_cooperative_batch(self, dispatch_id: str, reason: str) -> None:
        receipts: list[tuple[str, dict[str, Any]]]
        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, dispatch_id)
            receipts = self._fence_cooperative_members_locked(state, dispatch, reason)
            self._write_state(state)
        self._finalize_detached_attempt_receipts(receipts)

    def _fallback_dispatch(
        self,
        state: dict[str, Any],
        plan: Mapping[str, Any],
        rejected: dict[str, Any],
    ) -> dict[str, Any] | None:
        next_cursor = rejected["route_cursor"] + 1
        if next_cursor >= len(rejected["route_candidates"]):
            return None
        members = rejected.get("members")
        if not isinstance(members, list) or len(members) != 1:
            raise ControlPlaneError("rejected dispatch has multiple logical members")
        unit = {
            "assurance": rejected["assurance"],
            "baseline_id": rejected["baseline_id"],
            "context_turns": 0
            if rejected["native"]["fork_turns"] == "none"
            else int(rejected["native"]["fork_turns"]),
            "id": members[0],
            "members": members,
            "role": rejected["role"],
            "route": {"candidates": rejected["route_candidates"]},
            "scopes": rejected["scopes"],
        }
        source_id = rejected.get("reused_from")
        source = state["dispatches"].get(source_id) if isinstance(source_id, str) else None
        if source_id is not None and not isinstance(source, Mapping):
            raise ControlPlaneError("rejected reuse route has no source dispatch")
        fallback = self._dispatch_record(
            state,
            plan,
            rejected["wave_id"],
            unit,
            route_cursor=next_cursor,
            reused_from=source,
        )
        if source is not None:
            route = fallback["route_candidates"][next_cursor]
            message = fallback["native"]["message"]
            fallback["native"] = {
                "agent_type": WRITE_ROLE
                if fallback["role"] == "worker"
                else READ_ROLE,
                "fork_turns": "none"
                if fallback["context_turns"] == 0
                else str(fallback["context_turns"]),
                "message": message,
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": fallback["task_name"],
            }
            fallback["fallback_from_owner"] = rejected.get("fallback_from_owner")
            fallback["owner"] = None
            fallback["tool_kind"] = "spawn"
        return fallback

    def _reject_route_locked(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
    ) -> dict[str, Any] | None:
        dispatch["tool_use_id"] = None
        dispatch["claim_expires_at"] = None
        if dispatch["tool_kind"] == "continuation":
            dispatch["state"] = "paused"
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "paused"
            return None
        dispatch["state"] = "rejected"
        self._append_tombstone(state, dispatch, "native_route_rejected")
        plan = self._read_plan(state)
        fallback = self._fallback_dispatch(state, plan, dispatch)
        if fallback is None:
            self._fence_members(state, dispatch, "route_exhausted")
        else:
            state["dispatches"][fallback["dispatch_id"]] = fallback
            for member in fallback["members"]:
                state["logical"][member]["dispatch_id"] = fallback["dispatch_id"]
                state["logical"][member]["state"] = "starting"
        self._settle_wave(state)
        return fallback

    def postflight_tool(self, payload: Mapping[str, Any]) -> None:
        event = self._postflight_observation(payload)
        if event["kind"] != "native_attempt_observation":
            raise ControlPlaneError("native tool result is not a spawn or follow-up")
        self._observe_native_attempt(event)

    def _settle_native_success_event(
        self,
        event: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any],
    ) -> bool:
        dispatch_id = str(event["dispatch_id"])
        tool_use_id = str(event["tool_use_id"])
        if not self.state_path.exists():
            return False
        with self._coordinated_state() as state:
            try:
                dispatch = self._find_dispatch(state, dispatch_id)
            except ControlPlaneError:
                # A receipt from an explicitly cleaned-up plan cannot authorize
                # work in a replacement plan.
                return False
            # Receipt publication is intentionally before the state claim.  An
            # atomic replacement can become visible just before its directory
            # durability report fails, leaving a durable-but-unlinked
            # reservation.  It never admitted a host call, so it cannot turn a
            # later PostToolUse into a running dispatch merely by sharing the
            # prepared ciphertext/message shape.
            if not self._native_receipt_is_current(state, dispatch, receipt):
                return False
            if dispatch["state"] in {"fenced", "rejected", "retired", "ready_to_apply"} or (
                dispatch["state"] == "paused"
                and isinstance(dispatch.get("result"), Mapping)
            ):
                return True
            # A successful state transition may have been published just
            # before the Hook lost its directory-sync acknowledgement.  The
            # current receipt proves this is its exact replay; advance only its
            # receipt state below, rather than requiring a second host call.
            if dispatch["state"] == "running":
                return True
            if dispatch["state"] != "starting":
                return False
            if receipt.get("tool_input_sha256") != event.get("tool_input_sha256"):
                raise ControlPlaneError("native tool result input is stale")
            if dispatch.get("tool_use_id") != tool_use_id:
                raise ControlPlaneError("native tool result call identity is stale")
            self._assert_cross_task_compatible(
                dispatch["workspace_root"],
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
                cooperative_wave_id=(
                    dispatch["wave_id"] if _is_cooperative_dispatch(dispatch) else None
                ),
            )
            if dispatch["state"] == "starting" and dispatch["tool_kind"] == "spawn":
                owners = set(event["owners"])
                if len(owners) > 1:
                    receipts = self._fence_cooperative_members_locked(
                        state, dispatch, "native_owner_ambiguous"
                    )
                    self._write_state(state)
                    self._finalize_detached_attempt_receipts(receipts)
                    return True
                if owners:
                    owner = owners.pop()
                    if not _owner_matches_task(owner, dispatch["task_name"]):
                        receipts = self._fence_cooperative_members_locked(
                            state, dispatch, "native_owner_mismatch"
                        )
                        self._write_state(state)
                        self._finalize_detached_attempt_receipts(receipts)
                        return True
                    dispatch["owner"] = owner
            elif dispatch["pending_cursor"] is not None:
                dispatch["cursor"] = dispatch["pending_cursor"]
                dispatch["pending_cursor"] = None
            dispatch["state"] = "running"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "running"
            self._write_state(state)
            return True

    def prepare_continuation(self, dispatch_id: str, evidence_delta: object) -> dict[str, Any]:
        if not isinstance(evidence_delta, Mapping) or not evidence_delta:
            raise ControlPlaneError("continuation requires a non-empty evidence object")
        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, dispatch_id)
            if _is_cooperative_dispatch(dispatch):
                raise ControlPlaneError(
                    "cooperative writer isolation does not permit continuation"
                )
            if dispatch["state"] != "paused" or not isinstance(dispatch.get("owner"), str):
                raise ControlPlaneError("dispatch is not continuable")
            if (
                dispatch.get("interrupt_receipt_id") is not None
                or dispatch.get("interrupt_tool_use_id") is not None
                or dispatch.get("interrupt_unresolved") is True
            ):
                raise ControlPlaneError(
                    "continuation owner has an unresolved interrupt attempt"
                )
            if dispatch.get("pending_cursor") is not None:
                raise ControlPlaneError("dispatch already has a prepared continuation")
            self._assert_cross_task_compatible(
                dispatch["workspace_root"],
                role=dispatch["role"],
                scopes=dispatch["scopes"],
                current_dispatch=dispatch["dispatch_id"],
            )
            cursor = dispatch["cursor"] + 1
            try:
                message = _render_continue(dispatch, evidence_delta, cursor)
            except ProtocolHashError as error:
                raise ControlPlaneError(str(error)) from error
            dispatch["native"] = {"message": message, "target": dispatch["owner"]}
            dispatch["pending_cursor"] = cursor
            dispatch["tool_kind"] = "continuation"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            self._write_state(state)
            return _tool_action(
                "continue_same_owner",
                "followup_task",
                dispatch["native"],
            )

    def owner_is_managed(self, owner: str) -> bool:
        if not self.state_path.exists():
            return False
        with self._coordinated_state() as state:
            return any(
                _interrupt_target_matches_dispatch(owner, item)
                for item in state["dispatches"].values()
            )

    def preflight_interrupt(self, payload: Mapping[str, Any]) -> bool:
        tool_input = payload.get("tool_input")
        tool_use_id = payload.get("tool_use_id")
        if (
            not isinstance(tool_input, Mapping)
            or not isinstance(tool_input.get("target"), str)
            or not isinstance(tool_use_id, str)
            or not tool_use_id
        ):
            raise ControlPlaneError("interrupt input is incomplete")
        target = tool_input["target"]
        if not self.state_path.exists():
            return False
        with self._coordinated_state() as state:
            self._discard_unlinked_reserved_attempt_receipts(state)
            reconciled, expired_receipts = self._reconcile_expired_claims(state)
            if reconciled:
                self._write_state(state)
                self._discard_reserved_attempt_receipts(expired_receipts)
            matches = [
                item
                for item in state["dispatches"].values()
                if _interrupt_target_matches_dispatch(target, item)
                and item["state"] in {"running", "ready_to_apply", "paused"}
            ]
            managed = any(
                _interrupt_target_matches_dispatch(target, item)
                for item in state["dispatches"].values()
            )
            if not matches and not managed:
                return False
            if len(matches) != 1:
                raise ControlPlaneError("interrupt target has no unique active dispatch")
            dispatch = matches[0]
            if dispatch.get("interrupt_unresolved") is True:
                raise ControlPlaneError(
                    "interrupt target has an unresolved interrupt attempt"
                )
            existing_receipt_id = dispatch.get("interrupt_receipt_id")
            if isinstance(existing_receipt_id, str):
                existing = self._interrupt_attempt_for_dispatch(dispatch)
                if existing is None:
                    raise ControlPlaneUnavailable(
                        "interrupt target lost its durable settlement receipt"
                    )
                if dispatch.get("interrupt_tool_use_id") == tool_use_id:
                    # Host retries of one exact preflight are idempotent; they
                    # retain the same immutable attempt receipt.
                    if existing.get("owner") != target:
                        raise ControlPlaneError(
                            "interrupt target changed for the existing tool call"
                        )
                    return True
                raise ControlPlaneError(
                    "interrupt target already has an unresolved attempt"
                )
            elif dispatch.get("interrupt_tool_use_id") is not None:
                raise ControlPlaneError(
                    "interrupt target already has an unresolved attempt"
                )
            receipt = self._reserve_interrupt_attempt_receipt(
                state,
                dispatch,
                target,
                tool_use_id,
            )
            try:
                dispatch["interrupt_tool_use_id"] = tool_use_id
                dispatch["interrupt_claim_expires_at"] = (
                    _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
                )
                self._write_state(state)
            except _AtomicWriteUncertain:
                # A durable interrupt reservation may already exist.  Do not
                # clear the local pointer or delete its replay receipt.
                raise
            except Exception:
                dispatch["interrupt_receipt_id"] = None
                dispatch["interrupt_tool_use_id"] = None
                dispatch["interrupt_claim_expires_at"] = None
                self._discard_reserved_interrupt_attempt_receipt(receipt)
                raise
            return True

    def _observe_interrupt_attempt(self, event: Mapping[str, Any]) -> bool:
        normalized = self._validate_interrupt_attempt_observation(event)
        if normalized.get("kind") != "interrupt_attempt_observation":
            raise ControlPlaneError("interrupt observation is invalid")
        receipt = self._find_interrupt_attempt_receipt(
            str(normalized["target"]),
            str(normalized["tool_use_id"]),
        )
        if receipt is None:
            return False
        if receipt.get("phase") == "acknowledged":
            self._clear_pending_event(receipt)
            return True
        if receipt.get("phase") not in {"reserved", "observed"}:
            raise ControlPlaneError("interrupt receipt is not ready for observation")
        observed = dict(receipt)
        observed["previous_status"] = normalized["previous_status"]
        observed["phase"] = "observed"
        observed = self._write_interrupt_attempt_receipt(observed)
        if observed.get("phase") == "acknowledged":
            self._clear_pending_event(observed)
            return True
        if observed.get("phase") != "observed":
            raise ControlPlaneError("interrupt receipt observation is not active")
        settlement_event = dict(normalized)
        settlement_event["previous_status"] = observed["previous_status"]
        settled = self._settle_interrupt_event(settlement_event, receipt=observed)
        if not settled:
            self._finalize_interrupt_attempt_receipt(observed)
            return False
        self._finalize_interrupt_attempt_receipt(observed)
        return True

    def postflight_interrupt(self, payload: Mapping[str, Any]) -> bool:
        event = self._postflight_observation(payload)
        if event["kind"] != "interrupt_attempt_observation":
            raise ControlPlaneError("native tool result is not an interrupt")
        return self._observe_interrupt_attempt(event)

    def _settle_interrupt_event(
        self,
        event: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any],
    ) -> bool:
        tool_use_id = str(event["tool_use_id"])
        target = str(event["target"])
        previous_status = str(event["previous_status"])
        if (
            receipt.get("tool_use_id") != tool_use_id
            or receipt.get("owner") != target
        ):
            raise ControlPlaneError("interrupt observation is not receipt-bound")
        if not self.state_path.exists():
            return False
        receipts: list[tuple[str, dict[str, Any]]] = []
        with self._coordinated_state() as state:
            dispatch = state["dispatches"].get(receipt.get("dispatch_id"))
            if not isinstance(dispatch, dict) or not self._interrupt_receipt_is_current(
                state, dispatch, receipt
            ):
                return False
            if (
                previous_status in {"interrupted", "pending_init", "running"}
                and dispatch["state"] in {"running", "ready_to_apply", "paused"}
            ):
                receipts.extend(
                    self._fence_cooperative_members_locked(
                        state, dispatch, "interrupted"
                    )
                )
            # Observation and pointer release are one settlement transaction.
            # A second state write would leave replay linked to an already-
            # consumed native call if the Hook exited between those writes.
            dispatch["interrupt_receipt_id"] = None
            dispatch["interrupt_tool_use_id"] = None
            dispatch["interrupt_claim_expires_at"] = None
            dispatch["interrupt_unresolved"] = False
            self._write_state(state)
        deduplicated = {
            item["event_id"]: (kind, item) for kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))
        return True

    def record_result(self, owner: str, raw_result: object) -> dict[str, Any]:
        result = parse_result(raw_result)
        settled = self._record_normalized_result(owner, result)
        # Direct control-plane callers predate Hook receipts.  Preserve their
        # interface while finalizing a matching already-reserved slot, so a
        # late PostToolUse cannot revive the completed attempt.
        receipt = self._find_native_attempt_receipt(
            dispatch_id=result["dispatch_id"],
            owner=owner,
        )
        if receipt is not None and receipt.get("cursor") == result.get("cursor"):
            observed = receipt
            if receipt.get("phase") != "result_observed":
                observed = dict(receipt)
                observed["owner"] = owner
                observed["observation"] = {
                    "kind": "valid_result",
                    "result": result,
                    "result_sha256": "sha256:" + hashlib.sha256(
                        canonical_bytes(result)
                    ).hexdigest(),
                }
                observed["result_sha256"] = observed["observation"]["result_sha256"]
                observed["phase"] = "result_observed"
                observed = self._write_native_attempt_receipt(observed)
            self._release_settled_result_receipt(observed)
        return settled

    @staticmethod
    def _cooperative_wave_dispatches(
        state: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        records = [
            item
            for item in state["dispatches"].values()
            if isinstance(item, dict)
            and item.get("wave_id") == dispatch.get("wave_id")
            and _is_cooperative_dispatch(item)
        ]
        if not _cooperative_group_size_valid(len(records)):
            raise ControlPlaneError("cooperative wave has an invalid writer count")
        if len({item.get("unit_id") for item in records}) != len(records):
            raise ControlPlaneError("cooperative wave unit identity is ambiguous")
        return sorted(records, key=lambda item: str(item["unit_id"]))

    def _record_cooperative_result(
        self,
        owner: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Hold a successful isolate result until every batch peer is ready."""

        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, result["dispatch_id"])
            if not _is_cooperative_dispatch(dispatch):
                raise ControlPlaneError("dispatch is not a cooperative writer")
            if (
                dispatch.get("state") == "retired"
                and dispatch.get("owner") == owner
                and dispatch.get("result") == result
            ):
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "members": dispatch["members"],
                    "replayed": True,
                    "state": dispatch["state"],
                    "verification": dispatch.get("isolate_verification"),
                }
            was_ready = dispatch.get("state") == "ready_to_apply"
            if dispatch.get("state") not in {"starting", "running", "ready_to_apply"}:
                raise ControlPlaneError("result owner is stale or fenced")
            if was_ready and (
                dispatch.get("owner") != owner or dispatch.get("result") != result
            ):
                raise ControlPlaneError("result owner is stale or fenced")
            if dispatch.get("owner") is None:
                if not _owner_matches_task(owner, dispatch.get("task_name")):
                    raise ControlPlaneError("result owner does not match the prepared task")
                dispatch["owner"] = owner
                owner_was_pending = True
            elif dispatch.get("owner") != owner:
                raise ControlPlaneError("result owner is stale or fenced")
            else:
                owner_was_pending = False
            if result["cursor"] != dispatch.get("cursor"):
                raise ControlPlaneError("result cursor is stale")
            if (
                result["status"] != "complete"
                or result["outcome"] != "retire"
                or result["blockers"]
                or result["deviations"]
                or result["failure_signature"] is not None
            ):
                receipts = self._fence_cooperative_members_locked(
                    state, dispatch, "cooperative_incomplete_result"
                )
                self._write_state(state)
                self._finalize_detached_attempt_receipts(receipts)
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "members": dispatch["members"],
                    "state": "fenced",
                    "verification": None,
                }
            plan = self._read_plan(state)
            nodes = _node_map(plan)
            acceptance_ids = sorted(
                acceptance
                for member in dispatch["members"]
                for acceptance in nodes[member]["acceptance"]
            )
            if sorted(result["evidence"]) != acceptance_ids:
                raise ControlPlaneError("complete result does not cover every acceptance ID")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            records = self._cooperative_wave_dispatches(state, dispatch)
            isolation = dispatch.get("isolation")
            assert isinstance(isolation, Mapping)
            try:
                record = validate_isolation_record(
                    isolation.get("record"),
                    self.root,
                    terminal_recovery=dispatch.get("state")
                    in {"retired", "fenced", "rejected"},
                )
            except WriterIsolationError as error:
                receipts = self._fence_cooperative_members_locked(
                    state, dispatch, "isolate_identity_drift"
                )
                self._write_state(state)
                self._finalize_detached_attempt_receipts(receipts)
                raise ControlPlaneError(str(error)) from error
            snapshots = wave.get("isolate_snapshots")
            source = (
                snapshots.get(dispatch["unit_id"])
                if isinstance(snapshots, Mapping)
                else None
            )
            if not isinstance(source, Mapping):
                raise ControlPlaneError("cooperative isolate baseline is unavailable")
            canonical = deepcopy(self._dispatch_baseline(dispatch, wave))
            source_baseline = deepcopy(dict(source))
            workspace_root = Path(dispatch["workspace_root"])
            revision = state["revision"]
            if owner_was_pending:
                self._write_state(state)
                revision = state["revision"]

        try:
            # A canonical write by a child is detected here; CCO never attempts
            # to repair it, and the whole batch is fenced before integration.
            verify_isolation_canonical(
                workspace_root,
                canonical,
                scope=canonical["scopes"],
            )
            verification = verify_isolate(record, self.root, source_baseline)
        except WriterIsolationUnavailable as error:
            raise ControlPlaneUnavailable(str(error)) from error
        except WriterIsolationError as error:
            reason = (
                "canonical_drift"
                if str(error).startswith("canonical workspace drift:")
                else "isolate_identity_drift"
            )
            self._fence_cooperative_batch(str(result["dispatch_id"]), reason)
            raise ControlPlaneError(str(error)) from error
        actual = verification["owner_changed_paths"]
        if actual != result["changed_paths"]:
            self._fence_cooperative_batch(
                str(result["dispatch_id"]), "cooperative_delta_mismatch"
            )
            raise ControlPlaneError(
                "declared changed paths do not match the verified isolate delta"
            )

        with self._coordinated_state() as state:
            if state["revision"] != revision:
                raise ControlPlaneError("lifecycle changed while verifying cooperative result")
            dispatch = self._find_dispatch(state, result["dispatch_id"])
            if (
                dispatch.get("state") not in {"starting", "running", "ready_to_apply"}
                or dispatch.get("owner") != owner
                or not _is_cooperative_dispatch(dispatch)
            ):
                raise ControlPlaneError("cooperative result owner is stale or fenced")
            if dispatch.get("state") == "ready_to_apply" and dispatch.get("result") != result:
                raise ControlPlaneError("cooperative result changed while replaying")
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            records = self._cooperative_wave_dispatches(state, dispatch)
            dispatch["claim_expires_at"] = None
            dispatch["isolate_verification"] = verification
            dispatch["result"] = dict(result)
            dispatch["state"] = "ready_to_apply"
            dispatch["tool_use_id"] = None
            for member in dispatch["members"]:
                state["logical"][member]["state"] = "ready_to_apply"
            ready = all(
                item.get("state") == "ready_to_apply"
                and isinstance(item.get("result"), Mapping)
                and item["result"].get("status") == "complete"
                and item["result"].get("outcome") == "retire"
                for item in records
            )
            self._write_state(state)
            wave_id = str(dispatch["wave_id"])
            settled = {
                "dispatch_id": dispatch["dispatch_id"],
                "members": dispatch["members"],
                "state": dispatch["state"],
                "verification": verification,
            }
            if was_ready:
                settled["replayed"] = True
        return self._integrate_cooperative_wave(wave_id) if ready else settled

    def _integrate_cooperative_wave(self, wave_id: str) -> dict[str, Any]:
        """Apply every verified isolate delta as one recoverable transaction."""

        with self._coordinated_state() as state:
            if state.get("active_wave_id") != wave_id:
                raise ControlPlaneError("cooperative wave is no longer active")
            wave = self._read_wave(state)
            dispatches = [
                item
                for item in state["dispatches"].values()
                if item.get("wave_id") == wave_id and _is_cooperative_dispatch(item)
            ]
            if not _cooperative_group_size_valid(len(dispatches)) or not all(
                item.get("state") == "ready_to_apply" and isinstance(item.get("result"), Mapping)
                for item in dispatches
            ):
                raise ControlPlaneError("cooperative wave does not have every required result")
            previous_journal = state.get("cooperative_journal")
            if previous_journal is not None:
                try:
                    bound_journal = validate_isolation_journal(previous_journal, self.root)
                except WriterIsolationError as error:
                    raise ControlPlaneError(
                        "cooperative journal is invalid: " + str(error)
                    ) from error
                if bound_journal["phase"] not in {"applied", "rolled_back"}:
                    # A prior Hook can time out after persisting an ``applying``
                    # entry but before returning from the filesystem mutation.
                    # Recover that exact durable journal before any new apply;
                    # do not create a second backup or treat the tail as a new
                    # successful result.
                    self._recover_cooperative_journal_locked(state)
                    raise ControlPlaneError(
                        "cooperative apply recovery fenced the interrupted wave"
                    )
                raise ControlPlaneError(
                    "cooperative journal conflicts with a ready-to-apply wave"
                )
            changes: dict[str, dict[str, Any]] = {}
            try:
                for dispatch in sorted(dispatches, key=lambda item: str(item["unit_id"])):
                    isolation = dispatch.get("isolation")
                    assert isinstance(isolation, Mapping)
                    record = validate_isolation_record(isolation.get("record"), self.root)
                    canonical = self._dispatch_baseline(dispatch, wave)
                    snapshots = wave.get("isolate_snapshots")
                    source = (
                        snapshots.get(dispatch["unit_id"])
                        if isinstance(snapshots, Mapping)
                        else None
                    )
                    if not isinstance(source, Mapping):
                        raise WriterIsolationError("cooperative isolate baseline is unavailable")
                    verify_isolation_canonical(
                        Path(dispatch["workspace_root"]),
                        canonical,
                        scope=canonical["scopes"],
                    )
                    verification = verify_isolate(record, self.root, source)
                    if verification["owner_changed_paths"] != dispatch["result"]["changed_paths"]:
                        raise WriterIsolationError("cooperative isolate delta changed before apply")
                    ready_snapshot = dispatch.get("isolate_verification")
                    if (
                        not isinstance(ready_snapshot, Mapping)
                        or verification.get("current_state")
                        != ready_snapshot.get("current_state")
                    ):
                        raise WriterIsolationError(
                            "cooperative isolate changed after its ready snapshot"
                        )
                    for path in verification["owner_changed_paths"]:
                        if path in changes:
                            raise WriterIsolationError("cooperative isolate deltas overlap")
                        changes[path] = {"source_root": record["isolate_root"]}
            except WriterIsolationUnavailable as error:
                raise ControlPlaneUnavailable(str(error)) from error
            except WriterIsolationError as error:
                reason = (
                    "canonical_drift"
                    if str(error).startswith("canonical workspace drift:")
                    else "isolate_identity_drift"
                )
                receipts = self._fence_cooperative_members_locked(
                    state, dispatches[0], reason
                )
                self._write_state(state)
                self._finalize_detached_attempt_receipts(receipts)
                raise ControlPlaneError(str(error)) from error
            if not changes:
                # Empty, fully evidenced changes still retire atomically without
                # creating a zero-entry backup journal.
                journal: dict[str, Any] | None = None
            else:
                namespace_guard = acquire(
                    self.root,
                    ISOLATION_NAMESPACE_LOCK,
                    timeout=_isolation_lock_timeout(self.lock_timeout),
                )
                namespace_guard.__enter__()
                journal: dict[str, Any] | None = None
                journal_published = False
                try:
                    journal = stage_apply_journal(
                        self.root,
                        wave_id=wave_id,
                        canonical_root=Path(dispatches[0]["workspace_root"]),
                        changes=changes,
                    )
                except WriterIsolationUnavailable as error:
                    namespace_guard.__exit__(None, None, None)
                    receipts = self._fence_cooperative_members_locked(
                        state, dispatches[0], "cooperative_journal_unavailable"
                    )
                    self._write_state(state)
                    self._finalize_detached_attempt_receipts(receipts)
                    raise ControlPlaneUnavailable(str(error)) from error
                except WriterIsolationError as error:
                    namespace_guard.__exit__(None, None, None)
                    receipts = self._fence_cooperative_members_locked(
                        state, dispatches[0], "cooperative_journal_failed"
                    )
                    self._write_state(state)
                    self._finalize_detached_attempt_receipts(receipts)
                    raise ControlPlaneError(str(error)) from error
                except BaseException:
                    namespace_guard.__exit__(None, None, None)
                    raise
                try:
                    try:
                        canonical = self._dispatch_baseline(dispatches[0], wave)
                        verify_isolation_canonical(
                            Path(dispatches[0]["workspace_root"]),
                            canonical,
                            scope=canonical["scopes"],
                        )
                        ready_isolates: list[dict[str, Any]] = []
                        for dispatch in dispatches:
                            isolation = dispatch.get("isolation")
                            assert isinstance(isolation, Mapping)
                            record = validate_isolation_record(
                                isolation.get("record"), self.root
                            )
                            snapshots = wave.get("isolate_snapshots")
                            source = (
                                snapshots.get(dispatch["unit_id"])
                                if isinstance(snapshots, Mapping)
                                else None
                            )
                            ready_snapshot = dispatch.get("isolate_verification")
                            post_stage = verify_isolate(record, self.root, source)
                            if (
                                not isinstance(ready_snapshot, Mapping)
                                or post_stage.get("current_state")
                                != ready_snapshot.get("current_state")
                            ):
                                raise WriterIsolationError(
                                    "cooperative isolate changed after its ready snapshot"
                                )
                            ready_isolates.append(
                                {
                                    "baseline": deepcopy(dict(source)),
                                    "ready_state": str(ready_snapshot["current_state"]),
                                    "record": record,
                                }
                            )
                        canonical_identity = scoped_content_identity(
                            Path(dispatches[0]["workspace_root"]),
                            canonical["scopes"],
                        )
                    except WriterIsolationUnavailable as error:
                        receipts = self._fence_cooperative_members_locked(
                            state, dispatches[0], "canonical_drift"
                        )
                        self._write_state(state)
                        self._finalize_detached_attempt_receipts(receipts)
                        raise ControlPlaneUnavailable(str(error)) from error
                    except WriterIsolationError as error:
                        receipts = self._fence_cooperative_members_locked(
                            state, dispatches[0], "canonical_drift"
                        )
                        self._write_state(state)
                        self._finalize_detached_attempt_receipts(receipts)
                        raise ControlPlaneError(str(error)) from error
                    if len(canonical_bytes(journal)) > MAX_COOPERATIVE_JOURNAL_LIFECYCLE_BYTES:
                        receipts = self._fence_cooperative_members_locked(
                            state, dispatches[0], "cooperative_journal_failed"
                        )
                        self._write_state(state)
                        self._finalize_detached_attempt_receipts(receipts)
                        raise ControlPlaneError(
                            "cooperative lifecycle journal exceeds its capacity"
                        )
                    # Keep the namespace locked from physical creation through
                    # this first authoritative state publication.  A concurrent
                    # orphan scan can now see either no journal or its durable
                    # liveness record, never an unreferenced live backup.
                    state["cooperative_journal"] = journal
                    try:
                        self._write_state(state)
                    except _AtomicWriteUncertain:
                        # The authoritative state may already reference this
                        # backup tree.  Retain it for replay rather than
                        # deleting a journal whose publication was uncertain.
                        journal_published = True
                        raise
                    journal_published = True
                finally:
                    if journal is not None and not journal_published:
                        try:
                            cleanup_isolation_journal(journal, self.root)
                        except (WriterIsolationError, WriterIsolationUnavailable):
                            pass
                    namespace_guard.__exit__(None, None, None)
                journal["phase"] = "applying"
                state["cooperative_journal"] = journal
                self._write_state(state)
                try:
                    def persist_apply_progress() -> None:
                        state["cooperative_journal"] = journal
                        self._write_state(state)

                    apply_isolation_journal(
                        journal,
                        self.root,
                        canonical_identity=canonical_identity,
                        canonical_scopes=canonical["scopes"],
                        progress=persist_apply_progress,
                        ready_isolates=ready_isolates,
                    )
                except OperationDeadlineExceeded as error:
                    # ``applying`` and each entry's pre-mutation phase are
                    # written before that entry can change the canonical tree.
                    # Once the Hook budget is exhausted, attempting rollback
                    # under the same expired deadline would make a partial
                    # mutation less recoverable.  Leave the journal as the
                    # sole durable recovery authority and ask the host to
                    # replay/restart it with a fresh bounded operation.
                    raise ControlPlaneUnavailable(
                        "cooperative apply reached its deadline after durable progress; "
                        "retry lifecycle recovery"
                    ) from error
                except _AtomicWriteUncertain:
                    # A progress checkpoint may have been replaced before its
                    # directory sync failed.  Its durable receipt/state is the
                    # recovery authority; never start a second rollback from a
                    # stale in-memory journal.
                    raise
                except ControlPlaneUnavailable:
                    raise
                except (
                    WriterIsolationError,
                    WriterIsolationUnavailable,
                    ControlPlaneError,
                ) as apply_error:
                    journal["phase"] = "rolling_back"
                    state["cooperative_journal"] = journal
                    self._write_state(state)
                    try:
                        rollback_isolation_journal(journal, self.root)
                    except OperationDeadlineExceeded as rollback_error:
                        raise ControlPlaneUnavailable(
                            "cooperative apply rollback reached its deadline; "
                            "retry lifecycle recovery"
                        ) from rollback_error
                    except (WriterIsolationError, WriterIsolationUnavailable) as rollback_error:
                        journal["phase"] = "recovery_required"
                        state["cooperative_journal"] = journal
                        for item in dispatches:
                            item["state"] = "paused"
                            for member in item["members"]:
                                state["logical"][member]["state"] = "paused"
                        self._write_state(state)
                        raise ControlPlaneError(
                            "cooperative apply requires explicit recovery: "
                            + str(rollback_error)
                        ) from apply_error
                    journal["phase"] = "rolled_back"
                    state["cooperative_journal"] = journal
                    receipts = self._fence_cooperative_members_locked(
                        state, dispatches[0], "cooperative_apply_failed"
                    )
                    self._write_state(state)
                    self._finalize_detached_attempt_receipts(receipts)
                    raise ControlPlaneError("cooperative apply rolled back: " + str(apply_error)) from apply_error
                journal["phase"] = "applied"
                state["cooperative_journal"] = journal
            plan = self._read_plan(state)
            nodes = _node_map(plan)
            for dispatch in dispatches:
                dispatch["state"] = "retired"
                self._append_tombstone(state, dispatch, "retired")
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "retired"
                    state["logical"][member]["result"] = {
                        "changed_paths": [
                            path
                            for path in dispatch["result"]["changed_paths"]
                            if any(
                                repository_scopes_overlap(
                                    {"kind": "exact", "path": path}, scope
                                )
                                for scope in nodes[member]["scopes"]
                            )
                        ],
                        "evidence": {
                            key: dispatch["result"]["evidence"][key]
                            for key in nodes[member]["acceptance"]
                        },
                        "outcome": "retire",
                        "summary": dispatch["result"]["summary"],
                    }
            self._refresh_ready(state, plan)
            self._settle_wave(state)
            self._write_state(state)
            return {
                "dispatch_id": None,
                "members": [member for item in dispatches for member in item["members"]],
                "state": "retired",
                "verification": "cooperative_apply",
            }

    def _record_normalized_result(
        self,
        owner: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = dict(result)

        with self._coordinated_state() as state:
            candidate = self._find_dispatch(state, result["dispatch_id"])
            cooperative = _is_cooperative_dispatch(candidate)
        if cooperative:
            return self._record_cooperative_result(owner, result)

        def claim(
            state: dict[str, Any],
        ) -> tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, dict[str, Any]],
            dict[str, Any],
            list[dict[str, str]],
        ]:
            dispatch = self._find_dispatch(state, result["dispatch_id"])
            if dispatch["state"] not in {"running", "starting"}:
                raise ControlPlaneError("result owner is stale or fenced")
            if dispatch["state"] == "starting" and dispatch.get("tool_kind") not in {
                "spawn",
                "continuation",
                "reuse",
            }:
                raise ControlPlaneError("result owner is stale or fenced")
            if dispatch.get("owner") is None:
                if not _owner_matches_task(owner, dispatch.get("task_name")):
                    raise ControlPlaneError("result owner does not match the prepared task")
                dispatch["owner"] = owner
            elif dispatch.get("owner") != owner:
                raise ControlPlaneError("result owner is stale or fenced")
            expected_cursor = (
                dispatch.get("pending_cursor")
                if dispatch["state"] == "starting"
                and dispatch.get("tool_kind") == "continuation"
                else dispatch["cursor"]
            )
            if result["cursor"] != expected_cursor:
                raise ControlPlaneError("result cursor is stale")
            if result["outcome"] == "accept" and dispatch["role"] != "reviewer":
                raise ControlPlaneError("only a reviewer may claim acceptance")
            if result["outcome"] == "pause" and result["status"] == "complete":
                raise ControlPlaneError("a complete result cannot pause")
            plan = self._read_plan(state)
            nodes = _node_map(plan)
            acceptance_ids = sorted(
                {
                    acceptance
                    for member in dispatch["members"]
                    for acceptance in nodes[member]["acceptance"]
                }
            )
            if not set(result["evidence"]) <= set(acceptance_ids):
                raise ControlPlaneError(
                    "result evidence contains an unknown acceptance ID"
                )
            if (
                result["status"] == "complete"
                and sorted(result["evidence"]) != acceptance_ids
            ):
                raise ControlPlaneError(
                    "complete result does not cover every acceptance ID"
                )
            wave = self._read_wave(state)
            self._validate_dispatch_wave(dispatch, wave)
            sibling_writer_scopes = _sibling_writer_scopes(state, dispatch)
            if dispatch["role"] == "worker" and sibling_writer_scopes:
                raise ControlPlaneError(
                    "another write owner is already bound to this wave"
                )
            allowed = (
                dispatch["scopes"]
                if dispatch["role"] == "worker"
                else sibling_writer_scopes
            )
            return dispatch, plan, nodes, wave, allowed

        with self._coordinated_state() as state:
            existing = self._find_dispatch(state, result["dispatch_id"])
            if (
                existing.get("state") in {"paused", "retired"}
                and existing.get("owner") == owner
                and existing.get("result") == result
            ):
                return {
                    "dispatch_id": existing["dispatch_id"],
                    "members": existing["members"],
                    "replayed": True,
                    "state": existing["state"],
                    "verification": None,
                }
            owner_was_pending = self._find_dispatch(
                state, result["dispatch_id"]
            ).get("owner") is None
            dispatch, _plan, _nodes, wave, allowed = claim(state)
            if owner_was_pending:
                self._write_state(state)
            workspace_root = Path(dispatch["workspace_root"])
            baseline = deepcopy(self._dispatch_baseline(dispatch, wave))
            owner_scopes = deepcopy(dispatch["scopes"])
            role = dispatch["role"]
        try:
            verification = verify_workspace(
                workspace_root,
                baseline,
                allowed_scopes=allowed,
                owner_scopes=owner_scopes,
            )
        except (OperationDeadlineExceeded, WorkspaceGuardUnavailable) as error:
            raise ControlPlaneUnavailable(str(error)) from error
        except WorkspaceGuardError as error:
            raise ControlPlaneError(str(error)) from error
        actual = verification["owner_changed_paths"]
        if actual != result["changed_paths"]:
            raise ControlPlaneError(
                "declared changed paths do not match the verified owner delta"
            )
        if role != "worker" and actual:
            raise ControlPlaneError("read-only child changed its declared scope")

        with self._coordinated_state() as state:
            dispatch, plan, nodes, _wave, _allowed = claim(state)
            if dispatch.get("pending_cursor") is not None:
                dispatch["cursor"] = dispatch["pending_cursor"]
                dispatch["pending_cursor"] = None
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = None
            dispatch["result"] = result
            if result["outcome"] == "pause":
                dispatch["state"] = "paused"
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "paused"
                    state["logical"][member]["result"] = {
                        "evidence": {
                            key: result["evidence"][key]
                            for key in nodes[member]["acceptance"]
                            if key in result["evidence"]
                        },
                        "failure_signature": result["failure_signature"],
                        "summary": result["summary"],
                    }
            else:
                dispatch["state"] = "retired"
                self._append_tombstone(state, dispatch, "retired")
                for member in dispatch["members"]:
                    state["logical"][member]["state"] = "retired"
                    state["logical"][member]["result"] = {
                        "changed_paths": [
                            path
                            for path in actual
                            if any(
                                repository_scopes_overlap(
                                    {"kind": "exact", "path": path},
                                    scope,
                                )
                                for scope in nodes[member]["scopes"]
                            )
                        ],
                        "evidence": {key: result["evidence"][key] for key in nodes[member]["acceptance"]},
                        "outcome": result["outcome"],
                        "summary": result["summary"],
                    }
                self._refresh_ready(state, plan)
                self._settle_wave(state)
            self._write_state(state)
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "members": dispatch["members"],
                "state": dispatch["state"],
                "verification": verification,
            }

    def _settle_native_failure_locked(
        self,
        state: dict[str, Any],
        dispatch: dict[str, Any],
        kind: str,
        terminal_receipts: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        if _is_cooperative_dispatch(dispatch):
            terminal_receipts.extend(
                self._fence_cooperative_members_locked(
                    state, dispatch, "cooperative_native_failure"
                )
            )
            return _tool_action("fenced", None, None)
        if kind == "owner_unavailable":
            if (
                dispatch.get("state") != "starting"
                or dispatch.get("tool_kind") != "reuse"
                or not isinstance(dispatch.get("owner"), str)
                or dispatch.get("fallback_from_owner") is not None
            ):
                raise ControlPlaneError(
                    "only one prepared owner reuse can fall back to a fresh spawn"
                )
            route = self._selected_dispatch_route(dispatch)
            native = dispatch.get("native")
            if not isinstance(route, Mapping) or not isinstance(native, Mapping):
                raise ControlPlaneError("owner reuse fallback contract is invalid")
            previous_owner = dispatch["owner"]
            dispatch["native"] = {
                "agent_type": WRITE_ROLE
                if dispatch["role"] == "worker"
                else READ_ROLE,
                "fork_turns": "none"
                if dispatch["context_turns"] == 0
                else str(dispatch["context_turns"]),
                "message": native["message"],
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "task_name": dispatch["task_name"],
            }
            dispatch["fallback_from_owner"] = previous_owner
            dispatch["owner"] = None
            dispatch["tool_kind"] = "spawn"
            dispatch["tool_use_id"] = None
            dispatch["claim_expires_at"] = (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            )
            return _tool_action(
                "spawn_new_owner",
                "spawn_agent",
                dispatch["native"],
            )
        if kind == "route_rejected":
            fallback = self._reject_route_locked(state, dispatch)
            if fallback is None:
                return _tool_action("fenced", None, None)
            return _tool_action("fallback_route", "spawn_agent", fallback["native"])
        if kind == "other":
            self._fence_members(state, dispatch, "native_call_failed")
            self._settle_wave(state)
            return _tool_action("fenced", None, None)
        attempts = dispatch.get("transient_retries", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ControlPlaneError("transient retry counter is invalid")
        if attempts >= MAX_TRANSIENT_RETRIES:
            self._fence_members(state, dispatch, "transient_retry_exhausted")
            self._settle_wave(state)
            return _tool_action("fenced", None, None)
        attempts += 1
        dispatch["transient_retries"] = attempts
        dispatch["last_transient_failure"] = f"native_{kind}"
        dispatch["tool_use_id"] = None
        dispatch["claim_expires_at"] = None
        if dispatch["state"] == "starting" and dispatch["tool_kind"] in {
            "spawn",
            "reuse",
        }:
            dispatch["claim_expires_at"] = (
                _now_milliseconds() + NATIVE_CLAIM_TTL_MILLISECONDS
            )
            if dispatch["tool_kind"] == "reuse":
                return _tool_action("reuse_owner", "followup_task", dispatch["native"])
            return _tool_action("retry_same_call", "spawn_agent", dispatch["native"])
        if not isinstance(dispatch.get("owner"), str):
            raise ControlPlaneError("native failure dispatch has no continuation owner")
        if dispatch.get("pending_cursor") is None:
            cursor = dispatch["cursor"] + 1
            dispatch["native"] = {
                "message": _render_continue(
                    dispatch,
                    {"native_failure": kind, "retry": attempts},
                    cursor,
                ),
                "target": dispatch["owner"],
            }
            dispatch["pending_cursor"] = cursor
        dispatch["state"] = "paused"
        dispatch["tool_kind"] = "continuation"
        for member in dispatch["members"]:
            state["logical"][member]["state"] = "paused"
        return _tool_action(
            "continue_same_owner",
            "followup_task",
            dispatch["native"],
        )

    def settle_native_failure(self, dispatch_id: str, kind: str) -> dict[str, Any]:
        """Settle one Primary-observed typed native failure without parsing prose."""

        if kind not in NATIVE_FAILURE_KINDS:
            raise ControlPlaneError("native failure kind is invalid")
        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, dispatch_id)
            if dispatch["state"] == "paused":
                raise ControlPlaneError("native failure has no unsettled native call")
            if dispatch["state"] not in {"starting", "running"}:
                raise ControlPlaneError("native failure dispatch is not active")
            if dispatch["state"] == "starting" and not isinstance(
                dispatch.get("tool_use_id"), str
            ):
                raise ControlPlaneError("native failure has no unsettled native call")
            if kind == "route_rejected" and (
                dispatch["state"] != "starting" or dispatch["tool_kind"] != "spawn"
            ):
                raise ControlPlaneError("only a prepared spawn route can be rejected")
            if kind == "owner_unavailable" and (
                dispatch["state"] != "starting" or dispatch["tool_kind"] != "reuse"
            ):
                raise ControlPlaneError("only a prepared owner reuse can be unavailable")
            receipt = self._native_attempt_for_dispatch(dispatch)
            consumed_tool_use_id = dispatch.get("tool_use_id")
            if not _tool_use_id_valid(consumed_tool_use_id):
                consumed_tool_use_id = (
                    receipt.get("tool_use_id") if isinstance(receipt, Mapping) else None
                )
            if not _tool_use_id_valid(consumed_tool_use_id):
                raise ControlPlaneError("native failure has no consumed call identity")
            receipts: list[tuple[str, dict[str, Any]]] = []
            # A typed host failure means this exact call reached the native
            # boundary even though it has no PostToolUse success receipt. Keep
            # its call identity durably before releasing the receipt pointer.
            # Deliberately do not retain the input digest here: a *new* native
            # call ID is allowed to retry the same prepared envelope.
            self._append_tombstone(
                state,
                dispatch,
                "native_failure_consumed",
                consumed_tool_use_id=consumed_tool_use_id,
            )
            result = self._settle_native_failure_locked(
                state, dispatch, kind, receipts
            )
            if dispatch.get("receipt_id") is not None:
                dispatch["receipt_id"] = None
            if receipt is not None:
                receipts.append(("native", receipt))
            self._write_state(state)
        deduplicated = {
            item["event_id"]: (receipt_kind, item)
            for receipt_kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))
        return result

    def fence_invalid_result(self, owner: str, reason: str = "invalid_result") -> None:
        receipts: list[tuple[str, dict[str, Any]]] = []
        with self._coordinated_state() as state:
            matches = [
                item
                for item in state["dispatches"].values()
                if item["state"] in {"starting", "running", "paused"}
                and (
                    item.get("owner") == owner
                    or (
                        item.get("owner") is None
                        and _owner_matches_task(owner, item.get("task_name"))
                    )
                )
            ]
            for dispatch in matches:
                if dispatch.get("owner") is None:
                    dispatch["owner"] = owner
                receipts.extend(
                    self._fence_cooperative_members_locked(state, dispatch, reason)
                )
            if matches:
                self._write_state(state)
        deduplicated = {
            item["event_id"]: (kind, item) for kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))

    def close_unmappable_owner_leases(self) -> int:
        """Fail closed when SubagentStop cannot prove which child ended.

        A UUID-to-owner mapping failure is deterministic host metadata failure,
        not an infrastructure retry.  Leaving any matching active lease alive
        would make a later writer admission unsafe, so close the unresolved
        session leases in the same lifecycle transaction.
        """

        if not self.state_path.exists():
            return 0
        receipts: list[tuple[str, dict[str, Any]]] = []
        with self._coordinated_state() as state:
            count = 0
            for dispatch in state["dispatches"].values():
                if dispatch.get("state") not in {
                    "starting",
                    "running",
                    "ready_to_apply",
                    "paused",
                }:
                    continue
                receipts.extend(
                    self._fence_cooperative_members_locked(
                        state, dispatch, "unmappable_owner"
                    )
                )
                count += 1
            if count:
                self._settle_wave(state)
                self._write_state(state)
        deduplicated = {
            item["event_id"]: (kind, item) for kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))
        self._cleanup_unused_cooperative_isolates()
        return count

    def _cooperative_isolate_records(
        self,
        state: Mapping[str, Any],
        *,
        wave_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Derive owned isolate records from validated dispatches, never state mirrors."""

        records: list[dict[str, Any]] = []
        roots: set[str] = set()
        for dispatch in state["dispatches"].values():
            if not isinstance(dispatch, Mapping) or not _is_cooperative_dispatch(dispatch):
                continue
            if wave_id is not None and dispatch.get("wave_id") != wave_id:
                continue
            isolation = dispatch.get("isolation")
            assert isinstance(isolation, Mapping)
            try:
                record = validate_isolation_record(isolation.get("record"), self.root)
            except WriterIsolationError as error:
                raise ControlPlaneError(str(error)) from error
            root = record["isolate_root"]
            if root in roots:
                raise ControlPlaneError("cooperative isolate lifecycle records are ambiguous")
            roots.add(root)
            records.append(record)
        return sorted(records, key=lambda item: item["isolate_root"])

    def _preparing_isolate_roots(self, state: Mapping[str, Any]) -> list[str]:
        preparing = state.get("cooperative_preparing")
        if preparing is None:
            return []
        reservation = _validate_cooperative_preparing(
            preparing, plan_id=str(state["plan_id"])
        )
        try:
            return preparing_isolate_roots(
                self.root,
                canonical_root=Path(state["workspace_root"]),
                session_id=str(state["session_id"]),
                batch_id=reservation["batch_id"],
                count=len(reservation["members"]),
            )
        except WriterIsolationError as error:
            raise ControlPlaneError(str(error)) from error

    def _recover_cooperative_journal_locked(self, state: dict[str, Any]) -> bool:
        """Replay the only safe recovery action: exact rollback of an incomplete apply."""

        journal = state.get("cooperative_journal")
        if journal is None:
            return False
        try:
            bound = validate_isolation_journal(journal, self.root)
        except WriterIsolationError as error:
            raise ControlPlaneError("cooperative journal is invalid: " + str(error)) from error
        if bound["phase"] in {"applied", "rolled_back"}:
            return False
        bound["phase"] = "rolling_back"
        state["cooperative_journal"] = bound
        self._write_state(state)
        try:
            rollback_isolation_journal(bound, self.root)
        except OperationDeadlineExceeded as error:
            # ``rolling_back`` was persisted before the first rollback
            # mutation.  Do not fence or overwrite that recovery marker after
            # the Hook deadline; a fresh replay can continue the exact journal.
            raise ControlPlaneUnavailable(
                "cooperative apply recovery reached its deadline; retry lifecycle recovery"
            ) from error
        except (WriterIsolationError, WriterIsolationUnavailable) as error:
            bound["phase"] = "recovery_required"
            state["cooperative_journal"] = bound
            for dispatch in state["dispatches"].values():
                if _is_cooperative_dispatch(dispatch) and dispatch.get("state") not in {
                    "retired",
                    "fenced",
                    "rejected",
                }:
                    dispatch["state"] = "paused"
                    for member in dispatch["members"]:
                        state["logical"][member]["state"] = "paused"
            self._write_state(state)
            raise ControlPlaneError(
                "cooperative apply recovery requires explicit intervention: " + str(error)
            ) from error
        bound["phase"] = "rolled_back"
        state["cooperative_journal"] = bound
        matches = [
            dispatch
            for dispatch in state["dispatches"].values()
            if _is_cooperative_dispatch(dispatch)
        ]
        receipts: list[tuple[str, dict[str, Any]]] = []
        if matches:
            receipts = self._fence_cooperative_members_locked(
                state, matches[0], "cooperative_apply_recovered"
            )
        self._write_state(state)
        self._finalize_detached_attempt_receipts(receipts)
        return True

    def _recover_cooperative_preparing_locked(self, state: dict[str, Any]) -> bool:
        """Remove a pre-dispatch reservation and its deterministic partial roots."""

        preparing = state.get("cooperative_preparing")
        if preparing is None:
            return False
        reservation = _validate_cooperative_preparing(
            preparing, plan_id=str(state["plan_id"])
        )
        plan = self._read_plan(state)
        try:
            with acquire(
                self.root,
                ISOLATION_NAMESPACE_LOCK,
                timeout=_isolation_lock_timeout(self.lock_timeout),
            ):
                cleanup_preparing_isolates(
                    self.root,
                    canonical_root=Path(state["workspace_root"]),
                    session_id=str(state["session_id"]),
                    batch_id=reservation["batch_id"],
                    backend=str(plan["workspace_backend"]),
                    count=len(reservation["members"]),
                )
        except WriterIsolationUnavailable as error:
            raise ControlPlaneUnavailable(
                "cooperative preparation recovery is unavailable: " + str(error)
            ) from error
        except WriterIsolationError as error:
            raise ControlPlaneError(
                "cooperative preparation recovery is fenced: " + str(error)
            ) from error
        state.pop("cooperative_preparing", None)
        self._write_state(state)
        return True

    def _cleanup_terminal_cooperative_artifacts_locked(
        self, state: dict[str, Any]
    ) -> int:
        """Detach safely terminal isolate ownership, then reclaim its files."""

        records = self._cooperative_isolate_records(state)
        journal = state.get("cooperative_journal")
        if not records:
            return 0
        canonical_roots = {
            _workspace_key(item["recovery"]["canonical_root"]) for item in records
        }
        if len(canonical_roots) != 1:
            raise ControlPlaneError("cooperative isolate roots do not share a canonical workspace")
        record_roots = {item["isolate_root"] for item in records}
        dispatches = [
            item
            for item in state["dispatches"].values()
            if _is_cooperative_dispatch(item)
            and isinstance(item.get("isolation"), Mapping)
            and isinstance(item["isolation"].get("record"), Mapping)
            and item["isolation"]["record"].get("isolate_root") in record_roots
        ]
        bound: dict[str, Any] | None = None
        if journal is not None:
            if not isinstance(journal, Mapping):
                raise ControlPlaneError("cooperative journal is invalid")
            try:
                bound = validate_isolation_journal(journal, self.root)
            except WriterIsolationError as error:
                raise ControlPlaneError(
                    "cooperative journal is invalid: " + str(error)
                ) from error
        terminal_states = (
            {"fenced", "rejected", "retired"}
            if bound is not None and bound["phase"] == "rolled_back"
            else {"retired"}
        )
        if len(dispatches) != len(records) or not all(
            item.get("state") in terminal_states for item in dispatches
        ):
            return 0
        canonical_root = records[0]["recovery"]["canonical_root"]
        if self._has_live_cross_task_work(canonical_root):
            return 0
        if bound is not None and bound["phase"] not in {"applied", "rolled_back"}:
            return 0

        # Lifecycle ownership is authoritative.  Detach and publish it before
        # deleting any isolate or journal path; a process exit after this write
        # leaves only safe, unreferenced files for the bounded orphan scanner.
        for dispatch in dispatches:
            dispatch["isolation"] = None
            dispatch.pop("isolate_verification", None)
        state.pop("cooperative_journal", None)
        self._write_state(state)

        try:
            with acquire(
                self.root,
                ISOLATION_NAMESPACE_LOCK,
                timeout=_isolation_lock_timeout(self.lock_timeout),
            ):
                removed = cleanup_isolates(self.root, records)
                if bound is not None:
                    removed += cleanup_isolation_journal(bound, self.root)
        except WriterIsolationError as error:
            raise ControlPlaneError(str(error)) from error
        except WriterIsolationUnavailable as error:
            raise ControlPlaneUnavailable(str(error)) from error
        return removed

    def _cleanup_unused_cooperative_isolates(self) -> int:
        """Use lifecycle records, not a second ledger, as the isolate liveness set."""

        active: list[str] = []
        active_journals: list[str] = []
        try:
            with acquire(
                self.root,
                ISOLATION_NAMESPACE_LOCK,
                timeout=_isolation_lock_timeout(self.lock_timeout),
            ):
                scanned_bytes = 0
                for path in _state_json_paths(self.root):
                    raw = _read_bounded_bytes(path, "cco.v9 orphan liveness state")
                    scanned_bytes += len(raw)
                    if scanned_bytes > MAX_ISOLATION_LIVENESS_SCAN_BYTES:
                        # Cleanup is not authoritative.  If the complete
                        # liveness set cannot be read cheaply, retain every
                        # filesystem object and retry after stale states shrink.
                        return 0
                    state = self._validate_lifecycle_state(
                        _decode_object(raw, "cco.v9 orphan liveness state")
                    )
                    active.extend(self._preparing_isolate_roots(state))
                    active.extend(
                        record["isolate_root"]
                        for record in self._cooperative_isolate_records(state)
                    )
                    journal = state.get("cooperative_journal")
                    if journal is not None:
                        try:
                            active_journals.append(
                                validate_isolation_journal(journal, self.root)[
                                    "backup_root"
                                ]
                            )
                        except WriterIsolationError as error:
                            raise ControlPlaneError(str(error)) from error
                return cleanup_unused_isolate_batches(
                    self.root, active
                ) + cleanup_unused_journal_batches(self.root, active_journals)
        except (
            OSError,
            StateLockBusy,
            OperationDeadlineExceeded,
            ControlPlaneError,
            WriterIsolationError,
        ):
            # An incomplete liveness snapshot must never authorize deletion.
            return 0

    def _detach_restart_terminal_attempt_receipts_locked(
        self,
        state: Mapping[str, Any],
    ) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
        """Find terminal receipts left by an interrupted restart transaction.

        A restart commits the terminal fence before receipt acknowledgement.
        If the process dies after that state write, the state no longer points
        at its attempt receipts.  Derive them from the terminal dispatches so
        the restart receipt can finish the same transaction on replay instead
        of requiring a second receipt ledger.
        """

        terminal = [
            dispatch
            for dispatch in state["dispatches"].values()
            if dispatch.get("state") in {"fenced", "rejected", "retired"}
        ]
        changed = any(
            dispatch.get("receipt_id") is not None
            or dispatch.get("interrupt_receipt_id") is not None
            or dispatch.get("interrupt_tool_use_id") is not None
            or dispatch.get("interrupt_claim_expires_at") is not None
            for dispatch in terminal
        )
        return self._detach_terminal_attempt_receipts_locked(
            state,
            terminal,
        ), changed

    def restart(self) -> int:
        """Run CLI/manual recovery through the same durable restart receipt."""

        return self.process_restart_event("clear")

    def _settle_restart_event(self, event: Mapping[str, Any]) -> int:
        return self._restart(event)

    def _restart(self, event: Mapping[str, Any] | None) -> int:
        """Fence active work and finalize its receipts through ``event``.

        The state publication and native-receipt finalization are deliberately
        split: the state fence is authoritative, while acknowledgement/deletion
        is replayable through the restart receipt.  A replay that observes the
        already-incremented epoch therefore finishes receipt cleanup without
        incrementing the epoch again.
        """

        if not self.state_path.exists():
            return 0
        receipts: list[tuple[str, dict[str, Any]]] = []
        count = 0
        with self._coordinated_state() as state:
            replaying_committed_restart = False
            if event is not None and "plan_id" in event:
                if state.get("plan_id") != event.get("plan_id"):
                    # The receipt belongs to a superseded plan and cannot
                    # authorize a lifecycle write in its replacement.
                    return 0
                event_epoch = event.get("epoch")
                if state.get("epoch") == event_epoch + 1:
                    replaying_committed_restart = True
                elif state.get("epoch") != event_epoch:
                    # A newer restart already superseded this transaction.
                    return 0

            if replaying_committed_restart:
                receipts, changed = self._detach_restart_terminal_attempt_receipts_locked(
                    state
                )
                if changed:
                    self._write_state(state)
            else:
                prepared = self._recover_cooperative_preparing_locked(state)
                recovered = self._recover_cooperative_journal_locked(state)
                count = int(prepared) + int(recovered)
                for dispatch in state["dispatches"].values():
                    if dispatch["state"] in ACTIVE_STATES or _native_claim_active(dispatch):
                        receipts.extend(
                            self._fence_cooperative_members_locked(
                                state, dispatch, "host_restart"
                            )
                        )
                        count += 1
                # Sweep terminal dispatches too.  This handles a prior normal
                # lifecycle settlement whose receipt acknowledgement crashed,
                # while retaining state as the only source of receipt liveness.
                terminal_receipts, _changed = (
                    self._detach_restart_terminal_attempt_receipts_locked(state)
                )
                receipts.extend(terminal_receipts)
                self._settle_wave(state)
                state["epoch"] += 1
                if not recovered:
                    self._cleanup_terminal_cooperative_artifacts_locked(state)
                self._write_state(state)
        deduplicated = {
            item["event_id"]: (kind, item) for kind, item in receipts
        }
        self._finalize_detached_attempt_receipts(list(deduplicated.values()))
        self._cleanup_unused_cooperative_isolates()
        return count

    def abandon(self, node_id: str) -> None:
        receipts: list[tuple[str, dict[str, Any]]]
        with self._coordinated_state() as state:
            logical = state["logical"].get(node_id)
            if not isinstance(logical, dict) or logical["state"] != "paused":
                raise ControlPlaneError("only a paused node can be abandoned")
            dispatch = self._find_dispatch(state, logical["dispatch_id"])
            receipts = self._fence_cooperative_members_locked(state, dispatch, "abandoned")
            self._settle_wave(state)
            self._write_state(state)
        self._finalize_detached_attempt_receipts(receipts)

    def retry(self, node_id: str) -> None:
        with self._coordinated_state() as state:
            plan = self._read_plan(state)
            logical = state["logical"].get(node_id)
            if not isinstance(logical, dict) or logical["state"] != "fenced":
                raise ControlPlaneError("only a fenced node can start a newer generation")
            logical["generation"] += 1
            logical["assurance"] = "guarded"
            logical["dispatch_id"] = None
            logical["result"] = None
            nodes = _node_map(plan)
            logical["state"] = (
                "ready"
                if all(
                    self._logical_satisfied(state, plan, dependency)
                    for dependency in nodes[node_id]["depends_on"]
                )
                else "waiting"
            )
            self._write_state(state)

    def status(self) -> dict[str, Any]:
        with self._coordinated_state() as state:
            self._discard_unlinked_reserved_attempt_receipts(state)
            reconciled, expired_receipts = self._reconcile_expired_claims(state)
            if reconciled:
                self._write_state(state)
                self._discard_reserved_attempt_receipts(expired_receipts)
            plan = self._read_plan(state)
            counts = {name: 0 for name in sorted(LOGICAL_STATES)}
            for item in state["logical"].values():
                counts[item["state"]] += 1
            attention = []
            for node_id, logical in sorted(state["logical"].items()):
                if logical["state"] not in {"paused", "fenced"}:
                    continue
                dispatch_id = logical.get("dispatch_id")
                dispatch = state["dispatches"].get(dispatch_id, {})
                attention.append(
                    {
                        "dispatch_id": dispatch_id,
                        "nodes": [node_id],
                        "owner": dispatch.get("owner"),
                        "state": logical["state"],
                    }
                )
            for dispatch in sorted(
                state["dispatches"].values(),
                key=lambda item: item["dispatch_id"],
            ):
                if _native_settlement_overdue(dispatch):
                    attention.append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "nodes": list(dispatch["members"]),
                            "owner": dispatch.get("owner"),
                            "reason": "native_settlement_required",
                            "state": "starting",
                            "task_name": dispatch["task_name"],
                        }
                    )
                if dispatch["state"] == "running" and dispatch.get("owner") is None:
                    attention.append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "nodes": list(dispatch["members"]),
                            "owner": None,
                            "reason": "awaiting_native_owner",
                            "state": "running",
                            "task_name": dispatch["task_name"],
                        }
                    )
            journal = state.get("cooperative_journal")
            if isinstance(journal, Mapping) and journal.get("phase") == "recovery_required":
                attention.append(
                    {
                        "reason": "cooperative_recovery_required",
                        "state": "paused",
                        "wave_id": journal.get("wave_id"),
                    }
                )
            return {
                "attention": attention,
                "counts": counts,
                "epoch": state["epoch"],
                "plan_id": state["plan_id"],
                "protocol": "cco.status.v1",
                "state": self._overall_state(state, plan),
            }

    def cleanup(self) -> int:
        """Remove only this task's inactive v9 state and immutable artifacts."""

        pending = self.pending_event_reason()
        if pending is not None:
            raise ControlPlaneError(pending)

        def remove_artifacts() -> int:
            removed = 0
            for kind in ("plan", "wave"):
                for path in self._owned_artifact_paths(kind):
                    try:
                        path.unlink()
                        removed += 1
                    except FileNotFoundError:
                        pass
            return removed

        removed = 0
        if self.state_path.exists():
            with self._coordinated_state() as state:
                self._recover_cooperative_preparing_locked(state)
                self._recover_cooperative_journal_locked(state)
                self._discard_unlinked_reserved_attempt_receipts(state)
                reconciled, expired_receipts = self._reconcile_expired_claims(state)
                if reconciled:
                    self._write_state(state)
                    self._discard_reserved_attempt_receipts(expired_receipts)
                if any(
                    item["state"] in ACTIVE_STATES or _native_claim_active(item)
                    for item in state["dispatches"].values()
                ):
                    raise ControlPlaneError(
                        "active or paused child work must settle or be abandoned before cleanup"
                )
                cooperative_records = self._cooperative_isolate_records(state)
                cooperative_roots = {
                    item["isolate_root"] for item in cooperative_records
                }
                cooperative_dispatches = [
                    item
                    for item in state["dispatches"].values()
                    if _is_cooperative_dispatch(item)
                    and isinstance(item.get("isolation"), Mapping)
                    and isinstance(item["isolation"].get("record"), Mapping)
                    and item["isolation"]["record"].get("isolate_root")
                    in cooperative_roots
                ]
                if (
                    cooperative_records
                    and len(cooperative_dispatches) == len(cooperative_records)
                    and all(item.get("state") == "retired" for item in cooperative_dispatches)
                    and self._has_live_cross_task_work(
                        cooperative_records[0]["recovery"]["canonical_root"]
                    )
                ):
                    raise ControlPlaneError(
                        "cross-task workspace work must settle before cooperative cleanup"
                    )
                cooperative_removed = self._cleanup_terminal_cooperative_artifacts_locked(
                    state
                )
                with acquire(
                    self.root,
                    STATE_ROOT_LOCK,
                    timeout=_bounded_lock_timeout(self.lock_timeout),
                ):
                    prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
                    if any(
                        path.name.startswith(prefix)
                        for path in _pending_event_paths(self.root)
                    ):
                        raise ControlPlaneError(
                            "CCO has an unsettled native lifecycle receipt; finish restart "
                            "recovery before cleanup."
                        )
                    self.state_path.unlink()
                    _sync_directory(self.state_path.parent)
                removed = 1 + cooperative_removed + remove_artifacts()
        else:
            with acquire(self.root, self.session_id, timeout=self.lock_timeout):
                self._state_path = None
                if self.state_path.exists():
                    raise ControlPlaneUnavailable(
                        "lifecycle appeared during cleanup; retry the operation"
                    )
                removed = remove_artifacts()

        # A prior process may have exited after publishing state detachment but
        # before physical deletion.  The same bounded liveness scan completes
        # those now-unowned paths without storing a cleanup ledger.
        return removed + self._cleanup_unused_cooperative_isolates()

    def terminal_proof(self, owner: str, result: Mapping[str, Any]) -> dict[str, Any]:
        """Return a proof-backed retired dispatch for explicit host maintenance."""

        with self._coordinated_state() as state:
            dispatch = self._find_dispatch(state, str(result.get("dispatch_id")))
            if (
                dispatch.get("owner") != owner
                or dispatch.get("state") != "retired"
                or dispatch.get("result") != dict(result)
                or result.get("status") != "complete"
                or result.get("outcome") not in {"retire", "accept"}
                or result.get("blockers")
                or result.get("deviations")
            ):
                raise ControlPlaneError("CCO child is not proof-backed terminal work")
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "role": dispatch["role"],
                "state": dispatch["state"],
            }

    def stop_reason(self) -> str | None:
        if not self.state_path.exists():
            return None
        with self._coordinated_state() as state:
            self._discard_unlinked_reserved_attempt_receipts(state)
            reconciled, expired_receipts = self._reconcile_expired_claims(state)
            if reconciled:
                self._write_state(state)
                self._discard_reserved_attempt_receipts(expired_receipts)
            if any(
                item["state"] in ACTIVE_STATES or _native_claim_active(item)
                for item in state["dispatches"].values()
            ):
                return "CCO child work is still active; wait for its native terminal event."
            return None

    def _reconcile_attempt_reservations(self) -> None:
        """Recover expired and pre-link attempt reservations for this session."""

        if self.state_path.exists():
            with self._coordinated_state() as state:
                self._discard_unlinked_reserved_attempt_receipts(state)
                reconciled, expired_receipts = self._reconcile_expired_claims(state)
                if reconciled:
                    self._write_state(state)
                    self._discard_reserved_attempt_receipts(expired_receipts)
            return
        # A reservation without any lifecycle state can only have been
        # published before preflight linked it.  It never authorized a host
        # call, so release just those bounded attempt slots while retaining
        # unrelated current receipts for their owning lifecycle.
        with acquire(self.root, self.session_id, timeout=self.lock_timeout):
            self._state_path = None
            if self.state_path.exists():
                raise ControlPlaneUnavailable(
                    "lifecycle appeared while reconciling pending receipts"
                )
            self._discard_unlinked_reserved_attempt_receipts({})

    def pending_event_reason(self) -> str | None:
        """Expose unresolved one-shot receipts without performing heavy settlement."""

        self._reconcile_attempt_reservations()
        prefix = f".cco-pending-s{_session_digest(self.session_id)}-"
        with acquire(
            self.root,
            STATE_ROOT_LOCK,
            timeout=_bounded_lock_timeout(self.lock_timeout),
        ):
            pending = any(
                path.name.startswith(prefix) for path in _pending_event_paths(self.root)
            )
        if not pending:
            return None
        return (
            "CCO has an unsettled native lifecycle receipt; finish restart recovery "
            "before stopping."
        )


def _session_arg() -> str:
    session = os.environ.get("CODEX_THREAD_ID")
    if not session:
        raise ControlPlaneError("CODEX_THREAD_ID is unavailable")
    return session


def _stdin_json() -> Any:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ControlPlaneError("input exceeds 1 MiB")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=parse_safe_integer,
        )
    except (ControlPlaneError, ProtocolHashError) as error:
        raise ControlPlaneError(str(error)) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlPlaneError("input is not valid UTF-8 JSON") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Compile and operate one compact cco.v9 plan.")
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo", type=Path, default=Path.cwd())
    prepare.add_argument("--capacity", type=int, default=1)
    prepare.add_argument("--catalog", type=Path)
    next_parser = sub.add_parser("next")
    next_parser.add_argument("--capacity", type=int, required=True)
    next_parser.add_argument("--catalog", type=Path)
    continuation = sub.add_parser("continue")
    continuation.add_argument("--dispatch", required=True)
    abandon = sub.add_parser("abandon")
    abandon.add_argument("--node", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("--node", required=True)
    native_failure = sub.add_parser("native-failure")
    native_failure.add_argument("--dispatch", required=True)
    native_failure.add_argument("--kind", choices=sorted(NATIVE_FAILURE_KINDS), required=True)
    sub.add_parser("status")
    sub.add_parser("restart")
    sub.add_parser("cleanup")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "prepare":
            compiled = compile_delegation_request(_stdin_json())
            if compiled["disposition"] != DELEGATE:
                result = {
                    "protocol": "cco.prepare.v1",
                    "reason": compiled["reason"],
                    "state": "primary_direct",
                }
            else:
                if args.capacity < 1:
                    raise ControlPlaneError("native capacity must be a positive integer")
                catalog = (
                    _load_object(args.catalog, "native catalogue")
                    if args.catalog
                    else load_native_catalog()
                )
                control = ControlPlane(_session_arg())
                control.create_plan(
                    args.repo,
                    compiled["plan"],
                    resume_identical=True,
                )
                result = control.next_wave(
                    capacity=args.capacity,
                    native_catalog=catalog,
                )
        else:
            session = _session_arg()
            control = ControlPlane(session)
            if args.command == "next":
                catalog = _load_object(args.catalog, "native catalogue") if args.catalog else None
                result = control.next_wave(capacity=args.capacity, native_catalog=catalog)
            elif args.command == "continue":
                result = control.prepare_continuation(args.dispatch, _stdin_json())
            elif args.command == "abandon":
                control.abandon(args.node)
                result = control.status()
            elif args.command == "retry":
                control.retry(args.node)
                result = control.status()
            elif args.command == "native-failure":
                result = control.settle_native_failure(args.dispatch, args.kind)
            elif args.command == "restart":
                result = {"interrupted": control.restart(), "protocol": "cco.restart.v1"}
            elif args.command == "cleanup":
                result = {"protocol": "cco.cleanup.v1", "removed": control.cleanup()}
            else:
                result = control.status()
    except (
        ControlPlaneError,
        DelegationCompilerError,
        OSError,
        RoutingCatalogError,
        StateLockBusy,
        WorkspaceGuardError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(canonical_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

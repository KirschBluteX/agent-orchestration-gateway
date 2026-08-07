#!/usr/bin/env python3
"""v8 lifecycle adapter: reserve, fence, verify, and retain tiny tombstones."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping

from protocol_envelope import load_utf8_json

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from packet_compiler import (  # noqa: E402
    READ_ROLE,
    normalize_capsule,
    parse_result_message,
    validate_result_for_dispatch,
)
from prepared_graph import (  # noqa: E402
    PreparedGraphError,
    cleanup_graph_artifact,
    cleanup_session_artifacts,
    cleanup_stale_artifacts,
    dispatch_workspace_claim,
    load_artifact,
    verify_artifact_workspace,
)
from dispatch_transaction import (  # noqa: E402
    CONTINUATION_TOOL_NAMES,
    cleanup_stale_dispatch_state,
    DispatchTransactionError,
    INTERRUPT_TOOL_NAMES,
    SPAWN_TOOL_NAMES,
    fence_spawn_call,
    graph_has_live_transaction,
    repository_for_owner,
    retire_for_host_restart as retire_dispatch_for_host_restart,
    retire_owner as retire_transaction_owner,
    session_has_live_transaction,
    settle_spawn_rejection,
    settle_spawn_success,
    spawn_claim_for_call,
    stop_outcome as transaction_stop_outcome,
    user_prompt_context as transaction_user_prompt_context,
)
from protocol_hash import canonical_bytes, repository_scopes_overlap  # noqa: E402
from task_ledger import LedgerBusy, LedgerConflict, TaskLedger  # noqa: E402
from state_lock import acquire as acquire_state_lock  # noqa: E402
from host_paths import HostPathError, host_path, is_within  # noqa: E402
from rollout_io import RolloutError, first_record, is_rollout_path  # noqa: E402
from workspace_state import StateError, repository_root  # noqa: E402


SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
TASK_NAME = re.compile(
    r"^(?:(?:explorer|worker)_[a-z0-9][a-z0-9_]*_g[0-9]{2,}|review_e[0-9]{2,}_[a-z0-9][a-z0-9_]*_g[0-9]{2,})$"
)
V8_HEADER = "CCO_DISPATCH cco.v8"
TERMINAL_STALE_SECONDS = 24 * 60 * 60
LIVE_STALE_SECONDS = 7 * TERMINAL_STALE_SECONDS
REVIEW_SEED_MAX_BYTES = 64 * 1024
SESSION_CONTEXT = (
    "CCO is mandatory for every native Agent spawn. Prepare one closed cco.v8 "
    "graph before dispatch. Only an explicit user-authorized CCO_NATIVE_BYPASS v1 "
    "may use native inheritance. CCO leaves never delegate. After dispatch, wait for "
    "a native terminal, blocking-input, or user event; never forward opaque progress. "
    "A Codex Desktop restart retires and fences active or dispatching children as "
    "host_restart before the next task continues."
)


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _has_reparse_ancestor(path: Path) -> bool:
    absolute = Path(os.path.abspath(path.expanduser()))
    return any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents))


def _ledger_root(payload: Mapping[str, Any]) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    configured_root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    absolute_root = Path(os.path.abspath(configured_root))
    if _has_reparse_ancestor(absolute_root):
        raise ValueError("ledger directory cannot use a reparse ancestor")
    try:
        root = absolute_root.resolve()
        cwd = Path(payload.get("cwd")).resolve() if isinstance(payload.get("cwd"), str) else Path.cwd().resolve()
        try:
            protected_root = repository_root(cwd)
        except StateError:
            protected_root = cwd
        if root == protected_root or protected_root in root.parents or root in protected_root.parents:
            raise ValueError("ledger directory must be outside repository")
    except OSError as error:
        raise ValueError("ledger directory cannot be resolved") from error
    return root


def ledger_for(payload: Mapping[str, Any]) -> TaskLedger | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        return None
    return TaskLedger(_ledger_root(payload), session_id)


def _sessions_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    codex_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    try:
        return host_path(codex_home / "sessions").resolve()
    except (HostPathError, OSError) as error:
        raise LedgerConflict("Codex sessions root is unavailable") from error


def _transcript_owner(payload: Mapping[str, Any], agent_id: str) -> str:
    transcript_value = payload.get("agent_transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value:
        raise LedgerConflict("UUID result has no agent transcript")
    try:
        transcript = host_path(transcript_value)
    except HostPathError as error:
        raise LedgerConflict("agent transcript path is unsupported") from error
    sessions_root = _sessions_root()
    if _is_reparse(transcript):
        raise LedgerConflict("agent transcript cannot be a reparse point")
    try:
        resolved = transcript.resolve(strict=True)
        if not is_within(sessions_root, resolved):
            raise LedgerConflict("agent transcript is outside the Codex sessions root")
    except (OSError, ValueError) as error:
        if isinstance(error, LedgerConflict):
            raise
        raise LedgerConflict("agent transcript is unavailable") from error
    if not is_rollout_path(resolved) or not (
        resolved.name.endswith(f"-{agent_id}.jsonl")
        or resolved.name.endswith(f"-{agent_id}.jsonl.zst")
    ):
        raise LedgerConflict("agent transcript does not match the native thread")
    try:
        record = first_record(resolved)
    except RolloutError as error:
        raise LedgerConflict("agent session metadata is invalid") from error
    metadata = record.get("payload") if isinstance(record, Mapping) else None
    if record.get("type") != "session_meta" or not isinstance(metadata, Mapping):
        raise LedgerConflict("agent transcript does not begin with session metadata")
    if metadata.get("id") != agent_id:
        raise LedgerConflict("agent session metadata does not match the native thread")
    session_id = payload.get("session_id")
    if metadata.get("parent_thread_id") != session_id:
        raise LedgerConflict("agent session metadata does not match the parent task")
    owner_values: list[str] = []
    direct_owner = metadata.get("agent_path")
    if isinstance(direct_owner, str):
        owner_values.append(direct_owner)
    source = metadata.get("source")
    if isinstance(source, Mapping):
        subagent = source.get("subagent")
        if isinstance(subagent, Mapping):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, Mapping) and isinstance(spawn.get("agent_path"), str):
                owner_values.append(str(spawn["agent_path"]))
    owners = set(owner_values)
    if len(owners) != 1:
        raise LedgerConflict("agent session metadata has no unique canonical owner")
    owner = owners.pop()
    if TASK_PATH.fullmatch(owner) is None:
        raise LedgerConflict("agent session metadata owner is not canonical")
    return owner


def _result_owner(payload: Mapping[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str):
        raise LedgerConflict("result has no native agent identity")
    if TASK_PATH.fullmatch(agent_id) is not None:
        return agent_id
    if THREAD_ID.fullmatch(agent_id) is not None:
        return _transcript_owner(payload, agent_id)
    raise LedgerConflict("result has no canonical owner mapping")


def prepared_workspace_claim(payload: Mapping[str, Any], capsule: Mapping[str, Any]) -> dict[str, Any]:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise LedgerConflict("prepared baseline requires exact session identity")
    try:
        transaction_claim = spawn_claim_for_call(payload)
        repo = (
            Path(str(transaction_claim["repo"]))
            if transaction_claim is not None
            else Path(payload["cwd"])
            if isinstance(payload.get("cwd"), str)
            else Path.cwd()
        )
        return dispatch_workspace_claim(
            ledger_root=_ledger_root(payload),
            session_id=session_id,
            capsule=capsule,
            repo=repo,
        )
    except (DispatchTransactionError, PreparedGraphError) as error:
        raise LedgerConflict("prepared baseline artifact is invalid") from error


def claim_from_fields(fields: Mapping[str, Any], *, role: str | None = None, workspace: Mapping[str, Any] | None = None) -> dict[str, object]:
    capsule = normalize_capsule(dict(fields))
    contract = capsule["contract"]
    claim: dict[str, object] = {
        "node": capsule["node"],
        "contract_rev": int(contract.get("contract_rev", 1)),
        "contract_sha256": "sha256:" + hashlib.sha256(canonical_bytes(contract)).hexdigest(),
        "input_sha256": capsule["capsule_sha256"],
        "generation": capsule["generation"],
        "cursor": capsule["execution"]["cursor"],
        "epoch": capsule.get("epoch"),
        "fork_turns": capsule["execution"]["fork_turns"],
        "role": role or capsule["role"],
        "assurance": capsule["assurance"],
        "acceptance_ids": capsule["acceptance_ids"],
        "run": capsule["execution"]["task_name"],
        "route": {"assurance": capsule["assurance"], **capsule["route"]},
    }
    if workspace is not None:
        if capsule["workspace_root"] != workspace.get("repo"):
            raise LedgerConflict("capsule workspace root does not match prepared workspace")
        claim.update({
            "baseline": workspace.get("baseline"),
            "baseline_path": workspace.get("baseline_path"),
            "graph_scopes": workspace.get("graph_scopes"),
            "graph_sha256": workspace.get("graph_sha256"),
            "repo": workspace.get("repo"),
            "scopes": workspace.get("scopes"),
            "workspace_mode": workspace.get("workspace_mode"),
            "workspace_backend": workspace.get("workspace_backend"),
        })
    return claim


def reserve_spawn(payload: Mapping[str, Any], fields: Mapping[str, Any], role: str, *, workspace: Mapping[str, Any] | None = None) -> None:
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not isinstance(call_id, str):
        return
    ledger.reserve(call_id, claim_from_fields(fields, role=_logical_role_from_physical(fields, role), workspace=workspace))


def _logical_role_from_physical(fields: Mapping[str, Any], physical: str) -> str:
    capsule = normalize_capsule(dict(fields))
    expected = "cost_orchestrator_write_leaf" if capsule["role"] == "worker" else READ_ROLE
    if physical != expected:
        raise LedgerConflict("physical and logical roles do not match")
    return capsule["role"]


def _task_paths(value: Any, *, key: str = "") -> set[str]:
    if isinstance(value, Mapping):
        found: set[str] = set()
        for child_key, child in value.items():
            found.update(_task_paths(child, key=str(child_key)))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_task_paths(child, key=key))
        return found
    if not isinstance(value, str):
        return set()
    if TASK_PATH.fullmatch(value):
        return {value}
    if key in {"task_name", "canonical_task_path", "task_path", "agent_name"} and TASK_NAME.fullmatch(value):
        return {f"/root/{value}"}
    try:
        return _task_paths(json.loads(value), key=key)
    except (TypeError, ValueError):
        return set()


def _native_rejection(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = key.casefold()
            if normalized in {"error", "iserror", "rejected", "failed"} and (child is True or (isinstance(child, str) and child)):
                return True
            if normalized == "status" and isinstance(child, str) and child.casefold() in {"error", "failed", "rejected"}:
                return True
            if _native_rejection(child):
                return True
    elif isinstance(value, list):
        return any(_native_rejection(child) for child in value)
    return False


def postflight_spawn(payload: Mapping[str, Any]) -> dict[str, str]:
    # A v2 reference was expanded during PreToolUse.  PostToolUse may expose the
    # original ref or the updated v8 input, so the exact call id is authoritative.
    transaction_claim = None
    if isinstance(payload.get("session_id"), str):
        try:
            transaction_claim = spawn_claim_for_call(payload)
        except DispatchTransactionError as error:
            return {"decision": "block", "reason": f"CCO transaction state is invalid: {error}"}
    if transaction_claim is not None:
        ledger = ledger_for(payload)
        call_id = payload.get("tool_use_id")
        if ledger is None or not ledger.path.exists() or not isinstance(call_id, str):
            try:
                fence_spawn_call(payload)
            except DispatchTransactionError:
                pass
            return {"decision": "block", "reason": "CCO transaction spawn lost its reservation"}
        if transaction_claim["state"] != "dispatching":
            try:
                ledger.release(call_id)
                row = ledger.exhaust_rejection(call_id)
                fence_spawn_call(payload)
                _cleanup_terminal_graph_artifact(ledger, payload, row)
            except (LedgerConflict, DispatchTransactionError):
                pass
            return {"decision": "block", "reason": "CCO transaction spawn was fenced before native activation"}
        paths = _task_paths(payload.get("tool_response"))
        expected_owner = transaction_claim["owner"]
        if len(paths) == 1 and next(iter(paths)) == expected_owner:
            try:
                ledger.activate(call_id, expected_owner)
                settle_spawn_success(payload, expected_owner)
                return {}
            except (LedgerConflict, DispatchTransactionError) as error:
                try:
                    fence_spawn_call(payload)
                except DispatchTransactionError:
                    pass
                return {"decision": "block", "reason": f"CCO transaction activation failed: {error}"}
        if not paths and _native_rejection(payload.get("tool_response")):
            try:
                ledger.release(call_id)
                fallback_available = settle_spawn_rejection(payload)
                if not fallback_available:
                    row = ledger.exhaust_rejection(call_id)
                    _cleanup_terminal_graph_artifact(ledger, payload, row)
                return {}
            except (LedgerConflict, DispatchTransactionError) as error:
                try:
                    fence_spawn_call(payload)
                except DispatchTransactionError:
                    pass
                return {"decision": "block", "reason": f"CCO transaction rejection could not settle: {error}"}
        try:
            ledger.release(call_id)
            fence_spawn_call(payload)
        except (LedgerConflict, DispatchTransactionError):
            pass
        return {"decision": "block", "reason": "CCO transaction spawn did not expose its exact native owner"}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping) or not isinstance(tool_input.get("message"), str) or not tool_input["message"].startswith(V8_HEADER):
        return {}
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not ledger.path.exists() or not isinstance(call_id, str):
        return {}
    paths = _task_paths(payload.get("tool_response"))
    if len(paths) == 1:
        try:
            ledger.activate(call_id, next(iter(paths)))
            return {}
        except LedgerConflict as error:
            return {"decision": "block", "reason": f"CCO spawn result violated the reserved task path: {error}"}
    if not paths and _native_rejection(payload.get("tool_response")):
        ledger.release(call_id)
        return {}
    return {"decision": "block", "reason": "CCO spawn result did not expose one canonical task path"}


def preflight_continuation(payload: Mapping[str, Any], fields: Mapping[str, Any] | None = None) -> None:
    ledger = ledger_for(payload)
    if ledger is None or not ledger.path.exists():
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return
    target = tool_input.get("target")
    if fields is None:
        if ledger.is_managed_owner(target):
            raise LedgerConflict("managed CCO owner requires a v8 continuation capsule")
        return
    call_id = payload.get("tool_use_id")
    if not isinstance(target, str) or not isinstance(call_id, str):
        raise LedgerConflict("continuation has no exact call or target")
    capsule = normalize_capsule(dict(fields))
    workspace = prepared_workspace_claim(payload, capsule)
    matching = [row for row in ledger.read_rows() if row.get("owner") == target]
    if len(matching) != 1:
        raise LedgerConflict("continuation owner is missing or ambiguous")
    row = matching[0]
    candidate = claim_from_fields(capsule, role=str(row["role"]), workspace=workspace)
    immutable = (
        "acceptance_ids",
        "assurance",
        "baseline",
        "baseline_path",
        "contract_rev",
        "contract_sha256",
        "epoch",
        "fork_turns",
        "generation",
        "graph_scopes",
        "graph_sha256",
        "node",
        "role",
        "repo",
        "route",
        "run",
        "scopes",
        "workspace_mode",
        "workspace_backend",
    )
    if any(row.get(name) != candidate.get(name) for name in immutable):
        raise LedgerConflict("continuation immutable dispatch fields do not match owner")
    ledger.prepare_continuation(
        call_id,
        target,
        previous_input_sha256=str(capsule.get("previous_capsule_sha256")),
        next_input_sha256=str(capsule["capsule_sha256"]),
        cursor=int(capsule["execution"]["cursor"]),
    )


def postflight_continuation(payload: Mapping[str, Any]) -> None:
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not ledger.path.exists() or not isinstance(call_id, str):
        return
    ledger.settle_pending_continuation(call_id, accepted=not _native_rejection(payload.get("tool_response")))


def preflight_interrupt(payload: Mapping[str, Any]) -> None:
    ledger = ledger_for(payload)
    if ledger is None or not ledger.path.exists():
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping) or not isinstance(tool_input.get("target"), str) or TASK_PATH.fullmatch(tool_input["target"]) is None:
        raise LedgerConflict("interrupt target is not canonical")
    retired = ledger.retire_if_present(tool_input["target"])
    if retired:
        retire_transaction_owner(payload, tool_input["target"], fenced=True)


def _cleanup_terminal_graph_artifact(
    ledger: TaskLedger,
    payload: Mapping[str, Any],
    row: Mapping[str, Any],
) -> None:
    if not row.get("baseline_path"):
        return
    graph_identity = str(row["graph_sha256"])
    try:
        artifact = load_artifact(Path(str(row["baseline_path"])))
    except PreparedGraphError:
        return
    dispatch_nodes = set(artifact["dispatch_nodes"])
    graph_rows = [
        candidate
        for candidate in ledger.read_rows()
        if candidate.get("graph_sha256") == graph_identity
    ]
    terminal_nodes = {
        str(candidate["node"])
        for candidate in graph_rows
        if candidate.get("state") in {"exhausted", "retired"}
    }
    if dispatch_nodes <= terminal_nodes and all(
        candidate.get("state") in {"exhausted", "retired"}
        for candidate in graph_rows
    ):
        if graph_has_live_transaction(payload, graph_identity):
            return
        cleanup_graph_artifact(
            ledger.root,
            str(payload["session_id"]),
            graph_identity,
        )


def retire_invalid_subagent_stop(payload: Mapping[str, Any]) -> None:
    """Fence a final invalid result without issuing another stop block."""

    ledger = ledger_for(payload)
    if ledger is None or not ledger.path.exists():
        raise LedgerConflict("v8 result has no live dispatch ledger")
    owner = _result_owner(payload)
    row = ledger.retire_after_invalid_stop(owner)
    retire_transaction_owner(payload, owner, fenced=True)
    _cleanup_terminal_graph_artifact(ledger, payload, row)


def accept_subagent_result(payload: Mapping[str, Any], fields: Mapping[str, Any] | None = None) -> None:
    ledger = ledger_for(payload)
    parsed = fields or parse_result_message(payload.get("last_assistant_message"))
    if ledger is None or not ledger.path.exists():
        raise LedgerConflict("v8 result has no live dispatch ledger")
    owner = _result_owner(payload)
    rows = [row for row in ledger.read_rows() if row.get("owner") == owner and row.get("input_sha256") == parsed.get("dispatch_sha256")]
    if len(rows) != 1:
        raise LedgerConflict("v8 result dispatch identity is stale")
    row = rows[0]
    if row.get("state") not in {"owned", "continuable"}:
        raise LedgerConflict("v8 result dispatch identity is stale")
    physical = payload.get("agent_type")
    expected_physical = "cost_orchestrator_write_leaf" if row["role"] == "worker" else READ_ROLE
    if physical != expected_physical:
        raise LedgerConflict("result physical role does not match owner")
    if parsed.get("disposition") == "accept" and (parsed.get("status") != "complete" or row.get("role") != "reviewer" or physical != READ_ROLE):
        raise LedgerConflict("only a complete reviewer may return accept")
    try:
        parsed = validate_result_for_dispatch(
            parsed,
            role=str(row["role"]),
            acceptance_ids=row["acceptance_ids"],
        )
    except ValueError as error:
        raise LedgerConflict(str(error)) from error
    if "baseline_path" in row:
        try:
            repo = (
                Path(str(row["repo"]))
                if isinstance(row.get("repo"), str)
                else repository_for_owner(payload, owner)
            )
            verification = verify_artifact_workspace(
                Path(str(row["baseline_path"])),
                repo=repo,
                baseline=str(row["baseline"]),
                graph_sha256_value=str(row["graph_sha256"]),
                graph_scopes_value=row["graph_scopes"],
                workspace_mode=str(row["workspace_mode"]),
            )
        except (DispatchTransactionError, PreparedGraphError) as error:
            raise LedgerConflict("result workspace artifact is invalid") from error
        if verification["verdict"] != "pass":
            raise LedgerConflict("result workspace verification failed: " + ",".join(verification["violations"]))
        declared_paths = parsed["payload"]["changed_paths"]
        actual_node_paths = sorted(
            path
            for path in verification["changed_paths"]
            if any(
                repository_scopes_overlap(scope, {"kind": "exact", "path": path})
                for scope in row["scopes"]
            )
        )
        if declared_paths != actual_node_paths:
            raise LedgerConflict("result changed paths do not match the exact node workspace delta")
    disposition = "continuable" if parsed.get("disposition") == "continue" else "retired"
    result_payload = parsed["payload"]
    require_guarded = (
        parsed["status"] != "complete"
        or bool(result_payload["blockers"])
        or bool(result_payload["deviations"])
    )
    review_seed: dict[str, Any] | None = {
        "disposition": parsed["disposition"],
        "payload": parsed["payload"],
        "status": parsed["status"],
    }
    if len(canonical_bytes(review_seed)) > REVIEW_SEED_MAX_BYTES:
        review_seed = None
    ledger.record_result(
        node=str(row["node"]),
        contract_rev=int(row["contract_rev"]),
        run=str(row["run"]),
        generation=int(row["generation"]),
        input_sha256=str(row["input_sha256"]),
        owner=owner,
        disposition=disposition,
        cursor=int(row["cursor"]),
        require_guarded=require_guarded,
        review_seed=review_seed,
    )
    # SubagentStop is a native terminal event even when the TaskLedger retains
    # a continuable owner for an explicit later follow-up.
    retire_transaction_owner(payload, owner)
    if disposition == "retired":
        _cleanup_terminal_graph_artifact(ledger, payload, row)


def cleanup_terminal_prior_sessions(
    ledger_root: Path,
    *,
    keep_session_id: str,
) -> list[Path]:
    """Remove only validated terminal task ledgers from earlier sessions."""

    directory = Path(ledger_root)
    if not directory.is_dir():
        return []
    removed: list[Path] = []
    for candidate in sorted(directory.glob("*.json"), key=lambda path: path.name):
        if (
            candidate.stem == keep_session_id
            or candidate.name.endswith(".dispatch-transactions.json")
        ):
            continue
        try:
            if session_has_live_transaction(directory, candidate.stem):
                continue
            ledger = TaskLedger(directory, candidate.stem)
            terminal = ledger.cleanup_if_terminal()
        except (
            DispatchTransactionError,
            LedgerBusy,
            LedgerConflict,
            OSError,
            ValueError,
        ):
            continue
        if terminal and not candidate.exists():
            cleanup_session_artifacts(directory, candidate.stem)
            removed.append(candidate)
    return removed


def start_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    ledger = ledger_for(payload)
    if ledger is not None:
        session_id = str(payload.get("session_id"))
        source = payload.get("source")
        if source not in {None, "startup", "resume", "clear", "compact"}:
            raise LedgerConflict("SessionStart source is unsupported")
        if source != "compact":
            # Both stores share this OS-backed lock.  A crash can still occur
            # between file replacements, so the transition is deliberately
            # idempotent and is repeated on the next non-compact SessionStart.
            with acquire_state_lock(ledger.root, session_id):
                retired_owners = set(ledger.retire_for_host_restart())
                dispatch_recovery = retire_dispatch_for_host_restart(payload)
                retired_owners.update(dispatch_recovery["owners"])
            for graph_identity in dispatch_recovery["graphs"]:
                if not graph_has_live_transaction(payload, graph_identity):
                    cleanup_graph_artifact(ledger.root, session_id, graph_identity)
            if retired_owners:
                for row in ledger.read_rows():
                    if row.get("owner") in retired_owners and row.get("state") == "retired":
                        _cleanup_terminal_graph_artifact(ledger, payload, row)
        cleanup_terminal_prior_sessions(
            ledger.root,
            keep_session_id=session_id,
        )
        cleanup_stale_artifacts(
            ledger.root,
            keep_session_id=session_id,
            max_age_seconds=LIVE_STALE_SECONDS,
        )
        cleanup_stale_dispatch_state(
            ledger.root,
            keep_session_id=session_id,
            max_age_seconds=LIVE_STALE_SECONDS,
        )
        TaskLedger.cleanup_stale(
            ledger.root,
            keep_session_id=session_id,
            max_age_seconds=TERMINAL_STALE_SECONDS,
            live_max_age_seconds=LIVE_STALE_SECONDS,
        )
    return {
        "hookSpecificOutput": {
            "additionalContext": SESSION_CONTEXT,
            "hookEventName": "SessionStart",
        }
    }


def evaluate(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name")
    if event == "SessionStart":
        return start_task(payload)
    if event == "Stop":
        return transaction_stop_outcome(payload)
    if event == "UserPromptSubmit":
        return transaction_user_prompt_context(payload)
    if event == "PostToolUse":
        if tool_name in SPAWN_TOOL_NAMES:
            return postflight_spawn(payload)
        if tool_name in CONTINUATION_TOOL_NAMES:
            postflight_continuation(payload)
    elif event == "PreToolUse" and tool_name in INTERRUPT_TOOL_NAMES:
        preflight_interrupt(payload)
    return {}


def main() -> int:
    try:
        outcome = evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception as error:
        outcome = {"decision": "block", "reason": f"CCO lifecycle transition was rejected: {error}"}
    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

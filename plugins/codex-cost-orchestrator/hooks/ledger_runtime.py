#!/usr/bin/env python3
"""Codex hook adapter for the task-local CCO lifecycle module."""

from __future__ import annotations

import json
import hashlib
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

from task_ledger import LedgerConflict, TaskLedger  # noqa: E402
from packet_compiler import READ_ROLE, normalize_capsule, parse_result_message  # noqa: E402
from prepared_graph import (  # noqa: E402
    PreparedGraphError,
    cleanup_session_artifacts,
    cleanup_stale_artifacts,
    dispatch_workspace_claim,
    verify_artifact_workspace,
)
from protocol_hash import canonical_bytes  # noqa: E402
from workspace_state import StateError, repository_root  # noqa: E402


SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
TASK_NAME = re.compile(
    r"^(?:(?:work|analyze)_[a-z0-9][a-z0-9_]*_(?:routine|complex)_g[0-9]{2,}|review_e[0-9]{2,}_g[0-9]{2,})$"
)
def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        or getattr(path, "is_junction", lambda: False)()
    )


def _has_reparse_ancestor(path: Path) -> bool:
    absolute = Path(os.path.abspath(path.expanduser()))
    return any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents))


def _ledger_root(payload: Mapping[str, Any]) -> Path:
    configured = os.environ.get("CCO_LEDGER_DIR")
    configured_root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    )
    absolute_root = Path(os.path.abspath(configured_root))
    cwd_value = payload.get("cwd")
    if _has_reparse_ancestor(absolute_root):
        raise ValueError("ledger directory cannot use a reparse ancestor")
    try:
        root = absolute_root.resolve()
        cwd = Path(cwd_value).resolve() if isinstance(cwd_value, str) else Path.cwd().resolve()
        repo = repository_root(cwd)
        if root == repo or repo in root.parents or root in repo.parents:
            raise ValueError("ledger directory must be outside the repository")
    except (OSError, StateError) as error:
        raise ValueError("ledger directory cannot be resolved") from error
    return root


def ledger_for(payload: Mapping[str, Any]) -> TaskLedger | None:
    """Return this task's ledger without directory-wide hot-path maintenance."""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        return None
    return TaskLedger(_ledger_root(payload), session_id)


def prepared_workspace_claim(
    payload: Mapping[str, Any], capsule: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve and validate the task-local baseline bound by a prepared graph."""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise LedgerConflict("prepared baseline requires an exact session identity")
    cwd_value = payload.get("cwd")
    try:
        return dispatch_workspace_claim(
            ledger_root=_ledger_root(payload),
            session_id=session_id,
            capsule=capsule,
            repo=Path(cwd_value) if isinstance(cwd_value, str) else Path.cwd(),
        )
    except PreparedGraphError as error:
        raise LedgerConflict("prepared baseline artifact is invalid") from error


def claim_from_fields(
    fields: Mapping[str, Any],
    *,
    role: str | None = None,
    workspace: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Compile validated wire fields into the ledger's minimal lifecycle claim."""

    capsule = normalize_capsule(dict(fields))
    execution = capsule["execution"]
    contract = capsule.get("contract", {})
    contract_sha256 = "sha256:" + hashlib.sha256(canonical_bytes(contract)).hexdigest()
    claim: dict[str, object] = {
        "node": capsule["node"],
        "contract_rev": int(contract.get("contract_rev", 1)),
        "contract_sha256": contract_sha256,
        "input_sha256": capsule["capsule_sha256"],
        "generation": execution["generation"],
        "cursor": execution["cursor"],
        "role": role or capsule["role"],
        "run": execution["task_name"],
        "kind": capsule["kind"],
        "purpose": capsule["purpose"],
    }
    if workspace is not None:
        claim.update(
            {
                "baseline": workspace.get("baseline"),
                "baseline_path": workspace.get("baseline_path"),
                "graph_scopes": workspace.get("graph_scopes"),
                "graph_sha256": workspace.get("graph_sha256"),
                "scopes": workspace.get("scopes"),
                "workspace_mode": workspace.get("workspace_mode"),
            }
        )
    return claim


def result_claim_from_message(message: Any) -> dict[str, Any]:
    return parse_result_message(message)


def reserve_spawn(
    payload: Mapping[str, Any],
    fields: Mapping[str, str],
    role: str,
    *,
    workspace: Mapping[str, Any] | None = None,
) -> None:
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not isinstance(call_id, str):
        return
    ledger.reserve(
        call_id,
        claim_from_fields(fields, role=role, workspace=workspace),
    )


def _task_paths(value: Any, *, key: str = "") -> set[str]:
    if isinstance(value, dict):
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
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return set()
    return _task_paths(decoded, key=key)


def _native_rejection(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower()
            if normalized in {"error", "iserror", "rejected", "failed"} and (
                child is True or (isinstance(child, str) and child)
            ):
                return True
            if normalized == "status" and isinstance(child, str) and child.lower() in {
                "error",
                "failed",
                "rejected",
            }:
                return True
            if _native_rejection(child):
                return True
    return False


def postflight_spawn(payload: Mapping[str, Any]) -> dict[str, str]:
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not ledger.path.exists() or not isinstance(call_id, str):
        return {}
    paths = _task_paths(payload.get("tool_response"))
    if len(paths) == 1:
        ledger.activate(call_id, next(iter(paths)))
        return {}
    if not paths and _native_rejection(payload.get("tool_response")):
        ledger.release(call_id)
        return {}
    return {
        "decision": "block",
        "reason": "CCO spawn result did not expose one canonical task path; keep the reservation for Primary inspection.",
    }


def preflight_continuation(
    payload: Mapping[str, Any], fields: Mapping[str, Any] | None = None
) -> None:
    ledger = ledger_for(payload)
    if ledger is None or not ledger.path.exists():
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    target = tool_input.get("target")
    if fields is None:
        if ledger.is_managed_owner(target):
            raise LedgerConflict("managed CCO owner requires a v6 continuation capsule")
        return
    call_id = payload.get("tool_use_id")
    if not isinstance(target, str) or not isinstance(call_id, str):
        raise LedgerConflict("continuation has no exact call or target")
    capsule = normalize_capsule(dict(fields))
    if "graph_sha256" in capsule:
        workspace = prepared_workspace_claim(payload, capsule)
        matching_rows = [
            row for row in ledger.read_rows() if row.get("owner") == target
        ]
        workspace_fields = (
            "baseline",
            "baseline_path",
            "graph_scopes",
            "graph_sha256",
            "scopes",
            "workspace_mode",
        )
        if len(matching_rows) != 1 or any(
            matching_rows[0].get(name) != workspace.get(name)
            for name in workspace_fields
        ):
            raise LedgerConflict(
                "continuation prepared baseline does not match its owner"
            )
    previous = capsule.get("previous_capsule_sha256")
    current = capsule.get("capsule_sha256")
    cursor = capsule["execution"]["cursor"]
    ledger.prepare_continuation(
        call_id,
        target,
        previous_input_sha256=previous,
        next_input_sha256=current,
        cursor=cursor,
    )


def postflight_continuation(payload: Mapping[str, Any]) -> None:
    ledger = ledger_for(payload)
    call_id = payload.get("tool_use_id")
    if ledger is None or not ledger.path.exists() or not isinstance(call_id, str):
        return
    ledger.settle_pending_continuation(
        call_id,
        accepted=not _native_rejection(payload.get("tool_response")),
    )


def preflight_interrupt(payload: Mapping[str, Any]) -> None:
    ledger = ledger_for(payload)
    tool_input = payload.get("tool_input")
    if ledger is None or not ledger.path.exists():
        return
    if not isinstance(tool_input, dict):
        raise LedgerConflict("interrupt input is malformed")
    target = tool_input.get("target")
    if not isinstance(target, str) or TASK_PATH.fullmatch(target) is None:
        raise LedgerConflict("interrupt target is not canonical")
    ledger.retire(target)


def accept_subagent_result(
    payload: Mapping[str, Any], fields: Mapping[str, Any] | None = None
) -> None:
    ledger = ledger_for(payload)
    parsed = fields or result_claim_from_message(payload.get("last_assistant_message"))
    if not parsed:
        return
    if parsed.get("disposition") == "accept" and (
        ledger is None or not ledger.path.exists()
    ):
        raise LedgerConflict("accept result has no live dispatch ledger")
    if ledger is None or not ledger.path.exists():
        return
    owner = payload.get("agent_id")
    if not isinstance(owner, str) or TASK_PATH.fullmatch(owner) is None:
        raise LedgerConflict("result has no exact canonical owner")
    rows = ledger.read_rows()
    matching = [
        row for row in rows
        if row.get("owner") == owner
        and row.get("input_sha256") == parsed.get("dispatch_sha256")
        and row.get("role") == payload.get("agent_type")
    ]
    if len(matching) != 1:
        raise LedgerConflict("v6 result dispatch identity is stale")
    row = matching[0]
    if parsed.get("disposition") == "accept":
        if (
            parsed.get("status") != "complete"
            or payload.get("agent_type") != READ_ROLE
            or row.get("role") != READ_ROLE
            or row.get("kind") != "review"
            or row.get("purpose") != "acceptance"
        ):
            raise LedgerConflict(
                "only a complete review acceptance may return an accept disposition"
            )
    if "baseline_path" in row:
        cwd_value = payload.get("cwd")
        try:
            verification = verify_artifact_workspace(
                Path(str(row["baseline_path"])),
                repo=Path(cwd_value) if isinstance(cwd_value, str) else Path.cwd(),
                baseline=str(row["baseline"]),
                graph_sha256_value=str(row["graph_sha256"]),
                graph_scopes_value=row["graph_scopes"],
                workspace_mode=str(row["workspace_mode"]),
            )
        except PreparedGraphError as error:
            raise LedgerConflict("result workspace artifact is invalid") from error
        if verification["verdict"] != "pass":
            raise LedgerConflict(
                "result workspace verification failed: "
                + ",".join(verification["violations"])
            )
    disposition = "continuable" if parsed.get("disposition") == "continue" else "retired"
    ledger.record_result(
        node=str(row["node"]),
        contract_rev=int(row["contract_rev"]),
        run=str(row["run"]),
        generation=int(row["generation"]),
        input_sha256=str(row["input_sha256"]),
        owner=owner,
        disposition=disposition,
        cursor=int(row["cursor"]),
    )


def cleanup_task(payload: Mapping[str, Any]) -> None:
    """Remove current and abandoned residue at the cold session boundary."""

    ledger = ledger_for(payload)
    if ledger is None:
        return
    if ledger.path.exists():
        ledger.cleanup_if_terminal(force=True)
    cleanup_session_artifacts(ledger.root, str(payload.get("session_id")))
    cleanup_stale_artifacts(
        ledger.root,
        keep_session_id=str(payload.get("session_id")),
        max_age_seconds=24 * 60 * 60,
    )
    TaskLedger.cleanup_stale(
        ledger.root,
        keep_session_id=str(payload.get("session_id")),
        max_age_seconds=24 * 60 * 60,
    )


def evaluate(payload: object) -> dict[str, str]:
    """One shallow hook entrypoint for lifecycle-only Codex events."""

    if not isinstance(payload, dict):
        return {}
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name")
    if event == "PostToolUse":
        if tool_name in {"spawn_agent", "Agent"}:
            return postflight_spawn(payload)
        if tool_name in {"send_message", "followup_task"}:
            postflight_continuation(payload)
    elif event == "PreToolUse" and tool_name in {"interrupt_agent", "interruptAgent"}:
        preflight_interrupt(payload)
    elif event == "SessionEnd":
        cleanup_task(payload)
    return {}


def main() -> int:
    try:
        outcome = evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception as error:
        outcome = {
            "decision": "block",
            "reason": f"CCO lifecycle transition was rejected: {error}",
        }
    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

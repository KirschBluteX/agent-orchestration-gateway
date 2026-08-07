#!/usr/bin/env python3
"""Strict v8 dispatch gate with one explicit native-bypass marker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from protocol_envelope import load_utf8_json

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPAWN_FIELDS = frozenset(
    {"agent_type", "fork_turns", "message", "model", "reasoning_effort", "task_name"}
)
SPAWN_TOOL_NAMES = frozenset({"Agent", "spawn_agent", "collaborationspawn_agent"})
CONTINUATION_TOOL_NAMES = frozenset(
    {
        "send_message",
        "followup_task",
        "collaborationsend_message",
        "collaborationfollowup_task",
    }
)
CONTINUATION_FIELDS = frozenset({"message", "target"})
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
PROTECTED_TOKEN = re.compile(r"^gAAAA[A-Za-z0-9_-]{80,}={0,2}$")
DISPATCH_HEADER = "CCO_DISPATCH cco.v8"
READ_ROLE = "cost_orchestrator_read_leaf"
WRITE_ROLE = "cost_orchestrator_write_leaf"
BYPASS_HEADER = "CCO_NATIVE_BYPASS v1"
OLD_HEADERS = ("CCO_DISPATCH cco.v6", "CCO_DISPATCH cco.v7")


class PacketError(ValueError):
    """Native arguments do not match a canonical v8 capsule."""


def _transaction_module() -> Any:
    import dispatch_transaction

    return dispatch_transaction


def _ledger_module() -> Any:
    import ledger_runtime

    return ledger_runtime


def _packet_module() -> Any:
    import packet_compiler

    return packet_compiler


def _transaction_state_may_exist(payload: dict[str, Any]) -> bool:
    """Cheap negative check used before importing workspace and ledger runtimes."""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        return False
    if SESSION_ID.fullmatch(session_id) is None:
        return True
    configured = os.environ.get("CCO_LEDGER_DIR")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "codex-cost-orchestrator" / "ledger"
    )
    try:
        return (root / f"{session_id}.dispatch-transactions.json").exists()
    except OSError:
        return True


def block_outcome(error: Exception | None = None, *, code: str = "CCO_INVALID") -> dict[str, str]:
    detail = f": {error}" if error else ""
    return {
        "decision": "block",
        "reason": f"{code}{detail}",
    }


def _bypass_outcome(tool_input: dict[str, Any]) -> dict[str, Any]:
    message = tool_input.get("message")
    if not isinstance(message, str) or not message.startswith(BYPASS_HEADER + "\n"):
        raise PacketError("native bypass marker is malformed")
    stripped = message[len(BYPASS_HEADER) + 1 :]
    if not stripped:
        raise PacketError("native bypass task is empty")
    updated = dict(tool_input)
    updated["message"] = stripped
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }


def _is_protected_payload_text(message: object) -> bool:
    """Reject opaque payloads across host objects and repeated JSON wrapping."""

    seen: set[int] = set()
    visited = 0

    def protected(item: object, *, depth: int = 0) -> bool:
        nonlocal visited
        visited += 1
        if visited > 10_000 or depth > 32:
            return True
        if isinstance(item, str):
            stripped = item.strip()
            if PROTECTED_TOKEN.fullmatch(stripped) is not None:
                return True
            if depth >= 4 or not stripped.startswith(("{", "[", '"')):
                return False
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return False
            if decoded == item:
                return False
            return protected(decoded, depth=depth + 1)
        if isinstance(item, dict):
            identity = id(item)
            if identity in seen:
                return False
            seen.add(identity)
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted and (
                item.get("type") in {"encrypted_content", "reasoning"}
                or PROTECTED_TOKEN.fullmatch(encrypted.strip()) is not None
            ):
                return True
            return any(protected(child, depth=depth + 1) for child in item.values())
        if isinstance(item, list):
            identity = id(item)
            if identity in seen:
                return False
            seen.add(identity)
            return any(protected(child, depth=depth + 1) for child in item)
        return False

    return protected(message)


def _expanded_reference_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn one persisted exact v2 ref into the canonical native v8 input."""

    transaction = _transaction_module()
    ledger = _ledger_module()
    expanded = transaction.claim_spawn_reference(payload)
    try:
        capsule = validate_dispatch(expanded)
        workspace = ledger.prepared_workspace_claim(payload, capsule)
        ledger.reserve_spawn(
            payload,
            capsule,
            str(expanded["agent_type"]),
            workspace=workspace,
        )
    except Exception:
        transaction.release_spawn_claim(payload)
        raise
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": expanded,
        }
    }


def _pending_transaction_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Gate every local tool while a managed transaction still has work."""

    transaction = _transaction_module()
    transaction_id = transaction.exact_abort_for_payload(payload)
    if transaction_id is not None:
        transaction.abort_pending_transaction(payload, transaction_id)
        # The hook performs the exact fencing action itself.  Block the carrier
        # tool so a message/continuation cannot also become unrelated work.
        return block_outcome(
            PacketError("exact transaction abort fenced remaining undispatched nodes"),
            code="CCO_TRANSACTION_ABORTED",
        )
    if payload.get("tool_name") in SPAWN_TOOL_NAMES:
        return _expanded_reference_outcome(payload)
    return block_outcome(
        PacketError("only an exact pending spawn ref or exact abort command is allowed"),
        code="CCO_TRANSACTION_PENDING",
    )


def validate_dispatch(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != SPAWN_FIELDS:
        raise PacketError("v8 native spawn shape is invalid")
    capsule = _packet_module().parse_message(tool_input.get("message"))
    expected_role = WRITE_ROLE if capsule["role"] == "worker" else READ_ROLE
    expected = {
        "agent_type": expected_role,
        "fork_turns": capsule["execution"]["fork_turns"],
        "model": capsule["route"]["selected"]["model"],
        "reasoning_effort": capsule["route"]["selected"]["effort"],
        "task_name": capsule["execution"]["task_name"],
    }
    for field, value in expected.items():
        if tool_input.get(field) != value:
            raise PacketError(f"v8 native {field} does not match capsule")
    if capsule["execution"]["cursor"] != 0:
        raise PacketError("spawn requires an initial cursor")
    return capsule


def validate_v8_continuation(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != CONTINUATION_FIELDS:
        raise PacketError("v8 continuation shape is invalid")
    capsule = _packet_module().parse_message(tool_input.get("message"))
    target = tool_input.get("target")
    if (
        capsule["execution"]["cursor"] < 1
        or not isinstance(target, str)
        or TASK_PATH.fullmatch(target) is None
        or target != "/root/" + capsule["execution"]["task_name"]
    ):
        raise PacketError("v8 continuation target or cursor is invalid")
    return capsule


def evaluate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("hook_event_name") != "PreToolUse":
        return {}
    tool_input = value.get("tool_input")
    tool_name = value.get("tool_name")
    managed_tool = tool_name in SPAWN_TOOL_NAMES | CONTINUATION_TOOL_NAMES
    if not managed_tool and not _transaction_state_may_exist(value):
        return {}
    try:
        # PreToolUse now runs for every local tool.  A pending transaction gates
        # all of them, including ordinary tools and native-bypass attempts.
        transaction = _transaction_module()
        try:
            pending = transaction.has_pending_transaction(value)
        except transaction.DispatchTransactionError as error:
            # Older direct-capsule callers/tests have no host session identity.  The
            # host always supplies one for transaction enforcement; a v2 ref is
            # still fail-closed below because it cannot be a direct v8 packet.
            if isinstance(value.get("session_id"), str):
                return block_outcome(error, code="CCO_TRANSACTION_STATE")
            pending = False
        if pending:
            return _pending_transaction_outcome(value)
        if not isinstance(tool_input, dict):
            if tool_name not in SPAWN_TOOL_NAMES | CONTINUATION_TOOL_NAMES:
                return {}
            return block_outcome(PacketError("tool input is missing"))
        if tool_name in SPAWN_TOOL_NAMES:
            message = tool_input.get("message")
            if isinstance(message, str) and message.startswith(BYPASS_HEADER):
                return _bypass_outcome(tool_input)
            if isinstance(message, str) and message.startswith(OLD_HEADERS):
                return block_outcome(
                    code="CCO_OLD_TASK_REQUIRES_NEW_TASK",
                )
            if not isinstance(message, str) or not message.startswith(DISPATCH_HEADER):
                return block_outcome(
                    code="CCO_REQUIRED",
                    error=PacketError(
                        "prepare the spawn through cco.v8 or use the user-authorized CCO_NATIVE_BYPASS v1 marker"
                    ),
                )
            capsule = validate_dispatch(tool_input)
            ledger = _ledger_module()
            workspace = ledger.prepared_workspace_claim(value, capsule)
            ledger.reserve_spawn(
                value,
                capsule,
                str(tool_input["agent_type"]),
                workspace=workspace,
            )
            return {}
        if tool_name in CONTINUATION_TOOL_NAMES:
            message = tool_input.get("message")
            if _is_protected_payload_text(message):
                return block_outcome(
                    PacketError(
                        "opaque host collaboration content must remain in its native protected field; "
                        "wait for an authoritative native event instead of forwarding it"
                    ),
                    code="CCO_PROTECTED_MESSAGE",
                )
            if isinstance(message, str) and message.startswith(DISPATCH_HEADER):
                capsule = validate_v8_continuation(tool_input)
                _ledger_module().preflight_continuation(value, capsule)
                return {}
            # Native-bypass owners are unmanaged. Managed owners still fail closed.
            _ledger_module().preflight_continuation(value)
            return {}
        return {}
    except Exception as error:
        return block_outcome(error)


def main() -> int:
    try:
        outcome = evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception as error:
        outcome = block_outcome(error)
    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

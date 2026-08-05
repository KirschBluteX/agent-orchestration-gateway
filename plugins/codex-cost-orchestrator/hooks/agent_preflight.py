#!/usr/bin/env python3
"""Strict v7 dispatch gate with one explicit native-bypass marker."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

from protocol_envelope import load_utf8_json

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ledger_runtime import (  # noqa: E402
    preflight_continuation,
    prepared_workspace_claim,
    reserve_spawn,
)
from dispatch_transaction import (  # noqa: E402
    DispatchTransactionError,
    abort_pending_transaction,
    claim_spawn_reference,
    exact_abort_for_payload,
    has_pending_transaction,
    release_spawn_claim,
)
from packet_compiler import (  # noqa: E402
    DISPATCH_HEADER,
    READ_ROLE,
    WRITE_ROLE,
    parse_message,
)


SPAWN_FIELDS = frozenset(
    {"agent_type", "fork_turns", "message", "model", "reasoning_effort", "task_name"}
)
CONTINUATION_FIELDS = frozenset({"message", "target"})
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
BYPASS_HEADER = "CCO_NATIVE_BYPASS v1"
OLD_HEADER = "CCO_DISPATCH cco.v6"


class PacketError(ValueError):
    """Native arguments do not match a canonical v7 capsule."""


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


def _expanded_reference_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn one persisted exact v2 ref into the canonical native v7 input."""

    expanded = claim_spawn_reference(payload)
    try:
        capsule = validate_dispatch(expanded)
        workspace = prepared_workspace_claim(payload, capsule)
        reserve_spawn(payload, capsule, str(expanded["agent_type"]), workspace=workspace)
    except Exception:
        release_spawn_claim(payload)
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

    transaction_id = exact_abort_for_payload(payload)
    if transaction_id is not None:
        abort_pending_transaction(payload, transaction_id)
        # The hook performs the exact fencing action itself.  Block the carrier
        # tool so a message/continuation cannot also become unrelated work.
        return block_outcome(
            PacketError("exact transaction abort fenced remaining undispatched nodes"),
            code="CCO_TRANSACTION_ABORTED",
        )
    if payload.get("tool_name") in {"spawn_agent", "Agent"}:
        return _expanded_reference_outcome(payload)
    return block_outcome(
        PacketError("only an exact pending spawn ref or exact abort command is allowed"),
        code="CCO_TRANSACTION_PENDING",
    )


def validate_dispatch(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != SPAWN_FIELDS:
        raise PacketError("v7 native spawn shape is invalid")
    capsule = parse_message(tool_input.get("message"))
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
            raise PacketError(f"v7 native {field} does not match capsule")
    if capsule["execution"]["cursor"] != 0:
        raise PacketError("spawn requires an initial cursor")
    return capsule


def validate_v7_continuation(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != CONTINUATION_FIELDS:
        raise PacketError("v7 continuation shape is invalid")
    capsule = parse_message(tool_input.get("message"))
    target = tool_input.get("target")
    if (
        capsule["execution"]["cursor"] < 1
        or not isinstance(target, str)
        or TASK_PATH.fullmatch(target) is None
        or target != "/root/" + capsule["execution"]["task_name"]
    ):
        raise PacketError("v7 continuation target or cursor is invalid")
    return capsule


def evaluate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("hook_event_name") != "PreToolUse":
        return {}
    tool_input = value.get("tool_input")
    tool_name = value.get("tool_name")
    try:
        # PreToolUse now runs for every local tool.  A pending transaction gates
        # all of them, including ordinary tools and native-bypass attempts.
        try:
            pending = has_pending_transaction(value)
        except DispatchTransactionError as error:
            # Older direct-v7 callers/tests have no host session identity.  The
            # host always supplies one for transaction enforcement; a v2 ref is
            # still fail-closed below because it cannot be a direct v7 packet.
            if isinstance(value.get("session_id"), str):
                return block_outcome(error, code="CCO_TRANSACTION_STATE")
            pending = False
        if pending:
            return _pending_transaction_outcome(value)
        if not isinstance(tool_input, dict):
            if tool_name not in {"spawn_agent", "Agent", "send_message", "followup_task"}:
                return {}
            return block_outcome(PacketError("tool input is missing"))
        if tool_name in {"spawn_agent", "Agent"}:
            message = tool_input.get("message")
            if isinstance(message, str) and message.startswith(BYPASS_HEADER):
                return _bypass_outcome(tool_input)
            if isinstance(message, str) and message.startswith(OLD_HEADER):
                return block_outcome(
                    code="CCO_OLD_TASK_REQUIRES_NEW_TASK",
                )
            if not isinstance(message, str) or not message.startswith(DISPATCH_HEADER):
                return block_outcome(
                    code="CCO_REQUIRED",
                    error=PacketError(
                        "prepare the spawn through cco.v7 or use the user-authorized CCO_NATIVE_BYPASS v1 marker"
                    ),
                )
            capsule = validate_dispatch(tool_input)
            workspace = prepared_workspace_claim(value, capsule)
            reserve_spawn(value, capsule, str(tool_input["agent_type"]), workspace=workspace)
            return {}
        if tool_name in {"send_message", "followup_task"}:
            message = tool_input.get("message")
            if isinstance(message, str) and message.startswith(DISPATCH_HEADER):
                capsule = validate_v7_continuation(tool_input)
                preflight_continuation(value, capsule)
                return {}
            # Native-bypass owners are unmanaged. Managed owners still fail closed.
            preflight_continuation(value)
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

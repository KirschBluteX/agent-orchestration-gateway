#!/usr/bin/env python3
"""Validate compact CCO v6 native dispatch and continuation capsules."""

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

from ledger_runtime import preflight_continuation, reserve_spawn  # noqa: E402
from packet_compiler import (  # noqa: E402
    READ_ROLE,
    WRITE_ROLE,
    parse_message,
)


SPAWN_FIELDS = frozenset(
    {"agent_type", "fork_turns", "message", "model", "reasoning_effort", "task_name"}
)
CONTINUATION_FIELDS = frozenset({"message", "target"})
TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")


class PacketError(ValueError):
    """Native arguments do not match the canonical v6 capsule."""


def block_outcome(error: Exception | None = None) -> dict[str, str]:
    detail = f" ({error})" if error else ""
    return {
        "decision": "block",
        "reason": f"CCO native operation failed structural preflight{detail}; repair the v6 capsule or route request.",
    }


def validate_dispatch(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != SPAWN_FIELDS:
        raise PacketError("v6 native spawn shape is invalid")
    capsule = parse_message(tool_input.get("message"))
    execution = capsule["execution"]
    expected_role = WRITE_ROLE if capsule["purpose"] == "implementation" else READ_ROLE
    expected = {
        "agent_type": expected_role,
        "fork_turns": execution["fork_turns"],
        "model": capsule["requested_model"],
        "reasoning_effort": capsule["requested_effort"],
        "task_name": execution["task_name"],
    }
    for field, value in expected.items():
        if tool_input.get(field) != value:
            raise PacketError(f"v6 native {field} does not match capsule")
    if execution["cursor"] != 0:
        raise PacketError("spawn requires an initial capsule cursor")
    return capsule


def validate_v6_continuation(tool_input: object) -> dict[str, Any]:
    if not isinstance(tool_input, dict) or set(tool_input) != CONTINUATION_FIELDS:
        raise PacketError("v6 continuation shape is invalid")
    capsule = parse_message(tool_input.get("message"))
    execution = capsule["execution"]
    target = tool_input.get("target")
    if (
        execution["cursor"] < 1
        or not isinstance(target, str)
        or TASK_PATH.fullmatch(target) is None
        or target != "/root/" + execution["task_name"]
    ):
        raise PacketError("v6 continuation target or cursor is invalid")
    return capsule


def evaluate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("hook_event_name") != "PreToolUse":
        return {}
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        return block_outcome(PacketError("tool input is missing"))
    tool_name = value.get("tool_name")
    try:
        if tool_name in {"spawn_agent", "Agent"}:
            capsule = validate_dispatch(tool_input)
            reserve_spawn(value, capsule, str(tool_input["agent_type"]))
            return {}
        if tool_name in {"send_message", "followup_task"}:
            message = tool_input.get("message")
            if not isinstance(message, str) or not message.startswith("CCO_DISPATCH cco.v6"):
                return {}
            capsule = validate_v6_continuation(tool_input)
            preflight_continuation(value, capsule)
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

#!/usr/bin/env python3
"""Validate a compact CCO v8 result and fence stale owners."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from protocol_envelope import load_utf8_json

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ledger_runtime import accept_subagent_result, retire_invalid_subagent_stop  # noqa: E402
from packet_compiler import READ_ROLE, WRITE_ROLE  # noqa: E402


LEAF_ROLES = frozenset({READ_ROLE, WRITE_ROLE})


def evaluate(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SubagentStop":
        return {}
    if not isinstance(payload.get("stop_hook_active"), bool):
        return {}
    if payload.get("agent_type") not in LEAF_ROLES:
        return {}
    try:
        accept_subagent_result(payload)
    except Exception:
        try:
            retire_invalid_subagent_stop(payload)
        except Exception:
            return {
                "systemMessage": (
                    "CCO result was rejected; Primary must recover the child lifecycle state."
                ),
            }
        return {
            "systemMessage": (
                "CCO result was rejected and the child was retired; Primary must inspect the actual state."
            ),
        }
    return {}


# Keep the private spelling as a source-level adapter for hook harnesses.
_evaluate = evaluate


def main() -> int:
    try:
        outcome = evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception as error:
        outcome = {
            "decision": "block",
            "reason": f"CCO result validation failed ({error}); keep the result fenced.",
        }
    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

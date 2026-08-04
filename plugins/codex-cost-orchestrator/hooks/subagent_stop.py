#!/usr/bin/env python3
"""Validate a compact CCO v6 result and fence stale owners."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from protocol_envelope import load_utf8_json

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ledger_runtime import accept_subagent_result, result_claim_from_message  # noqa: E402
from packet_compiler import READ_ROLE, WRITE_ROLE  # noqa: E402


LEAF_ROLES = frozenset({READ_ROLE, WRITE_ROLE})


def evaluate(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SubagentStop":
        return {}
    if payload.get("stop_hook_active") is not False:
        return {}
    if payload.get("agent_type") not in LEAF_ROLES:
        return {}
    try:
        claim = result_claim_from_message(payload.get("last_assistant_message"))
        accept_subagent_result(payload, claim)
    except Exception as error:
        return {
            "decision": "block",
            "reason": f"Return one structurally complete CCO_RESULT cco.v6 packet ({error}); do not redo completed work.",
        }
    return {}


# Tests and integrators used the old private spelling; keep one harmless alias for
# the v6 adapter while removing all old wire compatibility.
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

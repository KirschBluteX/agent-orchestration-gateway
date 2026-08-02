#!/usr/bin/env python3
"""Read-only, fail-open completion gate for CCO subagents."""

from __future__ import annotations

import json
import re
import sys


WORKER_ROLES = frozenset(
    {
        "cost_orchestrator_routine_worker",
        "cost_orchestrator_complex_worker",
    }
)
REVIEWER_ROLE = "cost_orchestrator_reviewer"
WORK_RESULT_HEADER = "CCO_WORK_RESULT cco.v3"
WORK_RESULT_FIELDS = (
    "NODE",
    "CONTRACT_REV",
    "RUN",
    "LEASE",
    "STATUS",
    "CHANGED",
    "VERIFIED",
    "JUDGMENT",
    "DEVIATIONS",
    "BLOCKERS",
)
REVIEW_RESULT_HEADER = "CCO_REVIEW_RESULT cco.v3"
REVIEW_RESULT_FIELDS = (
    "EPOCH",
    "MODE",
    "REVIEWED_STATE",
    "VERDICT",
    "REASON",
    "FINDINGS",
    "RESIDUAL_RISK",
)
WORK_RESULT_ENUMS = {
    "STATUS": frozenset({"complete", "partial", "blocked"}),
}
REVIEW_RESULT_ENUMS = {
    "MODE": frozenset({"fresh", "delta"}),
    "VERDICT": frozenset({"ship", "fix-first", "rethink"}),
}
FIELD_LINE = re.compile(r"^([A-Z][A-Z0-9_]*):(?:\s*(.*))?$")


def _first_content_line(message: str) -> str | None:
    lines = iter(message.splitlines())
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            for fenced_line in lines:
                fenced = fenced_line.strip()
                if fenced:
                    return fenced
            return None
        return stripped
    return None


def _packet_fields(
    message: str, header: str
) -> tuple[dict[str, str], set[str]]:
    fields: dict[str, str] = {}
    duplicates: set[str] = set()
    current_name: str | None = None
    current_content: list[str] = []

    def finish_field() -> None:
        nonlocal current_name, current_content
        if current_name is None:
            return
        value = "\n".join(part for part in current_content if part).strip()
        if current_name in fields:
            duplicates.add(current_name)
        else:
            fields[current_name] = value
        current_name = None
        current_content = []

    found_header = False
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if not found_header:
            if not line or line.startswith("```"):
                continue
            found_header = line == header
            if not found_header:
                break
            continue
        if line.startswith("```"):
            break
        match = FIELD_LINE.fullmatch(line)
        if match:
            finish_field()
            current_name = match.group(1)
            inline_value = (match.group(2) or "").strip()
            current_content = [inline_value] if inline_value else []
        elif current_name is not None and line:
            current_content.append(line)
    finish_field()
    return fields, duplicates


def _missing_fields(
    message: str,
    *,
    header: str,
    required: tuple[str, ...],
    enums: dict[str, frozenset[str]],
) -> list[str]:
    if _first_content_line(message) != header:
        return [header]
    fields, duplicates = _packet_fields(message, header)
    missing = [
        name
        for name in required
        if not fields.get(name) or name in duplicates
    ]
    for name, allowed_values in enums.items():
        if fields.get(name) not in allowed_values and name not in missing:
            missing.append(name)
    return missing


def _continuation_reason(header: str, missing: list[str]) -> str:
    if missing == [header]:
        detail = ""
    else:
        shown = ", ".join(missing[:4])
        if len(missing) > 4:
            shown += ", ..."
        detail = f" (missing: {shown})"
    return (
        f"Return one structurally complete {header} packet{detail}; "
        "do not redo completed work."
    )


def _evaluate(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    if payload.get("hook_event_name") != "SubagentStop":
        return {}
    if not isinstance(payload.get("stop_hook_active"), bool):
        return {}
    if payload["stop_hook_active"]:
        return {}

    agent_type = payload.get("agent_type")
    message = payload.get("last_assistant_message")
    if not isinstance(agent_type, str):
        return {}

    if agent_type == REVIEWER_ROLE:
        header = REVIEW_RESULT_HEADER
        required = REVIEW_RESULT_FIELDS
        enums = REVIEW_RESULT_ENUMS
    elif agent_type in WORKER_ROLES:
        header = WORK_RESULT_HEADER
        required = WORK_RESULT_FIELDS
        enums = WORK_RESULT_ENUMS
    else:
        return {}

    missing = (
        _missing_fields(
            message,
            header=header,
            required=required,
            enums=enums,
        )
        if isinstance(message, str)
        else [header]
    )
    if not missing:
        return {}
    return {
        "decision": "block",
        "reason": _continuation_reason(header, missing),
    }


def main() -> int:
    outcome: dict[str, str] = {}
    try:
        outcome = _evaluate(json.load(sys.stdin))
    except Exception:
        pass

    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

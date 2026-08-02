#!/usr/bin/env python3
"""Emit allowlisted routing metadata from one exact Codex rollout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class InspectionError(Exception):
    pass


def default_sessions_dir() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "sessions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect safe routing metadata for one native Codex subagent."
    )
    parser.add_argument("--sessions-dir", type=Path, default=default_sessions_dir())
    parser.add_argument("thread_id")
    return parser.parse_args()


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def one_consistent(
    values: list[str | None], field: str, *, required: bool
) -> str | None:
    unique = set(values)
    if len(unique) != 1:
        raise InspectionError(f"conflicting {field}")
    value = values[0]
    if required and not value:
        raise InspectionError(f"missing {field}")
    return value


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as rollout:
            for line in rollout:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise InspectionError("rollout contains a non-object record")
                records.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise InspectionError("rollout is unavailable or invalid") from error
    return records


def inspect(sessions_dir: Path, thread_id: str) -> dict[str, str | None]:
    if THREAD_ID_PATTERN.fullmatch(thread_id) is None:
        raise InspectionError("THREAD_ID must be a lowercase UUID")
    sessions_dir = sessions_dir.expanduser().resolve()
    if not sessions_dir.is_dir():
        raise InspectionError("sessions directory is unavailable")

    matches = list(sessions_dir.rglob(f"rollout-*-{thread_id}.jsonl"))
    if len(matches) != 1:
        raise InspectionError("expected exactly one rollout for the requested thread")

    records = read_records(matches[0])
    sessions = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
    ]
    turns = [
        record.get("payload")
        for record in records
        if record.get("type") == "turn_context" and isinstance(record.get("payload"), dict)
    ]
    if len(sessions) != 1 or not turns:
        raise InspectionError("missing or ambiguous routing metadata")

    session = sessions[0]
    if string_or_none(session.get("id")) != thread_id:
        raise InspectionError("session metadata does not identify the requested thread")
    agent_role = string_or_none(session.get("agent_role"))
    if not agent_role:
        raise InspectionError("missing agent role")

    models = [string_or_none(turn.get("model")) for turn in turns]
    efforts = [string_or_none(turn.get("effort")) for turn in turns]
    sandbox_types = [
        string_or_none(
            turn.get("sandbox_policy", {}).get("type")
            if isinstance(turn.get("sandbox_policy"), dict)
            else None
        )
        for turn in turns
    ]
    permission_types = [
        string_or_none(
            turn.get("permission_profile", {}).get("type")
            if isinstance(turn.get("permission_profile"), dict)
            else None
        )
        for turn in turns
    ]
    return {
        "thread_id": thread_id,
        "agent_role": agent_role,
        "model": one_consistent(models, "model", required=True),
        "effort": one_consistent(efforts, "effort", required=True),
        "sandbox_policy_type": one_consistent(
            sandbox_types, "sandbox policy type", required=False
        ),
        "permission_profile_type": one_consistent(
            permission_types, "permission profile type", required=False
        ),
    }


def main() -> int:
    args = parse_args()
    try:
        output = inspect(args.sessions_dir, args.thread_id)
    except InspectionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

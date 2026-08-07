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

from rollout_io import RolloutError, iter_records, matching_rollouts


THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CANONICAL_TASK_PATH_PATTERN = re.compile(
    r"^/root(?:/[a-z0-9][a-z0-9_]*)+$"
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
    parser.add_argument("--expect-role")
    parser.add_argument("--expect-model")
    parser.add_argument("--expect-effort")
    parser.add_argument(
        "--parent-thread-id",
        default=os.environ.get("CODEX_THREAD_ID"),
        help="Parent UUID used to resolve a canonical native task path.",
    )
    parser.add_argument("target", help="Child thread UUID or exact canonical task path.")
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


def read_session_metadata(path: Path) -> dict[str, Any] | None:
    """Read only far enough to identify one rollout during path resolution."""
    try:
        for value in iter_records(path):
            payload = value.get("payload")
            if value.get("type") == "session_meta" and isinstance(payload, dict):
                return payload
    except RolloutError as error:
        raise InspectionError("rollout is unavailable or invalid") from error
    return None


def resolve_rollout(
    sessions_dir: Path, target: str, parent_thread_id: str | None
) -> tuple[str, Path]:
    if THREAD_ID_PATTERN.fullmatch(target) is not None:
        matches = matching_rollouts(sessions_dir, target)
        if len(matches) != 1:
            raise InspectionError("expected exactly one rollout for the requested thread")
        return target, matches[0]
    if CANONICAL_TASK_PATH_PATTERN.fullmatch(target) is None:
        raise InspectionError("TARGET must be a lowercase UUID or canonical task path")
    if (
        not isinstance(parent_thread_id, str)
        or THREAD_ID_PATTERN.fullmatch(parent_thread_id) is None
    ):
        raise InspectionError("canonical task path requires a lowercase parent thread UUID")

    matches: list[tuple[str, Path]] = []
    rollout_paths = [
        *sessions_dir.rglob("rollout-*.jsonl"),
        *sessions_dir.rglob("rollout-*.jsonl.zst"),
    ]
    for rollout_path in sorted(set(rollout_paths), key=str):
        try:
            session = read_session_metadata(rollout_path)
        except InspectionError:
            continue
        if session is None:
            continue
        if (
            string_or_none(session.get("parent_thread_id")) == parent_thread_id
            and string_or_none(session.get("agent_path")) == target
        ):
            child_id = string_or_none(session.get("id"))
            if child_id is None or THREAD_ID_PATTERN.fullmatch(child_id) is None:
                raise InspectionError("matching rollout has an invalid child thread ID")
            matches.append((child_id, rollout_path))

    if len(matches) != 1:
        raise InspectionError("expected exactly one rollout for the requested task path")
    return matches[0]


def inspect(
    sessions_dir: Path, target: str, parent_thread_id: str | None = None
) -> dict[str, str | None]:
    sessions_dir = sessions_dir.expanduser().resolve()
    if not sessions_dir.is_dir():
        raise InspectionError("sessions directory is unavailable")
    thread_id, rollout_path = resolve_rollout(sessions_dir, target, parent_thread_id)
    sessions: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    try:
        for record in iter_records(rollout_path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                sessions.append(payload)
            elif record.get("type") == "turn_context":
                turns.append(payload)
    except RolloutError as error:
        raise InspectionError("rollout is unavailable or invalid") from error
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
        output = inspect(args.sessions_dir, args.target, args.parent_thread_id)
        expected = {
            "agent_role": args.expect_role,
            "model": args.expect_model,
            "effort": args.expect_effort,
        }
        for field, value in expected.items():
            if value is not None and output[field] != value:
                label = "role" if field == "agent_role" else field
                raise InspectionError(f"runtime {label} does not match expectation")
    except InspectionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exact Codex Hook adapter for the cco.v9 control-plane interface."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from control_plane import (  # noqa: E402
    CONTINUE_HEADER,
    TASK_HEADER,
    TASK_PATH_RE,
    ControlPlane,
    ControlPlaneError,
    ControlPlaneUnavailable,
)
from host_paths import HostPathError, host_path, is_within  # noqa: E402
from operation_deadline import checkpoint, deadline_after  # noqa: E402
from rollout_io import RolloutError, first_record, is_rollout_path  # noqa: E402
from state_lock import StateLockBusy  # noqa: E402


SPAWN_TOOLS = frozenset({"Agent", "spawn_agent", "collaborationspawn_agent"})
FOLLOWUP_TOOLS = frozenset({"followup_task", "collaborationfollowup_task"})
MESSAGE_TOOLS = frozenset(
    {
        "send_message",
        "collaborationsend_message",
        *FOLLOWUP_TOOLS,
    }
)
INTERRUPT_TOOLS = frozenset(
    {"interrupt_agent", "interruptAgent", "collaborationinterrupt_agent"}
)
PROTECTED_TOKEN = re.compile(r"gAAAA[A-Za-z0-9_-]{80,}={0,2}")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_PROTECTED_DEPTH = 32
MAX_PROTECTED_NODES = 10_000
MAX_PROTECTED_BYTES = 1024 * 1024
PRETOOL_INTERNAL_BUDGET_SECONDS = 24.0


def _control(payload: Mapping[str, Any]) -> ControlPlane:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        raise ControlPlaneError("hook event has no session identity")
    event = payload.get("hook_event_name")
    lock_timeout = {
        "PostToolUse": 3.0,
        "PreToolUse": 2.5,
        "SessionStart": 3.0,
        "Stop": 3.0,
        "SubagentStop": 90.0,
    }.get(event, 3.0)
    return ControlPlane(session_id, lock_timeout=lock_timeout)


def _block(error: Exception | str) -> dict[str, str]:
    return {"decision": "block", "reason": f"CCO: {error}"}


def _event_error(event: object, error: Exception) -> dict[str, Any]:
    message = f"CCO: {error}"
    if isinstance(error, (ControlPlaneUnavailable, StateLockBusy, OSError)):
        if event == "SubagentStop":
            return {
                "decision": "block",
                "reason": (
                    "CCO state is temporarily busy; return the exact same result "
                    "(the same CCO_RESULT) without doing more work."
                ),
            }
        if event == "Stop":
            return {
                "decision": "block",
                "reason": "CCO state is temporarily busy; wait for the lifecycle event.",
            }
    if event == "SubagentStop":
        return {"continue": False, "systemMessage": message}
    if event == "SessionStart":
        return {"systemMessage": message}
    return _block(error)


def _protected(value: object) -> bool:
    visited = 0
    decoded_bytes = 0
    seen: set[int] = set()

    def walk(item: object, depth: int) -> bool:
        nonlocal visited, decoded_bytes
        checkpoint()
        visited += 1
        if visited > MAX_PROTECTED_NODES or depth > MAX_PROTECTED_DEPTH:
            return True
        if isinstance(item, str):
            stripped = item.strip()
            if PROTECTED_TOKEN.search(stripped) is not None:
                return True
            if not stripped.startswith(("{", "[", '"')):
                return False
            decoded_bytes += len(stripped.encode("utf-8"))
            if decoded_bytes > MAX_PROTECTED_BYTES:
                return True
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return False
            return decoded != item and walk(decoded, depth + 1)
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in seen:
                return False
            seen.add(identity)
        if isinstance(item, dict):
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted and (
                item.get("type") in {"encrypted_content", "reasoning"}
                or PROTECTED_TOKEN.search(encrypted.strip()) is not None
            ):
                return True
            return any(walk(child, depth + 1) for child in item.values())
        if isinstance(item, list):
            return any(walk(child, depth + 1) for child in item)
        return False

    return walk(value, 0)


def _sessions_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    try:
        return host_path(home / "sessions").resolve()
    except (HostPathError, OSError) as error:
        raise ControlPlaneError("Codex sessions root is unavailable") from error


def _owner_from_transcript(payload: Mapping[str, Any], agent_id: str) -> str:
    transcript_value = payload.get("agent_transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value:
        raise ControlPlaneError("native child has no transcript identity")
    try:
        transcript = host_path(transcript_value).resolve(strict=True)
    except (HostPathError, OSError) as error:
        raise ControlPlaneError("native child transcript is unavailable") from error
    if not is_within(_sessions_root(), transcript) or not is_rollout_path(transcript):
        raise ControlPlaneError("native child transcript is outside its trusted root")
    if not (
        transcript.name.endswith(f"-{agent_id}.jsonl")
        or transcript.name.endswith(f"-{agent_id}.jsonl.zst")
    ):
        raise ControlPlaneError("native child transcript does not match its UUID")
    try:
        record = first_record(transcript)
    except RolloutError as error:
        raise ControlPlaneError("native child session metadata is invalid") from error
    metadata = record.get("payload") if isinstance(record, Mapping) else None
    if record.get("type") != "session_meta" or not isinstance(metadata, Mapping):
        raise ControlPlaneError("native child transcript has no session metadata")
    if metadata.get("id") != agent_id or metadata.get("parent_thread_id") != payload.get("session_id"):
        raise ControlPlaneError("native child metadata does not match this task")
    owners: set[str] = set()
    if isinstance(metadata.get("agent_path"), str):
        owners.add(metadata["agent_path"])
    source = metadata.get("source")
    if isinstance(source, Mapping):
        subagent = source.get("subagent")
        if isinstance(subagent, Mapping):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, Mapping) and isinstance(spawn.get("agent_path"), str):
                owners.add(spawn["agent_path"])
    if len(owners) != 1:
        raise ControlPlaneError("native child metadata has no unique owner")
    owner = owners.pop()
    if TASK_PATH_RE.fullmatch(owner) is None:
        raise ControlPlaneError("native child owner is not canonical")
    return owner


def _owner(payload: Mapping[str, Any]) -> str:
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str):
        raise ControlPlaneError("native child has no identity")
    if TASK_PATH_RE.fullmatch(agent_id) is not None:
        return agent_id
    if UUID_RE.fullmatch(agent_id) is not None:
        return _owner_from_transcript(payload, agent_id)
    raise ControlPlaneError("native child identity is unsupported")


def evaluate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    event = value.get("hook_event_name")
    try:
        if event == "SessionStart":
            if value.get("source") not in {"resume", "clear"}:
                return {}
            interrupted = _control(value).restart()
            if interrupted:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            "CCO fenced child work interrupted by the host restart. "
                            "Inspect the workspace before starting a newer generation."
                        ),
                    }
                }
            return {}
        if event == "PreToolUse":
            with deadline_after(PRETOOL_INTERNAL_BUDGET_SECONDS):
                tool = value.get("tool_name")
                tool_input = value.get("tool_input")
                if tool in SPAWN_TOOLS:
                    if not isinstance(tool_input, Mapping):
                        raise ControlPlaneError("spawn input is missing")
                    message = tool_input.get("message")
                    if _protected(message):
                        raise ControlPlaneError(
                            "opaque collaboration content must remain in its protected host field"
                        )
                    if not isinstance(message, str) or not message.startswith(
                        TASK_HEADER + "\n"
                    ):
                        raise ControlPlaneError(
                            "prepare every native child through the current cco.v9 plan"
                        )
                    _control(value).preflight_spawn(value)
                    return {}
                if tool in MESSAGE_TOOLS:
                    if not isinstance(tool_input, Mapping):
                        raise ControlPlaneError("message input is missing")
                    message = tool_input.get("message")
                    if _protected(message):
                        raise ControlPlaneError(
                            "opaque collaboration content must remain in its protected host field"
                        )
                    target = tool_input.get("target")
                    control = _control(value)
                    if isinstance(message, str) and message.startswith(
                        CONTINUE_HEADER + "\n"
                    ):
                        if tool not in FOLLOWUP_TOOLS:
                            raise ControlPlaneError(
                                "a CCO continuation must use followup_task"
                            )
                        control.preflight_continuation(value)
                    elif isinstance(target, str) and control.owner_is_managed(target):
                        raise ControlPlaneError(
                            "raw messages cannot replace a managed CCO continuation"
                        )
                    return {}
                if tool in INTERRUPT_TOOLS:
                    if not isinstance(tool_input, Mapping) or not isinstance(
                        tool_input.get("target"), str
                    ):
                        raise ControlPlaneError("interrupt target is missing")
                    control = _control(value)
                    if control.owner_is_managed(tool_input["target"]):
                        control.preflight_interrupt(value)
                    return {}
                return {}
        if event == "PostToolUse":
            if value.get("tool_name") in INTERRUPT_TOOLS:
                tool_input = value.get("tool_input")
                target = tool_input.get("target") if isinstance(tool_input, Mapping) else None
                control = _control(value)
                if isinstance(target, str) and control.owner_is_managed(target):
                    control.postflight_interrupt(value)
                return {}
            tool_input = value.get("tool_input")
            message = tool_input.get("message") if isinstance(tool_input, Mapping) else None
            if isinstance(message, str) and message.startswith(
                (TASK_HEADER + "\n", CONTINUE_HEADER + "\n")
            ):
                _control(value).postflight_tool(value)
            return {}
        if event == "Stop":
            if value.get("stop_hook_active") is True:
                return {}
            reason = _control(value).stop_reason()
            return _block(reason) if reason else {}
        if event == "SubagentStop":
            control = _control(value)
            try:
                owner = _owner(value)
            except Exception:
                return {
                    "continue": False,
                    "systemMessage": (
                        "CCO could not map the native child result; Primary must inspect the actual state."
                    )
                }
            message = value.get("last_assistant_message")
            try:
                control.record_result(owner, message)
            except (ControlPlaneUnavailable, StateLockBusy, OSError) as error:
                return _event_error(event, error)
            except Exception:
                try:
                    control.fence_invalid_result(owner)
                except (ControlPlaneUnavailable, StateLockBusy, OSError) as error:
                    return _event_error(event, error)
                return {
                    "continue": False,
                    "systemMessage": (
                        "CCO rejected and fenced the child result; Primary must inspect the actual state."
                    )
                }
            return {"continue": False}
        return {}
    except Exception as error:
        return _event_error(event, error)


def _load_input() -> object:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ControlPlaneError("hook input exceeds 4 MiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlPlaneError("hook input is not valid UTF-8 JSON") from error


def main() -> int:
    try:
        outcome = evaluate(_load_input())
    except Exception as error:
        outcome = _block(error)
    if outcome:
        print(json.dumps(outcome, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

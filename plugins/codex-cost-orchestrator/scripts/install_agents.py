#!/usr/bin/env python3
"""Install and diagnose the two model-neutral cco.v9 native Agent profiles."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Mapping

from routing_catalog import (
    RoutingCatalogError,
    load_native_catalog,
    resolve_route_plan,
)
from workspace_guard import WorkspaceGuardError, discover_workspace


PLUGIN_ID = "codex-cost-orchestrator@codex-cost-orchestrator"
PLUGIN_VERSION = "0.9.3"
PLUGIN_RELEASE = PLUGIN_VERSION
PROFILES = {
    "read": ("codex-cost-orchestrator-read-leaf.toml", "cost_orchestrator_read_leaf"),
    "write": ("codex-cost-orchestrator-write-leaf.toml", "cost_orchestrator_write_leaf"),
}

# These names and matchers are the host-facing contract. Keep them independent
# of hooks.json so a damaged local manifest cannot redefine what doctor accepts.
HOST_HOOK_CONTRACT: tuple[tuple[str, str | None], ...] = (
    ("sessionStart", None),
    (
        "preToolUse",
        "^(Agent|spawn_agent|collaborationspawn_agent|send_message|followup_task|"
        "collaborationsend_message|collaborationfollowup_task|interrupt_agent|"
        "interruptAgent|collaborationinterrupt_agent)$",
    ),
    (
        "postToolUse",
        "^(Agent|spawn_agent|collaborationspawn_agent|followup_task|"
        "collaborationfollowup_task|interrupt_agent|interruptAgent|"
        "collaborationinterrupt_agent)$",
    ),
    ("stop", None),
    ("subagentStop", "^(cost_orchestrator_read_leaf|cost_orchestrator_write_leaf)$"),
)


def compressed_rollout_supported() -> bool:
    try:
        from compression import zstd as _zstd  # noqa: F401
    except ImportError:
        try:
            import zstandard as _zstandard  # noqa: F401
        except ImportError:
            return False
    return True


class InstallError(RuntimeError):
    pass


def _canonical_hook_schema(
    hook_value: object,
    *,
    plugin_root: Path,
) -> Counter[tuple[str, str | None, str, str, int | float, str]]:
    """Translate hooks.json into the Hook fields reported by Codex."""

    if not isinstance(hook_value, dict) or not hook_value:
        raise ValueError("Hook definition has no lifecycle entries")
    command_key = "commandWindows" if os.name == "nt" else "command"
    schema: Counter[tuple[str, str | None, str, str, int | float, str]] = Counter()
    for lifecycle, groups in hook_value.items():
        if not isinstance(lifecycle, str) or not lifecycle or not isinstance(groups, list):
            raise ValueError("Hook lifecycle definition is malformed")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Hook matcher group is malformed")
            matcher = group.get("matcher")
            handlers = group.get("hooks")
            if (
                (matcher is not None and not isinstance(matcher, str))
                or not isinstance(handlers, list)
                or not handlers
            ):
                raise ValueError("Hook matcher group is incomplete")
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise ValueError("Hook handler is malformed")
                handler_type = handler.get("type")
                command = handler.get(command_key)
                timeout = handler.get("timeout")
                status_message = handler.get("statusMessage")
                if (
                    not isinstance(handler_type, str)
                    or not isinstance(handler.get("command"), str)
                    or not isinstance(handler.get("commandWindows"), str)
                    or not isinstance(command, str)
                    or not isinstance(timeout, (int, float))
                    or isinstance(timeout, bool)
                    or not isinstance(status_message, str)
                    or not isinstance(handler.get("async"), bool)
                ):
                    raise ValueError("Hook handler is incomplete")
                schema[
                    (
                        lifecycle[:1].lower() + lifecycle[1:],
                        matcher,
                        handler_type,
                        command.replace("${PLUGIN_ROOT}", str(plugin_root)),
                        timeout,
                        status_message,
                    )
                ] += 1
    if not schema:
        raise ValueError("Hook definition has no handlers")
    return schema


def _inventory_hook_schema(
    item: Mapping[str, object],
) -> tuple[str, str | None, str, str, int | float, str]:
    event_name = item["eventName"]
    matcher = item["matcher"]
    handler_type = item["handlerType"]
    command = item["command"]
    timeout = item["timeoutSec"]
    status_message = item["statusMessage"]
    if (
        not isinstance(event_name, str)
        or (matcher is not None and not isinstance(matcher, str))
        or not isinstance(handler_type, str)
        or not isinstance(command, str)
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not isinstance(status_message, str)
    ):
        raise ValueError("Hook inventory definition is malformed")
    return event_name, matcher, handler_type, command, timeout, status_message


def _validate_local_hook_contract(hook_value: object) -> None:
    """Ensure the shipped manifest still declares the fixed host surface."""

    if not isinstance(hook_value, dict):
        raise ValueError("Hook lifecycle definition is malformed")
    expected_lifecycles = {
        event[:1].upper() + event[1:] for event, _matcher in HOST_HOOK_CONTRACT
    }
    if set(hook_value) != expected_lifecycles:
        raise ValueError("Hook lifecycle surface is incomplete or contains unknown entries")
    for event, expected_matcher in HOST_HOOK_CONTRACT:
        lifecycle = event[:1].upper() + event[1:]
        groups = hook_value.get(lifecycle)
        if (
            not isinstance(groups, list)
            or len(groups) != 1
            or not isinstance(groups[0], dict)
            or groups[0].get("matcher") != expected_matcher
        ):
            raise ValueError(f"{lifecycle} Hook matcher is not canonical")


def _default_target() -> Path:
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return home / "agents"


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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _templates() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[1] / "agents"
    result = {name: root / filename for name, (filename, _agent) in PROFILES.items()}
    for name, path in result.items():
        if not path.is_file() or _is_reparse(path):
            raise InstallError(f"{name} profile template is unavailable")
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise InstallError(f"{name} profile template is invalid") from error
        if parsed.get("name") != PROFILES[name][1]:
            raise InstallError(f"{name} profile template identity is invalid")
        if "model" in parsed or "model_reasoning_effort" in parsed:
            raise InstallError(f"{name} profile must remain model-neutral")
        if parsed.get("features", {}).get("multi_agent") is not False:
            raise InstallError(f"{name} profile must be non-delegating")
    return result


def _target(path: Path) -> Path:
    requested = Path(os.path.abspath(path.expanduser()))
    for candidate in (requested, *requested.parents):
        if candidate.exists() and _is_reparse(candidate):
            raise InstallError("profile target cannot traverse a reparse point")
    return requested


def _selected(names: list[str] | None) -> list[str]:
    selected = list(dict.fromkeys(names or PROFILES))
    if any(name not in PROFILES for name in selected):
        raise InstallError("unknown CCO profile")
    return selected


def _stage(parent: Path, contents: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=parent,
        prefix=".cco-profile-",
        delete=False,
    ) as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def install(target: Path, *, profiles: list[str] | None = None, replace: bool = False) -> int:
    templates = _templates()
    destination_root = _target(target)
    destination_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse(destination_root):
        raise InstallError("profile target cannot be a reparse point")
    selected = _selected(profiles)
    operations: list[tuple[Path, Path, Path | None]] = []
    staged_paths: list[Path] = []
    backups: list[Path] = []
    try:
        for name in selected:
            filename = PROFILES[name][0]
            source = templates[name]
            destination = destination_root / filename
            if destination.exists() and _is_reparse(destination):
                raise InstallError(f"profile destination is a reparse point: {destination}")
            if destination.is_file() and destination.read_bytes() == source.read_bytes():
                print(f"READY: {destination}")
                continue
            if destination.exists() and not replace:
                raise InstallError(
                    f"profile already exists with different content: {destination}; use --replace explicitly"
                )
            staged = _stage(destination_root, source.read_bytes())
            staged_paths.append(staged)
            backup = None
            if destination.exists():
                backup = _stage(destination_root, destination.read_bytes())
                backups.append(backup)
            operations.append((staged, destination, backup))
        applied: list[tuple[Path, Path | None]] = []
        try:
            for staged, destination, backup in operations:
                os.replace(staged, destination)
                staged_paths.remove(staged)
                applied.append((destination, backup))
        except Exception:
            for destination, backup in reversed(applied):
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, destination)
                    backups.remove(backup)
            raise
        for _staged, destination, _backup in operations:
            print(f"INSTALLED: {destination}")
        return 0
    finally:
        for path in [*staged_paths, *backups]:
            path.unlink(missing_ok=True)


def check(target: Path, *, profiles: list[str] | None = None) -> int:
    templates = _templates()
    destination_root = _target(target)
    errors: list[str] = []
    for name in _selected(profiles):
        destination = destination_root / PROFILES[name][0]
        if not destination.is_file() or _is_reparse(destination):
            errors.append(f"profile is missing or unsafe: {destination}")
        elif _sha(destination) != _sha(templates[name]):
            errors.append(f"profile does not match this plugin: {destination}")
        else:
            print(f"READY: {destination}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


def uninstall(target: Path, *, profiles: list[str] | None = None) -> int:
    templates = _templates()
    destination_root = _target(target)
    errors: list[str] = []
    for name in _selected(profiles):
        destination = destination_root / PROFILES[name][0]
        if not destination.exists():
            continue
        if not destination.is_file() or _is_reparse(destination):
            errors.append(f"refusing unsafe profile removal: {destination}")
            continue
        if _sha(destination) != _sha(templates[name]):
            errors.append(f"refusing modified profile removal: {destination}")
            continue
        destination.unlink()
        print(f"REMOVED: {destination}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


def load_hook_inventory(
    workspace: Path,
    *,
    executable: Path | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    """Read Codex's authoritative Hook discovery and trust state."""

    if executable is None:
        names = ("codex.cmd", "codex") if os.name == "nt" else ("codex",)
        resolved = next((shutil.which(name) for name in names if shutil.which(name)), None)
        executable = Path(resolved) if resolved else None
    if executable is None:
        raise OSError("Codex CLI is unavailable")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(executable), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=flags,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise OSError("Codex app-server pipes are unavailable")
    messages: queue.Queue[object] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        messages.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(message: Mapping[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("Codex Hook inspection timed out")
            message = messages.get(timeout=remaining)
            if message is None:
                raise OSError("Codex app-server closed during Hook inspection")
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise OSError(f"Codex rejected Hook inspection: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise OSError("Codex returned malformed Hook state")
            return result

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {"experimentalApi": True},
                    "clientInfo": {
                        "name": "cco_doctor",
                        "title": "CCO Doctor",
                        "version": PLUGIN_RELEASE,
                    },
                },
            }
        )
        response(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(workspace.resolve())]}})
        result = response(2)
        data = result.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise OSError("Codex returned no exact workspace Hook state")
        return data[0]
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)


def doctor(
    target: Path,
    *,
    workspace: Path,
    native_loader: Callable[[], dict[str, object]] | None = None,
    hook_loader: Callable[[Path], dict[str, object]] | None = None,
) -> int:
    errors: list[str] = []
    if sys.version_info < (3, 11):
        errors.append("Python 3.11 or newer is required")
    if not compressed_rollout_supported():
        errors.append(
            "compressed rollout support is unavailable; install zstandard>=0.23,<1"
        )
    try:
        _backend, canonical_workspace = discover_workspace(workspace)
    except (OSError, WorkspaceGuardError) as error:
        errors.append(f"workspace root is unavailable: {error}")
        canonical_workspace = workspace
    plugin_root = Path(__file__).resolve().parents[1]
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    hooks = plugin_root / "hooks" / "hooks.json"
    canonical_hook_schema: Counter[
        tuple[str, str | None, str, str, int | float, str]
    ] | None = None
    try:
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_value.get("version") != PLUGIN_RELEASE:
            errors.append("plugin manifest version does not match the installer")
        hook_value = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]
        _validate_local_hook_contract(hook_value)
        canonical_hook_schema = _canonical_hook_schema(
            hook_value,
            plugin_root=plugin_root,
        )
        if any(entry[1] == ".*" for entry in canonical_hook_schema):
            errors.append("global all-tool Hook matcher is forbidden")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        errors.append("plugin manifest or Hook definition is unreadable")
    if check(target) != 0:
        errors.append("installed CCO profiles are not ready")
    try:
        inventory = (hook_loader or load_hook_inventory)(canonical_workspace)
        discovered = [
            item
            for item in inventory.get("hooks", [])
            if isinstance(item, dict) and item.get("pluginId") == PLUGIN_ID
        ]
        expected_events = Counter(event for event, _matcher in HOST_HOOK_CONTRACT)
        expected_matchers = dict(HOST_HOOK_CONTRACT)
        counts: Counter[str] = Counter()
        unknown_definitions = False
        for item in discovered:
            event = item.get("eventName")
            if not isinstance(event, str) or event not in expected_events:
                unknown_definitions = True
                continue
            counts[event] += 1
            # Older hosts omitted detailed fields. When a matcher is present,
            # still bind it to the fixed event contract.
            if "matcher" in item and item.get("matcher") != expected_matchers[event]:
                errors.append(f"Codex {event} Hook matcher is not canonical")
        if any(counts[event] < count for event, count in expected_events.items()):
            errors.append("Codex did not discover every required CCO Hook")
        if any(counts[event] > count for event, count in expected_events.items()):
            errors.append("Codex discovered duplicate CCO Hook definitions")
        if unknown_definitions:
            errors.append("Codex discovered unknown CCO Hook definitions")
        if canonical_hook_schema is not None:
            schema_fields = (
                "matcher",
                "handlerType",
                "command",
                "timeoutSec",
                "statusMessage",
            )
            if any(any(field in item for field in schema_fields) for item in discovered):
                try:
                    discovered_schema = Counter(
                        _inventory_hook_schema(item) for item in discovered
                    )
                except (KeyError, TypeError, ValueError):
                    errors.append("Codex returned incomplete CCO Hook definitions")
                else:
                    if discovered_schema != canonical_hook_schema:
                        errors.append(
                            "Codex Hook definitions do not match the canonical local Hook schema"
                        )
        if any(
            item.get("enabled") is not True
            or str(item.get("trustStatus", "")).casefold() not in {"managed", "trusted"}
            for item in discovered
        ):
            errors.append("CCO Hooks are not enabled and trusted; review them in /hooks")
        if inventory.get("errors"):
            errors.append("Codex reported Hook discovery errors")
        if not errors:
            print(f"HOOKS READY: {len(discovered)} enabled and trusted definitions")
    except (OSError, ValueError, queue.Empty) as error:
        errors.append(f"CCO Hook trust could not be verified: {error}")
    try:
        catalog = (native_loader or load_native_catalog)()
        plan = resolve_route_plan(
            [
                {
                    "assurance": "mechanical",
                    "constraints": {"fixed_effort": None, "fixed_model": None, "source": "automatic"},
                    "node": "doctor_worker",
                    "role": "worker",
                }
            ],
            catalog,
        )
        selected = plan["routes"][0]["candidates"][0]
        print(f"ROUTE READY: {selected['model']}/{selected['effort']}")
    except (OSError, RoutingCatalogError) as error:
        errors.append(f"native route is unavailable: {error}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Install or diagnose cco.v9 native Agent profiles.")
    action = root.add_mutually_exclusive_group(required=True)
    action.add_argument("--bootstrap", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--doctor", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    root.add_argument("--replace", action="store_true")
    root.add_argument("--target-dir", type=Path, default=_default_target())
    root.add_argument("--workspace", type=Path, default=Path.cwd())
    root.add_argument("--profile", action="append", choices=sorted(PROFILES))
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.doctor:
            return doctor(args.target_dir, workspace=args.workspace)
        if args.check:
            return check(args.target_dir, profiles=args.profile)
        if args.uninstall:
            return uninstall(args.target_dir, profiles=args.profile)
        return install(args.target_dir, profiles=args.profile, replace=args.replace)
    except (InstallError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install role-bounded Codex profiles without overwriting user-owned files."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable

from routing_catalog import (  # noqa: E402
    RoutingCatalogError,
    load_native_catalog,
    resolve_route_plan,
)


PROFILE_FILENAMES = {
    "read": "codex-cost-orchestrator-read-leaf.toml",
    "write": "codex-cost-orchestrator-write-leaf.toml",
}
PROFILE_AGENT_NAMES = {
    "read": "cost_orchestrator_read_leaf",
    "write": "cost_orchestrator_write_leaf",
}
PLUGIN_ID = "codex-cost-orchestrator@codex-cost-orchestrator"
PLUGIN_VERSION = "1.2.0"
REQUIRED_HOOK_EVENTS = Counter(
    {
        "sessionStart": 1,
        "preToolUse": 2,
        "postToolUse": 1,
        "stop": 1,
        "userPromptSubmit": 1,
        "subagentStop": 1,
    }
)
LEGACY_PROFILE_SHA256 = {
    "codex-cost-orchestrator-read-leaf.toml": frozenset(
        {
            # Published 0.9.0 model-neutral read leaf.
            "61429cc3b87befab48353b6a9c8203a364a04dc2e6ddfafa0be61ef03c75af68",
            # Published 1.1.3 model-neutral read leaf.
            "c88e3ca0b09f3fe25219f0d219c207519899ef15b3015d982f73e10288baa7a0",
        }
    ),
    "codex-cost-orchestrator-write-leaf.toml": frozenset(
        {
            # Published 0.9.0 model-neutral write leaf.
            "e3759c8092e929e6f1dbde98a08b9b3a2f003304ed244531aa1ea6cd9c1415f1",
            # Published 1.1.3 model-neutral write leaf.
            "3d00f207cafc8e4eba5434349edddbd7672a570cad4884f5e6c51eb5de2b4611",
        }
    ),
    "codex-cost-orchestrator-routine-worker.toml": frozenset(
        {
            "2c5b1716312ad7be52eaec26676b52c1a5168cb1d3c602d39a82f907b4afa93d",
            "c9b2187367ab1c167cae594bf589c74adb3b8959c3c4292751117aec820cdc21",
            "bc8ce4bfb9b58b0fac32272f60456b3b327f1d21427dd8e01dff5fd22ac5ceb5",
            "cf9e6b654c6426717ebf738548cf1b5830615b248bddc9acda2f29428b7f62a1",
            # Previous locally-installed CCO v4 profile with an explicit Sol pin.
            "0471ca8bd00f5bad089aaf6b1bf45d9b7403c1df679f278c29f975d523d981c4",
            # CCO v5 model-neutral routine leaf.
            "5004b32698625ab551ae24e6206235c53a30d726eb77ec8981d6132ed2b971a3",
        }
    ),
    "codex-cost-orchestrator-complex-worker.toml": frozenset(
        {
            "881c3b606c1e9092a96e79ad85bd5b57fef97156f4838a26b18191d18bfee681",
            "e50aadfd85e83841750cea5af5b076e746e7bfddf63cc3f24c27beddc9b8a851",
            "cca439e5c44163be360c665c18fbd9a1641fcd3e28d1488ef60e5ce0b58eb884",
            "a5937769fe00b99480feb5dcc289b862e4529d9605836d1a62d59de01dfcbb90",
            "adfc26ef02fd70a987427b80429eb17be7c9b20fb31b8fe8d8c3eaaab77fd7a9",
            # CCO v5 model-neutral complex leaf.
            "58bdfe423732a4896e7331461b6c789115e3491af72643d67756c7fd6654e904",
        }
    ),
    "codex-cost-orchestrator-reviewer.toml": frozenset(
        {
            "015c8fe9da8b92a24f021120e71f6b0e3e0bfb10244ba529f7ff8981ce179e00",
            "395a35427315e71b37227a79ac38889aa9f102eebf7dbdb78d9ae86b510ee9bc",
            "1df307612992239960b4dececd79f6f1935b42662983204d350cb7ed519528b8",
            "309381c4bb2c009d0f062677b00b6f74ecf788b9eacd018dd344e3f4b0bb20f8",
            "e9769ef541b2bcd6ded6f407e90cfab077965a05a24b180735759418d00710f1",
            # CCO v5 model-neutral reviewer leaf.
            "0d8c3f65266444cebf48595e5a9257f54dcaea03775a8a66eb8d496e200bcddd",
        }
    ),
    "codex-cost-orchestrator-analysis-worker.toml": frozenset(
        {
            # Published v5 analysis leaf.  It never existed in the repository's
            # v4 tree, so its exact installed hash is recorded explicitly.
            "7978912a51a343e2efefc82ec56d82e036910138d66146f574a32f0546c131d9",
        }
    ),
}
OBSOLETE_ROUTE_CACHE_NAMES = frozenset(
    {
        "native-catalog-v1.json",
        "radar-lkg-v1.json",
        "radar-refresh-request-v1.json",
        "radar-refresh-v1.lock",
        "routing-state-v1.json",
        "routing-state-v2.json",
        "routing-v1.lock",
    }
)
OBSOLETE_ROUTE_CACHE_GLOBS = (
    "native-catalog-v1.*.tmp",
    "radar-lkg-v1.*.tmp",
    "routing-state-v1.*.tmp",
    "routing-state-v2.*.tmp",
)


class InstallTransactionError(Exception):
    pass


@dataclass
class PreparedChange:
    filename: str
    destination: Path
    template_bytes: bytes
    kind: str
    staged_path: Path | None
    backup_path: Path | None
    source_identity: tuple[int, int] | None = None
    source_sha256: str | None = None
    applied_identity: tuple[int, int] | None = None
    applied: bool = False


def default_target() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "agents"


def is_real_file(path: Path) -> bool:
    return path.is_file() and not is_reparse(path)


def is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    is_junction = getattr(path, "is_junction", lambda: False)()
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        or is_junction
    )


def has_reparse_ancestor(path: Path) -> bool:
    absolute = Path(os.path.abspath(path.expanduser()))
    return any(is_reparse(candidate) for candidate in (absolute, *absolute.parents))


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_critical_plugin_paths(plugin_root: Path) -> tuple[Path, ...]:
    paths = {Path("hooks") / "hooks.json"}
    for directory in ("hooks", "scripts"):
        root = plugin_root / directory
        if root.is_dir():
            paths.update(path.relative_to(plugin_root) for path in root.rglob("*.py"))
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def runtime_files_match(plugin_root: Path, discovered_root: Path) -> bool:
    for relative_path in runtime_critical_plugin_paths(plugin_root):
        expected = plugin_root / relative_path
        discovered = discovered_root / relative_path
        if not is_real_file(expected) or not is_real_file(discovered):
            return False
        if file_sha256(expected) != file_sha256(discovered):
            return False
    return True


def plugin_identity_matches(plugin_root: Path, discovered_root: Path) -> bool:
    manifest_relative_path = Path(".codex-plugin") / "plugin.json"
    expected_manifest_path = plugin_root / manifest_relative_path
    discovered_manifest_path = discovered_root / manifest_relative_path
    if not is_real_file(expected_manifest_path) or not is_real_file(discovered_manifest_path):
        return False
    expected_manifest = json.loads(expected_manifest_path.read_text(encoding="utf-8"))
    discovered_manifest = json.loads(discovered_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(expected_manifest, dict) or not isinstance(discovered_manifest, dict):
        return False
    expected_name = PLUGIN_ID.partition("@")[0]
    if expected_manifest.get("name") != expected_name:
        return False
    if discovered_manifest.get("name") != expected_name:
        return False
    if "version" not in discovered_manifest:
        return True
    discovered_version = discovered_manifest["version"]
    if not isinstance(discovered_version, str):
        return False
    if not discovered_version.strip():
        return True
    expected_version = expected_manifest.get("version", PLUGIN_VERSION)
    return isinstance(expected_version, str) and discovered_version == expected_version


def stage_bytes(target: Path, contents: bytes, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=prefix, dir=target, delete=False
    ) as staged:
        staged.write(contents)
        staged.flush()
        os.fsync(staged.fileno())
        return Path(staged.name)


def file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def stage_hardlink(target: Path, source: Path, prefix: str) -> Path:
    """Retain the original inode and metadata under a transaction-only name."""
    for _attempt in range(100):
        backup = target / f"{prefix}{secrets.token_hex(16)}"
        try:
            os.link(source, backup)
            return backup
        except FileExistsError:
            continue
    raise InstallTransactionError("could not reserve an upgrade backup path")


def cleanup_prepared(changes: list[PreparedChange]) -> None:
    for change in changes:
        for path in (change.staged_path, change.backup_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def removable_legacy_profiles(target: Path) -> list[Path]:
    """Return only old CCO profiles whose bytes match a published release."""

    removable: list[Path] = []
    for filename, hashes in LEGACY_PROFILE_SHA256.items():
        candidate = target / filename
        if is_real_file(candidate) and file_sha256(candidate) in hashes:
            removable.append(candidate)
    return removable


def cleanup_obsolete_route_cache(target: Path) -> list[Path]:
    """Remove only filenames owned by the removed Radar runtime."""

    cache = Path(os.path.abspath(target.expanduser())).parent / "cache" / "codex-cost-orchestrator"
    if not cache.is_dir() or is_reparse(cache):
        return []
    candidates = {cache / name for name in OBSOLETE_ROUTE_CACHE_NAMES}
    for pattern in OBSOLETE_ROUTE_CACHE_GLOBS:
        candidates.update(cache.glob(pattern))
    removed: list[Path] = []
    for candidate in sorted(candidates, key=lambda path: path.name):
        if not is_real_file(candidate):
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)
    return removed


def prepare_change(
    target: Path,
    filename: str,
    destination: Path,
    template_bytes: bytes,
    kind: str,
    source_identity: tuple[int, int] | None = None,
    source_sha256: str | None = None,
) -> PreparedChange:
    staged_path: Path | None = None
    backup_path: Path | None = None
    try:
        if kind != "remove":
            staged_path = stage_bytes(
                target, template_bytes, ".cost-orchestrator-agent-"
            )
        if kind in {"upgrade", "remove"}:
            if (
                source_identity is None
                or source_sha256 is None
                or not is_real_file(destination)
                or file_identity(destination) != source_identity
                or file_sha256(destination) != source_sha256
            ):
                raise InstallTransactionError(
                    f"destination changed before upgrade preparation: {destination}"
                )
            backup_path = stage_hardlink(
                target,
                destination,
                ".cost-orchestrator-backup-",
            )
            if not os.path.samefile(destination, backup_path):
                raise InstallTransactionError(
                    f"destination changed during upgrade preparation: {destination}"
                )
        return PreparedChange(
            filename=filename,
            destination=destination,
            template_bytes=template_bytes,
            kind=kind,
            staged_path=staged_path,
            backup_path=backup_path,
            source_identity=source_identity,
            source_sha256=source_sha256,
        )
    except (OSError, InstallTransactionError):
        for path in (staged_path, backup_path):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise


def rollback_prepared(changes: list[PreparedChange]) -> list[str]:
    errors: list[str] = []
    for change in reversed(changes):
        if not change.applied:
            continue
        try:
            if change.kind == "upgrade":
                if change.backup_path is None:
                    raise InstallTransactionError("upgrade backup is unavailable")
                if (
                    not is_real_file(change.destination)
                    or change.applied_identity is None
                    or file_identity(change.destination) != change.applied_identity
                    or change.destination.read_bytes() != change.template_bytes
                ):
                    raise InstallTransactionError(
                        "upgraded destination changed before rollback"
                    )
                os.replace(change.backup_path, change.destination)
                change.backup_path = None
            elif (
                change.kind == "remove"
                and not change.destination.exists()
                and change.backup_path is not None
            ):
                os.replace(change.backup_path, change.destination)
                change.backup_path = None
            elif (
                is_real_file(change.destination)
                and change.applied_identity is not None
                and file_identity(change.destination) == change.applied_identity
                and change.destination.read_bytes() == change.template_bytes
            ):
                change.destination.unlink()
            else:
                raise InstallTransactionError(
                    "new destination changed before rollback"
                )
            change.applied = False
        except (OSError, InstallTransactionError) as error:
            errors.append(f"rollback failed for {change.destination}: {error}")
    return errors


def same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def template_error(path: Path, profile: str) -> str | None:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return f"shipped profile is not valid UTF-8 TOML: {path}"
    if value.get("name") != PROFILE_AGENT_NAMES[profile]:
        return f"shipped profile has the wrong agent name: {path}"
    if profile in {"read", "write"} and (
        "model" in value or "model_reasoning_effort" in value
    ):
        return f"leaf profile must not pin model or model_reasoning_effort: {path}"
    if profile == "read" and value.get("sandbox_mode") != "read-only":
        return f"read profile must request read-only execution: {path}"
    if "agents" in value:
        return f"leaf profile must not declare an agents table: {path}"
    features = value.get("features", {})
    multi_agent_v2 = features.get("multi_agent_v2")
    if (
        features.get("multi_agent") is not False
        or not isinstance(multi_agent_v2, dict)
        or multi_agent_v2 != {"enabled": False}
    ):
        return f"leaf profile must disable multi-agent features: {path}"
    return None


def active_config_folders(workspace: Path) -> list[Path]:
    workspace = Path(os.path.abspath(workspace.expanduser()))
    if not workspace.is_dir():
        raise ValueError("workspace is not a directory")
    repository: Path | None = None
    probe = workspace
    while True:
        if (probe / ".git").exists():
            repository = probe
            break
        if probe == Path(probe.anchor):
            break
        probe = probe.parent
    if repository is None:
        return [workspace / ".codex"]
    folders: list[Path] = []
    current = workspace
    while True:
        folders.append(current / ".codex")
        if current == repository:
            break
        current = current.parent
    return folders


def shadow_errors(
    workspace: Path,
    templates: Path,
    selected_profiles: tuple[str, ...],
    config_home: Path,
    installed_target: Path,
) -> list[str]:
    """Reject active project-layer role files that replace shipped authority."""
    expected = {
        PROFILE_AGENT_NAMES[profile]: (templates / PROFILE_FILENAMES[profile]).read_bytes()
        for profile in selected_profiles
    }
    errors: list[str] = []
    try:
        config_folders = active_config_folders(workspace)
    except ValueError:
        return [f"workspace is not a directory: {workspace}"]
    resolved_home = Path(os.path.abspath(config_home.expanduser()))
    managed_destinations = {
        Path(os.path.abspath((installed_target / PROFILE_FILENAMES[profile]).expanduser()))
        for profile in selected_profiles
    }
    if resolved_home not in config_folders:
        config_folders.append(resolved_home)

    for config_folder in config_folders:
        agents_dir = config_folder / "agents"
        if agents_dir.is_dir() and not agents_dir.is_symlink():
            for candidate in agents_dir.rglob("*.toml"):
                if not is_real_file(candidate):
                    continue
                if any(
                    same_path(candidate, destination)
                    for destination in managed_destinations
                ):
                    continue
                try:
                    value = tomllib.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                    continue
                name = value.get("name")
                if isinstance(name, str) and name in expected:
                    if candidate.read_bytes() != expected[name]:
                        errors.append(
                            f"active project role shadows the shipped {name} profile: {candidate}"
                        )

        config_file = config_folder / "config.toml"
        if not is_real_file(config_file):
            continue
        try:
            config = tomllib.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        agents = config.get("agents")
        if not isinstance(agents, dict):
            continue
        for name, role in agents.items():
            if name not in expected or not isinstance(role, dict):
                continue
            declared_file = role.get("config_file")
            if declared_file is None:
                continue
            if not isinstance(declared_file, str) or not declared_file:
                errors.append(
                    f"active project role has an invalid shadow config_file for {name}: {config_file}"
                )
                continue
            candidate = Path(declared_file).expanduser()
            if not candidate.is_absolute():
                candidate = config_folder / candidate
            if not is_real_file(candidate) or candidate.read_bytes() != expected[name]:
                errors.append(
                    f"active project config shadows the shipped {name} profile: {config_file}"
                )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Codex Cost Orchestrator custom-agent profiles."
    )
    parser.add_argument("--target-dir", type=Path, default=default_target())
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Active workspace whose project config layers must not shadow selected roles.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check",
        action="store_true",
        help="Verify exact installed copies without creating files.",
    )
    actions.add_argument(
        "--upgrade",
        action="store_true",
        help="Atomically replace only byte-identical profiles from known prior CCO releases.",
    )
    actions.add_argument(
        "--bootstrap",
        action="store_true",
        help="Install or safely upgrade the two CCO-owned profiles.",
    )
    actions.add_argument(
        "--doctor",
        action="store_true",
        help="Check profiles, Python, native capabilities, and static route readiness without changes.",
    )
    actions.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove only byte-identical CCO-owned profiles; preserve user changes.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=tuple(PROFILE_FILENAMES),
        help="Limit installation or checking to a required role; repeat as needed.",
    )
    return parser.parse_args()


def uninstall(
    target: Path,
    *,
    profiles: list[str] | None = None,
) -> int:
    """Remove only files whose bytes are owned by this or a known CCO release."""

    target = Path(os.path.abspath(target.expanduser()))
    selected_profiles = tuple(dict.fromkeys(profiles or PROFILE_FILENAMES))
    filenames = tuple(PROFILE_FILENAMES[name] for name in selected_profiles)
    script_dir = Path(__file__).resolve().parent
    templates = script_dir.parent / "agents"
    if target == Path(target.anchor) or has_reparse_ancestor(target):
        fail(f"refusing to use an unsafe agent target directory: {target}")
        return 1
    if not target.exists():
        print(f"UNINSTALL: no agent directory exists at {target}")
        return 0
    if not target.is_dir() or is_reparse(target):
        fail(f"target directory is not a real directory: {target}")
        return 1

    errors: list[str] = []
    changes: list[PreparedChange] = []
    for profile in selected_profiles:
        filename = PROFILE_FILENAMES[profile]
        destination = target / filename
        template = templates / filename
        if not destination.exists() and not destination.is_symlink():
            continue
        if not is_real_file(destination):
            errors.append(f"preserved non-regular profile: {destination}")
            continue
        destination_hash = file_sha256(destination)
        if (
            destination.read_bytes() != template.read_bytes()
            and destination_hash not in LEGACY_PROFILE_SHA256.get(filename, frozenset())
        ):
            errors.append(f"preserved modified profile: {destination}")
            continue
        try:
            changes.append(
                prepare_change(
                    target,
                    filename,
                    destination,
                    destination.read_bytes(),
                    "remove",
                    file_identity(destination),
                    file_sha256(destination),
                )
            )
        except (OSError, InstallTransactionError) as error:
            errors.append(f"preserved profile {destination}: {error}")

    if profiles is None:
        for destination in removable_legacy_profiles(target):
            if destination.name in filenames:
                continue
            try:
                changes.append(
                    prepare_change(
                        target,
                        destination.name,
                        destination,
                        destination.read_bytes(),
                        "remove",
                        file_identity(destination),
                        file_sha256(destination),
                    )
                )
            except (OSError, InstallTransactionError) as error:
                errors.append(f"preserved legacy profile {destination}: {error}")

    try:
        for change in changes:
            if (
                not is_real_file(change.destination)
                or change.source_identity is None
                or file_identity(change.destination) != change.source_identity
                or change.source_sha256 is None
                or file_sha256(change.destination) != change.source_sha256
            ):
                raise InstallTransactionError(
                    f"profile changed during uninstall: {change.destination}"
                )
            change.destination.unlink()
            change.applied = True
    except (OSError, InstallTransactionError) as error:
        errors.append(str(error))
        rollback_errors = rollback_prepared(changes)
        errors.extend(rollback_errors)
    finally:
        cleanup_prepared(changes)

    for change in changes:
        if change.applied is False and change.destination.exists():
            continue
        if not change.destination.exists():
            print(f"REMOVED: {change.destination}")
    for error in errors:
        fail(error)
    return 1 if errors else 0


def load_hook_inventory(
    workspace: Path,
    *,
    executable: Path | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    """Read Codex's authoritative hook trust view without changing config."""

    if timeout_seconds <= 0:
        raise ValueError("hook inventory timeout must be positive")
    if executable is None:
        names = ("codex.cmd", "codex") if os.name == "nt" else ("codex",)
        resolved = next((shutil.which(name) for name in names if shutil.which(name)), None)
        executable = Path(resolved) if resolved else None
    if executable is None:
        raise OSError("Codex CLI is unavailable for hook trust inspection")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(executable), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=creationflags,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise OSError("Codex app-server pipes are unavailable")

    messages: queue.Queue[object] = queue.Queue()

    def read_messages() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        messages.put(None)

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()

    def send(message: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("Codex hook trust inspection timed out")
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty as error:
                raise OSError("Codex hook trust inspection timed out") from error
            if message is None:
                raise OSError("Codex app-server closed before returning hook state")
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise OSError(f"Codex app-server rejected hook inspection: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise OSError("Codex app-server returned malformed hook state")
            return result

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {"experimentalApi": True},
                    "clientInfo": {
                        "name": "codex_cost_orchestrator_doctor",
                        "title": "CCO Doctor",
                        "version": PLUGIN_VERSION,
                    },
                },
            }
        )
        response(1)
        send({"method": "initialized"})
        send(
            {
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(Path(workspace).resolve())]},
            }
        )
        result = response(2)
        data = result.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise OSError("Codex app-server returned no exact workspace hook state")
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
    """Perform read-only installation and static-route readiness checks."""

    errors: list[str] = []
    if sys.version_info < (3, 11):
        errors.append("Python 3.11 or newer is required")
    plugin_root = Path(__file__).resolve().parent.parent
    hooks_path = plugin_root / "hooks" / "hooks.json"
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    skill_path = plugin_root / "skills" / "orchestrate" / "SKILL.md"
    for path in (hooks_path, manifest_path, skill_path):
        if not is_real_file(path):
            errors.append(f"required plugin file is unavailable: {path}")
    if is_real_file(hooks_path):
        try:
            hook_document = json.loads(hooks_path.read_text(encoding="utf-8"))
            events = set(hook_document.get("hooks", {}))
            required_events = {
                "SessionStart",
                "PreToolUse",
                "PostToolUse",
                "Stop",
                "UserPromptSubmit",
                "SubagentStop",
            }
            if not required_events <= events:
                errors.append("hook lifecycle configuration is incomplete")
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append("hook lifecycle configuration is unreadable")
    profile_result = install(
        target,
        check_only=True,
        profiles=None,
        workspace=workspace,
    )
    if profile_result != 0:
        errors.append("installed CCO profiles are not ready")
    try:
        inventory = (hook_loader or load_hook_inventory)(workspace)
        hooks = inventory.get("hooks")
        warnings = inventory.get("warnings", [])
        discovery_errors = inventory.get("errors", [])
        if not isinstance(hooks, list) or not isinstance(warnings, list) or not isinstance(discovery_errors, list):
            raise OSError("Codex hook inventory is malformed")
        plugin_hooks = [
            hook
            for hook in hooks
            if isinstance(hook, dict) and hook.get("pluginId") == PLUGIN_ID
        ]
        counts = Counter(
            hook.get("eventName")
            for hook in plugin_hooks
            if isinstance(hook.get("eventName"), str)
        )
        if any(counts[event] < count for event, count in REQUIRED_HOOK_EVENTS.items()):
            errors.append("CCO hooks are not fully discovered by this Codex installation")
        if any(hook.get("source") != "plugin" for hook in plugin_hooks):
            errors.append("Codex discovered CCO hooks from a non-plugin source")
        discovered_sources: set[str] = set()
        for hook in plugin_hooks:
            source_path = hook.get("sourcePath")
            if not isinstance(source_path, str):
                continue
            try:
                discovered_sources.add(os.path.normcase(str(Path(source_path).resolve())))
            except (OSError, RuntimeError):
                continue
        if len(discovered_sources) > 1:
            errors.append("Codex discovered CCO hooks from inconsistent plugin sources")
        for hook in plugin_hooks:
            source_path = hook.get("sourcePath")
            source_matches = False
            if isinstance(source_path, str):
                try:
                    source = Path(source_path)
                    source_matches = (
                        source.is_absolute()
                        and source.name == "hooks.json"
                        and source.parent.name == "hooks"
                        and is_real_file(source)
                        and plugin_identity_matches(plugin_root, source.parent.parent)
                        and runtime_files_match(plugin_root, source.parent.parent)
                    )
                except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError):
                    source_matches = False
            if not source_matches:
                errors.append("Codex discovered CCO hooks from a different plugin version")
                break
        unready = [
            hook
            for hook in plugin_hooks
            if hook.get("enabled") is not True
            or str(hook.get("trustStatus", "")).casefold() not in {"managed", "trusted"}
        ]
        if unready:
            errors.append(
                "CCO hooks are NOT READY; open /hooks, review every CCO hook, and trust the current definitions"
            )
        if discovery_errors:
            errors.append("Codex reported hook discovery errors")
        if not errors and not unready and plugin_hooks and not discovery_errors:
            print(f"HOOKS READY: {len(plugin_hooks)} current CCO hooks are enabled and trusted.")
    except (OSError, ValueError) as error:
        errors.append(f"CCO hook trust could not be verified: {error}")
    try:
        catalog = (native_loader or load_native_catalog)()
        plan = resolve_route_plan(
            [
                {
                    "assurance": "mechanical",
                    "constraints": {
                        "fixed_effort": None,
                        "fixed_model": None,
                        "source": "automatic",
                    },
                    "node": "doctor_worker",
                    "role": "worker",
                }
            ],
            catalog,
        )
        selected = plan["routes"][0]["selected"]
        print(
            "STATIC ROUTE READY: "
            f"{selected['model']}/{selected['effort']} "
            f"(fallbacks={len(plan['routes'][0]['candidates']) - 1})"
        )
    except (OSError, RoutingCatalogError) as error:
        errors.append(f"static native route is unavailable: {error}")
    for error in errors:
        fail(error)
    return 1 if errors else 0


def install(
    target: Path,
    *,
    check_only: bool,
    upgrade: bool = False,
    profiles: list[str] | None = None,
    workspace: Path | None = None,
) -> int:
    script_dir = Path(__file__).resolve().parent
    templates = script_dir.parent / "agents"
    target = Path(os.path.abspath(target.expanduser()))
    selected_profiles = tuple(dict.fromkeys(profiles or PROFILE_FILENAMES))
    filenames = tuple(PROFILE_FILENAMES[name] for name in selected_profiles)
    workspace = Path.cwd() if workspace is None else workspace

    if target == Path(target.anchor):
        fail("refusing to use a filesystem root as the agent target directory")
        return 1
    if has_reparse_ancestor(target):
        fail(f"target directory has a reparse ancestor: {target}")
        return 1

    for profile, filename in zip(selected_profiles, filenames, strict=True):
        template = templates / filename
        if not is_real_file(template):
            fail(f"shipped template is missing or not a regular file: {template}")
            return 1
        semantic_error = template_error(template, profile)
        if semantic_error is not None:
            fail(semantic_error)
            return 1

    errors = shadow_errors(
        workspace, templates, selected_profiles, target.parent, target
    )
    if target.exists() or is_reparse(target):
        if not target.is_dir() or is_reparse(target):
            errors.append(f"target directory is not a real directory: {target}")

    upgrade_destinations: set[Path] = set()
    upgrade_expectations: dict[Path, tuple[tuple[int, int], str]] = {}
    for filename in filenames:
        template = templates / filename
        destination = target / filename
        if destination.exists() or destination.is_symlink():
            if not is_real_file(destination):
                errors.append(
                    f"destination is not a regular file and will not be replaced: {destination}"
                )
            elif destination.read_bytes() != template.read_bytes():
                if upgrade and file_sha256(destination) in LEGACY_PROFILE_SHA256.get(
                    filename, frozenset()
                ):
                    upgrade_destinations.add(destination)
                    upgrade_expectations[destination] = (
                        file_identity(destination),
                        file_sha256(destination),
                    )
                else:
                    errors.append(
                        f"destination differs from the shipped template and will not be overwritten: {destination}"
                    )
        elif check_only:
            errors.append(f"required installed agent file is missing: {destination}")

    if errors:
        for error in errors:
            fail(error)
        return 1

    if check_only:
        names = ", ".join(selected_profiles)
        print(f"CHECK PASSED: selected agent profiles ({names}) exactly match {templates}.")
        return 0

    target_existed = target.exists()
    changes: list[PreparedChange] = []
    current_destinations: list[Path] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        if has_reparse_ancestor(target) or not target.is_dir():
            raise InstallTransactionError(
                f"target directory changed before installation: {target}"
            )
        for filename in filenames:
            template = templates / filename
            destination = target / filename
            template_bytes = template.read_bytes()
            if destination.exists() and destination not in upgrade_destinations:
                current_destinations.append(destination)
                continue
            kind = "upgrade" if destination in upgrade_destinations else "install"
            expected_identity, expected_sha256 = upgrade_expectations.get(
                destination, (None, None)
            )
            changes.append(
                prepare_change(
                    target,
                    filename,
                    destination,
                    template_bytes,
                    kind,
                    expected_identity,
                    expected_sha256,
                )
            )

        if upgrade:
            for destination in removable_legacy_profiles(target):
                if destination.name in filenames:
                    continue
                changes.append(
                    prepare_change(
                        target,
                        destination.name,
                        destination,
                        destination.read_bytes(),
                        "remove",
                        file_identity(destination),
                        file_sha256(destination),
                    )
                )

        for change in changes:
            if change.kind == "remove":
                if (
                    not is_real_file(change.destination)
                    or change.backup_path is None
                    or not os.path.samefile(change.destination, change.backup_path)
                    or file_identity(change.destination) != change.source_identity
                    or file_sha256(change.destination) != change.source_sha256
                ):
                    raise InstallTransactionError(
                        f"legacy profile changed during upgrade: {change.destination}"
                    )
                change.destination.unlink()
                change.applied = True
                continue

            if change.staged_path is None:
                raise InstallTransactionError("staged profile is unavailable")
            if change.kind == "upgrade":
                if (
                    not is_real_file(change.destination)
                    or change.backup_path is None
                    or not os.path.samefile(change.destination, change.backup_path)
                    or file_identity(change.destination) != change.source_identity
                    or file_sha256(change.destination) != change.source_sha256
                ):
                    raise InstallTransactionError(
                        f"destination changed during upgrade: {change.destination}"
                    )
                change.applied_identity = file_identity(change.staged_path)
                os.replace(change.staged_path, change.destination)
                change.staged_path = None
                change.applied = True
            else:
                try:
                    change.applied_identity = file_identity(change.staged_path)
                    os.link(change.staged_path, change.destination)
                    change.applied = True
                except FileExistsError:
                    if (
                        not is_real_file(change.destination)
                        or change.destination.read_bytes() != change.template_bytes
                    ):
                        raise InstallTransactionError(
                            f"destination changed during installation: {change.destination}"
                        )

        for filename in filenames:
            template = templates / filename
            destination = target / filename
            if (
                not is_real_file(destination)
                or destination.read_bytes() != template.read_bytes()
            ):
                raise InstallTransactionError(
                    f"post-install exactness check failed: {destination}"
                )
        for change in changes:
            if change.kind == "remove" and change.destination.exists():
                raise InstallTransactionError(
                    f"legacy profile was not removed: {change.destination}"
                )
    except (OSError, InstallTransactionError) as error:
        rollback_errors = rollback_prepared(changes)
        if not rollback_errors:
            cleanup_prepared(changes)
        if not target_existed:
            try:
                target.rmdir()
            except OSError:
                pass
        fail(f"profile batch installation failed: {error}")
        for rollback_error in rollback_errors:
            fail(rollback_error)
        return 1

    cleanup_prepared(changes)
    for destination in current_destinations:
        print(f"ALREADY CURRENT: {destination}")
    for change in changes:
        if change.kind == "remove":
            print(f"REMOVED LEGACY: {change.destination}")
            continue
        action = "UPGRADED" if change.kind == "upgrade" else "INSTALLED"
        print(f"{action}: {change.destination}")

    names = ", ".join(selected_profiles)
    print(f"INSTALL PASSED: selected agent profiles ({names}) exactly match {templates}.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.doctor:
            return doctor(args.target_dir, workspace=args.workspace)
        if args.uninstall:
            result = uninstall(args.target_dir, profiles=args.profile)
            for path in cleanup_obsolete_route_cache(args.target_dir):
                print(f"REMOVED OBSOLETE CACHE: {path}")
            return result
        result = install(
            args.target_dir,
            check_only=args.check,
            upgrade=args.upgrade or args.bootstrap,
            profiles=args.profile,
            workspace=args.workspace,
        )
        if result == 0 and args.bootstrap:
            for path in cleanup_obsolete_route_cache(args.target_dir):
                print(f"REMOVED OBSOLETE CACHE: {path}")
        return result
    except (OSError, ValueError) as error:
        fail(f"profile installation could not start: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

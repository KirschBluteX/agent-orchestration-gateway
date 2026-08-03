#!/usr/bin/env python3
"""Install role-bounded Codex profiles without overwriting user-owned files."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import tomllib


PROFILE_FILENAMES = {
    "routine": "codex-cost-orchestrator-routine-worker.toml",
    "complex": "codex-cost-orchestrator-complex-worker.toml",
    "reviewer": "codex-cost-orchestrator-reviewer.toml",
}
PROFILE_AGENT_NAMES = {
    "routine": "cost_orchestrator_routine_worker",
    "complex": "cost_orchestrator_complex_worker",
    "reviewer": "cost_orchestrator_reviewer",
}
LEGACY_PROFILE_SHA256 = {
    "codex-cost-orchestrator-routine-worker.toml": frozenset(
        {
            "2c5b1716312ad7be52eaec26676b52c1a5168cb1d3c602d39a82f907b4afa93d",
            "c9b2187367ab1c167cae594bf589c74adb3b8959c3c4292751117aec820cdc21",
            "bc8ce4bfb9b58b0fac32272f60456b3b327f1d21427dd8e01dff5fd22ac5ceb5",
            "cf9e6b654c6426717ebf738548cf1b5830615b248bddc9acda2f29428b7f62a1",
        }
    ),
    "codex-cost-orchestrator-complex-worker.toml": frozenset(
        {
            "881c3b606c1e9092a96e79ad85bd5b57fef97156f4838a26b18191d18bfee681",
            "e50aadfd85e83841750cea5af5b076e746e7bfddf63cc3f24c27beddc9b8a851",
            "cca439e5c44163be360c665c18fbd9a1641fcd3e28d1488ef60e5ce0b58eb884",
            "a5937769fe00b99480feb5dcc289b862e4529d9605836d1a62d59de01dfcbb90",
        }
    ),
    "codex-cost-orchestrator-reviewer.toml": frozenset(
        {
            "015c8fe9da8b92a24f021120e71f6b0e3e0bfb10244ba529f7ff8981ce179e00",
            "395a35427315e71b37227a79ac38889aa9f102eebf7dbdb78d9ae86b510ee9bc",
            "1df307612992239960b4dececd79f6f1935b42662983204d350cb7ed519528b8",
            "309381c4bb2c009d0f062677b00b6f74ecf788b9eacd018dd344e3f4b0bb20f8",
        }
    ),
}


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


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        staged_path = stage_bytes(
            target, template_bytes, ".cost-orchestrator-agent-"
        )
        if kind == "upgrade":
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
    if profile in {"routine", "complex"} and (
        "model" in value or "model_reasoning_effort" in value
    ):
        return f"worker profile must not pin model or model_reasoning_effort: {path}"
    if profile == "reviewer" and (
        value.get("model") != "gpt-5.6-sol"
        or value.get("model_reasoning_effort") != "high"
        or value.get("sandbox_mode") != "read-only"
    ):
        return f"reviewer profile must retain Sol High and read-only defaults: {path}"
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
    folders: list[Path] = []
    current = workspace
    while True:
        folders.append(current / ".codex")
        if (current / ".git").exists() or current == Path(current.anchor):
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
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify exact installed copies without creating files.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Atomically replace only byte-identical profiles from known prior CCO releases.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=tuple(PROFILE_FILENAMES),
        help="Limit installation or checking to a required role; repeat as needed.",
    )
    return parser.parse_args()


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
        if is_reparse(target) or not target.is_dir():
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

        for change in changes:
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
    except (OSError, InstallTransactionError) as error:
        rollback_errors = rollback_prepared(changes)
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
        action = "UPGRADED" if change.kind == "upgrade" else "INSTALLED"
        print(f"{action}: {change.destination}")

    names = ", ".join(selected_profiles)
    print(f"INSTALL PASSED: selected agent profiles ({names}) exactly match {templates}.")
    return 0


def main() -> int:
    args = parse_args()
    if args.check and args.upgrade:
        fail("--check and --upgrade are mutually exclusive")
        return 1
    try:
        return install(
            args.target_dir,
            check_only=args.check,
            upgrade=args.upgrade,
            profiles=args.profile,
            workspace=args.workspace,
        )
    except (OSError, ValueError) as error:
        fail(f"profile installation could not start: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

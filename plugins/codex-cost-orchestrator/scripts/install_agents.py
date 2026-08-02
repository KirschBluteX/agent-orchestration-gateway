#!/usr/bin/env python3
"""Install role-bounded Codex agent profiles without overwriting user files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
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


def default_target() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "agents"


def is_real_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


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
    if features.get("multi_agent") is not False or features.get("multi_agent_v2") is not False:
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
    if resolved_home not in config_folders:
        config_folders.append(resolved_home)

    for config_folder in config_folders:
        agents_dir = config_folder / "agents"
        if agents_dir.is_dir() and not agents_dir.is_symlink():
            for candidate in agents_dir.rglob("*.toml"):
                if not is_real_file(candidate):
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

    errors = shadow_errors(workspace, templates, selected_profiles, target.parent)
    if target.exists() or target.is_symlink():
        if not target.is_dir() or target.is_symlink():
            errors.append(f"target directory is not a real directory: {target}")

    for filename in filenames:
        template = templates / filename
        destination = target / filename
        if destination.exists() or destination.is_symlink():
            if not is_real_file(destination):
                errors.append(
                    f"destination is not a regular file and will not be replaced: {destination}"
                )
            elif destination.read_bytes() != template.read_bytes():
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

    target.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        template = templates / filename
        destination = target / filename
        if destination.exists():
            print(f"ALREADY CURRENT: {destination}")
            continue

        staged_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".cost-orchestrator-agent-", dir=target, delete=False
            ) as staged:
                staged.write(template.read_bytes())
                staged.flush()
                os.fsync(staged.fileno())
                staged_path = Path(staged.name)
            try:
                os.link(staged_path, destination)
            except FileExistsError:
                if not is_real_file(destination) or destination.read_bytes() != template.read_bytes():
                    fail(f"destination changed during installation: {destination}")
                    return 1
            print(f"INSTALLED: {destination}")
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    for filename in filenames:
        template = templates / filename
        destination = target / filename
        if not is_real_file(destination) or destination.read_bytes() != template.read_bytes():
            fail(f"post-install exactness check failed: {destination}")
            return 1

    names = ", ".join(selected_profiles)
    print(f"INSTALL PASSED: selected agent profiles ({names}) exactly match {templates}.")
    return 0


def main() -> int:
    args = parse_args()
    return install(
        args.target_dir,
        check_only=args.check,
        profiles=args.profile,
        workspace=args.workspace,
    )


if __name__ == "__main__":
    raise SystemExit(main())

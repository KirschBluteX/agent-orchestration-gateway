#!/usr/bin/env python3
"""Install role-pinned Codex agent profiles without overwriting user files."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile


PROFILE_FILENAMES = {
    "routine": "codex-cost-orchestrator-routine-worker.toml",
    "complex": "codex-cost-orchestrator-complex-worker.toml",
    "reviewer": "codex-cost-orchestrator-reviewer.toml",
}


def default_target() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "agents"


def is_real_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Codex Cost Orchestrator custom-agent profiles."
    )
    parser.add_argument("--target-dir", type=Path, default=default_target())
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
) -> int:
    script_dir = Path(__file__).resolve().parent
    templates = script_dir.parent / "agents"
    target = Path(os.path.abspath(target.expanduser()))
    selected_profiles = tuple(dict.fromkeys(profiles or PROFILE_FILENAMES))
    filenames = tuple(PROFILE_FILENAMES[name] for name in selected_profiles)

    if target == Path(target.anchor):
        fail("refusing to use a filesystem root as the agent target directory")
        return 1

    for filename in filenames:
        template = templates / filename
        if not is_real_file(template):
            fail(f"shipped template is missing or not a regular file: {template}")
            return 1

    errors: list[str] = []
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
    return install(args.target_dir, check_only=args.check, profiles=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())

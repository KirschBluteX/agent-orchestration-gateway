#!/usr/bin/env python3
"""Capture and verify a Git workspace delta without mutating Git state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any

from protocol_hash import ProtocolHashError, require_repository_path


SCHEMA = "cco.workspace-state.v1"
CASE_INSENSITIVE_HOST = os.path.normcase("A") == os.path.normcase("a")


class StateError(Exception):
    pass


def git(repo: Path, *args: str, allow_failure: bool = False) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode and not allow_failure:
        raise StateError("Git repository inspection failed")
    return result.stdout


def decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape").replace("\\", "/")


def repository_root(repo: Path) -> Path:
    candidate = repo.expanduser().resolve()
    output = git(candidate, "rev-parse", "--show-toplevel")
    if not output:
        raise StateError("--repo must identify a Git work tree")
    return Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(root: Path, relative: str) -> dict[str, Any]:
    path = root / Path(relative)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        return {
            "kind": "symlink",
            "mode": mode,
            "sha256": hashlib.sha256(os.fsencode(target)).hexdigest(),
        }
    if path.is_file():
        return {
            "kind": "file",
            "mode": mode,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        return {"kind": "directory", "mode": mode}
    return {"kind": "special", "mode": mode}


def status_entries(root: Path) -> dict[str, dict[str, Any]]:
    raw = git(
        root,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    fields = raw.split(b"\0")
    entries: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        marker = record[:1]
        if marker == b"1":
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise StateError("unexpected Git status record")
            path = decode_path(parts[8])
            entries[path] = {
                "status": decode_path(b" ".join(parts[:8])),
                "fingerprint": fingerprint(root, path),
            }
        elif marker == b"2":
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index >= len(fields):
                raise StateError("unexpected Git rename record")
            path = decode_path(parts[9])
            original = decode_path(fields[index])
            index += 1
            status = decode_path(b" ".join(parts[:9]))
            entries[path] = {
                "status": status,
                "rename_role": "destination",
                "other_path": original,
                "fingerprint": fingerprint(root, path),
            }
            entries[original] = {
                "status": status,
                "rename_role": "source",
                "other_path": path,
                "fingerprint": fingerprint(root, original),
            }
        elif marker == b"u":
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise StateError("unexpected Git unmerged record")
            path = decode_path(parts[10])
            entries[path] = {
                "status": decode_path(b" ".join(parts[:10])),
                "fingerprint": fingerprint(root, path),
            }
        elif marker in (b"?", b"!"):
            if len(record) < 3:
                raise StateError("unexpected Git path record")
            path = decode_path(record[2:])
            entries[path] = {
                "status": marker.decode("ascii"),
                "fingerprint": fingerprint(root, path),
            }
        else:
            raise StateError("unsupported Git status record")
    return dict(sorted(entries.items()))


def index_digest(root: Path) -> str | None:
    raw_path = git(root, "rev-parse", "--git-path", "index").rstrip(b"\r\n")
    if not raw_path:
        raise StateError("Git index path is unavailable")
    index_path = Path(os.fsdecode(raw_path))
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        return sha256_file(index_path)
    except FileNotFoundError:
        return None


def head_oid(root: Path) -> str | None:
    output = git(root, "rev-parse", "--verify", "HEAD", allow_failure=True).strip()
    return output.decode("ascii") if output else None


def state_payload(root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "repo_root": str(root),
        "head": head_oid(root),
        "index_sha256": index_digest(root),
        "entries": status_entries(root),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    payload["state_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def validate_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise StateError("baseline uses an unsupported schema")
    state_id = value.get("state_id")
    unsigned = {key: item for key, item in value.items() if key != "state_id"}
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if not isinstance(state_id, str) or state_id != expected:
        raise StateError("baseline state identifier does not match its content")
    if not isinstance(value.get("entries"), dict):
        raise StateError("baseline entries are invalid")
    return value


def normalize_allow(value: str) -> str:
    is_prefix = value.endswith("/")
    candidate = value[:-1] if is_prefix else value
    try:
        normalized = require_repository_path(candidate, "lease path")
    except ProtocolHashError as error:
        raise StateError(f"invalid lease path: {value}") from error
    if normalized.split("/", 1)[0].casefold() == ".git":
        raise StateError(f"invalid lease path: {value}")
    return normalized + "/" if is_prefix else normalized


def is_allowed(path: str, allowed: list[str]) -> bool:
    candidate = path.casefold() if CASE_INSENSITIVE_HOST else path
    return any(
        candidate.startswith(item.casefold() if CASE_INSENSITIVE_HOST else item)
        if item.endswith("/")
        else candidate == (item.casefold() if CASE_INSENSITIVE_HOST else item)
        for item in allowed
    )


def verify(root: Path, baseline: dict[str, Any], allowed: list[str]) -> tuple[int, dict[str, Any]]:
    current = state_payload(root)
    baseline_entries = baseline["entries"]
    current_entries = current["entries"]
    changed = sorted(
        path
        for path in set(baseline_entries) | set(current_entries)
        if baseline_entries.get(path) != current_entries.get(path)
    )
    violations: list[str] = []
    if baseline.get("repo_root") != current["repo_root"]:
        violations.append("repository_changed")
    if baseline.get("head") != current["head"]:
        violations.append("head_changed")
    if baseline.get("index_sha256") != current["index_sha256"]:
        violations.append("index_changed")
    violations.extend(
        f"outside_lease:{path}" for path in changed if not is_allowed(path, allowed)
    )
    result = {
        "schema": "cco.workspace-verification.v1",
        "baseline_state": baseline["state_id"],
        "current_state": current["state_id"],
        "allowed_paths": allowed,
        "changed_paths": changed,
        "violations": violations,
        "verdict": "pass" if not violations else "violation",
    }
    return (0 if not violations else 1), result


def write_snapshot(root: Path, output: Path, serialized: str) -> None:
    requested = Path(os.path.abspath(output.expanduser()))
    if requested.is_symlink():
        raise StateError("baseline output must not be a symlink")
    resolved = requested.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise StateError("baseline output must be outside the repository")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists() and not resolved.is_file():
        raise StateError("baseline output must be a regular file")
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=resolved.parent,
            prefix=".cco-state-",
            delete=False,
        ) as staged:
            staged.write(serialized)
            staged.write("\n")
            staged.flush()
            os.fsync(staged.fileno())
            staged_path = Path(staged.name)
        os.replace(staged_path, resolved)
        staged_path = None
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Capture and verify baseline-relative Codex worker deltas."
    )
    subparsers = root.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--repo", type=Path, default=Path.cwd())
    capture.add_argument(
        "--output",
        type=Path,
        help="Write UTF-8 JSON atomically outside the repository instead of stdout.",
    )
    check = subparsers.add_parser("verify")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    check.add_argument("--baseline", type=Path, required=True)
    check.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Allow one exact path or trailing-slash directory prefix; omit for a read-only check.",
    )
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        root = repository_root(args.repo)
        if args.command == "capture":
            result = state_payload(root)
            code = 0
        else:
            baseline = validate_snapshot(
                json.loads(args.baseline.read_text(encoding="utf-8"))
            )
            allowed = sorted({normalize_allow(item) for item in args.allow})
            code, result = verify(root, baseline, allowed)
    except (OSError, json.JSONDecodeError, StateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
    if args.command == "capture" and args.output is not None:
        try:
            write_snapshot(root, args.output, serialized)
        except (OSError, StateError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
    else:
        print(serialized)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

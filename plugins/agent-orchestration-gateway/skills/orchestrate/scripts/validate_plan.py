#!/usr/bin/env python3
"""Validate and normalize an AOG module plan without touching repository state."""

from __future__ import annotations

import heapq
import json
import re
import sys
import unicodedata
from typing import Any

MAX_INPUT_BYTES = 256 * 1024
MAX_MODULES = 8
MAX_ITEMS = 32
MAX_TEXT_CHARS = 4096
MAX_PATH_CHARS = 1024

MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
ACCEPTANCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
GIT_OID_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")
WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(
        f"com{suffix}"
        for suffix in (
            *range(1, 10),
            "\N{SUPERSCRIPT ONE}",
            "\N{SUPERSCRIPT TWO}",
            "\N{SUPERSCRIPT THREE}",
        )
    ),
    *(
        f"lpt{suffix}"
        for suffix in (
            *range(1, 10),
            "\N{SUPERSCRIPT ONE}",
            "\N{SUPERSCRIPT TWO}",
            "\N{SUPERSCRIPT THREE}",
        )
    ),
}


class ValidationError(ValueError):
    """The supplied plan is not safe or structurally valid."""


def _label(value: str) -> str:
    rendered = ascii(value)
    return rendered if len(rendered) <= 80 else f"{rendered[:77]}..."


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-standard JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {_label(key)}")
        result[key] = value
    return result


def parse_plan(raw: bytes) -> dict[str, Any]:
    """Parse a bounded UTF-8 JSON object and reject ambiguous JSON constructs."""
    if len(raw) > MAX_INPUT_BYTES:
        raise ValidationError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("input must be UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValidationError(f"input must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("plan must be a JSON object")
    return payload


def _reject_unknown(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{path} has unknown field: {_label(unknown[0])}")


def _list(
    value: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_ITEMS,
) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValidationError(f"{path}.{key} must be an array")
    if not minimum <= len(result) <= maximum:
        raise ValidationError(
            f"{path}.{key} must contain between {minimum} and {maximum} items"
        )
    return result


def _text(
    value: dict[str, Any],
    key: str,
    path: str,
    *,
    maximum: int = MAX_TEXT_CHARS,
) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValidationError(f"{path}.{key} must be a non-empty string")
    result = result.strip()
    if _has_surrogate(result):
        raise ValidationError(f"{path}.{key} contains an invalid Unicode scalar")
    if len(result) > maximum:
        raise ValidationError(f"{path}.{key} exceeds {maximum} characters")
    return result


def _scope_path(raw: Any, kind: str, path: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValidationError(f"{path}.path must be a trimmed non-empty string")
    if len(raw) > MAX_PATH_CHARS:
        raise ValidationError(f"{path}.path exceeds {MAX_PATH_CHARS} characters")
    if _has_surrogate(raw):
        raise ValidationError(f"{path}.path contains an invalid Unicode scalar")
    if raw == ".":
        if kind == "exact":
            raise ValidationError(
                f"{path}.path cannot use the repository root as exact"
            )
        return raw
    if (
        raw.startswith("/")
        or DRIVE_RE.match(raw)
        or "\\" in raw
        or raw.endswith("/")
        or "//" in raw
    ):
        raise ValidationError(
            f"{path}.path must be a normalized repository-relative path"
        )

    parts = raw.split("/")
    forbidden = set('<>:"\\|?*')
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValidationError(f"{path}.path contains an unsafe component")
        if part != part.rstrip(" ."):
            raise ValidationError(
                f"{path}.path contains a platform-ambiguous component"
            )
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            or character in forbidden
            for character in part
        ):
            raise ValidationError(f"{path}.path contains a forbidden character")
        stem = part.split(".", 1)[0].rstrip(" ").casefold()
        if stem in WINDOWS_RESERVED or part.casefold() == ".git":
            raise ValidationError(f"{path}.path contains a reserved component")
    return "/".join(parts)


def _scope_contains(scope: dict[str, str], candidate: dict[str, str]) -> bool:
    left = unicodedata.normalize("NFC", scope["path"]).casefold()
    right = unicodedata.normalize("NFC", candidate["path"]).casefold()
    if scope["kind"] == "exact":
        return candidate["kind"] == "exact" and left == right
    return left == "." or right == left or right.startswith(f"{left}/")


def _scopes_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    return _scope_contains(left, right) or _scope_contains(right, left)


def _validate_scope(raw: Any, path: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{path} must be an object")
    _reject_unknown(raw, {"kind", "path"}, path)
    kind = raw.get("kind")
    if kind not in {"exact", "prefix"}:
        raise ValidationError(f"{path}.kind must be exact or prefix")
    return {"kind": kind, "path": _scope_path(raw.get("path"), kind, path)}


def _validate_acceptance(raw: Any, path: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{path} must be an object")
    _reject_unknown(raw, {"id", "criterion"}, path)
    identifier = _text(raw, "id", path, maximum=64)
    if ACCEPTANCE_ID_RE.fullmatch(identifier) is None:
        raise ValidationError(f"{path}.id is not a valid acceptance id")
    return {
        "id": identifier,
        "criterion": _text(raw, "criterion", path),
    }


def _validate_module(raw: Any, index: int) -> dict[str, Any]:
    path = f"modules[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{path} must be an object")
    _reject_unknown(
        raw,
        {"id", "type", "objective", "depends_on", "writes", "acceptance"},
        path,
    )
    identifier = _text(raw, "id", path, maximum=64)
    if MODULE_ID_RE.fullmatch(identifier) is None:
        raise ValidationError(f"{path}.id is not a valid module id")
    kind = raw.get("type")
    if kind not in {"work", "integration"}:
        raise ValidationError(f"{path}.type must be work or integration")

    dependencies: list[str] = []
    seen_dependencies: set[str] = set()
    for dependency in _list(raw, "depends_on", path, maximum=MAX_MODULES - 1):
        if (
            not isinstance(dependency, str)
            or MODULE_ID_RE.fullmatch(dependency) is None
        ):
            raise ValidationError(f"{path}.depends_on contains an invalid module id")
        if dependency in seen_dependencies:
            raise ValidationError(f"{path}.depends_on contains a duplicate module id")
        seen_dependencies.add(dependency)
        dependencies.append(dependency)
    if kind == "integration" and len(dependencies) < 2:
        raise ValidationError(f"{path} integration must depend on at least two modules")

    scopes = [
        _validate_scope(scope, f"{path}.writes[{scope_index}]")
        for scope_index, scope in enumerate(_list(raw, "writes", path))
    ]
    for left_index, left in enumerate(scopes):
        for right in scopes[left_index + 1 :]:
            if _scopes_overlap(left, right):
                raise ValidationError(f"{path} write scopes overlap within module")

    criteria = [
        _validate_acceptance(item, f"{path}.acceptance[{item_index}]")
        for item_index, item in enumerate(_list(raw, "acceptance", path, minimum=1))
    ]
    return {
        "id": identifier,
        "type": kind,
        "objective": _text(raw, "objective", path),
        "depends_on": sorted(dependencies),
        "writes": sorted(
            scopes, key=lambda scope: (scope["path"].casefold(), scope["kind"])
        ),
        "acceptance": sorted(criteria, key=lambda item: item["id"].casefold()),
    }


def _topological_ids(modules: dict[str, dict[str, Any]]) -> list[str]:
    indegree = {
        identifier: len(module["depends_on"]) for identifier, module in modules.items()
    }
    children = {identifier: [] for identifier in modules}
    for identifier, module in modules.items():
        for dependency in module["depends_on"]:
            if dependency not in modules:
                raise ValidationError(
                    f"module {identifier} depends on unknown module {dependency}"
                )
            children[dependency].append(identifier)

    ready = [identifier for identifier, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        identifier = heapq.heappop(ready)
        ordered.append(identifier)
        for child in sorted(children[identifier]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(ordered) != len(modules):
        raise ValidationError("module dependencies contain a cycle")
    return ordered


def validate_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic plan or raise ValidationError."""
    if not isinstance(payload, dict):
        raise ValidationError("plan must be a JSON object")
    _reject_unknown(payload, {"goal", "base_sha", "modules"}, "plan")
    goal = _text(payload, "goal", "plan")

    base_sha = payload.get("base_sha")
    if base_sha is not None:
        if not isinstance(base_sha, str) or GIT_OID_RE.fullmatch(base_sha) is None:
            raise ValidationError(
                "plan.base_sha must be a 40- or 64-character Git object id or null"
            )
        base_sha = base_sha.lower()

    raw_modules = _list(payload, "modules", "plan", minimum=1, maximum=MAX_MODULES)
    modules: dict[str, dict[str, Any]] = {}
    acceptance_ids: set[str] = set()
    all_scopes: list[tuple[str, dict[str, str]]] = []
    for index, raw_module in enumerate(raw_modules):
        module = _validate_module(raw_module, index)
        identifier = module["id"]
        if identifier in modules:
            raise ValidationError(f"duplicate module id: {identifier}")
        modules[identifier] = module
        for criterion in module["acceptance"]:
            key = criterion["id"].casefold()
            if key in acceptance_ids:
                raise ValidationError(f"duplicate acceptance id: {criterion['id']}")
            acceptance_ids.add(key)
        for scope in module["writes"]:
            for owner, existing in all_scopes:
                if _scopes_overlap(existing, scope):
                    raise ValidationError(
                        f"write scopes overlap across modules {owner} and {identifier}"
                    )
            all_scopes.append((identifier, scope))

    ordered_ids = _topological_ids(modules)
    if base_sha is None:
        if any(module["type"] == "integration" for module in modules.values()):
            raise ValidationError("a non-Git plan cannot contain an integration module")
        writers = [module for module in modules.values() if module["writes"]]
        if writers and len(modules) != 1:
            raise ValidationError(
                "a non-Git plan with writes must contain a single module"
            )

    return {
        "goal": goal,
        "base_sha": base_sha,
        "modules": [modules[identifier] for identifier in ordered_ids],
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        normalized = validate_plan(parse_plan(raw))
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json.dump(
        normalized, sys.stdout, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

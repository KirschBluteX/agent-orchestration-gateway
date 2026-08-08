#!/usr/bin/env python3
"""Canonical JSON and repository-scope helpers for compact cco.v9 messages."""

from __future__ import annotations

import json
import ntpath
import re
from typing import Any
import unicodedata


MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_NESTING_LEVELS = 64
SCOPE_KINDS = frozenset({"exact", "prefix"})
WIN32_DEVICE_BASENAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)
WIN32_FORBIDDEN_PATH_CHARACTERS = frozenset('<>"|?*')


class ProtocolHashError(ValueError):
    """A value has no single safe canonical encoding or path spelling."""


def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if not key.isascii():
            raise ProtocolHashError("object keys must be ASCII")
        if key in value:
            raise ProtocolHashError(f"duplicate object key: {key}")
        value[key] = item
    return value


def reject_float(_value: str) -> None:
    raise ProtocolHashError("floating-point numbers are not supported")


def reject_constant(_value: str) -> None:
    raise ProtocolHashError("non-JSON numeric constants are not supported")


def parse_safe_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > len(str(MAX_SAFE_INTEGER)):
        raise ProtocolHashError("integer is outside the safe range")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProtocolHashError("integer is outside the safe range") from error
    if not -MAX_SAFE_INTEGER <= parsed <= MAX_SAFE_INTEGER:
        raise ProtocolHashError("integer is outside the safe range")
    return parsed


def validate_structure(value: Any, depth: int = 0) -> None:
    if depth > MAX_NESTING_LEVELS:
        raise ProtocolHashError(f"nesting exceeds {MAX_NESTING_LEVELS} levels")
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ProtocolHashError("strings must use NFC normalization")
    elif type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ProtocolHashError("integer is outside the safe range")
    elif value is None or type(value) is bool:
        return
    elif type(value) is list:
        for item in value:
            validate_structure(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise ProtocolHashError("object keys must be ASCII")
            validate_structure(key, depth + 1)
            validate_structure(item, depth + 1)
    else:
        raise ProtocolHashError("value is not supported by canonical JSON")


def canonical_bytes(value: Any) -> bytes:
    if type(value) is not dict:
        raise ProtocolHashError("input must be a JSON object")
    validate_structure(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def parse_canonical_json_object(value: Any, label: str) -> dict[str, Any]:
    """Parse one exact canonical object without duplicate-key ambiguity."""

    if type(value) is not str:
        raise ProtocolHashError(f"{label} must be JSON text")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=parse_safe_integer,
        )
    except (json.JSONDecodeError, ProtocolHashError) as error:
        raise ProtocolHashError(f"{label} is not safe JSON") from error
    if type(parsed) is not dict or canonical_bytes(parsed).decode("utf-8") != value:
        raise ProtocolHashError(f"{label} must use exact canonical JSON")
    return parsed


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise ProtocolHashError(f"{label} must be non-empty text")
    return value


def _ambiguous_win32_segment(segment: str) -> bool:
    basename = segment.split(".", 1)[0]
    return (
        segment.endswith((" ", "."))
        or any(character in WIN32_FORBIDDEN_PATH_CHARACTERS for character in segment)
        or WIN32_DEVICE_BASENAME.fullmatch(basename) is not None
    )


def require_repository_path(value: Any, label: str) -> str:
    path = _text(value, label)
    segments = path.split("/")
    if (
        unicodedata.normalize("NFC", path) != path
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or ":" in path
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(segment.casefold() == ".git" for segment in segments)
        or any(_ambiguous_win32_segment(segment) for segment in segments)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ProtocolHashError(f"{label} must be a canonical repository-relative path")
    return path


def require_repository_scope(value: Any, label: str) -> dict[str, str]:
    if type(value) is not dict or set(value) != {"kind", "path"}:
        raise ProtocolHashError(f"{label} must contain kind and path")
    kind = value["kind"]
    if kind not in SCOPE_KINDS:
        raise ProtocolHashError(f"{label}.kind must be exact or prefix")
    return {"kind": kind, "path": require_repository_path(value["path"], f"{label}.path")}


def parse_repository_scope_text(value: Any, label: str) -> dict[str, str]:
    text = _text(value, label)
    kind, separator, path = text.partition(":")
    if not separator:
        raise ProtocolHashError(f"{label} must use exact:<path> or prefix:<path>")
    return require_repository_scope({"kind": kind, "path": path}, label)


def repository_scopes_overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    first = require_repository_scope(left, "left scope")
    second = require_repository_scope(right, "right scope")
    left_path = ntpath.normcase(first["path"]).replace("\\", "/")
    right_path = ntpath.normcase(second["path"]).replace("\\", "/")
    return (
        left_path == right_path
        or (first["kind"] == "prefix" and right_path.startswith(left_path + "/"))
        or (second["kind"] == "prefix" and left_path.startswith(right_path + "/"))
    )

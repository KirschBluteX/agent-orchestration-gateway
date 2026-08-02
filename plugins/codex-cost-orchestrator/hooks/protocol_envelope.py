"""Strict parser shared by the read-only CCO hook adapters."""

from __future__ import annotations

import json
import re
from typing import Any, BinaryIO


FIELD_LINE = re.compile(r"^([A-Z][A-Z0-9_]*):(?:\s*(.*))?$")
MAX_ENVELOPE_BYTES = 1024 * 1024


class EnvelopeError(Exception):
    """A protocol envelope is incomplete or structurally ambiguous."""

    def __init__(self, *issues: str) -> None:
        self.issues = tuple(dict.fromkeys(issues)) or ("ENVELOPE",)
        super().__init__(", ".join(self.issues))


def split_wire_lines(value: str) -> list[str]:
    """Split protocol structure on CR/LF only, never Unicode line separators."""
    return re.split(r"\r\n|\r|\n", value)


def load_utf8_json(stream: BinaryIO) -> Any:
    """Decode the hook transport deterministically instead of using the host code page."""
    return json.loads(stream.read().decode("utf-8"))


def parse_envelope(
    message: Any,
    *,
    header: str,
    required: tuple[str, ...],
    list_fields: frozenset[str],
    allow_text_fence: bool = False,
) -> dict[str, str]:
    """Parse one exact field envelope and reject every unowned text line."""
    if not isinstance(message, str):
        raise EnvelopeError(header)
    try:
        if len(message.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise EnvelopeError("ENVELOPE")
    except UnicodeEncodeError as error:
        raise EnvelopeError("ENVELOPE") from error
    stripped = message.strip("\r\n")
    if not stripped:
        raise EnvelopeError(header)
    lines = split_wire_lines(stripped)

    if lines[0].strip(" \t").startswith("```"):
        if (
            not allow_text_fence
            or lines[0].strip(" \t") not in {"```", "```text"}
            or len(lines) < 3
            or lines[-1].strip(" \t") != "```"
            or any(line.strip(" \t").startswith("```") for line in lines[1:-1])
        ):
            raise EnvelopeError(header)
        lines = lines[1:-1]
    elif any(line.strip(" \t").startswith("```") for line in lines):
        raise EnvelopeError(header)

    if not lines or lines[0].strip(" \t") != header:
        raise EnvelopeError(header)

    fields: dict[str, str] = {}
    duplicates: set[str] = set()
    outside_field = False
    current: str | None = None
    content: list[str] = []

    def finish() -> None:
        nonlocal current, content
        if current is None:
            return
        value = "\n".join(content).strip(" \t")
        if current in fields:
            duplicates.add(current)
        else:
            fields[current] = value
        current = None
        content = []

    for raw_line in lines[1:]:
        line = raw_line.strip(" \t")
        if not line:
            continue
        if line.startswith("CCO_"):
            outside_field = True
            continue
        match = FIELD_LINE.fullmatch(line)
        if match:
            finish()
            current = match.group(1)
            inline = (match.group(2) or "").strip(" \t")
            content = [inline] if inline else []
        elif current is None:
            outside_field = True
        else:
            content.append(line)
    finish()

    required_set = set(required)
    issues: list[str] = []
    if outside_field:
        issues.append("ENVELOPE")
    issues.extend(name for name in required if not fields.get(name) or name in duplicates)
    issues.extend(sorted(set(fields) - required_set))
    for name in required_set - list_fields:
        if "\n" in fields.get(name, ""):
            issues.append(name)
    for name in list_fields:
        value = fields.get(name, "")
        lines_in_value = value.split("\n")
        if not lines_in_value or any(
            not item.startswith("- ") or len(item) <= 2 for item in lines_in_value
        ):
            issues.append(name)
    if issues:
        raise EnvelopeError(*issues)
    return fields

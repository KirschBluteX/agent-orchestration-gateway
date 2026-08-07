#!/usr/bin/env python3
"""Bounded readers for plain and zstd-compressed Codex rollout JSONL files."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO


MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 1_000_000


class RolloutError(ValueError):
    """A rollout cannot be read safely with the available local runtime."""


def is_rollout_path(path: Path) -> bool:
    return path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")


@contextmanager
def open_rollout(path: Path) -> Iterator[TextIO]:
    """Open one supported rollout without introducing a mandatory dependency."""

    if path.name.endswith(".jsonl"):
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                yield stream
        except OSError as error:
            raise RolloutError("rollout is unavailable") from error
        return
    if not path.name.endswith(".jsonl.zst"):
        raise RolloutError("rollout has an unsupported suffix")
    try:
        from compression import zstd
    except ImportError:  # Python 3.11-3.13 may provide the optional zstandard wheel.
        try:
            import zstandard
        except ImportError as error:
            raise RolloutError(
                "compressed rollout requires Python 3.14+ or the optional zstandard package"
            ) from error
        try:
            with path.open("rb") as source:
                with zstandard.ZstdDecompressor().stream_reader(source) as reader:
                    with io.TextIOWrapper(reader, encoding="utf-8", newline="") as stream:
                        yield stream
        except (OSError, zstandard.ZstdError) as error:
            raise RolloutError("compressed rollout is unavailable or invalid") from error
        return
    try:
        with zstd.open(path, "rt", encoding="utf-8", newline="") as stream:
            yield stream
    except (OSError, ValueError) as error:
        raise RolloutError("compressed rollout is unavailable or invalid") from error


def iter_records(path: Path) -> Iterator[Mapping[str, Any]]:
    """Yield bounded JSON objects and reject ambiguous or oversized records."""

    with open_rollout(path) as stream:
        for index, line in enumerate(stream):
            if index >= MAX_RECORDS:
                raise RolloutError("rollout exceeds the record limit")
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise RolloutError("rollout record exceeds the size limit")
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise RolloutError("rollout contains invalid JSON") from error
            if not isinstance(value, Mapping):
                raise RolloutError("rollout contains a non-object record")
            yield value


def first_record(path: Path) -> Mapping[str, Any]:
    try:
        return next(iter_records(path))
    except StopIteration as error:
        raise RolloutError("rollout is empty") from error


def matching_rollouts(sessions_root: Path, thread_id: str) -> list[Path]:
    matches = [
        *sessions_root.rglob(f"rollout-*-{thread_id}.jsonl"),
        *sessions_root.rglob(f"rollout-*-{thread_id}.jsonl.zst"),
    ]
    return sorted(set(matches), key=str)

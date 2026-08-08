#!/usr/bin/env python3
"""Bounded readers for plain and zstd-compressed Codex rollout JSONL files."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping


MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 1_000_000
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class RolloutError(ValueError):
    """A rollout cannot be read safely with the available local runtime."""


def is_rollout_path(path: Path) -> bool:
    return path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")


@contextmanager
def open_rollout(path: Path) -> Iterator[BinaryIO]:
    """Open one rollout through the stdlib or the declared pre-3.14 dependency."""

    if path.name.endswith(".jsonl"):
        try:
            with path.open("rb") as stream:
                yield stream
        except OSError as error:
            raise RolloutError("rollout is unavailable") from error
        return
    if not path.name.endswith(".jsonl.zst"):
        raise RolloutError("rollout has an unsupported suffix")
    try:
        from compression import zstd
    except ImportError:  # Python 3.11-3.13 use the declared zstandard wheel.
        try:
            import zstandard
        except ImportError as error:
            raise RolloutError(
                "compressed rollout requires Python 3.14+ or the zstandard package"
            ) from error
        try:
            with path.open("rb") as source:
                with zstandard.ZstdDecompressor().stream_reader(source) as stream:
                    yield stream
        except (OSError, zstandard.ZstdError) as error:
            raise RolloutError("compressed rollout is unavailable or invalid") from error
        return
    try:
        with zstd.open(path, "rb") as stream:
            yield stream
    except (OSError, ValueError, zstd.ZstdError) as error:
        raise RolloutError("compressed rollout is unavailable or invalid") from error


def _parse_record(line: bytes) -> Mapping[str, Any] | None:
    if len(line) > MAX_LINE_BYTES:
        raise RolloutError("rollout record exceeds the size limit")
    try:
        decoded = line.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RolloutError("rollout contains invalid UTF-8") from error
    if not decoded.strip():
        return None
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError as error:
        raise RolloutError("rollout contains invalid JSON") from error
    if not isinstance(value, Mapping):
        raise RolloutError("rollout contains a non-object record")
    return value


def iter_records(path: Path) -> Iterator[Mapping[str, Any]]:
    """Yield bounded JSON objects and reject ambiguous or oversized records."""

    with open_rollout(path) as stream:
        total_bytes = 0
        for _index in range(MAX_RECORDS):
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            total_bytes += len(line)
            if total_bytes > MAX_DECOMPRESSED_BYTES:
                raise RolloutError("rollout exceeds the decompressed byte limit")
            value = _parse_record(line)
            if value is not None:
                yield value
        if stream.readline(1):
            raise RolloutError("rollout exceeds the record limit")


def _parse_record_lines(data: bytes) -> Iterator[Mapping[str, Any]]:
    with io.BytesIO(data) as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            value = _parse_record(line)
            if value is not None:
                yield value


def iter_tail_records(
    path: Path,
    *,
    max_bytes: int,
) -> Iterator[Mapping[str, Any]]:
    """Yield only a bounded terminal tail of a rollout.

    Plain JSONL files are read from the end without scanning their history.
    Compressed streams cannot seek, so they are consumed with the same bounded
    decompression and rolling-tail limits.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise RolloutError("rollout tail limit is invalid")
    if path.name.endswith(".jsonl"):
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                start = max(0, size - max_bytes)
                previous = b""
                if start:
                    stream.seek(start - 1)
                    previous = stream.read(1)
                stream.seek(start)
                data = stream.read(size - start)
        except OSError as error:
            raise RolloutError("rollout is unavailable") from error
        if start and previous != b"\n":
            boundary = data.find(b"\n")
            if boundary < 0:
                raise RolloutError("rollout terminal tail is incomplete")
            data = data[boundary + 1 :]
        if len(data) > max_bytes:
            raise RolloutError("rollout terminal tail exceeds the size limit")
        yield from _parse_record_lines(data)
        return

    tail = bytearray()
    total_bytes = 0
    with open_rollout(path) as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                raise RolloutError("rollout record exceeds the size limit")
            total_bytes += len(line)
            if total_bytes > MAX_DECOMPRESSED_BYTES:
                raise RolloutError("rollout exceeds the decompressed byte limit")
            tail.extend(line)
            while len(tail) > max_bytes:
                boundary = tail.find(b"\n")
                if boundary < 0:
                    raise RolloutError("rollout terminal tail is incomplete")
                del tail[: boundary + 1]
    yield from _parse_record_lines(bytes(tail))


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

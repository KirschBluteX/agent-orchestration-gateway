#!/usr/bin/env python3
"""Bounded readers for plain and zstd-compressed Codex rollout JSONL files."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping


MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 1_000_000
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class RolloutError(ValueError):
    """A rollout cannot be read safely with the available local runtime."""


class RolloutUnavailable(RolloutError):
    """A rollout could not be read because local I/O is temporarily unavailable."""


def _compressed_stream_is_incomplete(error: BaseException) -> bool:
    """Recognize decoder reports that can result from an append in progress."""

    # ``compression.zstd`` uses EOFError for this case.  Older ``zstandard``
    # builds use a ZstdError whose wording varies by the bundled zstd version.
    # Keep malformed frames deterministic, but never turn a known incomplete
    # frame into an invalid child result while the host is still appending it.
    if isinstance(error, EOFError):
        return True
    detail = str(error).casefold()
    return any(
        marker in detail
        for marker in (
            "incomplete",
            "truncated",
            "end of stream",
            "end-of-stream",
            "did not decompress full frame",
        )
    )


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
            raise RolloutUnavailable("rollout is unavailable") from error
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
                with zstandard.ZstdDecompressor().stream_reader(source) as raw:
                    with io.BufferedReader(raw) as stream:
                        yield stream
        except OSError as error:
            raise RolloutUnavailable("compressed rollout is unavailable") from error
        except (EOFError, zstandard.ZstdError) as error:
            if _compressed_stream_is_incomplete(error):
                raise RolloutUnavailable(
                    "compressed rollout terminal tail is still being written"
                ) from error
            raise RolloutError("compressed rollout is invalid") from error
        return
    try:
        with zstd.open(path, "rb") as stream:
            yield stream
    except OSError as error:
        raise RolloutUnavailable("compressed rollout is unavailable") from error
    except (EOFError, ValueError, zstd.ZstdError) as error:
        if _compressed_stream_is_incomplete(error):
            raise RolloutUnavailable(
                "compressed rollout terminal tail is still being written"
            ) from error
        raise RolloutError("compressed rollout is invalid") from error


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


def _require_complete_tail(data: bytes) -> None:
    """Reject an actively appended terminal record as a retryable condition."""

    if data and not data.endswith(b"\n"):
        # A JSON object can be syntactically complete before its writer has
        # finished the record or flushed the following host framing.  The tail
        # reader is used for authoritative SubagentStop recovery, so accepting
        # that ambiguous snapshot would fence/retire based on a moving
        # transcript.  A retry gets a stable terminal newline.
        raise RolloutUnavailable("rollout terminal tail is still being written")


def _snapshot_changed(before: os.stat_result, after: os.stat_result) -> bool:
    """Return whether a rollout was replaced or changed during one read."""

    return (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or getattr(after, "st_mtime_ns", None) != getattr(before, "st_mtime_ns", None)
    )


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
                before = os.fstat(stream.fileno())
                size = before.st_size
                start = max(0, size - max_bytes)
                previous = b""
                if start:
                    stream.seek(start - 1)
                    previous = stream.read(1)
                stream.seek(start)
                data = stream.read(size - start)
                after = os.fstat(stream.fileno())
        except OSError as error:
            raise RolloutUnavailable("rollout is unavailable") from error
        if _snapshot_changed(before, after):
            raise RolloutUnavailable("rollout terminal tail changed while being read")
        if start and previous != b"\n":
            boundary = data.find(b"\n")
            if boundary < 0:
                raise RolloutUnavailable("rollout terminal tail is still being written")
            data = data[boundary + 1 :]
        if len(data) > max_bytes:
            raise RolloutError("rollout terminal tail exceeds the size limit")
        _require_complete_tail(data)
        yield from _parse_record_lines(data)
        return

    try:
        before = path.stat()
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
                        raise RolloutUnavailable(
                            "rollout terminal tail is still being written"
                        )
                    del tail[: boundary + 1]
        after = path.stat()
    except OSError as error:
        raise RolloutUnavailable("compressed rollout is unavailable") from error
    if _snapshot_changed(before, after):
        raise RolloutUnavailable("rollout terminal tail changed while being read")
    data = bytes(tail)
    _require_complete_tail(data)
    yield from _parse_record_lines(data)


def first_record(path: Path) -> Mapping[str, Any]:
    try:
        return next(iter_records(path))
    except StopIteration as error:
        raise RolloutError("rollout is empty") from error

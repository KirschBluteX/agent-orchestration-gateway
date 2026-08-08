#!/usr/bin/env python3
"""Shared in-process deadlines for synchronous CCO Hook work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time
from typing import Iterator


class OperationDeadlineExceeded(RuntimeError):
    """The current bounded Hook operation exhausted its internal budget."""


_DEADLINE: ContextVar[float | None] = ContextVar("cco_operation_deadline", default=None)


@contextmanager
def deadline_after(seconds: float) -> Iterator[None]:
    """Apply a nested monotonic deadline without extending an existing one."""

    if seconds <= 0:
        raise ValueError("operation deadline must be positive")
    candidate = time.monotonic() + seconds
    current = _DEADLINE.get()
    token = _DEADLINE.set(candidate if current is None else min(current, candidate))
    try:
        yield
        remaining_seconds()
    finally:
        _DEADLINE.reset(token)


def remaining_seconds(*, reserve: float = 0.0) -> float | None:
    """Return the remaining budget, or ``None`` outside bounded Hook work."""

    if reserve < 0:
        raise ValueError("deadline reserve must be non-negative")
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic() - reserve
    if remaining <= 0:
        raise OperationDeadlineExceeded("CCO internal Hook deadline exceeded")
    return remaining


def checkpoint() -> None:
    """Fail promptly when bounded synchronous work has exhausted its budget."""

    remaining_seconds()

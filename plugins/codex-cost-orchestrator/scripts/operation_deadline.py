#!/usr/bin/env python3
"""Shared in-process deadlines for synchronous CCO Hook work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import time
from typing import Iterator


class OperationDeadlineExceeded(RuntimeError):
    """The current bounded Hook operation exhausted its internal budget."""


_DEADLINE: ContextVar[float | None] = ContextVar("cco_operation_deadline", default=None)


def _finite_budget(value: object, label: str, *, positive: bool) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be a finite {qualifier} number")
    return float(value)


@contextmanager
def deadline_after(seconds: float) -> Iterator[None]:
    """Apply a nested monotonic deadline without extending an existing one."""

    budget = _finite_budget(seconds, "operation deadline", positive=True)
    candidate = time.monotonic() + budget
    if not math.isfinite(candidate):
        raise ValueError("operation deadline must be a finite positive number")
    current = _DEADLINE.get()
    token = _DEADLINE.set(candidate if current is None else min(current, candidate))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_seconds(*, reserve: float = 0.0) -> float | None:
    """Return the remaining budget, or ``None`` outside bounded Hook work."""

    reserved = _finite_budget(reserve, "deadline reserve", positive=False)
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic() - reserved
    if remaining <= 0:
        raise OperationDeadlineExceeded("CCO internal Hook deadline exceeded")
    return remaining


def checkpoint() -> None:
    """Fail promptly when bounded synchronous work has exhausted its budget."""

    remaining_seconds()

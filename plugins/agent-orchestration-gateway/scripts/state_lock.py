#!/usr/bin/env python3
"""Small re-entrant cross-process locks for aog.v1 state coordination."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Iterator


class StateLockBusy(RuntimeError):
    """The session state lock could not be acquired before its short deadline."""


_DEFAULT_WAIT_SECONDS = 5.0
_LOCAL_GUARD = threading.RLock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_HELD: dict[str, tuple[int, int]] = {}
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


def _require_identity(value: object) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ValueError("state lock identity is invalid")
    return value


def _require_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("state lock timeout must be a finite non-negative number")
    return float(value)


def lock_path(root: Path, identity: str) -> Path:
    """Return the lock path for one validated coordination identity."""

    return Path(root) / f".{_require_identity(identity)}.aog-state.lock"


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _local_lock(key: str) -> threading.RLock:
    with _LOCAL_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


def _os_try_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, PermissionError):
            return False
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError) as error:
        if getattr(error, "errno", None) in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _os_unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def _open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        os.fspath(path),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def acquire(
    root: Path,
    identity: str,
    *,
    timeout: float = _DEFAULT_WAIT_SECONDS,
) -> Iterator[None]:
    """Acquire one re-entrant OS lock for a lifecycle or workspace identity."""

    timeout = _require_timeout(timeout)
    path = lock_path(root, identity)
    key = _key(path)
    local = _local_lock(key)
    if not local.acquire(timeout=timeout):
        raise StateLockBusy("session state lock acquisition timed out")
    descriptor: int | None = None
    owner = threading.get_ident()
    try:
        with _LOCAL_GUARD:
            held = _HELD.get(key)
            if held is not None:
                if held[0] != owner:
                    raise StateLockBusy("session state lock is owned by another thread")
                _HELD[key] = (owner, held[1] + 1)
                nested = True
            else:
                nested = False
        if not nested:
            descriptor = _open_lock(path)
            deadline = time.monotonic() + timeout
            while not _os_try_lock(descriptor):
                if time.monotonic() >= deadline:
                    raise StateLockBusy("session state lock acquisition timed out")
                time.sleep(0.01)
            with _LOCAL_GUARD:
                _HELD[key] = (owner, 1)
        yield
    finally:
        with _LOCAL_GUARD:
            held = _HELD.get(key)
            if held is not None and held[0] == owner:
                if held[1] > 1:
                    _HELD[key] = (owner, held[1] - 1)
                    descriptor = None
                else:
                    del _HELD[key]
        if descriptor is not None:
            _os_unlock(descriptor)
            os.close(descriptor)
        local.release()

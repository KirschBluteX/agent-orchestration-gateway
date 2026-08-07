#!/usr/bin/env python3
"""One re-entrant cross-process lock for each cco.v9 lifecycle state."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time
from typing import Iterator


class StateLockBusy(RuntimeError):
    """The session state lock could not be acquired before its short deadline."""


_DEFAULT_WAIT_SECONDS = 0.25
_LOCAL_GUARD = threading.RLock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_HELD: dict[str, tuple[int, int]] = {}


def lock_path(root: Path, session_id: str) -> Path:
    """Return the one lock path shared by a task's lifecycle operations."""

    return Path(root) / f".{session_id}.cco-state.lock"


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
    session_id: str,
    *,
    timeout: float = _DEFAULT_WAIT_SECONDS,
) -> Iterator[None]:
    """Acquire one re-entrant OS lock for a session state directory."""

    if timeout < 0:
        raise ValueError("state lock timeout must be non-negative")
    path = lock_path(root, session_id)
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


def is_locked(root: Path, session_id: str) -> bool:
    """Return whether another process currently owns the session lock."""

    try:
        with acquire(root, session_id, timeout=0):
            return False
    except StateLockBusy:
        return True

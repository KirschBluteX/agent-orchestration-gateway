#!/usr/bin/env python3
"""Task-local lifecycle and fencing for native CCO agent operations.

The ledger is a compact transactional cursor outside the repository.  Codex remains
the agent runtime; this module only prevents duplicate ownership, concurrent
continuations, and stale results.  It is deliberately not an acceptance record or
durable audit log.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator, Mapping


class LedgerConflict(RuntimeError):
    """The requested lifecycle transition conflicts with the current owner."""


class LedgerBusy(RuntimeError):
    """The short ledger lock could not be acquired in time."""


_TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
_STATES = frozenset({"reserved", "owned", "continuable", "retired"})
_LOCK_WAIT_SECONDS = 0.25
_STALE_LOCK_SECONDS = 60.0


class TaskLedger:
    """Own the lifecycle of one row per ``node@contract_rev``."""

    def __init__(self, root: Path, session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", session_id):
            raise ValueError("session_id is invalid")
        self.root = Path(root)
        self.path = self.root / f"{session_id}.json"
        self.lock_path = self.root / f".{session_id}.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > _STALE_LOCK_SECONDS:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise LedgerBusy("ledger lock acquisition timed out")
                time.sleep(0.01)
        os.close(descriptor)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"rows": {}}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LedgerConflict("ledger document is unreadable") from error
        if not isinstance(document, dict) or not isinstance(document.get("rows"), dict):
            raise LedgerConflict("ledger document is malformed")
        for row in document["rows"].values():
            if not isinstance(row, dict) or row.get("state") not in _STATES:
                raise LedgerConflict("ledger row is malformed")
        return document

    def _write(self, document: Mapping[str, Any]) -> None:
        """Atomically replace the cursor; power-loss durability is not required."""

        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.tmp-",
            suffix=".json",
            dir=self.root,
        )
        temporary = Path(temporary_name)
        try:
            payload = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(value: Mapping[str, object]) -> str:
        try:
            node = str(value["node"])
            revision = int(value["contract_rev"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("claim lacks node/contract_rev") from error
        if not node or revision < 1:
            raise ValueError("claim identity is invalid")
        return f"{node}@{revision}"

    @staticmethod
    def _integer(value: object, label: str, *, minimum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} is invalid")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} is invalid") from error
        if parsed < minimum:
            raise ValueError(f"{label} is invalid")
        return parsed

    @classmethod
    def _claim(cls, identity: Mapping[str, object]) -> dict[str, Any]:
        """Normalize the compact v6 lifecycle identity."""

        claim = {
            "node": identity.get("node"),
            "contract_rev": cls._integer(identity.get("contract_rev"), "contract revision", minimum=1),
            "contract_sha256": identity.get("contract_sha256"),
            "run": identity.get("run"),
            "generation": cls._integer(identity.get("generation"), "generation", minimum=1),
            "input_sha256": identity.get("input_sha256"),
            "cursor": cls._integer(identity.get("cursor"), "cursor", minimum=0),
            "role": identity.get("role"),
        }
        cls._key(claim)
        for name in ("contract_sha256", "run", "input_sha256", "role"):
            if not isinstance(claim[name], str) or not claim[name]:
                raise ValueError(f"claim {name} is invalid")
        return claim

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in row.items() if not key.startswith("_")})

    @classmethod
    def _new_row(cls, call_id: str, identity: Mapping[str, object]) -> dict[str, Any]:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id is invalid")
        return {
            **cls._claim(identity),
            "state": "reserved",
            "call_id": call_id,
            "owner": None,
        }

    def reserve(self, call_id: str, identity: Mapping[str, object]) -> dict[str, Any]:
        row = self._new_row(call_id, identity)
        key = self._key(row)
        with self._lock():
            document = self._read()
            existing = document["rows"].get(key)
            if isinstance(existing, dict) and existing.get("state") in {
                "reserved",
                "owned",
                "continuable",
            }:
                if existing.get("state") == "reserved" and self._public(existing) == self._public(row):
                    return self._public(existing)
                raise LedgerConflict(f"{key} already has a live owner")
            if isinstance(existing, dict):
                if (
                    row["generation"] <= existing["generation"]
                    or row["run"] == existing["run"]
                    or row["contract_sha256"] != existing["contract_sha256"]
                ):
                    raise LedgerConflict(f"{key} requires a newer generation")
                row["_rollback"] = existing
            document["rows"][key] = row
            self._write(document)
            return self._public(row)

    def activate(self, call_id: str, owner: str) -> dict[str, Any]:
        if _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("owner is not a canonical task path")
        with self._lock():
            document = self._read()
            row = self._find_by_call(document, call_id)
            if row.get("state") == "owned" and row.get("owner") == owner:
                return self._public(row)
            if row.get("state") != "reserved":
                raise LedgerConflict("reservation is no longer activatable")
            row["state"] = "owned"
            row["owner"] = owner
            row.pop("_rollback", None)
            self._write(document)
            return self._public(row)

    def release(self, call_id: str) -> None:
        with self._lock():
            document = self._read()
            rows = document["rows"]
            match = next(
                ((key, row) for key, row in rows.items() if isinstance(row, dict) and row.get("call_id") == call_id),
                None,
            )
            if match is None:
                return
            key, row = match
            if row.get("state") != "reserved":
                raise LedgerConflict("only an unowned reservation can be released")
            rollback = row.get("_rollback")
            if isinstance(rollback, dict):
                rows[key] = rollback
                self._write(document)
            else:
                del rows[key]
                if rows:
                    self._write(document)
                else:
                    self.path.unlink(missing_ok=True)

    def prepare_continuation(
        self,
        call_id: str,
        owner: str,
        *,
        previous_input_sha256: str,
        next_input_sha256: str,
        cursor: int,
    ) -> dict[str, Any]:
        """Reserve the current continuation cursor before the native call."""

        if not call_id:
            raise ValueError("call_id is invalid")
        with self._lock():
            document = self._read()
            row = self._find_by_owner(document, owner)
            if row.get("state") not in {"owned", "continuable"}:
                raise LedgerConflict("owner is not continuable")
            pending = row.get("_pending")
            requested = {
                "call_id": call_id,
                "from_state": row["state"],
                "previous_input_sha256": previous_input_sha256,
                "next_input_sha256": next_input_sha256,
                "cursor": cursor,
            }
            if isinstance(pending, dict):
                if pending == requested:
                    return self._public(row)
                raise LedgerConflict("another continuation is pending")
            if row.get("input_sha256") != previous_input_sha256:
                raise LedgerConflict("continuation input is stale")
            if cursor != int(row.get("cursor", 0)) + 1:
                raise LedgerConflict("continuation cursor is stale")
            if not isinstance(next_input_sha256, str) or next_input_sha256 == previous_input_sha256:
                raise LedgerConflict("continuation output is invalid")
            row["_pending"] = requested
            self._write(document)
            return self._public(row)

    def settle_continuation(self, call_id: str, *, accepted: bool) -> dict[str, Any]:
        """Commit or roll back the prepared cursor after the native call."""

        with self._lock():
            document = self._read()
            row = self._find_pending(document, call_id)
            pending = row.pop("_pending")
            if accepted:
                row["input_sha256"] = pending["next_input_sha256"]
                row["cursor"] = pending["cursor"]
                row["state"] = "owned"
            else:
                row["state"] = pending["from_state"]
            self._write(document)
            return self._public(row)

    def retire(self, owner: str) -> dict[str, Any]:
        with self._lock():
            document = self._read()
            row = self._find_by_owner(document, owner)
            if row.get("state") == "retired":
                return self._public(row)
            if row.get("state") not in {"owned", "continuable"}:
                raise LedgerConflict("only an owner can be retired")
            row["state"] = "retired"
            row.pop("_pending", None)
            self._write(document)
            return self._public(row)

    def record_result(
        self,
        *,
        node: str,
        contract_rev: int,
        run: str,
        generation: int,
        input_sha256: str,
        owner: str,
        disposition: str,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Record an exact returned turn without asserting Primary acceptance."""

        if disposition not in {"continuable", "retired"}:
            raise ValueError("result disposition is invalid")
        if _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("result owner is not canonical")
        key = f"{node}@{contract_rev}"
        with self._lock():
            document = self._read()
            row = document["rows"].get(key)
            if not isinstance(row, dict):
                raise LedgerConflict("result owner does not exist")
            if (
                row.get("state") not in {"owned", "continuable"}
                or row.get("owner") != owner
                or row.get("run") != run
                or row.get("generation") != generation
                or row.get("input_sha256") != input_sha256
                or (cursor is not None and row.get("cursor") != cursor)
            ):
                raise LedgerConflict("result identity is stale")
            if row.get("state") == "continuable" and disposition == "continuable":
                return self._public(row)
            if row.get("state") != "owned":
                raise LedgerConflict("result owner is no longer current")
            row["state"] = disposition
            self._write(document)
            return self._public(row)

    def read_rows(self) -> list[dict[str, Any]]:
        with self._lock():
            document = self._read()
            return [self._public(document["rows"][key]) for key in sorted(document["rows"])]

    def cleanup_if_terminal(self, *, force: bool = False) -> bool:
        """Check terminality and unlink under the same lock."""

        with self._lock():
            if not self.path.exists():
                return True
            document = self._read()
            rows = document["rows"].values()
            if force or all(row.get("state") == "retired" for row in rows):
                self.path.unlink(missing_ok=True)
                return True
            return False

    def _find_by_call(self, document: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        for row in document["rows"].values():
            if isinstance(row, dict) and row.get("call_id") == call_id:
                return row
        raise LedgerConflict("reservation does not exist")

    def _find_by_owner(self, document: Mapping[str, Any], owner: str) -> dict[str, Any]:
        for row in document["rows"].values():
            if isinstance(row, dict) and row.get("owner") == owner:
                return row
        raise LedgerConflict("owner does not exist")

    def _find_pending(self, document: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        for row in document["rows"].values():
            pending = row.get("_pending") if isinstance(row, dict) else None
            if isinstance(pending, dict) and pending.get("call_id") == call_id:
                return row
        raise LedgerConflict("continuation reservation does not exist")

    @classmethod
    def cleanup_stale(
        cls,
        root: Path,
        *,
        keep_session_id: str,
        max_age_seconds: float,
    ) -> list[Path]:
        """Remove abandoned residue from a cold lifecycle boundary."""

        if max_age_seconds < 60:
            raise ValueError("stale cleanup threshold must be at least 60 seconds")
        directory = Path(root)
        if not directory.exists():
            return []
        now = time.time()
        removed: list[Path] = []
        for candidate in directory.glob("*.json"):
            session_id = candidate.stem
            if session_id == keep_session_id or session_id.startswith("."):
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", session_id) is None:
                continue
            lock = directory / f".{session_id}.lock"
            try:
                expired = now - candidate.stat().st_mtime > max_age_seconds
            except FileNotFoundError:
                continue
            if expired and not lock.exists():
                candidate.unlink(missing_ok=True)
                removed.append(candidate)
        for temporary in directory.glob(".*.json.tmp-*.json"):
            try:
                expired = now - temporary.stat().st_mtime > max_age_seconds
            except FileNotFoundError:
                continue
            if expired:
                temporary.unlink(missing_ok=True)
        return sorted(removed, key=lambda path: path.name)

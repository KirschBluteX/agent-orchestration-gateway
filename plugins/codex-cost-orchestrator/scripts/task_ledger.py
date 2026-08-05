#!/usr/bin/env python3
"""Small transactional v7 owner cursor with terminal tombstones."""

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
_STATES = frozenset({"reserved", "rejected", "owned", "continuable", "retired"})
_ROLES = frozenset({"explorer", "worker", "reviewer"})
_PHYSICAL_ROLES = frozenset({"cost_orchestrator_read_leaf", "cost_orchestrator_write_leaf"})
_ASSURANCES = frozenset({"mechanical", "bounded", "guarded"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_NODE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_EPOCH = re.compile(r"^e[0-9]{2,}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_WORKSPACE_FIELDS = frozenset({"baseline", "baseline_path", "graph_scopes", "graph_sha256", "scopes", "workspace_mode"})
_LOCK_WAIT_SECONDS = 0.25
_STALE_LOCK_SECONDS = 60.0


class TaskLedger:
    """Own one row per ``node@contract_rev`` and retain terminal tombstones."""

    def __init__(self, root: Path, session_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", session_id):
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
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
            return {"fenced_owners": [], "guarded_floors": [], "rows": {}}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LedgerConflict("ledger document is unreadable") from error
        if not isinstance(document, dict) or not isinstance(document.get("rows"), dict):
            raise LedgerConflict("ledger document is malformed")
        for row in document["rows"].values():
            if not isinstance(row, dict) or row.get("state") not in _STATES:
                raise LedgerConflict("ledger row is malformed")
        fenced = document.get("fenced_owners", [])
        if not isinstance(fenced, list) or any(not isinstance(owner, str) or _TASK_PATH.fullmatch(owner) is None for owner in fenced) or len(set(fenced)) != len(fenced):
            raise LedgerConflict("ledger owner fences are malformed")
        floors = document.get("guarded_floors", [])
        if not isinstance(floors, list) or any(not isinstance(floor, dict) or set(floor) != {"node", "role"} or not isinstance(floor.get("node"), str) or _NODE.fullmatch(floor["node"]) is None or floor.get("role") not in _ROLES for floor in floors):
            raise LedgerConflict("ledger guarded floors are malformed")
        if floors != sorted(floors, key=lambda item: (item["node"], item["role"])) or len({(item["node"], item["role"]) for item in floors}) != len(floors):
            raise LedgerConflict("ledger guarded floors are not canonical")
        document["fenced_owners"] = fenced
        document["guarded_floors"] = floors
        return document

    def _write(self, document: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.tmp-", suffix=".json", dir=self.root)
        temporary = Path(temporary_name)
        try:
            payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _key(value: Mapping[str, object]) -> str:
        node = value.get("node")
        revision = value.get("contract_rev")
        if not isinstance(node, str) or _NODE.fullmatch(node) is None or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("claim identity is invalid")
        return f"{node}@{revision}"

    @staticmethod
    def _integer(value: object, label: str, minimum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{label} is invalid")
        return value

    @classmethod
    def _route(cls, value: object) -> dict[str, Any]:
        required = {"assurance", "constraints", "decision_sha256", "plan_sha256", "rank", "selected"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("claim route is malformed")
        assurance = value["assurance"]
        constraints = value["constraints"]
        selected = value["selected"]
        if assurance not in _ASSURANCES or not isinstance(constraints, Mapping) or set(constraints) != {"fixed_effort", "fixed_model", "source"} or not isinstance(selected, Mapping) or set(selected) != {"effort", "model"}:
            raise ValueError("claim route is malformed")
        if constraints["source"] not in {"automatic", "user"}:
            raise ValueError("claim route source is invalid")
        model, effort = selected["model"], selected["effort"]
        if not isinstance(model, str) or _MODEL.fullmatch(model) is None or not isinstance(effort, str) or _EFFORT.fullmatch(effort) is None:
            raise ValueError("claim route selected pair is invalid")
        for key, pattern in (("fixed_model", _MODEL), ("fixed_effort", _EFFORT)):
            item = constraints[key]
            if item is not None and (not isinstance(item, str) or pattern.fullmatch(item) is None):
                raise ValueError("claim route constraint is invalid")
        if constraints["fixed_model"] is not None and constraints["fixed_model"] != model or constraints["fixed_effort"] is not None and constraints["fixed_effort"] != effort:
            raise ValueError("claim route violates fixed constraint")
        for key in ("decision_sha256", "plan_sha256"):
            if not isinstance(value[key], str) or _SHA256.fullmatch(value[key]) is None:
                raise ValueError("claim route identity is invalid")
        rank = value["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("claim route rank is invalid")
        return {
            "assurance": assurance,
            "constraints": {"fixed_effort": constraints["fixed_effort"], "fixed_model": constraints["fixed_model"], "source": constraints["source"]},
            "decision_sha256": value["decision_sha256"],
            "plan_sha256": value["plan_sha256"],
            "rank": rank,
            "selected": {"effort": effort, "model": model},
        }

    @classmethod
    def _claim(cls, identity: Mapping[str, object]) -> dict[str, Any]:
        claim = {
            "node": identity.get("node"),
            "acceptance_ids": identity.get("acceptance_ids"),
            "contract_rev": cls._integer(identity.get("contract_rev"), "contract revision", 1),
            "contract_sha256": identity.get("contract_sha256"),
            "run": identity.get("run"),
            "generation": cls._integer(identity.get("generation"), "generation", 1),
            "input_sha256": identity.get("input_sha256"),
            "cursor": cls._integer(identity.get("cursor"), "cursor", 0),
            "epoch": identity.get("epoch"),
            "fork_turns": identity.get("fork_turns", "none"),
            "role": identity.get("role"),
            "assurance": identity.get("assurance"),
        }
        cls._key(claim)
        if claim["role"] not in _ROLES or claim["assurance"] not in _ASSURANCES:
            raise ValueError("claim role or assurance is invalid")
        if claim["epoch"] is not None and (
            not isinstance(claim["epoch"], str) or _EPOCH.fullmatch(claim["epoch"]) is None
        ):
            raise ValueError("claim epoch is invalid")
        if claim["fork_turns"] != "none" and (
            not isinstance(claim["fork_turns"], str)
            or _POSITIVE_INTEGER.fullmatch(claim["fork_turns"]) is None
        ):
            raise ValueError("claim fork_turns is invalid")
        acceptance_ids = claim["acceptance_ids"]
        if (
            not isinstance(acceptance_ids, list)
            or not acceptance_ids
            or any(not isinstance(item, str) or re.fullmatch(r"A[0-9]{2,}", item) is None for item in acceptance_ids)
            or acceptance_ids != sorted(set(acceptance_ids))
        ):
            raise ValueError("claim acceptance IDs are invalid")
        for name in ("contract_sha256", "run", "input_sha256"):
            if not isinstance(claim[name], str) or not claim[name] or (name.endswith("sha256") and _SHA256.fullmatch(claim[name]) is None):
                raise ValueError(f"claim {name} is invalid")
        claim["route"] = cls._route(identity.get("route"))
        present = _WORKSPACE_FIELDS.intersection(identity)
        if present:
            if present != _WORKSPACE_FIELDS:
                raise ValueError("claim workspace identity is incomplete")
            if not isinstance(identity["baseline"], str) or _SHA256.fullmatch(identity["baseline"]) is None or not isinstance(identity["graph_sha256"], str) or _SHA256.fullmatch(identity["graph_sha256"]) is None or not isinstance(identity["baseline_path"], str) or not Path(identity["baseline_path"]).is_absolute() or identity["workspace_mode"] not in {"light", "strict"} or not isinstance(identity["scopes"], list) or not isinstance(identity["graph_scopes"], list):
                raise ValueError("claim workspace identity is invalid")
            for scope_set in (identity["scopes"], identity["graph_scopes"]):
                if scope_set != sorted(scope_set, key=lambda item: (item.get("kind", ""), item.get("path", ""))):
                    raise ValueError("claim workspace scopes are not canonical")
            if not all(scope in identity["graph_scopes"] for scope in identity["scopes"]):
                raise ValueError("claim node scopes are outside graph scopes")
            claim.update({name: identity[name] for name in _WORKSPACE_FIELDS})
        return claim

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in row.items() if not key.startswith("_")})

    @classmethod
    def _new_row(cls, call_id: str, identity: Mapping[str, object]) -> dict[str, Any]:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id is invalid")
        return {**cls._claim(identity), "state": "reserved", "call_id": call_id, "owner": None}

    def reserve(self, call_id: str, identity: Mapping[str, object]) -> dict[str, Any]:
        row = self._new_row(call_id, identity)
        key = self._key(row)
        with self._lock():
            document = self._read()
            floor = {"node": row["node"], "role": row["role"]}
            if floor in document["guarded_floors"] and row["assurance"] != "guarded":
                raise LedgerConflict(f"{row['node']} requires guarded assurance after a Luna generation")
            existing = document["rows"].get(key)
            if not isinstance(existing, Mapping) and row["route"]["rank"] != 1:
                raise LedgerConflict(f"{key} initial route must start at rank 1")
            if isinstance(existing, Mapping) and existing.get("state") in {"reserved", "owned", "continuable"}:
                if existing.get("state") == "reserved" and self._public(existing) == self._public(row):
                    return self._public(existing)
                raise LedgerConflict(f"{key} already has a live owner")
            if isinstance(existing, Mapping) and existing.get("state") == "rejected":
                immutable = (
                    "acceptance_ids",
                    "assurance",
                    "contract_rev",
                    "contract_sha256",
                    "epoch",
                    "fork_turns",
                    "generation",
                    "node",
                    "role",
                    *_WORKSPACE_FIELDS,
                )
                previous_route = existing["route"]
                next_route = row["route"]
                if (
                    any(existing.get(name) != row.get(name) for name in immutable)
                    or next_route["rank"] != previous_route["rank"] + 1
                    or next_route["assurance"] != previous_route["assurance"]
                    or next_route["constraints"] != previous_route["constraints"]
                    or next_route["decision_sha256"] != previous_route["decision_sha256"]
                ):
                    raise LedgerConflict(f"{key} requires the next fallback rank after rejection")
                document["rows"][key] = row
                self._write(document)
                return self._public(row)
            if isinstance(existing, Mapping) and (row["generation"] <= existing.get("generation", 0) or row["run"] == existing.get("run") or row["contract_sha256"] != existing.get("contract_sha256")):
                raise LedgerConflict(f"{key} requires a newer generation")
            if isinstance(existing, Mapping) and isinstance(existing.get("owner"), str):
                self._fence_owner(document, existing["owner"])
            document["rows"][key] = row
            self._write(document)
            return self._public(row)

    def activate(self, call_id: str, owner: str) -> dict[str, Any]:
        if _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("owner is not canonical")
        with self._lock():
            document = self._read()
            row = self._find_by_call(document, call_id)
            if owner != f"/root/{row['run']}":
                raise LedgerConflict("spawn owner does not match reserved task path")
            if row.get("state") == "owned" and row.get("owner") == owner:
                return self._public(row)
            if row.get("state") != "reserved":
                raise LedgerConflict("reservation is no longer activatable")
            row["state"] = "owned"
            row["owner"] = owner
            self._write(document)
            return self._public(row)

    def release(self, call_id: str) -> None:
        with self._lock():
            document = self._read()
            match = next(((key, row) for key, row in document["rows"].items() if row.get("call_id") == call_id), None)
            if match is None:
                return
            _key, row = match
            if row.get("state") != "reserved":
                raise LedgerConflict("only an unowned reservation can be released")
            row["state"] = "rejected"
            row["owner"] = None
            row.pop("_pending", None)
            self._write(document)

    def prepare_continuation(self, call_id: str, owner: str, *, previous_input_sha256: str, next_input_sha256: str, cursor: int) -> dict[str, Any]:
        with self._lock():
            document = self._read()
            row = self._find_by_owner(document, owner)
            if row.get("state") not in {"owned", "continuable"}:
                raise LedgerConflict("owner is not continuable")
            requested = {"call_id": call_id, "from_state": row["state"], "previous_input_sha256": previous_input_sha256, "next_input_sha256": next_input_sha256, "cursor": cursor}
            pending = row.get("_pending")
            if isinstance(pending, Mapping):
                if dict(pending) == requested:
                    return self._public(row)
                raise LedgerConflict("another continuation is pending")
            if row.get("input_sha256") != previous_input_sha256 or cursor != int(row.get("cursor", 0)) + 1 or next_input_sha256 == previous_input_sha256:
                raise LedgerConflict("continuation identity is stale")
            row["_pending"] = requested
            self._write(document)
            return self._public(row)

    def settle_pending_continuation(self, call_id: str, *, accepted: bool) -> dict[str, Any] | None:
        with self._lock():
            document = self._read()
            row = next((row for row in document["rows"].values() if isinstance(row.get("_pending"), Mapping) and row["_pending"].get("call_id") == call_id), None)
            if row is None:
                return None
            pending = row.pop("_pending")
            if accepted:
                row["input_sha256"] = pending["next_input_sha256"]
                row["cursor"] = pending["cursor"]
            self._write(document)
            return self._public(row)

    def retire(self, owner: str) -> dict[str, Any]:
        with self._lock():
            document = self._read()
            row = self._find_by_owner(document, owner)
            if row.get("state") not in {"owned", "continuable", "retired"}:
                raise LedgerConflict("owner cannot be retired")
            row["state"] = "retired"
            row.pop("_pending", None)
            self._fence_owner(document, owner)
            self._set_guarded_floor(document, row)
            self._write(document)
            return self._public(row)

    def retire_if_present(self, owner: str) -> bool:
        """Retire a current managed owner; leave unmanaged or fenced-only owners alone."""

        if _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("owner is not canonical")
        with self._lock():
            document = self._read()
            row = next(
                (item for item in document["rows"].values() if item.get("owner") == owner),
                None,
            )
            if row is None:
                return False
            if row.get("state") not in {"owned", "continuable", "retired"}:
                raise LedgerConflict("owner cannot be retired")
            row["state"] = "retired"
            row.pop("_pending", None)
            self._fence_owner(document, owner)
            self._set_guarded_floor(document, row)
            self._write(document)
            return True

    def retire_after_invalid_stop(self, owner: str) -> dict[str, Any]:
        """Atomically fence a final invalid SubagentStop and require guarded retry."""

        if _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("owner is not canonical")
        with self._lock():
            document = self._read()
            row = self._find_by_owner(document, owner)
            if row.get("state") not in {"owned", "continuable", "retired"}:
                raise LedgerConflict("owner cannot be retired")
            row["state"] = "retired"
            row.pop("_pending", None)
            self._fence_owner(document, owner)
            self._set_guarded_floor(document, row, force=True)
            self._write(document)
            return self._public(row)

    def record_result(self, *, node: str, contract_rev: int, run: str, generation: int, input_sha256: str, owner: str, disposition: str, cursor: int | None = None, require_guarded: bool = False) -> dict[str, Any]:
        if disposition not in {"continuable", "retired"} or _TASK_PATH.fullmatch(owner) is None:
            raise ValueError("result identity is invalid")
        if type(require_guarded) is not bool:
            raise ValueError("result guarded requirement is invalid")
        with self._lock():
            document = self._read()
            row = document["rows"].get(f"{node}@{contract_rev}")
            if not isinstance(row, Mapping) or row.get("state") not in {"owned", "continuable"} or row.get("owner") != owner or row.get("run") != run or row.get("generation") != generation or row.get("input_sha256") != input_sha256 or (cursor is not None and row.get("cursor") != cursor):
                raise LedgerConflict("result identity is stale")
            if row.get("state") == "continuable" and disposition == "continuable":
                return self._public(row)
            row["state"] = disposition
            if disposition == "retired":
                self._fence_owner(document, owner)
                self._set_guarded_floor(document, row, force=require_guarded)
            self._write(document)
            return self._public(row)

    def read_rows(self) -> list[dict[str, Any]]:
        with self._lock():
            document = self._read()
            return [
                self._public(document["rows"][key])
                for key in sorted(document["rows"])
            ]

    def is_managed_owner(self, owner: object) -> bool:
        if not isinstance(owner, str) or _TASK_PATH.fullmatch(owner) is None:
            return False
        with self._lock():
            document = self._read()
            return owner in document["fenced_owners"] or any(row.get("owner") == owner for row in document["rows"].values())

    def cleanup_if_terminal(self, *, force: bool = False) -> bool:
        with self._lock():
            if not self.path.exists():
                return True
            document = self._read()
            if force or all(row.get("state") == "retired" for row in document["rows"].values()):
                self.path.unlink(missing_ok=True)
                return True
            return False

    @staticmethod
    def _fence_owner(document: Mapping[str, Any], owner: str) -> bool:
        if owner in document["fenced_owners"]:
            return False
        document["fenced_owners"].append(owner)
        return True

    @staticmethod
    def _set_guarded_floor(document: Mapping[str, Any], row: Mapping[str, Any], *, force: bool = False) -> bool:
        route = row.get("route", {})
        if not force and (not isinstance(route, Mapping) or "luna" not in str(route.get("selected", {}).get("model", "")).casefold()):
            return False
        floor = {"node": row["node"], "role": row["role"]}
        if floor in document["guarded_floors"]:
            return False
        document["guarded_floors"].append(floor)
        document["guarded_floors"].sort(key=lambda item: (item["node"], item["role"]))
        return True

    def _find_by_call(self, document: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        for row in document["rows"].values():
            if row.get("call_id") == call_id:
                return row
        raise LedgerConflict("reservation does not exist")

    def _find_by_owner(self, document: Mapping[str, Any], owner: str) -> dict[str, Any]:
        for row in document["rows"].values():
            if row.get("owner") == owner:
                return row
        raise LedgerConflict("owner does not exist")

    @classmethod
    def cleanup_stale(
        cls,
        root: Path,
        *,
        keep_session_id: str,
        max_age_seconds: float,
        live_max_age_seconds: float | None = None,
    ) -> list[Path]:
        if max_age_seconds < 60:
            raise ValueError("stale cleanup threshold must be at least 60 seconds")
        live_threshold = (
            max_age_seconds * 7
            if live_max_age_seconds is None
            else live_max_age_seconds
        )
        if live_threshold < max_age_seconds:
            raise ValueError("live stale cleanup threshold cannot be shorter")
        directory = Path(root)
        if not directory.exists():
            return []
        now = time.time()
        removed: list[Path] = []
        for candidate in directory.glob("*.json"):
            if candidate.stem == keep_session_id or candidate.stem.startswith("."):
                continue
            lock = directory / f".{candidate.stem}.lock"
            try:
                age = now - candidate.stat().st_mtime
            except FileNotFoundError:
                continue
            try:
                document = json.loads(candidate.read_text(encoding="utf-8"))
                rows = document.get("rows") if isinstance(document, Mapping) else None
                live = not isinstance(rows, Mapping) or any(
                    not isinstance(row, Mapping)
                    or row.get("state") in {"reserved", "rejected", "owned", "continuable"}
                    for row in rows.values()
                )
            except (OSError, ValueError):
                live = True
            expired = age > (live_threshold if live else max_age_seconds)
            if expired and not lock.exists():
                candidate.unlink(missing_ok=True)
                removed.append(candidate)
        return sorted(removed, key=lambda path: path.name)

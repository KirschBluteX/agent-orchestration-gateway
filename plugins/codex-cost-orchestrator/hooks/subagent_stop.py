#!/usr/bin/env python3
"""Read-only, fail-open completion gate for CCO subagents."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Callable

from protocol_envelope import EnvelopeError, load_utf8_json, parse_envelope


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    from protocol_hash import (  # noqa: E402
        ProtocolHashError as _ProtocolHashError,
        digest as protocol_digest,
    )
    PROTOCOL_HASH_ERRORS: tuple[type[Exception], ...] = (_ProtocolHashError,)
except Exception:
    protocol_digest = None
    PROTOCOL_HASH_ERRORS = ()


WORKER_ROLES = frozenset(
    {
        "cost_orchestrator_routine_worker",
        "cost_orchestrator_complex_worker",
    }
)
REVIEWER_ROLE = "cost_orchestrator_reviewer"
WORK_RESULT_HEADER = "CCO_WORK_RESULT cco.v4"
WORK_RESULT_FIELDS = (
    "NODE",
    "CONTRACT_REV",
    "CONTRACT_SHA256",
    "INPUT_CLOSURE_SHA256",
    "GRAPH_MANIFEST_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "RUN",
    "ATTEMPT",
    "FOLLOWUP",
    "LEASE",
    "LEASE_GENERATION",
    "STOP_GENERATION",
    "ACCEPTANCE_IDS",
    "STATUS",
    "FAILURE_ACCEPTANCE_OR_VERIFICATION_ID",
    "FAILURE_CLASS",
    "FAILURE_EXIT_STATUS",
    "FAILURE_DIAGNOSTIC_IDS",
    "FAILURE_SIGNATURE",
    "CHANGED",
    "VERIFIED",
    "JUDGMENT",
    "DEVIATIONS",
    "BLOCKERS",
)
REVIEW_RESULT_HEADER = "CCO_REVIEW_RESULT cco.v4"
REVIEW_RESULT_FIELDS = (
    "EPOCH",
    "MODE",
    "ATTEMPT",
    "FOLLOWUP",
    "INPUT_CLOSURE_SHA256",
    "GRAPH_MANIFEST_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "ACCEPTANCE_IDS",
    "EVIDENCE_SHA256",
    "REVIEWED_STATE",
    "VERDICT",
    "REASON",
    "FINDINGS",
    "RESIDUAL_RISK",
)
WORK_RESULT_ENUMS = {
    "STATUS": frozenset({"complete", "partial", "blocked"}),
}
REVIEW_RESULT_ENUMS = {
    "MODE": frozenset({"fresh", "delta"}),
    "VERDICT": frozenset({"ship", "fix-first", "rethink"}),
}
SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")
WORK_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9_]*$")
RUN_IDENTITY = re.compile(r"^run_([a-z0-9][a-z0-9_]*)_(r0[1-3])$")
EPOCH_IDENTITY = re.compile(r"^e[0-9]{2,}$")
FAILURE_ID = re.compile(r"^[AV][0-9]{2,}$")
FAILURE_CLASS = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DIAGNOSTIC_ID = re.compile(r"^D[A-Z0-9_]{1,63}$")
VERIFIED_LINE = re.compile(
    r"^- (V[0-9]{2,}) (\[A[0-9]{2,}(?:,A[0-9]{2,})*\]): (.+) => (.+)$"
)
FINDING_LINE = re.compile(r"^- (F[0-9]{2,}): .+$")
WORK_RESULT_PATTERNS = {
    "CONTRACT_SHA256": SHA256_VALUE,
    "INPUT_CLOSURE_SHA256": SHA256_VALUE,
    "GRAPH_MANIFEST_SHA256": SHA256_VALUE,
    "ACCEPTANCE_CHAIN_SHA256": SHA256_VALUE,
    "NODE": WORK_IDENTITY,
    "RUN": WORK_IDENTITY,
    "LEASE": WORK_IDENTITY,
}
REVIEW_RESULT_PATTERNS = {
    "EPOCH": EPOCH_IDENTITY,
    "INPUT_CLOSURE_SHA256": SHA256_VALUE,
    "GRAPH_MANIFEST_SHA256": SHA256_VALUE,
    "ACCEPTANCE_CHAIN_SHA256": SHA256_VALUE,
    "EVIDENCE_SHA256": SHA256_VALUE,
    "REVIEWED_STATE": SHA256_VALUE,
}
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_SAFE_INTEGER_DIGITS = len(str(MAX_SAFE_INTEGER))
MAX_WORKER_ATTEMPTS = 3
MAX_WORKER_FOLLOWUPS = 2
MAX_REVIEW_ATTEMPTS = 2
MAX_REVIEW_FOLLOWUPS = 2
WORK_LIST_FIELDS = frozenset(
    {"CHANGED", "VERIFIED", "JUDGMENT", "DEVIATIONS", "BLOCKERS"}
)
REVIEW_LIST_FIELDS = frozenset({"FINDINGS", "RESIDUAL_RISK"})


def _integer_in_range(value: str, *, minimum: int) -> bool:
    if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        return False
    if len(value) > MAX_SAFE_INTEGER_DIGITS:
        return False
    parsed = int(value)
    return minimum <= parsed <= MAX_SAFE_INTEGER


def _counter_in_range(
    value: str, *, minimum: int, maximum_limit: int
) -> bool:
    match = re.fullmatch(r"(0|[1-9][0-9]*)/([1-9][0-9]*)", value)
    if match is None:
        return False
    parts = match.groups()
    if any(len(part) > MAX_SAFE_INTEGER_DIGITS for part in parts):
        return False
    current, limit = (int(part) for part in parts)
    return minimum <= current <= limit <= maximum_limit


def _optional_exit_status(value: str) -> bool:
    if value == "none":
        return True
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", value) is None:
        return False
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_SAFE_INTEGER_DIGITS:
        return False
    return -MAX_SAFE_INTEGER <= int(value) <= MAX_SAFE_INTEGER


def _valid_acceptance_ids(value: str) -> bool:
    if re.fullmatch(r"\[A[0-9]{2,}(?:,A[0-9]{2,})*\]", value) is None:
        return False
    identifiers = value[1:-1].split(",")
    return identifiers == sorted(set(identifiers))


def _valid_diagnostic_ids(value: str) -> bool:
    if value == "[]":
        return True
    if re.fullmatch(r"\[D[A-Z0-9_]{1,63}(?:,D[A-Z0-9_]{1,63})*\]", value) is None:
        return False
    identifiers = value[1:-1].split(",")
    return all(DIAGNOSTIC_ID.fullmatch(item) for item in identifiers) and (
        identifiers == sorted(set(identifiers))
    )


def _acceptance_ids(value: str) -> list[str]:
    return value[1:-1].split(",")


def _valid_verified(value: str, status: str, accepted: str) -> bool:
    lines = value.split("\n")
    if lines == ["- none"]:
        return status != "complete"
    accepted_ids = set(_acceptance_ids(accepted))
    seen_verifications: list[str] = []
    covered: set[str] = set()
    for line in lines:
        match = VERIFIED_LINE.fullmatch(line)
        if match is None:
            return False
        verification_id, ids_value, _operation, _outcome = match.groups()
        ids = _acceptance_ids(ids_value)
        if ids != sorted(set(ids)) or not set(ids) <= accepted_ids:
            return False
        seen_verifications.append(verification_id)
        covered.update(ids)
    if seen_verifications != sorted(set(seen_verifications)):
        return False
    return covered == accepted_ids if status == "complete" else covered <= accepted_ids


def _valid_findings(value: str, verdict: str) -> bool:
    lines = value.split("\n")
    if lines == ["- none"]:
        return verdict not in {"fix-first", "rethink"}
    identifiers: list[str] = []
    for line in lines:
        match = FINDING_LINE.fullmatch(line)
        if match is None:
            return False
        identifiers.append(match.group(1))
    if identifiers != sorted(set(identifiers)):
        return False
    return verdict != "ship"


WORK_RESULT_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "CONTRACT_REV": lambda value: _integer_in_range(value, minimum=1),
    "ATTEMPT": lambda value: _counter_in_range(
        value, minimum=1, maximum_limit=MAX_WORKER_ATTEMPTS
    ),
    "FOLLOWUP": lambda value: _counter_in_range(
        value, minimum=0, maximum_limit=MAX_WORKER_FOLLOWUPS
    ),
    "LEASE_GENERATION": lambda value: _integer_in_range(value, minimum=1),
    "STOP_GENERATION": lambda value: _integer_in_range(value, minimum=0),
    "ACCEPTANCE_IDS": _valid_acceptance_ids,
    "FAILURE_EXIT_STATUS": _optional_exit_status,
    "FAILURE_DIAGNOSTIC_IDS": _valid_diagnostic_ids,
}
REVIEW_RESULT_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "ATTEMPT": lambda value: _counter_in_range(
        value, minimum=1, maximum_limit=MAX_REVIEW_ATTEMPTS
    ),
    "FOLLOWUP": lambda value: _counter_in_range(
        value, minimum=0, maximum_limit=MAX_REVIEW_FOLLOWUPS
    ),
    "ACCEPTANCE_IDS": _valid_acceptance_ids,
}


def _valid_failure(fields: dict[str, str]) -> bool:
    status = fields.get("STATUS")
    failure_id = fields.get("FAILURE_ACCEPTANCE_OR_VERIFICATION_ID", "")
    failure_class = fields.get("FAILURE_CLASS", "")
    exit_status = fields.get("FAILURE_EXIT_STATUS", "")
    diagnostics_value = fields.get("FAILURE_DIAGNOSTIC_IDS", "")
    signature = fields.get("FAILURE_SIGNATURE", "")
    if status == "complete":
        return (
            failure_id == "none"
            and failure_class == "none"
            and exit_status == "none"
            and diagnostics_value == "[]"
            and signature == "none"
        )
    if status not in {"partial", "blocked"}:
        return False
    if (
        FAILURE_ID.fullmatch(failure_id) is None
        or FAILURE_CLASS.fullmatch(failure_class) is None
        or failure_class == "none"
        or not _optional_exit_status(exit_status)
        or not _valid_diagnostic_ids(diagnostics_value)
        or SHA256_VALUE.fullmatch(signature) is None
    ):
        return False
    accepted_ids = set(_acceptance_ids(fields.get("ACCEPTANCE_IDS", "[]")))
    if failure_id.startswith("A"):
        if failure_id not in accepted_ids:
            return False
    else:
        verification_ids = {
            match.group(1)
            for line in fields.get("VERIFIED", "").split("\n")
            if (match := VERIFIED_LINE.fullmatch(line)) is not None
        }
        if failure_id not in verification_ids:
            return False
    diagnostics = [] if diagnostics_value == "[]" else diagnostics_value[1:-1].split(",")
    payload = {
        "acceptance_or_verification_id": failure_id,
        "contract_sha256": fields.get("CONTRACT_SHA256"),
        "diagnostic_ids": diagnostics,
        "exit_status": None if exit_status == "none" else int(exit_status),
        "failure_class": failure_class,
        "node": fields.get("NODE"),
        "protocol": "cco.v4",
    }
    if protocol_digest is None:
        raise RuntimeError("protocol hash helper is unavailable")
    try:
        return protocol_digest("failure", payload) == signature
    except PROTOCOL_HASH_ERRORS + (TypeError, ValueError):
        return False


def _missing_fields(
    message: str,
    *,
    header: str,
    required: tuple[str, ...],
    enums: dict[str, frozenset[str]],
    patterns: dict[str, re.Pattern[str]],
    validators: dict[str, Callable[[str], bool]],
) -> list[str]:
    list_fields = WORK_LIST_FIELDS if header == WORK_RESULT_HEADER else REVIEW_LIST_FIELDS
    try:
        fields = parse_envelope(
            message,
            header=header,
            required=required,
            list_fields=list_fields,
            allow_text_fence=True,
        )
    except EnvelopeError as error:
        return list(error.issues)
    missing: list[str] = []
    for name, allowed_values in enums.items():
        if fields.get(name) not in allowed_values and name not in missing:
            missing.append(name)
    for name, pattern in patterns.items():
        value = fields.get(name)
        if value and pattern.fullmatch(value) is None and name not in missing:
            missing.append(name)
    for name, validator in validators.items():
        value = fields.get(name)
        if value and not validator(value) and name not in missing:
            missing.append(name)
    if header == WORK_RESULT_HEADER:
        run_match = RUN_IDENTITY.fullmatch(fields.get("RUN", ""))
        attempt_match = re.fullmatch(
            r"(0|[1-9][0-9]*)/[1-9][0-9]*", fields.get("ATTEMPT", "")
        )
        attempt_current = (
            int(attempt_match.group(1))
            if attempt_match is not None
            and len(attempt_match.group(1)) <= MAX_SAFE_INTEGER_DIGITS
            else None
        )
        run_number = (
            int(run_match.group(2)[1:])
            if run_match is not None
            and len(run_match.group(2)[1:]) <= MAX_SAFE_INTEGER_DIGITS
            else None
        )
        if (
            run_match is None
            or run_match.group(1) != fields.get("NODE")
            or fields.get("LEASE")
            != f"wl_{fields.get('NODE')}_{run_match.group(2)}"
            or run_number != attempt_current
            or run_match.group(2) != f"r{attempt_current:02d}"
        ):
            missing.extend(
                name for name in ("RUN", "ATTEMPT", "LEASE") if name not in missing
            )
        if not _valid_failure(fields) and "FAILURE_SIGNATURE" not in missing:
            missing.append("FAILURE_SIGNATURE")
        verified = fields.get("VERIFIED")
        accepted = fields.get("ACCEPTANCE_IDS")
        status = fields.get("STATUS")
        if (
            verified
            and accepted
            and _valid_acceptance_ids(accepted)
            and not _valid_verified(verified, status or "", accepted)
            and "VERIFIED" not in missing
        ):
            missing.append("VERIFIED")
        blockers = fields.get("BLOCKERS", "")
        if (
            (status == "complete" and blockers != "- none")
            or (status == "blocked" and blockers == "- none")
        ) and "BLOCKERS" not in missing:
            missing.append("BLOCKERS")
    else:
        followup = fields.get("FOLLOWUP", "")
        match = re.fullmatch(r"(0|[1-9][0-9]*)/[1-9][0-9]*", followup)
        current_text = match.group(1) if match is not None else ""
        current = (
            int(current_text)
            if current_text and len(current_text) <= MAX_SAFE_INTEGER_DIGITS
            else None
        )
        mode = fields.get("MODE")
        if (
            (mode == "fresh" and current != 0)
            or (mode == "delta" and (current is None or current < 1))
        ) and "FOLLOWUP" not in missing:
            missing.append("FOLLOWUP")
        if not _valid_findings(
            fields.get("FINDINGS", ""), fields.get("VERDICT", "")
        ) and "FINDINGS" not in missing:
            missing.append("FINDINGS")
    return missing


def _continuation_reason(header: str, missing: list[str]) -> str:
    if missing == [header]:
        detail = ""
    else:
        shown = ", ".join(missing[:4])
        if len(missing) > 4:
            shown += ", ..."
        detail = f" (missing: {shown})"
    return (
        f"Return one structurally complete {header} packet{detail}; "
        "do not redo completed work."
    )


def _evaluate(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    if payload.get("hook_event_name") != "SubagentStop":
        return {}
    if not isinstance(payload.get("stop_hook_active"), bool):
        return {}
    if payload["stop_hook_active"]:
        return {}

    agent_type = payload.get("agent_type")
    message = payload.get("last_assistant_message")
    if not isinstance(agent_type, str):
        return {}

    if agent_type == REVIEWER_ROLE:
        header = REVIEW_RESULT_HEADER
        required = REVIEW_RESULT_FIELDS
        enums = REVIEW_RESULT_ENUMS
        patterns = REVIEW_RESULT_PATTERNS
        validators = REVIEW_RESULT_VALIDATORS
    elif agent_type in WORKER_ROLES:
        header = WORK_RESULT_HEADER
        required = WORK_RESULT_FIELDS
        enums = WORK_RESULT_ENUMS
        patterns = WORK_RESULT_PATTERNS
        validators = WORK_RESULT_VALIDATORS
    else:
        return {}

    missing = (
        _missing_fields(
            message,
            header=header,
            required=required,
            enums=enums,
            patterns=patterns,
            validators=validators,
        )
        if isinstance(message, str)
        else [header]
    )
    if not missing:
        return {}
    return {
        "decision": "block",
        "reason": _continuation_reason(header, missing),
    }


def main() -> int:
    outcome: dict[str, str] = {}
    try:
        outcome = _evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception:
        pass

    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

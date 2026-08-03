#!/usr/bin/env python3
"""Create domain-separated hashes for canonical CCO v4 protocol preimages.

The JSON wire format deliberately has a small, local schema.  Keeping the schema
here makes the bytes accepted by the hash helper explicit instead of allowing an
arbitrary JSON object to acquire protocol identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import re
import sys
from typing import Any, Callable
import unicodedata


DOMAINS = (
    "contract",
    "graph_manifest",
    "acceptance_decision",
    "acceptance_chain",
    "input_closure",
    "failure",
    "evidence",
)
HASH_PREFIX = b"cco.protocol-hash.v1\x00"
PROTOCOL = "cco.v4"
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_NESTING_LEVELS = 64
MAX_WORKER_ATTEMPTS = 3
MAX_WORKER_FOLLOWUPS = 2
MAX_REVIEW_ATTEMPTS = 2
MAX_REVIEW_FOLLOWUPS = 2
MAX_REPOSITORY_PATHS = 128

LANES = frozenset({"routine", "complex", "sol"})
WORKER_ROLES = frozenset(
    {
        "cost_orchestrator_routine_worker",
        "cost_orchestrator_complex_worker",
    }
)
POLICIES = frozenset({"user", "route_default", "native"})
FOLLOWUP_TYPES = frozenset({"correction", "verification", "completion"})
CONTRACT_STATUSES = frozenset({"preserved"})
EVIDENCE_OUTCOMES = frozenset({"passed", "failed", "unavailable"})
SCOPE_KINDS = frozenset({"exact", "prefix"})
ACCEPTANCE_MODES = frozenset({"primary", "independent"})
CONTRACT_RISK_FLAGS = frozenset(
    {
        "authentication_authorization",
        "build_release",
        "concurrency",
        "dependency_boundary",
        "destructive_data",
        "external_side_effect",
        "migration",
        "nondeterministic_verification",
        "public_interface",
        "schema",
        "security",
    }
)
INITIAL_INDEPENDENT_REASONS = frozenset(
    {
        "complex_lane",
        "declared_risk",
        "explicit_independent_review",
        "multiple_contracts",
        "sol_owned_change",
    }
)
UPGRADE_REASONS = frozenset(
    {
        "material_judgment",
        "routing_mismatch",
        "scope_surprise",
        "verification_failure",
        "worker_deviation",
        "worker_followup",
        "worker_partial_or_blocked",
        "worker_retry",
    }
)

SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_VALUE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
RUN_VALUE = re.compile(r"^run_([a-z0-9][a-z0-9_]*)_(r[0-9]{2,})$")
LEASE_VALUE = re.compile(r"^wl_([a-z0-9][a-z0-9_]*)_(r[0-9]{2,})$")
ACCEPTANCE_ID = re.compile(r"^A[0-9]{2,}$")
VERIFICATION_ID = re.compile(r"^V[0-9]{2,}$")
FINDING_ID = re.compile(r"^F[0-9]{2,}$")
DIAGNOSTIC_ID = re.compile(r"^D[A-Z0-9_]{1,63}$")
ANCHOR_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
EPOCH_ID = re.compile(r"^e[0-9]{2,}$")
FAILURE_CLASS = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CANONICAL_TASK_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]*)+$")
WIN32_DEVICE_BASENAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$",
    re.IGNORECASE,
)
WIN32_FORBIDDEN_PATH_CHARACTERS = frozenset('<>"|?*')


class ProtocolHashError(Exception):
    """Raised when protocol input cannot be hashed safely."""


def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate or non-ASCII keys."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if not key.isascii():
            raise ProtocolHashError("object keys must be ASCII")
        if key in value:
            raise ProtocolHashError(f"duplicate object key: {key}")
        value[key] = item
    return value


def reject_float(_value: str) -> None:
    raise ProtocolHashError("floating-point numbers are not supported")


def reject_constant(_value: str) -> None:
    raise ProtocolHashError("non-JSON numeric constants are not supported")


def parse_safe_integer(value: str) -> int:
    """Parse only interoperable integers without invoking Python's digit limit."""
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > len(str(MAX_SAFE_INTEGER)):
        raise ProtocolHashError("integer is outside the safe range")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProtocolHashError("integer is outside the safe range") from error
    if not -MAX_SAFE_INTEGER <= parsed <= MAX_SAFE_INTEGER:
        raise ProtocolHashError("integer is outside the safe range")
    return parsed


def validate_structure(value: Any, depth: int = 0) -> None:
    """Reject values whose JSON representation is not protocol-canonical."""
    if depth > MAX_NESTING_LEVELS:
        raise ProtocolHashError(
            f"nesting exceeds {MAX_NESTING_LEVELS} levels"
        )
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise ProtocolHashError("strings must use NFC normalization")
    elif type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ProtocolHashError("integer is outside the safe range")
    elif value is None or type(value) is bool:
        return
    elif isinstance(value, list):
        for item in value:
            validate_structure(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise ProtocolHashError("object keys must be ASCII")
            validate_structure(key, depth + 1)
            validate_structure(item, depth + 1)
    else:
        raise ProtocolHashError("value is not supported by canonical JSON")


def canonical_bytes(value: Any) -> bytes:
    if type(value) is not dict:
        raise ProtocolHashError("input must be a JSON object")
    validate_structure(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProtocolHashError(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any], keys: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    missing = sorted(keys - actual)
    unknown = sorted(actual - keys)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("missing keys: " + ", ".join(missing))
        if unknown:
            parts.append("unknown keys: " + ", ".join(unknown))
        raise ProtocolHashError(f"{label} has invalid keys ({'; '.join(parts)})")


def _require_text(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ProtocolHashError(f"{label} must be a string")
    if not value:
        raise ProtocolHashError(f"{label} must not be empty")
    return value


def _require_integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise ProtocolHashError(f"{label} must be an integer")
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise ProtocolHashError(f"{label} is outside the safe range")
    if value < minimum:
        raise ProtocolHashError(f"{label} must be at least {minimum}")
    return value


def _require_enum(value: Any, values: frozenset[str], label: str) -> str:
    text = _require_text(value, label)
    if text not in values:
        allowed = ", ".join(sorted(values))
        raise ProtocolHashError(f"{label} must be one of: {allowed}")
    return text


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = _require_text(value, label)
    if pattern.fullmatch(text) is None:
        raise ProtocolHashError(f"{label} has an invalid identifier")
    return text


def _require_sha256(value: Any, label: str) -> str:
    return _require_pattern(value, SHA256_VALUE, label)


def _is_ambiguous_win32_segment(segment: str) -> bool:
    device_basename = segment.split(".", 1)[0]
    return (
        segment.endswith((" ", "."))
        or any(character in WIN32_FORBIDDEN_PATH_CHARACTERS for character in segment)
        or WIN32_DEVICE_BASENAME.fullmatch(device_basename) is not None
    )


def require_repository_path(value: Any, label: str) -> str:
    """Require one unambiguous, repository-relative Git path spelling."""
    path = _require_text(value, label)
    segments = path.split("/")
    if (
        unicodedata.normalize("NFC", path) != path
        or
        path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or ":" in path
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(segment.lower() == ".git" for segment in segments)
        or any(_is_ambiguous_win32_segment(segment) for segment in segments)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ProtocolHashError(
            f"{label} must be a canonical repository-relative path"
        )
    return path


def _validate_repository_scope(value: Any, label: str) -> tuple[str, str]:
    scope = _require_object(value, label)
    _require_exact_keys(scope, frozenset({"kind", "path"}), label)
    path = require_repository_path(scope["path"], f"{label}.path")
    kind = _require_enum(scope["kind"], SCOPE_KINDS, f"{label}.kind")
    return path, kind


def require_repository_scope(value: Any, label: str) -> dict[str, str]:
    """Require one explicit exact/prefix repository scope record."""
    path, kind = _validate_repository_scope(value, label)
    return {"kind": kind, "path": path}


def parse_repository_scope_text(value: Any, label: str) -> dict[str, str]:
    """Parse the readable packet/CLI spelling ``kind:path``."""
    text = _require_text(value, label)
    kind, separator, path = text.partition(":")
    if not separator:
        raise ProtocolHashError(f"{label} must use exact:<path> or prefix:<path>")
    return require_repository_scope({"kind": kind, "path": path}, label)


def _require_repository_scopes(
    value: Any,
    label: str,
    *,
    maximum: int,
) -> list[tuple[str, str]]:
    if type(value) is not list:
        raise ProtocolHashError(f"{label} must be an array")
    if len(value) > maximum:
        raise ProtocolHashError(f"{label} must contain at most {maximum} item(s)")
    if any(type(item) is not dict for item in value):
        raise ProtocolHashError(
            f"{label} must contain explicit exact/prefix scope objects"
        )
    scopes = [
        _validate_repository_scope(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(scopes) != len(set(scopes)):
        raise ProtocolHashError(f"{label} must not contain duplicates")
    if scopes != sorted(
        scopes,
        key=lambda item: (_utf8_key(item[0]), _utf8_key(item[1])),
    ):
        raise ProtocolHashError(
            f"{label} must be sorted by path and kind in NFC UTF-8 byte order"
        )
    return scopes


def _portable_scope_path(path: str) -> str:
    """Return a conservative Win32 collision key without widening authorization."""
    return ntpath.normcase(path).replace("\\", "/")


def _repository_scopes_overlap(
    left: tuple[str, str], right: tuple[str, str]
) -> bool:
    left_path, left_kind = left
    right_path, right_kind = right
    left_path = _portable_scope_path(left_path)
    right_path = _portable_scope_path(right_path)
    if left_path == right_path:
        return True
    if left_kind == "prefix" and right_path.startswith(left_path + "/"):
        return True
    if right_kind == "prefix" and left_path.startswith(right_path + "/"):
        return True
    return False


def require_canonical_task_path(value: Any, label: str) -> str:
    """Require the exact absolute task path returned by native spawn."""
    path = _require_text(value, label)
    if CANONICAL_TASK_PATH.fullmatch(path) is None:
        raise ProtocolHashError(f"{label} must be a canonical native task path")
    return path


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _require_sorted_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ProtocolHashError(f"{label} must not contain duplicates")
    if values != sorted(values, key=_utf8_key):
        raise ProtocolHashError(
            f"{label} must be sorted by NFC UTF-8 byte order"
        )


def _require_sorted_unique_strings(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    item_validator: Callable[[Any, str], str] = _require_text,
) -> list[str]:
    if type(value) is not list:
        raise ProtocolHashError(f"{label} must be an array")
    if len(value) < minimum:
        raise ProtocolHashError(f"{label} must contain at least {minimum} item(s)")
    if maximum is not None and len(value) > maximum:
        raise ProtocolHashError(f"{label} must contain at most {maximum} item(s)")
    values = [item_validator(item, f"{label}[{index}]") for index, item in enumerate(value)]
    _require_sorted_unique(values, label)
    return values


def _require_ordered_texts(
    value: Any, label: str, *, minimum: int = 0
) -> list[str]:
    if type(value) is not list:
        raise ProtocolHashError(f"{label} must be an array")
    if len(value) < minimum:
        raise ProtocolHashError(f"{label} must contain at least {minimum} item(s)")
    return [_require_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _require_counter(
    value: Any,
    label: str,
    *,
    minimum_current: int,
    exact_current: int | None = None,
    maximum_limit: int | None = None,
) -> tuple[int, int]:
    counter = _require_object(value, label)
    _require_exact_keys(counter, frozenset({"current", "limit"}), label)
    current = _require_integer(counter["current"], f"{label}.current", minimum=minimum_current)
    limit = _require_integer(counter["limit"], f"{label}.limit", minimum=1)
    if current > limit:
        raise ProtocolHashError(f"{label}.current must not exceed {label}.limit")
    if maximum_limit is not None and limit > maximum_limit:
        raise ProtocolHashError(f"{label}.limit must not exceed {maximum_limit}")
    if exact_current is not None and current != exact_current:
        raise ProtocolHashError(f"{label}.current must equal {exact_current}")
    return current, limit


def _require_fork_turns(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if text == "none":
        return text
    if re.fullmatch(r"[1-9][0-9]*", text) is None:
        raise ProtocolHashError(f"{label} must be none or a positive integer string")
    if len(text) > len(str(MAX_SAFE_INTEGER)) or int(text) > MAX_SAFE_INTEGER:
        raise ProtocolHashError(f"{label} is outside the safe range")
    return text


def _require_sorted_records(
    value: Any,
    label: str,
    *,
    minimum: int,
    validator: Callable[[Any, str], str],
) -> list[str]:
    if type(value) is not list:
        raise ProtocolHashError(f"{label} must be an array")
    if len(value) < minimum:
        raise ProtocolHashError(f"{label} must contain at least {minimum} item(s)")
    identifiers = [validator(item, f"{label}[{index}]") for index, item in enumerate(value)]
    _require_sorted_unique(identifiers, f"{label} record IDs")
    return identifiers


def _validate_acceptance(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(record, frozenset({"criterion", "id"}), label)
    _require_text(record["criterion"], f"{label}.criterion")
    return _require_pattern(record["id"], ACCEPTANCE_ID, f"{label}.id")


def _require_acceptance_records(value: Any, label: str) -> list[str]:
    return _require_sorted_records(
        value, label, minimum=1, validator=_validate_acceptance
    )


def _validate_verification(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(
        record,
        frozenset({"acceptance_ids", "expected", "id", "operation"}),
        label,
    )
    _require_sorted_unique_strings(
        record["acceptance_ids"],
        f"{label}.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    _require_text(record["expected"], f"{label}.expected")
    identifier = _require_pattern(record["id"], VERIFICATION_ID, f"{label}.id")
    _require_text(record["operation"], f"{label}.operation")
    return identifier


def _require_verification_records(
    value: Any, label: str, *, minimum: int = 1
) -> list[str]:
    return _require_sorted_records(
        value, label, minimum=minimum, validator=_validate_verification
    )


def _validate_dependency(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(record, frozenset({"id", "state_sha256"}), label)
    identifier = _require_pattern(record["id"], ANCHOR_ID, f"{label}.id")
    _require_sha256(record["state_sha256"], f"{label}.state_sha256")
    return identifier


def _validate_content_anchor(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(record, frozenset({"content_sha256", "id"}), label)
    _require_sha256(record["content_sha256"], f"{label}.content_sha256")
    return _require_pattern(record["id"], ANCHOR_ID, f"{label}.id")


def _validate_contract_reference(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(
        record, frozenset({"contract_rev", "contract_sha256", "node"}), label
    )
    _require_integer(record["contract_rev"], f"{label}.contract_rev", minimum=1)
    _require_sha256(record["contract_sha256"], f"{label}.contract_sha256")
    return _require_pattern(record["node"], NODE_VALUE, f"{label}.node")


def _validate_resolution(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(record, frozenset({"id", "resolution"}), label)
    identifier = _require_pattern(record["id"], FINDING_ID, f"{label}.id")
    _require_text(record["resolution"], f"{label}.resolution")
    return identifier


def _validate_evidence_record(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(
        record,
        frozenset(
            {
                "acceptance_ids",
                "artifact_sha256s",
                "exit_status",
                "implementation_owner",
                "observed_outcome",
                "operation",
                "outcome",
                "verification_id",
            }
        ),
        label,
    )
    _require_sorted_unique_strings(
        record["acceptance_ids"],
        f"{label}.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    _require_sorted_unique_strings(
        record["artifact_sha256s"],
        f"{label}.artifact_sha256s",
        item_validator=_require_sha256,
    )
    exit_status = record["exit_status"]
    if exit_status is not None:
        _require_integer(
            exit_status, f"{label}.exit_status", minimum=-MAX_SAFE_INTEGER
        )
    _require_pattern(
        record["implementation_owner"], NODE_VALUE, f"{label}.implementation_owner"
    )
    _require_text(record["observed_outcome"], f"{label}.observed_outcome")
    _require_text(record["operation"], f"{label}.operation")
    outcome = _require_enum(
        record["outcome"], EVIDENCE_OUTCOMES, f"{label}.outcome"
    )
    if outcome == "passed" and exit_status not in {None, 0}:
        raise ProtocolHashError(f"{label}.passed outcome requires exit status 0 or null")
    if outcome == "failed" and exit_status == 0:
        raise ProtocolHashError(f"{label}.failed outcome cannot use exit status 0")
    if outcome == "unavailable" and exit_status is not None:
        raise ProtocolHashError(f"{label}.unavailable outcome requires null exit status")
    return _require_pattern(
        record["verification_id"], VERIFICATION_ID, f"{label}.verification_id"
    )


def _validate_policy_pair(
    value: dict[str, Any], policy_key: str, requested_key: str, label: str
) -> None:
    policy = _require_enum(value[policy_key], POLICIES, f"{label}.{policy_key}")
    requested = value[requested_key]
    if policy == "native":
        if requested is not None:
            raise ProtocolHashError(
                f"{label}.{requested_key} must be null when {policy_key} is native"
            )
    else:
        _require_text(requested, f"{label}.{requested_key}")


def _validate_worker_binding(
    value: Any, label: str, *, require_exact_keys: bool = True
) -> None:
    binding = _require_object(value, label)
    if require_exact_keys:
        _require_exact_keys(
            binding,
            frozenset(
                {
                    "acceptance_chain_sha256",
                    "attempt",
                    "acceptance_ids",
                    "baseline",
                    "content_anchors",
                    "contract_rev",
                    "contract_sha256",
                    "dependencies",
                    "effort_policy",
                    "fork_turns",
                    "graph_manifest_sha256",
                    "lease",
                    "lease_generation",
                    "model_policy",
                    "node",
                    "requested_effort",
                    "requested_model",
                    "role",
                    "run",
                    "stop_generation",
                }
            ),
            label,
        )
    _require_sha256(
        binding["acceptance_chain_sha256"],
        f"{label}.acceptance_chain_sha256",
    )
    attempt_current, _attempt_limit = _require_counter(
        binding["attempt"],
        f"{label}.attempt",
        minimum_current=1,
        maximum_limit=MAX_WORKER_ATTEMPTS,
    )
    _require_sorted_unique_strings(
        binding["acceptance_ids"],
        f"{label}.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    _require_sha256(binding["baseline"], f"{label}.baseline")
    _require_sorted_records(
        binding["content_anchors"],
        f"{label}.content_anchors",
        minimum=0,
        validator=_validate_content_anchor,
    )
    _require_integer(binding["contract_rev"], f"{label}.contract_rev", minimum=1)
    _require_sha256(binding["contract_sha256"], f"{label}.contract_sha256")
    _require_sorted_records(
        binding["dependencies"],
        f"{label}.dependencies",
        minimum=0,
        validator=_validate_dependency,
    )
    _validate_policy_pair(
        binding, "effort_policy", "requested_effort", label
    )
    _require_fork_turns(binding["fork_turns"], f"{label}.fork_turns")
    _require_sha256(
        binding["graph_manifest_sha256"], f"{label}.graph_manifest_sha256"
    )
    lease = _require_pattern(binding["lease"], LEASE_VALUE, f"{label}.lease")
    _require_integer(
        binding["lease_generation"], f"{label}.lease_generation", minimum=1
    )
    _validate_policy_pair(binding, "model_policy", "requested_model", label)
    node = _require_pattern(binding["node"], NODE_VALUE, f"{label}.node")
    _require_enum(binding["role"], WORKER_ROLES, f"{label}.role")
    run = _require_pattern(binding["run"], RUN_VALUE, f"{label}.run")
    run_match = RUN_VALUE.fullmatch(run)
    lease_match = LEASE_VALUE.fullmatch(lease)
    if (
        run_match is None
        or lease_match is None
        or run_match.group(1) != node
        or lease_match.groups() != run_match.groups()
    ):
        raise ProtocolHashError(f"{label} run/lease identity is inconsistent")
    run_number = run_match.group(2)[1:]
    if (
        len(run_number) > len(str(MAX_SAFE_INTEGER))
        or int(run_number) != attempt_current
    ):
        raise ProtocolHashError(f"{label} run suffix must match attempt.current")
    if run_match.group(2) != f"r{attempt_current:02d}":
        raise ProtocolHashError(f"{label} run suffix must use canonical rNN spelling")
    _require_integer(
        binding["stop_generation"], f"{label}.stop_generation", minimum=0
    )


def _validate_contract(value: Any) -> None:
    contract = _require_object(value, "contract")
    _require_exact_keys(
        contract,
        frozenset(
            {
                "acceptance",
                "constraints",
                "contract_rev",
                "discretion",
                "exclusions",
                "interfaces",
                "lane",
                "node",
                "objective",
                "protocol",
                "risk_flags",
                "verification",
                "write",
            }
        ),
        "contract",
    )
    acceptance_ids = _require_acceptance_records(contract["acceptance"], "contract.acceptance")
    _require_sorted_unique_strings(contract["constraints"], "contract.constraints")
    _require_integer(contract["contract_rev"], "contract.contract_rev", minimum=1)
    _require_sorted_unique_strings(contract["discretion"], "contract.discretion")
    _require_sorted_unique_strings(contract["exclusions"], "contract.exclusions")
    _require_sorted_unique_strings(contract["interfaces"], "contract.interfaces")
    _require_enum(contract["lane"], LANES, "contract.lane")
    _require_pattern(contract["node"], NODE_VALUE, "contract.node")
    _require_text(contract["objective"], "contract.objective")
    if contract["protocol"] != PROTOCOL:
        raise ProtocolHashError("contract.protocol must equal cco.v4")
    _require_sorted_unique_strings(
        contract["risk_flags"],
        "contract.risk_flags",
        item_validator=lambda item, item_label: _require_enum(
            item, CONTRACT_RISK_FLAGS, item_label
        ),
    )
    _require_verification_records(contract["verification"], "contract.verification")
    verification_ids = {
        identifier
        for record in contract["verification"]
        for identifier in record["acceptance_ids"]
    }
    if verification_ids != set(acceptance_ids):
        raise ProtocolHashError(
            "contract.verification acceptance IDs must cover contract.acceptance"
        )
    _require_repository_scopes(
        contract["write"],
        "contract.write",
        maximum=MAX_REPOSITORY_PATHS,
    )


def _validate_acceptance_owner(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(
        record,
        frozenset({"acceptance_id", "implementation_owner"}),
        label,
    )
    _require_pattern(
        record["implementation_owner"],
        NODE_VALUE,
        f"{label}.implementation_owner",
    )
    return _require_pattern(
        record["acceptance_id"], ACCEPTANCE_ID, f"{label}.acceptance_id"
    )


def _validate_bundled_contract(value: Any, label: str) -> str:
    record = _require_object(value, label)
    _require_exact_keys(record, frozenset({"contract", "contract_sha256"}), label)
    contract = _require_object(record["contract"], f"{label}.contract")
    _validate_contract(contract)
    expected = digest("contract", contract)
    if _require_sha256(
        record["contract_sha256"], f"{label}.contract_sha256"
    ) != expected:
        raise ProtocolHashError(f"{label}.contract_sha256 does not match contract")
    return contract["node"]


def _validate_graph_manifest(value: Any) -> None:
    manifest = _require_object(value, "graph_manifest")
    _require_exact_keys(
        manifest,
        frozenset({"acceptance_owners", "contracts", "protocol"}),
        "graph_manifest",
    )
    if manifest["protocol"] != PROTOCOL:
        raise ProtocolHashError("graph_manifest.protocol must equal cco.v4")
    _require_sorted_records(
        manifest["contracts"],
        "graph_manifest.contracts",
        minimum=1,
        validator=_validate_bundled_contract,
    )
    owner_ids = _require_sorted_records(
        manifest["acceptance_owners"],
        "graph_manifest.acceptance_owners",
        minimum=1,
        validator=_validate_acceptance_owner,
    )
    contract_acceptance_ids = [
        acceptance["id"]
        for record in manifest["contracts"]
        for acceptance in record["contract"]["acceptance"]
    ]
    if len(contract_acceptance_ids) != len(set(contract_acceptance_ids)):
        raise ProtocolHashError("graph_manifest acceptance IDs must be globally unique")
    if owner_ids != sorted(contract_acceptance_ids, key=_utf8_key):
        raise ProtocolHashError(
            "graph_manifest.acceptance_owners must cover every contract acceptance ID"
        )
    declaring_nodes = {
        acceptance["id"]: record["contract"]["node"]
        for record in manifest["contracts"]
        for acceptance in record["contract"]["acceptance"]
    }
    if any(
        owner["implementation_owner"]
        != declaring_nodes[owner["acceptance_id"]]
        for owner in manifest["acceptance_owners"]
    ):
        raise ProtocolHashError(
            "graph_manifest implementation owner must equal the declaring contract node"
        )
    verification_ids = [
        verification["id"]
        for record in manifest["contracts"]
        for verification in record["contract"]["verification"]
    ]
    if len(verification_ids) != len(set(verification_ids)):
        raise ProtocolHashError(
            "graph_manifest verification IDs must be globally unique"
        )
    graph_write_scope_list = [
        scope
        for record in manifest["contracts"]
        for scope in _require_repository_scopes(
            record["contract"]["write"],
            "graph_manifest.contract.write",
            maximum=MAX_REPOSITORY_PATHS,
        )
    ]
    if len(set(graph_write_scope_list)) > MAX_REPOSITORY_PATHS:
        raise ProtocolHashError(
            "graph_manifest write scope must contain at most "
            f"{MAX_REPOSITORY_PATHS} scopes"
        )
    for index, left in enumerate(graph_write_scope_list):
        if any(
            _repository_scopes_overlap(left, right)
            for right in graph_write_scope_list[index + 1 :]
        ):
            raise ProtocolHashError("graph_manifest contains overlapping write scopes")


def _validate_acceptance_decision(value: Any) -> None:
    decision = _require_object(value, "acceptance_decision")
    _require_exact_keys(
        decision,
        frozenset(
            {
                "graph_manifest_sha256",
                "mode",
                "previous_decision_sha256",
                "protocol",
                "reasons",
                "revision",
            }
        ),
        "acceptance_decision",
    )
    _require_sha256(
        decision["graph_manifest_sha256"],
        "acceptance_decision.graph_manifest_sha256",
    )
    mode = _require_enum(
        decision["mode"], ACCEPTANCE_MODES, "acceptance_decision.mode"
    )
    revision = _require_integer(
        decision["revision"], "acceptance_decision.revision", minimum=1
    )
    if revision > 2:
        raise ProtocolHashError("acceptance_decision.revision must not exceed 2")
    previous = decision["previous_decision_sha256"]
    if revision == 1:
        if previous is not None:
            raise ProtocolHashError(
                "acceptance_decision revision 1 requires null previous decision"
            )
        allowed_reasons = INITIAL_INDEPENDENT_REASONS
    else:
        _require_sha256(previous, "acceptance_decision.previous_decision_sha256")
        if mode != "independent":
            raise ProtocolHashError(
                "acceptance_decision revision 2 must upgrade to independent"
            )
        allowed_reasons = UPGRADE_REASONS
    reasons = _require_sorted_unique_strings(
        decision["reasons"], "acceptance_decision.reasons"
    )
    if not set(reasons) <= allowed_reasons:
        raise ProtocolHashError("acceptance_decision contains an invalid reason")
    if mode == "primary" and reasons:
        raise ProtocolHashError("primary acceptance decision must not contain reasons")
    if mode == "independent" and not reasons:
        raise ProtocolHashError("independent acceptance decision requires a reason")
    if decision["protocol"] != PROTOCOL:
        raise ProtocolHashError("acceptance_decision.protocol must equal cco.v4")


def _manifest_independent_reasons(manifest: dict[str, Any]) -> set[str]:
    contracts = [record["contract"] for record in manifest["contracts"]]
    reasons: set[str] = set()
    if len(contracts) > 1:
        reasons.add("multiple_contracts")
    if any(contract["lane"] == "complex" for contract in contracts):
        reasons.add("complex_lane")
    if any(contract["lane"] == "sol" for contract in contracts):
        reasons.add("sol_owned_change")
    if any(contract["risk_flags"] for contract in contracts):
        reasons.add("declared_risk")
    return reasons


def _validate_acceptance_chain(value: Any) -> None:
    chain = _require_object(value, "acceptance_chain")
    _require_exact_keys(
        chain,
        frozenset(
            {"decisions", "graph_manifest", "graph_manifest_sha256", "protocol"}
        ),
        "acceptance_chain",
    )
    manifest = _require_object(chain["graph_manifest"], "acceptance_chain.graph_manifest")
    _validate_graph_manifest(manifest)
    manifest_sha256 = _require_sha256(
        chain["graph_manifest_sha256"], "acceptance_chain.graph_manifest_sha256"
    )
    if manifest_sha256 != digest("graph_manifest", manifest):
        raise ProtocolHashError(
            "acceptance_chain.graph_manifest_sha256 does not match graph manifest"
        )
    if chain["protocol"] != PROTOCOL:
        raise ProtocolHashError("acceptance_chain.protocol must equal cco.v4")
    decisions = chain["decisions"]
    if type(decisions) is not list or not 1 <= len(decisions) <= 2:
        raise ProtocolHashError(
            "acceptance_chain.decisions must contain one or two records"
        )
    prior_sha256: str | None = None
    for index, value_record in enumerate(decisions):
        label = f"acceptance_chain.decisions[{index}]"
        record = _require_object(value_record, label)
        _require_exact_keys(
            record, frozenset({"decision", "decision_sha256"}), label
        )
        decision = _require_object(record["decision"], f"{label}.decision")
        _validate_acceptance_decision(decision)
        expected_sha256 = digest("acceptance_decision", decision)
        if _require_sha256(
            record["decision_sha256"], f"{label}.decision_sha256"
        ) != expected_sha256:
            raise ProtocolHashError(f"{label}.decision_sha256 does not match decision")
        if decision["revision"] != index + 1:
            raise ProtocolHashError(
                "acceptance_chain decision revisions must be consecutive"
            )
        if decision["graph_manifest_sha256"] != manifest_sha256:
            raise ProtocolHashError(
                "acceptance_chain decision graph manifest does not match the chain"
            )
        if decision["previous_decision_sha256"] != prior_sha256:
            raise ProtocolHashError(
                "acceptance_chain previous decision hash is not append-only"
            )
        prior_sha256 = expected_sha256
    if len(decisions) == 2 and decisions[0]["decision"]["mode"] != "primary":
        raise ProtocolHashError(
            "acceptance_chain may append only a primary-to-independent upgrade"
        )
    initial = decisions[0]["decision"]
    structural_reasons = _manifest_independent_reasons(manifest)
    initial_reasons = set(initial["reasons"])
    if initial["mode"] == "primary":
        if structural_reasons:
            raise ProtocolHashError(
                "primary acceptance is ineligible for this graph manifest"
            )
    else:
        allowed_initial = set(structural_reasons)
        allowed_initial.add("explicit_independent_review")
        if (
            not structural_reasons <= initial_reasons
            or not initial_reasons <= allowed_initial
            or (not structural_reasons and initial_reasons != {"explicit_independent_review"})
        ):
            raise ProtocolHashError(
                "initial independent reasons do not match the graph manifest"
            )


def _graph_manifest_requirements(
    manifest: dict[str, Any],
) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
    acceptance_ids = sorted(
        (
            acceptance["id"]
            for record in manifest["contracts"]
            for acceptance in record["contract"]["acceptance"]
        ),
        key=_utf8_key,
    )
    owners = {
        record["acceptance_id"]: record["implementation_owner"]
        for record in manifest["acceptance_owners"]
    }
    verifications = {
        verification["id"]: verification
        for record in manifest["contracts"]
        for verification in record["contract"]["verification"]
    }
    return acceptance_ids, owners, verifications


def _validate_worker_initial(value: Any) -> None:
    packet = _require_object(value, "worker_initial")
    _require_exact_keys(
        packet,
        frozenset(
            {
                "acceptance_chain_sha256",
                "attempt",
                "acceptance_ids",
                "baseline",
                "content_anchors",
                "contract_rev",
                "contract_sha256",
                "dependencies",
                "effort_policy",
                "fork_turns",
                "followup",
                "kind",
                "graph_manifest_sha256",
                "lease",
                "lease_generation",
                "model_policy",
                "node",
                "protocol",
                "requested_effort",
                "requested_model",
                "role",
                "run",
                "stop_generation",
            }
        ),
        "worker_initial",
    )
    if packet["kind"] != "worker_initial":
        raise ProtocolHashError("worker_initial.kind must equal worker_initial")
    if packet["protocol"] != PROTOCOL:
        raise ProtocolHashError("worker_initial.protocol must equal cco.v4")
    _validate_worker_binding(
        packet, "worker_initial", require_exact_keys=False
    )
    _require_counter(
        packet["followup"],
        "worker_initial.followup",
        minimum_current=0,
        exact_current=0,
        maximum_limit=MAX_WORKER_FOLLOWUPS,
    )


def _validate_worker_followup(value: Any) -> None:
    packet = _require_object(value, "worker_followup")
    _require_exact_keys(
        packet,
        frozenset(
            {
                "acceptance_chain_sha256",
                "binding",
                "delta",
                "followup",
                "kind",
                "previous_input_closure_sha256",
                "protocol",
                "target",
                "type",
                "verify",
            }
        ),
        "worker_followup",
    )
    if packet["kind"] != "worker_followup":
        raise ProtocolHashError("worker_followup.kind must equal worker_followup")
    if packet["protocol"] != PROTOCOL:
        raise ProtocolHashError("worker_followup.protocol must equal cco.v4")
    _require_sha256(
        packet["acceptance_chain_sha256"],
        "worker_followup.acceptance_chain_sha256",
    )
    _validate_worker_binding(packet["binding"], "worker_followup.binding")
    _require_ordered_texts(packet["delta"], "worker_followup.delta", minimum=1)
    _require_counter(
        packet["followup"],
        "worker_followup.followup",
        minimum_current=1,
        maximum_limit=MAX_WORKER_FOLLOWUPS,
    )
    _require_sha256(
        packet["previous_input_closure_sha256"],
        "worker_followup.previous_input_closure_sha256",
    )
    require_canonical_task_path(packet["target"], "worker_followup.target")
    _require_enum(packet["type"], FOLLOWUP_TYPES, "worker_followup.type")
    _require_verification_records(
        packet["verify"], "worker_followup.verify", minimum=0
    )
    declared = set(packet["binding"]["acceptance_ids"])
    if any(
        not set(record["acceptance_ids"]) <= declared
        for record in packet["verify"]
    ):
        raise ProtocolHashError(
            "worker_followup.verify acceptance IDs must stay inside the binding"
        )


def _validate_review_fresh(value: Any) -> None:
    packet = _require_object(value, "review_fresh")
    _require_exact_keys(
        packet,
        frozenset(
            {
                "acceptance",
                "acceptance_ids",
                "accumulated_delta",
                "allowed_paths",
                "attempt",
                "baseline",
                "acceptance_chain_sha256",
                "contracts",
                "current_state",
                "epoch",
                "evidence_sha256",
                "followup",
                "fork_turns",
                "goal",
                "graph_manifest_sha256",
                "interfaces",
                "kind",
                "open_risks",
                "protocol",
            }
        ),
        "review_fresh",
    )
    if packet["kind"] != "review_fresh":
        raise ProtocolHashError("review_fresh.kind must equal review_fresh")
    if packet["protocol"] != PROTOCOL:
        raise ProtocolHashError("review_fresh.protocol must equal cco.v4")
    _require_pattern(packet["epoch"], EPOCH_ID, "review_fresh.epoch")
    acceptance_ids = _require_acceptance_records(packet["acceptance"], "review_fresh.acceptance")
    packet_ids = _require_sorted_unique_strings(
        packet["acceptance_ids"],
        "review_fresh.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    if packet_ids != acceptance_ids:
        raise ProtocolHashError(
            "review_fresh.acceptance_ids must match review_fresh.acceptance"
        )
    _require_ordered_texts(
        packet["accumulated_delta"], "review_fresh.accumulated_delta", minimum=1
    )
    _require_repository_scopes(
        packet["allowed_paths"],
        "review_fresh.allowed_paths",
        maximum=MAX_REPOSITORY_PATHS,
    )
    _require_counter(
        packet["attempt"],
        "review_fresh.attempt",
        minimum_current=1,
        maximum_limit=MAX_REVIEW_ATTEMPTS,
    )
    _require_sha256(packet["baseline"], "review_fresh.baseline")
    _require_sha256(
        packet["acceptance_chain_sha256"], "review_fresh.acceptance_chain_sha256"
    )
    _require_sorted_records(
        packet["contracts"],
        "review_fresh.contracts",
        minimum=1,
        validator=_validate_contract_reference,
    )
    _require_sha256(packet["current_state"], "review_fresh.current_state")
    _require_sha256(packet["evidence_sha256"], "review_fresh.evidence_sha256")
    _require_counter(
        packet["followup"],
        "review_fresh.followup",
        minimum_current=0,
        exact_current=0,
        maximum_limit=MAX_REVIEW_FOLLOWUPS,
    )
    if _require_fork_turns(packet["fork_turns"], "review_fresh.fork_turns") != "none":
        raise ProtocolHashError("review_fresh.fork_turns must equal none")
    _require_text(packet["goal"], "review_fresh.goal")
    _require_sha256(
        packet["graph_manifest_sha256"], "review_fresh.graph_manifest_sha256"
    )
    _require_sorted_unique_strings(packet["interfaces"], "review_fresh.interfaces")
    _require_sorted_unique_strings(packet["open_risks"], "review_fresh.open_risks")


def _validate_review_delta(value: Any) -> None:
    packet = _require_object(value, "review_delta")
    _require_exact_keys(
        packet,
        frozenset(
            {
                "acceptance_ids",
                "acceptance_chain_sha256",
                "attempt",
                "contract_status",
                "contracts",
                "current_state",
                "delta",
                "epoch",
                "evidence_sha256",
                "followup",
                "graph_manifest_sha256",
                "kind",
                "open_risks",
                "previous_input_closure_sha256",
                "prior_reviewed_state",
                "protocol",
                "resolves",
                "target",
            }
        ),
        "review_delta",
    )
    if packet["kind"] != "review_delta":
        raise ProtocolHashError("review_delta.kind must equal review_delta")
    if packet["protocol"] != PROTOCOL:
        raise ProtocolHashError("review_delta.protocol must equal cco.v4")
    _require_pattern(packet["epoch"], EPOCH_ID, "review_delta.epoch")
    _require_sorted_unique_strings(
        packet["acceptance_ids"],
        "review_delta.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    _require_counter(
        packet["attempt"],
        "review_delta.attempt",
        minimum_current=1,
        maximum_limit=MAX_REVIEW_ATTEMPTS,
    )
    _require_sha256(
        packet["acceptance_chain_sha256"], "review_delta.acceptance_chain_sha256"
    )
    _require_enum(
        packet["contract_status"], CONTRACT_STATUSES, "review_delta.contract_status"
    )
    _require_sorted_records(
        packet["contracts"],
        "review_delta.contracts",
        minimum=1,
        validator=_validate_contract_reference,
    )
    _require_sha256(packet["current_state"], "review_delta.current_state")
    _require_ordered_texts(packet["delta"], "review_delta.delta", minimum=1)
    _require_sha256(packet["evidence_sha256"], "review_delta.evidence_sha256")
    _require_counter(
        packet["followup"],
        "review_delta.followup",
        minimum_current=1,
        maximum_limit=MAX_REVIEW_FOLLOWUPS,
    )
    _require_sha256(
        packet["graph_manifest_sha256"], "review_delta.graph_manifest_sha256"
    )
    _require_sorted_unique_strings(packet["open_risks"], "review_delta.open_risks")
    _require_sha256(
        packet["previous_input_closure_sha256"],
        "review_delta.previous_input_closure_sha256",
    )
    _require_sha256(
        packet["prior_reviewed_state"], "review_delta.prior_reviewed_state"
    )
    require_canonical_task_path(packet["target"], "review_delta.target")
    _require_sorted_records(
        packet["resolves"],
        "review_delta.resolves",
        minimum=1,
        validator=_validate_resolution,
    )


def _validate_failure(value: Any) -> None:
    failure = _require_object(value, "failure")
    _require_exact_keys(
        failure,
        frozenset(
            {
                "acceptance_or_verification_id",
                "contract_sha256",
                "diagnostic_ids",
                "exit_status",
                "failure_class",
                "node",
                "protocol",
            }
        ),
        "failure",
    )
    _require_pattern(
        failure["acceptance_or_verification_id"],
        re.compile(r"^[AV][0-9]{2,}$"),
        "failure.acceptance_or_verification_id",
    )
    _require_sha256(failure["contract_sha256"], "failure.contract_sha256")
    _require_sorted_unique_strings(
        failure["diagnostic_ids"],
        "failure.diagnostic_ids",
        item_validator=lambda item, item_label: _require_pattern(
            item, DIAGNOSTIC_ID, item_label
        ),
    )
    if failure["exit_status"] is not None:
        _require_integer(failure["exit_status"], "failure.exit_status", minimum=-MAX_SAFE_INTEGER)
    failure_class = _require_pattern(
        failure["failure_class"], FAILURE_CLASS, "failure.failure_class"
    )
    if failure_class == "none":
        raise ProtocolHashError(
            "failure.failure_class must name an observed failure"
        )
    _require_pattern(failure["node"], NODE_VALUE, "failure.node")
    if failure["protocol"] != PROTOCOL:
        raise ProtocolHashError("failure.protocol must equal cco.v4")


def _validate_evidence(value: Any) -> None:
    evidence = _require_object(value, "evidence")
    _require_exact_keys(
        evidence,
        frozenset(
            {
                "acceptance_ids",
                "acceptance_chain",
                "acceptance_chain_sha256",
                "current_state",
                "protocol",
                "records",
            }
        ),
        "evidence",
    )
    acceptance_ids = _require_sorted_unique_strings(
        evidence["acceptance_ids"],
        "evidence.acceptance_ids",
        minimum=1,
        item_validator=lambda item, item_label: _require_pattern(
            item, ACCEPTANCE_ID, item_label
        ),
    )
    acceptance_chain = _require_object(
        evidence["acceptance_chain"], "evidence.acceptance_chain"
    )
    _validate_acceptance_chain(acceptance_chain)
    if _require_sha256(
        evidence["acceptance_chain_sha256"], "evidence.acceptance_chain_sha256"
    ) != digest("acceptance_chain", acceptance_chain):
        raise ProtocolHashError(
            "evidence.acceptance_chain_sha256 does not match acceptance chain"
        )
    _require_sha256(evidence["current_state"], "evidence.current_state")
    if evidence["protocol"] != PROTOCOL:
        raise ProtocolHashError("evidence.protocol must equal cco.v4")
    record_ids = _require_sorted_records(
        evidence["records"],
        "evidence.records",
        minimum=1,
        validator=_validate_evidence_record,
    )
    graph_manifest = acceptance_chain["graph_manifest"]
    manifest_acceptance_ids, manifest_owners, manifest_verifications = (
        _graph_manifest_requirements(graph_manifest)
    )
    if acceptance_ids != manifest_acceptance_ids:
        raise ProtocolHashError(
            "evidence.acceptance_ids must match the graph manifest"
        )
    if record_ids != sorted(manifest_verifications, key=_utf8_key):
        raise ProtocolHashError(
            "evidence.records must cover every contract verification exactly once"
        )
    for record in evidence["records"]:
        required_verification = manifest_verifications[record["verification_id"]]
        if record["acceptance_ids"] != required_verification["acceptance_ids"]:
            raise ProtocolHashError(
                "evidence record acceptance IDs must match its contract verification"
            )
        if record["operation"] != required_verification["operation"]:
            raise ProtocolHashError(
                "evidence record operation must match its contract verification"
            )
        expected_owners = {
            manifest_owners[identifier] for identifier in record["acceptance_ids"]
        }
        if len(expected_owners) != 1 or record["implementation_owner"] not in expected_owners:
            raise ProtocolHashError(
                "evidence record implementation owner must match the graph manifest"
            )
    covered = {
        identifier
        for record in evidence["records"]
        for identifier in record["acceptance_ids"]
    }
    expected = set(acceptance_ids)
    if not covered <= expected:
        raise ProtocolHashError(
            "evidence.records acceptance IDs must be declared by evidence.acceptance_ids"
        )
    if covered != expected:
        raise ProtocolHashError(
            "evidence.records must cover every evidence.acceptance_ids entry"
        )
    owners: dict[str, str] = {}
    for record in evidence["records"]:
        owner = record["implementation_owner"]
        for identifier in record["acceptance_ids"]:
            prior = owners.setdefault(identifier, owner)
            if prior != owner:
                raise ProtocolHashError(
                    "evidence acceptance ID has conflicting implementation owner"
                )


def validate_preimage(domain: str, value: Any) -> None:
    """Validate one complete CCO v4 preimage for its hash domain."""
    if domain not in DOMAINS:
        raise ProtocolHashError(f"unsupported hash domain: {domain}")
    if type(value) is not dict:
        raise ProtocolHashError("input must be a JSON object")
    validate_structure(value)
    if domain == "contract":
        _validate_contract(value)
    elif domain == "graph_manifest":
        _validate_graph_manifest(value)
    elif domain == "acceptance_decision":
        _validate_acceptance_decision(value)
    elif domain == "acceptance_chain":
        _validate_acceptance_chain(value)
    elif domain == "failure":
        _validate_failure(value)
    elif domain == "evidence":
        _validate_evidence(value)
    else:
        kind = value.get("kind")
        if kind == "worker_initial":
            _validate_worker_initial(value)
        elif kind == "worker_followup":
            _validate_worker_followup(value)
        elif kind == "review_fresh":
            _validate_review_fresh(value)
        elif kind == "review_delta":
            _validate_review_delta(value)
        else:
            raise ProtocolHashError(
                "input_closure.kind must be worker_initial, worker_followup, "
                "review_fresh, or review_delta"
            )


def digest(domain: str, value: Any) -> str:
    validate_preimage(domain, value)
    canonical = canonical_bytes(value)
    if len(canonical) > MAX_INPUT_BYTES:
        raise ProtocolHashError(
            f"canonical input exceeds {MAX_INPUT_BYTES} bytes"
        )
    payload = HASH_PREFIX + domain.encode("ascii") + b"\x00" + canonical
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Hash one canonical CCO v4 protocol JSON object from standard input."
    )
    commands = root.add_subparsers(dest="command", required=True)
    hash_command = commands.add_parser("hash")
    hash_command.add_argument("--domain", choices=DOMAINS, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ProtocolHashError(f"input exceeds {MAX_INPUT_BYTES} bytes")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=parse_safe_integer,
        )
        result = digest(args.domain, value)
    except (
        OSError,
        UnicodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
        ProtocolHashError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

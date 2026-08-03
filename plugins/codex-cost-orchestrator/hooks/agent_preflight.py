#!/usr/bin/env python3
"""Read-only, fail-open structural preflight for native CCO spawns."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

from protocol_envelope import EnvelopeError, load_utf8_json, parse_envelope


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from protocol_hash import (  # noqa: E402
    ProtocolHashError,
    canonical_bytes,
    digest as protocol_digest,
    object_from_pairs,
    parse_safe_integer,
    parse_repository_scope_text,
    require_canonical_task_path,
    reject_constant,
    reject_float,
)
from routing_catalog import (  # noqa: E402
    RoutingCatalogError,
    canonical_bytes as routing_canonical_bytes,
    load_json_bytes as load_routing_json_bytes,
    validate_route_decision,
)
from workspace_state import (  # noqa: E402
    StateError,
    repository_control_roots,
    repository_gitlinks,
    repository_index_records,
    repository_path_spelling_map,
    repository_root,
    validate_repository_lease_path,
)


WORKER_ROLES = {
    "cost_orchestrator_routine_worker": "routine",
    "cost_orchestrator_complex_worker": "complex",
}
REVIEWER_ROLE = "cost_orchestrator_reviewer"
WORK_HEADER = "CCO_WORK cco.v4"
REVIEW_HEADER = "CCO_REVIEW cco.v4"
WORK_FOLLOWUP_HEADER = "CCO_WORK_FOLLOWUP cco.v4"
REVIEW_DELTA_HEADER = "CCO_REVIEW_DELTA cco.v4"
WORK_FIELDS = (
    "NODE",
    "CONTRACT_REV",
    "CONTRACT_SHA256",
    "INPUT_CLOSURE_SHA256",
    "GRAPH_MANIFEST_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "ACCEPTANCE_CHAIN_JSON",
    "LANE",
    "ROLE",
    "RUN",
    "ATTEMPT",
    "FOLLOWUP",
    "FORK_TURNS",
    "BASELINE",
    "LEASE",
    "LEASE_GENERATION",
    "STOP_GENERATION",
    "MODEL_POLICY",
    "REQUESTED_MODEL",
    "EFFORT_POLICY",
    "REQUESTED_EFFORT",
    "ROUTING_DECISION_JSON",
    "ACCEPTANCE_IDS",
    "WRITE",
    "OBJECTIVE",
    "INTERFACES",
    "DISCRETION",
    "CONSTRAINTS",
    "EXCLUSIONS",
    "RISK_FLAGS",
    "DEPENDENCIES",
    "INPUTS",
    "ACCEPTANCE",
    "VERIFY",
)
WORK_LIST_FIELDS = frozenset(
    {
        "WRITE",
        "INTERFACES",
        "DISCRETION",
        "CONSTRAINTS",
        "EXCLUSIONS",
        "RISK_FLAGS",
        "DEPENDENCIES",
        "INPUTS",
        "ACCEPTANCE",
        "VERIFY",
    }
)
REVIEW_FIELDS = (
    "EPOCH",
    "MODE",
    "ATTEMPT",
    "FOLLOWUP",
    "FORK_TURNS",
    "INPUT_CLOSURE_SHA256",
    "GRAPH_MANIFEST_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "CONTRACTS",
    "GOAL",
    "ACCEPTANCE_IDS",
    "ACCEPTANCE",
    "INTERFACES",
    "BASELINE",
    "CURRENT_STATE",
    "ALLOWED_PATHS",
    "ACCUMULATED_DELTA",
    "EVIDENCE_SHA256",
    "EVIDENCE_JSON",
    "OPEN_RISKS",
)
REVIEW_LIST_FIELDS = frozenset(
    {
        "CONTRACTS",
        "ACCEPTANCE",
        "INTERFACES",
        "ALLOWED_PATHS",
        "ACCUMULATED_DELTA",
        "OPEN_RISKS",
    }
)
WORK_FOLLOWUP_FIELDS = (
    "NODE",
    "CONTRACT_REV",
    "CONTRACT_SHA256",
    "PREVIOUS_INPUT_CLOSURE_SHA256",
    "INPUT_CLOSURE_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "ACCEPTANCE_CHAIN_JSON",
    "BINDING_JSON",
    "TARGET",
    "RUN",
    "ATTEMPT",
    "FOLLOWUP",
    "LEASE",
    "LEASE_GENERATION",
    "STOP_GENERATION",
    "ACCEPTANCE_IDS",
    "TYPE",
    "DELTA",
    "VERIFY",
)
WORK_FOLLOWUP_LIST_FIELDS = frozenset({"DELTA", "VERIFY"})
REVIEW_DELTA_FIELDS = (
    "EPOCH",
    "MODE",
    "ATTEMPT",
    "FOLLOWUP",
    "PREVIOUS_INPUT_CLOSURE_SHA256",
    "INPUT_CLOSURE_SHA256",
    "TARGET",
    "PRIOR_REVIEWED_STATE",
    "CURRENT_STATE",
    "CONTRACT_STATUS",
    "GRAPH_MANIFEST_SHA256",
    "ACCEPTANCE_CHAIN_SHA256",
    "CONTRACTS",
    "ACCEPTANCE_IDS",
    "EVIDENCE_SHA256",
    "RESOLVES",
    "DELTA",
    "EVIDENCE_JSON",
    "OPEN_RISKS",
)
REVIEW_DELTA_LIST_FIELDS = frozenset(
    {"CONTRACTS", "RESOLVES", "DELTA", "OPEN_RISKS"}
)
SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_VALUE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
RUN_VALUE = re.compile(r"^run_([a-z0-9][a-z0-9_]*)_(r0[1-3])$")
WORK_TASK_VALUE = re.compile(
    r"^work_[a-z0-9][a-z0-9_]*_(?:routine|complex)_r0[1-3]$"
)
REVIEW_TASK_VALUE = re.compile(r"^review_e[0-9]{2,}_r0[1-2]$")
EPOCH_VALUE = re.compile(r"^e[0-9]{2,}$")
ACCEPTANCE_LINE = re.compile(r"^- (A[0-9]{2,}): .+$")
CONTRACT_REFERENCE_LINE = re.compile(
    r"^- ([a-z0-9][a-z0-9_]*)@([1-9][0-9]*)#(sha256:[0-9a-f]{64})$"
)
RESOLUTION_LINE = re.compile(r"^- (F[0-9]{2,}): (.+)$")
VERIFY_LINE = re.compile(
    r"^- (V[0-9]{2,}) (\[A[0-9]{2,}(?:,A[0-9]{2,})*\]): (.+) => (.+)$"
)
ANCHOR_LINE = re.compile(
    r"^- ([A-Za-z][A-Za-z0-9_.:-]*)#(sha256:[0-9a-f]{64})$"
)
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_SAFE_INTEGER_DIGITS = len(str(MAX_SAFE_INTEGER))
MAX_WORKER_ATTEMPTS = 3
MAX_WORKER_FOLLOWUPS = 2
MAX_REVIEW_ATTEMPTS = 2
MAX_REVIEW_FOLLOWUPS = 2
MAX_REPOSITORY_PATHS = 128
SPAWN_INPUT_FIELDS = frozenset(
    {"agent_type", "fork_turns", "message", "model", "reasoning_effort", "task_name"}
)


class PacketError(Exception):
    pass


def block_outcome() -> dict[str, str]:
    return {
        "decision": "block",
        "reason": "CCO native spawn failed structural preflight; repair the packet or routing request.",
    }


def is_reserved_cco_dispatch(tool_input: dict[str, Any]) -> bool:
    task_name = tool_input.get("task_name")
    role = tool_input.get("agent_type")
    message = tool_input.get("message")
    if isinstance(task_name, str) and (
        WORK_TASK_VALUE.fullmatch(task_name) is not None
        or REVIEW_TASK_VALUE.fullmatch(task_name) is not None
    ):
        return True
    if isinstance(role, str) and role.startswith("cost_orchestrator_"):
        return True
    return isinstance(message, str) and re.search(
        r"(?:^|[\r\n])[ \t]*CCO_[A-Z_]+ cco\.v4(?=$|[\r\n \t])",
        message,
    ) is not None


def validate_spawn_input_shape(tool_input: dict[str, Any]) -> None:
    if set(tool_input) - SPAWN_INPUT_FIELDS:
        raise PacketError("spawn input contains an unsupported override")
    if not {"agent_type", "fork_turns", "message", "task_name"} <= set(tool_input):
        raise PacketError("spawn input is incomplete")


def parse_packet(
    message: Any,
    *,
    header: str,
    required: tuple[str, ...],
    list_fields: frozenset[str],
) -> dict[str, str]:
    try:
        return parse_envelope(
            message,
            header=header,
            required=required,
            list_fields=list_fields,
        )
    except EnvelopeError as error:
        raise PacketError(str(error)) from error


def integer(value: str, *, minimum: int) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise PacketError("integer is invalid")
    if len(value) > MAX_SAFE_INTEGER_DIGITS:
        raise PacketError("integer is out of range")
    parsed = int(value)
    if not minimum <= parsed <= MAX_SAFE_INTEGER:
        raise PacketError("integer is out of range")
    return parsed


def counter(
    value: str,
    *,
    expected_current: int | None = None,
    minimum_current: int = 0,
    maximum_limit: int,
) -> tuple[int, int]:
    match = re.fullmatch(r"(0|[1-9][0-9]*)/([1-9][0-9]*)", value)
    if match is None:
        raise PacketError("counter is invalid")
    parts = match.groups()
    if any(len(part) > MAX_SAFE_INTEGER_DIGITS for part in parts):
        raise PacketError("counter is out of range")
    current, limit = (int(part) for part in parts)
    if current < minimum_current or current > limit or limit > maximum_limit:
        raise PacketError("counter is out of range")
    if expected_current is not None and current != expected_current:
        raise PacketError("counter is stale")
    return current, limit


def acceptance_ids(value: str) -> list[str]:
    if re.fullmatch(r"\[A[0-9]{2,}(?:,A[0-9]{2,})*\]", value) is None:
        raise PacketError("acceptance IDs are invalid")
    identifiers = value[1:-1].split(",")
    if identifiers != sorted(set(identifiers)):
        raise PacketError("acceptance IDs are not canonical")
    return identifiers


def bullet_values(value: str, *, allow_none: bool = False) -> list[str]:
    values = [line[2:] for line in value.split("\n")]
    if allow_none and values == ["none"]:
        return []
    if "none" in values:
        raise PacketError("none must be the only list item")
    return values


def parse_contract_references(value: str) -> list[dict[str, object]]:
    nodes: list[str] = []
    records: list[dict[str, object]] = []
    for line in value.split("\n"):
        match = CONTRACT_REFERENCE_LINE.fullmatch(line)
        if match is None:
            raise PacketError("contract reference is invalid")
        node, revision, _digest = match.groups()
        revision_value = integer(revision, minimum=1)
        nodes.append(node)
        records.append(
            {
                "contract_rev": revision_value,
                "contract_sha256": match.group(3),
                "node": node,
            }
        )
    if nodes != sorted(set(nodes), key=lambda item: item.encode("utf-8")):
        raise PacketError("contract references are not canonical")
    return records


def validate_repository_paths(
    value: str,
    label: str,
    *,
    allow_none: bool = False,
    active_root: Path | None = None,
    protected_roots: tuple[Path, ...] | None = None,
) -> list[dict[str, str]]:
    scopes: list[dict[str, str]] = []
    try:
        for index, item in enumerate(bullet_values(value, allow_none=allow_none)):
            scopes.append(
                parse_repository_scope_text(item, f"{label}[{index}]")
            )
    except ProtocolHashError as error:
        raise PacketError(f"{label} contains an invalid scope") from error
    identities = [(scope["path"], scope["kind"]) for scope in scopes]
    if len(identities) != len(set(identities)) or identities != sorted(
        identities,
        key=lambda item: (item[0].encode("utf-8"), item[1].encode("utf-8")),
    ):
        raise PacketError(f"{label} scopes are not canonical")
    if len(scopes) > MAX_REPOSITORY_PATHS:
        raise PacketError(f"{label} contains too many scopes")
    try:
        root = repository_root(Path.cwd()) if active_root is None else active_root
        control_roots = (
            repository_control_roots(root)
            if protected_roots is None
            else protected_roots
        )
        index_records = repository_index_records(root)
        gitlinks = repository_gitlinks(root, index_records)
        tracked_spellings = repository_path_spelling_map(index_records)
        directory_spellings: dict[str, frozenset[str]] = {}
        for scope in scopes:
            validate_repository_lease_path(
                root,
                scope["path"],
                scope_kind=scope["kind"],
                protected_roots=control_roots,
                gitlinks=gitlinks,
                tracked_spellings=tracked_spellings,
                directory_spellings=directory_spellings,
            )
    except (OSError, StateError) as error:
        raise PacketError(f"{label} scope is unsafe in the active repository") from error
    return scopes


def validate_acceptance_and_verify(fields: dict[str, str]) -> None:
    expected = acceptance_ids(fields["ACCEPTANCE_IDS"])
    criteria = parse_acceptance_records(fields["ACCEPTANCE"])
    if [record["id"] for record in criteria] != expected:
        raise PacketError("acceptance criteria are incomplete")

    verification_ids: list[str] = []
    covered: set[str] = set()
    for record in parse_verification_records(fields["VERIFY"]):
        verification_id = str(record["id"])
        identifiers = record["acceptance_ids"]
        if not isinstance(identifiers, list):
            raise PacketError("verification mapping is invalid")
        if not set(identifiers) <= set(expected):
            raise PacketError("verification is outside acceptance")
        verification_ids.append(verification_id)
        covered.update(identifiers)
    if verification_ids != sorted(set(verification_ids)) or covered != set(expected):
        raise PacketError("verification mapping is incomplete")


def parse_acceptance_records(value: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in value.split("\n"):
        match = ACCEPTANCE_LINE.fullmatch(line)
        if match is None:
            raise PacketError("acceptance criteria are invalid")
        identifier = match.group(1)
        records.append({"criterion": line[len(f"- {identifier}: ") :], "id": identifier})
    return records


def parse_verification_records(
    value: str, *, allow_none: bool = False
) -> list[dict[str, object]]:
    if allow_none and bullet_values(value, allow_none=True) == []:
        return []
    records: list[dict[str, object]] = []
    for line in value.split("\n"):
        match = VERIFY_LINE.fullmatch(line)
        if match is None:
            raise PacketError("verification mapping is invalid")
        identifier, ids_value, operation, expected = match.groups()
        records.append(
            {
                "acceptance_ids": acceptance_ids(ids_value),
                "expected": expected,
                "id": identifier,
                "operation": operation,
            }
        )
    return records


def parse_anchor_records(value: str, hash_key: str) -> list[dict[str, str]]:
    if bullet_values(value, allow_none=True) == []:
        return []
    records: list[dict[str, str]] = []
    for line in value.split("\n"):
        match = ANCHOR_LINE.fullmatch(line)
        if match is None:
            raise PacketError("anchor mapping is invalid")
        identifier, digest_value = match.groups()
        records.append({hash_key: digest_value, "id": identifier})
    return records


def parse_counter(
    value: str, *, minimum_current: int, maximum_limit: int
) -> dict[str, int]:
    current, limit = counter(
        value, minimum_current=minimum_current, maximum_limit=maximum_limit
    )
    return {"current": current, "limit": limit}


def requested_value(value: str) -> str | None:
    return None if value == "none" else value


def validate_fork_turns(value: object) -> str:
    if value == "none":
        return "none"
    if not isinstance(value, str):
        raise PacketError("fork policy is invalid")
    integer(value, minimum=1)
    return value


def worker_contract_preimage(
    fields: dict[str, str], *, write_paths: list[dict[str, str]] | None = None
) -> dict[str, object]:
    return {
        "acceptance": parse_acceptance_records(fields["ACCEPTANCE"]),
        "constraints": bullet_values(fields["CONSTRAINTS"], allow_none=True),
        "contract_rev": integer(fields["CONTRACT_REV"], minimum=1),
        "discretion": bullet_values(fields["DISCRETION"], allow_none=True),
        "exclusions": bullet_values(fields["EXCLUSIONS"], allow_none=True),
        "interfaces": bullet_values(fields["INTERFACES"], allow_none=True),
        "lane": fields["LANE"],
        "node": fields["NODE"],
        "objective": fields["OBJECTIVE"],
        "protocol": "cco.v4",
        "risk_flags": bullet_values(fields["RISK_FLAGS"], allow_none=True),
        "verification": parse_verification_records(fields["VERIFY"]),
        "write": (
            validate_repository_paths(fields["WRITE"], "WRITE", allow_none=True)
            if write_paths is None
            else write_paths
        ),
    }


def worker_input_preimage(fields: dict[str, str]) -> dict[str, object]:
    return {
        "acceptance_chain_sha256": fields["ACCEPTANCE_CHAIN_SHA256"],
        "attempt": parse_counter(
            fields["ATTEMPT"],
            minimum_current=1,
            maximum_limit=MAX_WORKER_ATTEMPTS,
        ),
        "acceptance_ids": acceptance_ids(fields["ACCEPTANCE_IDS"]),
        "baseline": fields["BASELINE"],
        "content_anchors": parse_anchor_records(fields["INPUTS"], "content_sha256"),
        "contract_rev": integer(fields["CONTRACT_REV"], minimum=1),
        "contract_sha256": fields["CONTRACT_SHA256"],
        "dependencies": parse_anchor_records(fields["DEPENDENCIES"], "state_sha256"),
        "effort_policy": fields["EFFORT_POLICY"],
        "followup": parse_counter(
            fields["FOLLOWUP"],
            minimum_current=0,
            maximum_limit=MAX_WORKER_FOLLOWUPS,
        ),
        "fork_turns": validate_fork_turns(fields["FORK_TURNS"]),
        "graph_manifest_sha256": fields["GRAPH_MANIFEST_SHA256"],
        "kind": "worker_initial",
        "lease": fields["LEASE"],
        "lease_generation": integer(fields["LEASE_GENERATION"], minimum=1),
        "model_policy": fields["MODEL_POLICY"],
        "node": fields["NODE"],
        "protocol": "cco.v4",
        "requested_effort": requested_value(fields["REQUESTED_EFFORT"]),
        "requested_model": requested_value(fields["REQUESTED_MODEL"]),
        "role": fields["ROLE"],
        "run": fields["RUN"],
        "stop_generation": integer(fields["STOP_GENERATION"], minimum=0),
    }


def review_input_preimage(
    fields: dict[str, str], *, allowed_paths: list[dict[str, str]] | None = None
) -> dict[str, object]:
    return {
        "acceptance": parse_acceptance_records(fields["ACCEPTANCE"]),
        "acceptance_ids": acceptance_ids(fields["ACCEPTANCE_IDS"]),
        "accumulated_delta": bullet_values(fields["ACCUMULATED_DELTA"]),
        "allowed_paths": (
            validate_repository_paths(
                fields["ALLOWED_PATHS"], "ALLOWED_PATHS", allow_none=True
            )
            if allowed_paths is None
            else allowed_paths
        ),
        "attempt": parse_counter(
            fields["ATTEMPT"],
            minimum_current=1,
            maximum_limit=MAX_REVIEW_ATTEMPTS,
        ),
        "acceptance_chain_sha256": fields["ACCEPTANCE_CHAIN_SHA256"],
        "baseline": fields["BASELINE"],
        "contracts": parse_contract_references(fields["CONTRACTS"]),
        "current_state": fields["CURRENT_STATE"],
        "epoch": fields["EPOCH"],
        "evidence_sha256": fields["EVIDENCE_SHA256"],
        "followup": parse_counter(
            fields["FOLLOWUP"],
            minimum_current=0,
            maximum_limit=MAX_REVIEW_FOLLOWUPS,
        ),
        "fork_turns": validate_fork_turns(fields["FORK_TURNS"]),
        "goal": fields["GOAL"],
        "graph_manifest_sha256": fields["GRAPH_MANIFEST_SHA256"],
        "interfaces": bullet_values(fields["INTERFACES"], allow_none=True),
        "kind": "review_fresh",
        "open_risks": bullet_values(fields["OPEN_RISKS"], allow_none=True),
        "protocol": "cco.v4",
    }


def parse_canonical_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
            parse_int=parse_safe_integer,
        )
        if canonical_bytes(parsed).decode("utf-8") != value:
            raise PacketError(f"{label} is not canonical")
    except (
        TypeError,
        UnicodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
        ProtocolHashError,
    ) as error:
        raise PacketError(f"{label} is invalid") from error
    return parsed


def validate_worker_acceptance_chain(
    fields: dict[str, str], contract: dict[str, object]
) -> None:
    chain = parse_canonical_object(
        fields["ACCEPTANCE_CHAIN_JSON"], "acceptance chain JSON"
    )
    try:
        if (
            protocol_digest("acceptance_chain", chain)
            != fields["ACCEPTANCE_CHAIN_SHA256"]
        ):
            raise PacketError("acceptance chain hash does not match its preimage")
        if chain["graph_manifest_sha256"] != fields["GRAPH_MANIFEST_SHA256"]:
            raise PacketError("graph manifest hash does not match the worker closure")
        matching = [
            record
            for record in chain["graph_manifest"]["contracts"]
            if record["contract"]["node"] == fields["NODE"]
        ]
        if len(matching) != 1:
            raise PacketError("worker contract is not uniquely declared by the graph")
        graph_contract = matching[0]
        if (
            graph_contract["contract"] != contract
            or graph_contract["contract_sha256"] != fields["CONTRACT_SHA256"]
        ):
            raise PacketError("worker contract does not match the graph manifest")
        graph_acceptance_ids = [
            record["id"] for record in graph_contract["contract"]["acceptance"]
        ]
        if graph_acceptance_ids != acceptance_ids(fields["ACCEPTANCE_IDS"]):
            raise PacketError("worker acceptance IDs do not match the graph manifest")
    except (KeyError, TypeError, ProtocolHashError) as error:
        raise PacketError("worker acceptance chain is invalid") from error


def validate_worker_followup_acceptance_chain(
    fields: dict[str, str], binding: dict[str, Any]
) -> None:
    chain = parse_canonical_object(
        fields["ACCEPTANCE_CHAIN_JSON"], "acceptance chain JSON"
    )
    try:
        current_sha256 = protocol_digest("acceptance_chain", chain)
        if current_sha256 != fields["ACCEPTANCE_CHAIN_SHA256"]:
            raise PacketError("acceptance chain hash does not match its preimage")
        if chain["graph_manifest_sha256"] != binding["graph_manifest_sha256"]:
            raise PacketError("follow-up graph manifest does not match its binding")
        decisions = chain["decisions"]
        if decisions[-1]["decision"]["mode"] != "independent":
            raise PacketError("worker follow-up requires independent acceptance")
        bound_chain_sha256 = binding["acceptance_chain_sha256"]
        if bound_chain_sha256 != current_sha256:
            if len(decisions) != 2 or "worker_followup" not in decisions[-1][
                "decision"
            ]["reasons"]:
                raise PacketError("worker follow-up lacks its one-way acceptance upgrade")
            prior_chain = {
                "decisions": decisions[:1],
                "graph_manifest": chain["graph_manifest"],
                "graph_manifest_sha256": chain["graph_manifest_sha256"],
                "protocol": "cco.v4",
            }
            if protocol_digest("acceptance_chain", prior_chain) != bound_chain_sha256:
                raise PacketError("worker follow-up acceptance history is not append-only")
        matching = [
            record
            for record in chain["graph_manifest"]["contracts"]
            if record["contract"]["node"] == fields["NODE"]
        ]
        if len(matching) != 1:
            raise PacketError("worker contract is not uniquely declared by the graph")
        graph_contract = matching[0]
        if graph_contract["contract_sha256"] != fields["CONTRACT_SHA256"]:
            raise PacketError("worker contract hash does not match the graph manifest")
        if [item["id"] for item in graph_contract["contract"]["acceptance"]] != acceptance_ids(
            fields["ACCEPTANCE_IDS"]
        ):
            raise PacketError("worker acceptance IDs do not match the graph manifest")
    except (IndexError, KeyError, TypeError, ProtocolHashError) as error:
        raise PacketError("worker follow-up acceptance chain is invalid") from error


def parse_passing_evidence(
    evidence_json: str,
    evidence_sha256: str,
    current_state: str,
    expected_ids: list[str],
    expected_graph_manifest_sha256: str,
    expected_acceptance_chain_sha256: str,
    expected_contracts: list[dict[str, object]],
    expected_acceptance: list[dict[str, str]] | None = None,
    expected_interfaces: list[str] | None = None,
    expected_paths: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    evidence = parse_canonical_object(evidence_json, "evidence JSON")
    try:
        if protocol_digest("evidence", evidence) != evidence_sha256:
            raise PacketError("review evidence hash does not match its preimage")
        if (
            evidence["acceptance_ids"] != expected_ids
            or evidence["current_state"] != current_state
            or evidence["acceptance_chain_sha256"]
            != expected_acceptance_chain_sha256
        ):
            raise PacketError("review evidence does not match the review closure")
        if any(record["outcome"] != "passed" for record in evidence["records"]):
            raise PacketError("review evidence is not fully passing")
        chain = evidence["acceptance_chain"]
        if chain["graph_manifest_sha256"] != expected_graph_manifest_sha256:
            raise PacketError("review graph manifest does not match the review closure")
        if chain["decisions"][-1]["decision"]["mode"] != "independent":
            raise PacketError(
                "review requires an independent acceptance decision"
            )
        bundled_contracts = chain["graph_manifest"]["contracts"]
        contract_references = [
            {
                "contract_rev": record["contract"]["contract_rev"],
                "contract_sha256": record["contract_sha256"],
                "node": record["contract"]["node"],
            }
            for record in bundled_contracts
        ]
        if contract_references != expected_contracts:
            raise PacketError("review contract references do not match the graph manifest")
        if expected_acceptance is not None:
            bundled_acceptance = sorted(
                (
                    acceptance
                    for record in bundled_contracts
                    for acceptance in record["contract"]["acceptance"]
                ),
                key=lambda record: record["id"].encode("utf-8"),
            )
            if bundled_acceptance != expected_acceptance:
                raise PacketError("review acceptance does not match the graph manifest")
        if expected_interfaces is not None:
            bundled_interfaces = sorted(
                {
                    interface
                    for record in bundled_contracts
                    for interface in record["contract"]["interfaces"]
                },
                key=lambda item: item.encode("utf-8"),
            )
            if bundled_interfaces != expected_interfaces:
                raise PacketError("review interfaces do not match the graph manifest")
        if expected_paths is not None:
            bundled_paths = sorted(
                (
                    scope
                    for record in bundled_contracts
                    for scope in record["contract"]["write"]
                ),
                key=lambda item: (
                    item["path"].encode("utf-8"),
                    item["kind"].encode("utf-8"),
                ),
            )
            if bundled_paths != expected_paths:
                raise PacketError("review paths do not match the graph manifest")
    except (KeyError, TypeError, ProtocolHashError) as error:
        raise PacketError("review evidence preimage is invalid") from error
    return evidence


def parse_resolution_records(value: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in value.split("\n"):
        match = RESOLUTION_LINE.fullmatch(line)
        if match is None:
            raise PacketError("review resolution is invalid")
        identifier, resolution = match.groups()
        records.append({"id": identifier, "resolution": resolution})
    return records


def canonical_target(value: object) -> str:
    try:
        return require_canonical_task_path(value, "continuation target")
    except ProtocolHashError as error:
        raise PacketError("continuation target is invalid") from error


def validate_worker_followup(tool_input: dict[str, Any]) -> None:
    if set(tool_input) != {"message", "target"}:
        raise PacketError("worker steer input is invalid")
    fields = parse_packet(
        tool_input.get("message"),
        header=WORK_FOLLOWUP_HEADER,
        required=WORK_FOLLOWUP_FIELDS,
        list_fields=WORK_FOLLOWUP_LIST_FIELDS,
    )
    binding = parse_canonical_object(fields["BINDING_JSON"], "worker binding JSON")
    target = canonical_target(fields["TARGET"])
    if tool_input.get("target") != target:
        raise PacketError("worker follow-up target does not match its envelope")
    attempt = parse_counter(
        fields["ATTEMPT"],
        minimum_current=1,
        maximum_limit=MAX_WORKER_ATTEMPTS,
    )
    followup = parse_counter(
        fields["FOLLOWUP"],
        minimum_current=1,
        maximum_limit=MAX_WORKER_FOLLOWUPS,
    )
    expected_acceptance_ids = acceptance_ids(fields["ACCEPTANCE_IDS"])
    verify = parse_verification_records(fields["VERIFY"], allow_none=True)
    preimage: dict[str, object] = {
        "acceptance_chain_sha256": fields["ACCEPTANCE_CHAIN_SHA256"],
        "binding": binding,
        "delta": bullet_values(fields["DELTA"]),
        "followup": followup,
        "kind": "worker_followup",
        "previous_input_closure_sha256": fields["PREVIOUS_INPUT_CLOSURE_SHA256"],
        "protocol": "cco.v4",
        "target": target,
        "type": fields["TYPE"],
        "verify": verify,
    }
    try:
        if protocol_digest("input_closure", preimage) != fields["INPUT_CLOSURE_SHA256"]:
            raise PacketError("worker follow-up hash does not match its packet")
    except ProtocolHashError as error:
        raise PacketError("worker follow-up preimage is invalid") from error
    validate_worker_followup_acceptance_chain(fields, binding)

    comparisons = {
        "acceptance_ids": expected_acceptance_ids,
        "attempt": attempt,
        "contract_rev": integer(fields["CONTRACT_REV"], minimum=1),
        "contract_sha256": fields["CONTRACT_SHA256"],
        "lease": fields["LEASE"],
        "lease_generation": integer(fields["LEASE_GENERATION"], minimum=1),
        "node": fields["NODE"],
        "run": fields["RUN"],
        "stop_generation": integer(fields["STOP_GENERATION"], minimum=0),
    }
    if any(binding.get(name) != expected for name, expected in comparisons.items()):
        raise PacketError("worker follow-up binding does not match its envelope")
    role = binding.get("role")
    lane = WORKER_ROLES.get(role) if isinstance(role, str) else None
    run_match = RUN_VALUE.fullmatch(fields["RUN"])
    if lane is None or run_match is None or run_match.group(1) != fields["NODE"]:
        raise PacketError("worker follow-up identity is invalid")
    expected_task = f"work_{fields['NODE']}_{lane}_{run_match.group(2)}"
    if target.rsplit("/", 1)[-1] != expected_task:
        raise PacketError("worker follow-up target is invalid")


def validate_review_delta(tool_input: dict[str, Any]) -> None:
    if set(tool_input) != {"message", "target"}:
        raise PacketError("review follow-up input is invalid")
    fields = parse_packet(
        tool_input.get("message"),
        header=REVIEW_DELTA_HEADER,
        required=REVIEW_DELTA_FIELDS,
        list_fields=REVIEW_DELTA_LIST_FIELDS,
    )
    if EPOCH_VALUE.fullmatch(fields["EPOCH"]) is None or fields["MODE"] != "delta":
        raise PacketError("review delta identity is invalid")
    target = canonical_target(fields["TARGET"])
    if tool_input.get("target") != target:
        raise PacketError("review delta target does not match its envelope")
    attempt = parse_counter(
        fields["ATTEMPT"],
        minimum_current=1,
        maximum_limit=MAX_REVIEW_ATTEMPTS,
    )
    followup = parse_counter(
        fields["FOLLOWUP"],
        minimum_current=1,
        maximum_limit=MAX_REVIEW_FOLLOWUPS,
    )
    expected = acceptance_ids(fields["ACCEPTANCE_IDS"])
    preimage: dict[str, object] = {
        "acceptance_ids": expected,
        "acceptance_chain_sha256": fields["ACCEPTANCE_CHAIN_SHA256"],
        "attempt": attempt,
        "contract_status": fields["CONTRACT_STATUS"],
        "contracts": parse_contract_references(fields["CONTRACTS"]),
        "current_state": fields["CURRENT_STATE"],
        "delta": bullet_values(fields["DELTA"]),
        "epoch": fields["EPOCH"],
        "evidence_sha256": fields["EVIDENCE_SHA256"],
        "followup": followup,
        "graph_manifest_sha256": fields["GRAPH_MANIFEST_SHA256"],
        "kind": "review_delta",
        "open_risks": bullet_values(fields["OPEN_RISKS"], allow_none=True),
        "previous_input_closure_sha256": fields["PREVIOUS_INPUT_CLOSURE_SHA256"],
        "prior_reviewed_state": fields["PRIOR_REVIEWED_STATE"],
        "protocol": "cco.v4",
        "resolves": parse_resolution_records(fields["RESOLVES"]),
        "target": target,
    }
    try:
        if protocol_digest("input_closure", preimage) != fields["INPUT_CLOSURE_SHA256"]:
            raise PacketError("review delta hash does not match its packet")
    except ProtocolHashError as error:
        raise PacketError("review delta preimage is invalid") from error
    parse_passing_evidence(
        fields["EVIDENCE_JSON"],
        fields["EVIDENCE_SHA256"],
        fields["CURRENT_STATE"],
        expected,
        fields["GRAPH_MANIFEST_SHA256"],
        fields["ACCEPTANCE_CHAIN_SHA256"],
        parse_contract_references(fields["CONTRACTS"]),
    )
    expected_task = f"review_{fields['EPOCH']}_r{attempt['current']:02d}"
    if target.rsplit("/", 1)[-1] != expected_task:
        raise PacketError("review follow-up target is invalid")


def validate_policy(fields: dict[str, str], tool_input: dict[str, Any]) -> None:
    for policy_field, request_field, input_field in (
        ("MODEL_POLICY", "REQUESTED_MODEL", "model"),
        ("EFFORT_POLICY", "REQUESTED_EFFORT", "reasoning_effort"),
    ):
        policy = fields[policy_field]
        requested = fields[request_field]
        present = input_field in tool_input
        actual = tool_input.get(input_field)
        if policy == "native":
            if requested != "none" or present:
                raise PacketError("native routing is not omitted")
        elif policy in {"user", "route_default"}:
            if requested == "none" or not present or actual != requested:
                raise PacketError("routing request does not match spawn")
        else:
            raise PacketError("routing policy is invalid")


def validate_routing_decision_binding(
    fields: dict[str, str], tool_input: dict[str, Any]
) -> None:
    model_policy = fields["MODEL_POLICY"]
    effort_policy = fields["EFFORT_POLICY"]
    adaptive = "route_default" in {model_policy, effort_policy}
    anchors = parse_anchor_records(fields["INPUTS"], "content_sha256")
    route_anchors = [item for item in anchors if item["id"] == "routing_decision"]
    encoded = fields["ROUTING_DECISION_JSON"]
    if not adaptive:
        if encoded != "none" or route_anchors:
            raise PacketError("non-adaptive routing carries an unexpected decision")
        return
    if "native" in {model_policy, effort_policy}:
        raise PacketError("adaptive routing cannot be mixed with a native dimension")
    if encoded == "none" or len(route_anchors) != 1:
        raise PacketError("adaptive routing decision is not uniquely bound")
    try:
        decision = load_routing_json_bytes(
            encoded.encode("utf-8"), "routing decision"
        )
        if routing_canonical_bytes(decision).decode("ascii") != encoded:
            raise PacketError("routing decision is not canonical")
        validated = validate_route_decision(
            decision,
            lane=fields["LANE"],
            model=tool_input.get("model"),
            effort=tool_input.get("reasoning_effort"),
        )
    except (RoutingCatalogError, UnicodeError) as error:
        raise PacketError("adaptive routing decision is invalid") from error
    if route_anchors[0]["content_sha256"] != validated["decision_sha256"]:
        raise PacketError("adaptive routing decision hash is not bound in INPUTS")
    constraints = validated["constraints"]
    if model_policy == "user":
        if constraints["fixed_model"] != fields["REQUESTED_MODEL"]:
            raise PacketError("adaptive routing does not preserve the user model")
    elif constraints["fixed_model"] is not None:
        raise PacketError("adaptive routing has an unexpected fixed model")
    if effort_policy == "user":
        if constraints["fixed_effort"] != fields["REQUESTED_EFFORT"]:
            raise PacketError("adaptive routing does not preserve the user effort")
    elif constraints["fixed_effort"] is not None:
        raise PacketError("adaptive routing has an unexpected fixed effort")


def validate_worker(tool_input: dict[str, Any], role: str) -> None:
    validate_spawn_input_shape(tool_input)
    fields = parse_packet(
        tool_input.get("message"),
        header=WORK_HEADER,
        required=WORK_FIELDS,
        list_fields=WORK_LIST_FIELDS,
    )
    lane = WORKER_ROLES[role]
    if fields["ROLE"] != role or fields["LANE"] != lane:
        raise PacketError("worker role or lane is invalid")
    if NODE_VALUE.fullmatch(fields["NODE"]) is None:
        raise PacketError("node is invalid")
    integer(fields["CONTRACT_REV"], minimum=1)
    for name in (
        "CONTRACT_SHA256",
        "INPUT_CLOSURE_SHA256",
        "GRAPH_MANIFEST_SHA256",
        "ACCEPTANCE_CHAIN_SHA256",
        "BASELINE",
    ):
        if SHA256_VALUE.fullmatch(fields[name]) is None:
            raise PacketError("hash is invalid")
    counter(
        fields["ATTEMPT"],
        minimum_current=1,
        maximum_limit=MAX_WORKER_ATTEMPTS,
    )
    counter(
        fields["FOLLOWUP"],
        expected_current=0,
        maximum_limit=MAX_WORKER_FOLLOWUPS,
    )
    integer(fields["LEASE_GENERATION"], minimum=1)
    integer(fields["STOP_GENERATION"], minimum=0)
    validate_acceptance_and_verify(fields)
    write_paths = validate_repository_paths(fields["WRITE"], "WRITE", allow_none=True)
    validate_policy(fields, tool_input)
    validate_routing_decision_binding(fields, tool_input)

    fork = validate_fork_turns(tool_input.get("fork_turns"))
    if fields["FORK_TURNS"] != fork:
        raise PacketError("fork closure does not match spawn")
    try:
        contract = worker_contract_preimage(fields, write_paths=write_paths)
        if protocol_digest("contract", contract) != fields["CONTRACT_SHA256"]:
            raise PacketError("contract hash does not match the work packet")
        validate_worker_acceptance_chain(fields, contract)
        input_preimage = worker_input_preimage(fields)
        if protocol_digest("input_closure", input_preimage) != fields["INPUT_CLOSURE_SHA256"]:
            raise PacketError("input hash does not match the work packet")
    except ProtocolHashError as error:
        raise PacketError("work packet preimage is invalid") from error

    run_match = RUN_VALUE.fullmatch(fields["RUN"])
    if run_match is None or run_match.group(1) != fields["NODE"]:
        raise PacketError("run identity is invalid")
    suffix = run_match.group(2)
    if tool_input.get("task_name") != f"work_{fields['NODE']}_{lane}_{suffix}":
        raise PacketError("task name is invalid")
    if fields["LEASE"] != f"wl_{fields['NODE']}_{suffix}":
        raise PacketError("lease identity is invalid")
    validate_fork_turns(fork)


def validate_reviewer(tool_input: dict[str, Any]) -> None:
    validate_spawn_input_shape(tool_input)
    fields = parse_packet(
        tool_input.get("message"),
        header=REVIEW_HEADER,
        required=REVIEW_FIELDS,
        list_fields=REVIEW_LIST_FIELDS,
    )
    if EPOCH_VALUE.fullmatch(fields["EPOCH"]) is None or fields["MODE"] != "fresh":
        raise PacketError("review epoch is invalid")
    attempt, _limit = counter(
        fields["ATTEMPT"],
        minimum_current=1,
        maximum_limit=MAX_REVIEW_ATTEMPTS,
    )
    counter(
        fields["FOLLOWUP"],
        expected_current=0,
        maximum_limit=MAX_REVIEW_FOLLOWUPS,
    )
    for name in (
        "INPUT_CLOSURE_SHA256",
        "GRAPH_MANIFEST_SHA256",
        "ACCEPTANCE_CHAIN_SHA256",
        "BASELINE",
        "CURRENT_STATE",
        "EVIDENCE_SHA256",
    ):
        if SHA256_VALUE.fullmatch(fields[name]) is None:
            raise PacketError("review hash is invalid")
    expected = acceptance_ids(fields["ACCEPTANCE_IDS"])
    acceptance_lines = fields["ACCEPTANCE"].split("\n")
    criteria = [
        match.group(1)
        for line in acceptance_lines
        if (match := ACCEPTANCE_LINE.fullmatch(line)) is not None
    ]
    if len(criteria) != len(acceptance_lines) or criteria != expected:
        raise PacketError("review acceptance closure is incomplete")
    parse_contract_references(fields["CONTRACTS"])
    allowed_paths = validate_repository_paths(
        fields["ALLOWED_PATHS"], "ALLOWED_PATHS", allow_none=True
    )
    if fields["FORK_TURNS"] != "none" or tool_input.get("fork_turns") != "none":
        raise PacketError("review fork policy is invalid")
    try:
        input_preimage = review_input_preimage(fields, allowed_paths=allowed_paths)
        if protocol_digest("input_closure", input_preimage) != fields["INPUT_CLOSURE_SHA256"]:
            raise PacketError("review input hash does not match its packet")
    except ProtocolHashError as error:
        raise PacketError("review input preimage is invalid") from error
    parse_passing_evidence(
        fields["EVIDENCE_JSON"],
        fields["EVIDENCE_SHA256"],
        fields["CURRENT_STATE"],
        expected,
        fields["GRAPH_MANIFEST_SHA256"],
        fields["ACCEPTANCE_CHAIN_SHA256"],
        parse_contract_references(fields["CONTRACTS"]),
        parse_acceptance_records(fields["ACCEPTANCE"]),
        bullet_values(fields["INTERFACES"], allow_none=True),
        allowed_paths,
    )
    if tool_input.get("task_name") != f"review_{fields['EPOCH']}_r{attempt:02d}":
        raise PacketError("review task name is invalid")
    if "model" in tool_input or "reasoning_effort" in tool_input:
        raise PacketError("reviewer override is invalid")


def evaluate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("hook_event_name") != "PreToolUse":
        return {}
    tool_name = value.get("tool_name")
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}

    if tool_name in {"send_message", "followup_task"}:
        target = tool_input.get("target")
        leaf = target.rsplit("/", 1)[-1] if isinstance(target, str) else ""
        message = tool_input.get("message")
        is_worker_target = WORK_TASK_VALUE.fullmatch(leaf) is not None
        is_review_target = REVIEW_TASK_VALUE.fullmatch(leaf) is not None
        is_worker_packet = isinstance(message, str) and message.lstrip("\r\n \t").startswith(
            WORK_FOLLOWUP_HEADER
        )
        is_review_packet = isinstance(message, str) and message.lstrip("\r\n \t").startswith(
            REVIEW_DELTA_HEADER
        )
        if not any(
            (is_worker_target, is_review_target, is_worker_packet, is_review_packet)
        ):
            return {}
        try:
            if tool_name == "send_message" and not (is_review_target or is_review_packet):
                validate_worker_followup(tool_input)
            elif tool_name == "followup_task" and not (is_worker_target or is_worker_packet):
                validate_review_delta(tool_input)
            else:
                raise PacketError("CCO continuation uses the wrong native operation")
        except PacketError:
            return block_outcome()
        return {}

    if tool_name not in {"spawn_agent", "Agent"}:
        return {}
    role = tool_input.get("agent_type")
    if role not in set(WORKER_ROLES) | {REVIEWER_ROLE}:
        return block_outcome() if is_reserved_cco_dispatch(tool_input) else {}
    try:
        if role == REVIEWER_ROLE:
            validate_reviewer(tool_input)
        else:
            validate_worker(tool_input, role)
    except PacketError:
        return block_outcome()
    return {}


def main() -> int:
    outcome: dict[str, str] = {}
    try:
        outcome = evaluate(load_utf8_json(sys.stdin.buffer))
    except Exception:
        pass
    if outcome:
        print(json.dumps(outcome, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

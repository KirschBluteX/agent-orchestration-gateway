import json
import ctypes
from datetime import datetime, timezone
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
HOOK = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "hooks"
    / "agent_preflight.py"
)
HOOKS_CONFIG = HOOK.parent / "hooks.json"
HASH_HELPER = HOOK.parent.parent / "scripts" / "protocol_hash.py"
sys.path.insert(0, str(HOOK.parent))
sys.path.insert(0, str(HASH_HELPER.parent))
import agent_preflight as preflight_module  # noqa: E402
import routing_catalog  # noqa: E402


def payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "spawn_agent",
        "tool_input": {},
    }
    value.update(overrides)
    return value


@lru_cache(maxsize=None)
def adaptive_decision_json(
    lane: str,
    model: str,
    effort: str,
    fixed_model: str | None,
    fixed_effort: str | None,
) -> str:
    now = datetime.now(timezone.utc)
    radar = {
        "schema": 2,
        "type": "distributed_intelligence_efficiency",
        "source_updated_at": now.isoformat(),
        "fingerprint": "a" * 64,
        "points": [
            {
                "model": model,
                "effort": effort,
                "iq": 112.5,
                "passed": 75,
                "valid_tasks": 100,
                "average_price_usd": 1.0,
                "price_samples": 100,
                "average_minutes": 20.0,
                "duration_samples": 100,
                "incomplete_cost_samples": 0,
            }
        ],
    }
    native = {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [{"effort": effort}],
            }
        ]
    }
    recommendation = routing_catalog.resolve_route(
        radar,
        native,
        lane,
        now=now,
        fixed_model=fixed_model,
        fixed_effort=fixed_effort,
    )
    decision, _state = routing_catalog.stabilize_route(recommendation, None)
    return json.dumps(decision, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def worker_message(
    *,
    role: str = "cost_orchestrator_routine_worker",
    lane: str = "routine",
    model_policy: str = "route_default",
    requested_model: str = "gpt-5.6-luna",
    effort_policy: str = "route_default",
    requested_effort: str = "max",
    fork_turns: str = "none",
    objective: str = "Implement the closed authentication behavior.",
    write_path: str = "src/auth.py",
    write_kind: str = "exact",
    manifest_node: str = "n01_auth",
) -> str:
    adaptive = "route_default" in {model_policy, effort_policy}
    decision_model = (
        requested_model if requested_model != "none" else "gpt-5.6-luna"
    )
    decision_effort = requested_effort if requested_effort != "none" else "max"
    routing_json = (
        adaptive_decision_json(
            lane,
            decision_model,
            decision_effort,
            requested_model if model_policy == "user" else None,
            requested_effort if effort_policy == "user" else None,
        )
        if adaptive
        else "none"
    )
    routing_sha = (
        json.loads(routing_json)["decision_sha256"] if adaptive else None
    )
    write_scope: object = {"kind": write_kind, "path": write_path}
    write_line = f"{write_kind}:{write_path}"
    contract: dict[str, object] = {
        "acceptance": [
            {"criterion": "Authentication behavior passes.", "id": "A01"}
        ],
        "constraints": ["Preserve unrelated work."],
        "contract_rev": 1,
        "discretion": ["Choose local mechanics only."],
        "exclusions": ["No architecture changes."],
        "interfaces": ["Preserve the public authenticate interface."],
        "lane": lane,
        "node": "n01_auth",
        "objective": objective,
        "protocol": "cco.v4",
        "risk_flags": [],
        "verification": [
            {
                "acceptance_ids": ["A01"],
                "expected": "passed",
                "id": "V01",
                "operation": "python -m unittest tests.test_auth",
            }
        ],
        "write": [write_scope],
    }
    contract_sha256 = protocol_hash("contract", contract)
    manifest_contract = json.loads(json.dumps(contract))
    manifest_contract["node"] = manifest_node
    graph_manifest: dict[str, object] = {
        "acceptance_owners": [
            {"acceptance_id": "A01", "implementation_owner": manifest_node}
        ],
        "contracts": [
            {
                "contract": manifest_contract,
                "contract_sha256": protocol_hash("contract", manifest_contract),
            }
        ],
        "protocol": "cco.v4",
    }
    graph_manifest_sha256 = protocol_hash("graph_manifest", graph_manifest)
    acceptance_mode = "primary" if lane == "routine" else "independent"
    acceptance_decision: dict[str, object] = {
        "graph_manifest_sha256": graph_manifest_sha256,
        "mode": acceptance_mode,
        "previous_decision_sha256": None,
        "protocol": "cco.v4",
        "reasons": [] if acceptance_mode == "primary" else ["complex_lane"],
        "revision": 1,
    }
    acceptance_chain: dict[str, object] = {
        "decisions": [
            {
                "decision": acceptance_decision,
                "decision_sha256": protocol_hash(
                    "acceptance_decision", acceptance_decision
                ),
            }
        ],
        "graph_manifest": graph_manifest,
        "graph_manifest_sha256": graph_manifest_sha256,
        "protocol": "cco.v4",
    }
    acceptance_chain_sha256 = protocol_hash("acceptance_chain", acceptance_chain)
    acceptance_chain_json = json.dumps(
        acceptance_chain, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    content_anchors = [
        {"content_sha256": "sha256:" + "d" * 64, "id": "I01"}
    ]
    if routing_sha is not None:
        content_anchors.append(
            {"content_sha256": routing_sha, "id": "routing_decision"}
        )
    content_anchors.sort(key=lambda item: str(item["id"]).encode("utf-8"))
    input_preimage: dict[str, object] = {
        "acceptance_chain_sha256": acceptance_chain_sha256,
        "attempt": {"current": 1, "limit": 2},
        "acceptance_ids": ["A01"],
        "baseline": "sha256:" + "c" * 64,
        "content_anchors": content_anchors,
        "contract_rev": 1,
        "contract_sha256": contract_sha256,
        "dependencies": [],
        "effort_policy": effort_policy,
        "followup": {"current": 0, "limit": 1},
        "fork_turns": fork_turns,
        "graph_manifest_sha256": graph_manifest_sha256,
        "kind": "worker_initial",
        "lease": "wl_n01_auth_r01",
        "lease_generation": 1,
        "model_policy": model_policy,
        "node": "n01_auth",
        "protocol": "cco.v4",
        "requested_effort": None if requested_effort == "none" else requested_effort,
        "requested_model": None if requested_model == "none" else requested_model,
        "role": role,
        "run": "run_n01_auth_r01",
        "stop_generation": 0,
    }
    input_sha256 = protocol_hash("input_closure", input_preimage)
    return f"""CCO_WORK cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: {contract_sha256}
INPUT_CLOSURE_SHA256: {input_sha256}
GRAPH_MANIFEST_SHA256: {graph_manifest_sha256}
ACCEPTANCE_CHAIN_SHA256: {acceptance_chain_sha256}
ACCEPTANCE_CHAIN_JSON: {acceptance_chain_json}
LANE: {lane}
ROLE: {role}
RUN: run_n01_auth_r01
ATTEMPT: 1/2
FOLLOWUP: 0/1
FORK_TURNS: {fork_turns}
BASELINE: sha256:{"c" * 64}
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
MODEL_POLICY: {model_policy}
REQUESTED_MODEL: {requested_model}
EFFORT_POLICY: {effort_policy}
REQUESTED_EFFORT: {requested_effort}
ROUTING_DECISION_JSON: {routing_json}
ACCEPTANCE_IDS: [A01]
WRITE:
- {write_line}
OBJECTIVE: {objective}
INTERFACES:
- Preserve the public authenticate interface.
DISCRETION:
- Choose local mechanics only.
CONSTRAINTS:
- Preserve unrelated work.
EXCLUSIONS:
- No architecture changes.
RISK_FLAGS:
- none
DEPENDENCIES:
- none
INPUTS:
- I01#sha256:{"d" * 64}
{"- routing_decision#" + routing_sha if routing_sha is not None else ""}
ACCEPTANCE:
- A01: Authentication behavior passes.
VERIFY:
- V01 [A01]: python -m unittest tests.test_auth => passed
"""


def protocol_hash(domain: str, value: dict[str, object]) -> str:
    result = subprocess.run(
        [sys.executable, str(HASH_HELPER), "hash", "--domain", domain],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def reviewer_evidence() -> dict[str, object]:
    contract: dict[str, object] = {
        "acceptance": [
            {"criterion": "Authentication behavior passes.", "id": "A01"}
        ],
        "constraints": ["Preserve unrelated work."],
        "contract_rev": 1,
        "discretion": ["Choose local mechanics only."],
        "exclusions": ["No architecture changes."],
        "interfaces": ["Preserve the public authenticate interface."],
        "lane": "routine",
        "node": "n01_auth",
        "objective": "Implement the closed authentication behavior.",
        "protocol": "cco.v4",
        "risk_flags": [],
        "verification": [
            {
                "acceptance_ids": ["A01"],
                "expected": "passed",
                "id": "V01",
                "operation": "python -m unittest tests.test_auth",
            }
        ],
        "write": [{"kind": "exact", "path": "src/auth.py"}],
    }
    graph_manifest: dict[str, object] = {
        "acceptance_owners": [
            {"acceptance_id": "A01", "implementation_owner": "n01_auth"}
        ],
        "contracts": [
            {
                "contract": contract,
                "contract_sha256": protocol_hash("contract", contract),
            }
        ],
        "protocol": "cco.v4",
    }
    graph_manifest_sha256 = protocol_hash("graph_manifest", graph_manifest)
    decision: dict[str, object] = {
        "graph_manifest_sha256": graph_manifest_sha256,
        "mode": "independent",
        "previous_decision_sha256": None,
        "protocol": "cco.v4",
        "reasons": ["explicit_independent_review"],
        "revision": 1,
    }
    acceptance_chain: dict[str, object] = {
        "decisions": [
            {
                "decision": decision,
                "decision_sha256": protocol_hash("acceptance_decision", decision),
            }
        ],
        "graph_manifest": graph_manifest,
        "graph_manifest_sha256": graph_manifest_sha256,
        "protocol": "cco.v4",
    }
    return {
        "acceptance_ids": ["A01"],
        "acceptance_chain": acceptance_chain,
        "acceptance_chain_sha256": protocol_hash(
            "acceptance_chain", acceptance_chain
        ),
        "current_state": "sha256:" + "d" * 64,
        "protocol": "cco.v4",
        "records": [
            {
                "acceptance_ids": ["A01"],
                "artifact_sha256s": [],
                "exit_status": 0,
                "implementation_owner": "n01_auth",
                "observed_outcome": "Authentication verification passed.",
                "operation": "python -m unittest tests.test_auth",
                "outcome": "passed",
                "verification_id": "V01",
            }
        ],
    }


def reviewer_message(
    evidence: dict[str, object] | None = None, *, attempt: int = 1
) -> str:
    evidence = reviewer_evidence() if evidence is None else evidence
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    evidence_sha256 = protocol_hash("evidence", evidence)
    acceptance_chain = evidence["acceptance_chain"]
    acceptance_chain_sha256 = evidence["acceptance_chain_sha256"]
    graph_manifest = acceptance_chain["graph_manifest"]
    graph_manifest_sha256 = acceptance_chain["graph_manifest_sha256"]
    contract_record = graph_manifest["contracts"][0]
    input_preimage: dict[str, object] = {
        "acceptance": [
            {"criterion": "Authentication behavior passes.", "id": "A01"}
        ],
        "acceptance_ids": ["A01"],
        "acceptance_chain_sha256": acceptance_chain_sha256,
        "accumulated_delta": ["D01#sha256:" + "e" * 64],
        "allowed_paths": [{"kind": "exact", "path": "src/auth.py"}],
        "attempt": {"current": attempt, "limit": 2},
        "baseline": "sha256:" + "c" * 64,
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": contract_record["contract_sha256"],
                "node": "n01_auth",
            }
        ],
        "current_state": "sha256:" + "d" * 64,
        "epoch": "e01",
        "evidence_sha256": evidence_sha256,
        "followup": {"current": 0, "limit": 1},
        "fork_turns": "none",
        "goal": "Ship the fixed authentication behavior.",
        "graph_manifest_sha256": graph_manifest_sha256,
        "interfaces": ["Preserve the public authenticate interface."],
        "kind": "review_fresh",
        "open_risks": [],
        "protocol": "cco.v4",
    }
    input_sha256 = protocol_hash("input_closure", input_preimage)
    return f"""CCO_REVIEW cco.v4
EPOCH: e01
MODE: fresh
ATTEMPT: {attempt}/2
FOLLOWUP: 0/1
FORK_TURNS: none
INPUT_CLOSURE_SHA256: {input_sha256}
GRAPH_MANIFEST_SHA256: {graph_manifest_sha256}
ACCEPTANCE_CHAIN_SHA256: {acceptance_chain_sha256}
CONTRACTS:
- n01_auth@1#{contract_record["contract_sha256"]}
GOAL: Ship the fixed authentication behavior.
ACCEPTANCE_IDS: [A01]
ACCEPTANCE:
- A01: Authentication behavior passes.
INTERFACES:
- Preserve the public authenticate interface.
BASELINE: sha256:{"c" * 64}
CURRENT_STATE: sha256:{"d" * 64}
ALLOWED_PATHS:
- exact:src/auth.py
ACCUMULATED_DELTA:
- D01#sha256:{"e" * 64}
EVIDENCE_SHA256: {evidence_sha256}
EVIDENCE_JSON: {evidence_json}
OPEN_RISKS:
- none
"""


def worker_followup_message() -> str:
    contract: dict[str, object] = {
        "acceptance": [
            {"criterion": "Authentication behavior passes.", "id": "A01"}
        ],
        "constraints": ["Preserve unrelated work."],
        "contract_rev": 1,
        "discretion": ["Choose local mechanics only."],
        "exclusions": ["No architecture changes."],
        "interfaces": ["Preserve the public authenticate interface."],
        "lane": "routine",
        "node": "n01_auth",
        "objective": "Implement the closed authentication behavior.",
        "protocol": "cco.v4",
        "risk_flags": [],
        "verification": [
            {
                "acceptance_ids": ["A01"],
                "expected": "passed",
                "id": "V01",
                "operation": "python -m unittest tests.test_auth",
            }
        ],
        "write": [{"kind": "exact", "path": "src/auth.py"}],
    }
    contract_sha256 = protocol_hash("contract", contract)
    graph_manifest: dict[str, object] = {
        "acceptance_owners": [
            {"acceptance_id": "A01", "implementation_owner": "n01_auth"}
        ],
        "contracts": [
            {"contract": contract, "contract_sha256": contract_sha256}
        ],
        "protocol": "cco.v4",
    }
    graph_manifest_sha256 = protocol_hash("graph_manifest", graph_manifest)
    initial_decision: dict[str, object] = {
        "graph_manifest_sha256": graph_manifest_sha256,
        "mode": "primary",
        "previous_decision_sha256": None,
        "protocol": "cco.v4",
        "reasons": [],
        "revision": 1,
    }
    initial_decision_sha256 = protocol_hash(
        "acceptance_decision", initial_decision
    )
    initial_chain: dict[str, object] = {
        "decisions": [
            {
                "decision": initial_decision,
                "decision_sha256": initial_decision_sha256,
            }
        ],
        "graph_manifest": graph_manifest,
        "graph_manifest_sha256": graph_manifest_sha256,
        "protocol": "cco.v4",
    }
    initial_chain_sha256 = protocol_hash("acceptance_chain", initial_chain)
    upgraded_decision: dict[str, object] = {
        "graph_manifest_sha256": graph_manifest_sha256,
        "mode": "independent",
        "previous_decision_sha256": initial_decision_sha256,
        "protocol": "cco.v4",
        "reasons": ["worker_followup"],
        "revision": 2,
    }
    upgraded_chain = json.loads(json.dumps(initial_chain))
    upgraded_chain["decisions"].append(
        {
            "decision": upgraded_decision,
            "decision_sha256": protocol_hash(
                "acceptance_decision", upgraded_decision
            ),
        }
    )
    upgraded_chain_sha256 = protocol_hash("acceptance_chain", upgraded_chain)
    upgraded_chain_json = json.dumps(
        upgraded_chain, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    binding: dict[str, object] = {
        "acceptance_chain_sha256": initial_chain_sha256,
        "attempt": {"current": 1, "limit": 2},
        "acceptance_ids": ["A01"],
        "baseline": "sha256:" + "c" * 64,
        "content_anchors": [
            {"content_sha256": "sha256:" + "d" * 64, "id": "I01"}
        ],
        "contract_rev": 1,
        "contract_sha256": contract_sha256,
        "dependencies": [],
        "effort_policy": "route_default",
        "fork_turns": "none",
        "graph_manifest_sha256": graph_manifest_sha256,
        "lease": "wl_n01_auth_r01",
        "lease_generation": 1,
        "model_policy": "route_default",
        "node": "n01_auth",
        "requested_effort": "max",
        "requested_model": "gpt-5.6-luna",
        "role": "cost_orchestrator_routine_worker",
        "run": "run_n01_auth_r01",
        "stop_generation": 0,
    }
    verify = [
        {
            "acceptance_ids": ["A01"],
            "expected": "passed",
            "id": "V01",
            "operation": "python -m unittest tests.test_auth",
        }
    ]
    preimage: dict[str, object] = {
        "acceptance_chain_sha256": upgraded_chain_sha256,
        "binding": binding,
        "delta": ["Correct the bounded authentication behavior."],
        "followup": {"current": 1, "limit": 1},
        "kind": "worker_followup",
        "previous_input_closure_sha256": "sha256:" + "b" * 64,
        "protocol": "cco.v4",
        "target": "/root/work_n01_auth_routine_r01",
        "type": "correction",
        "verify": verify,
    }
    input_sha256 = protocol_hash("input_closure", preimage)
    binding_json = json.dumps(
        binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return f"""CCO_WORK_FOLLOWUP cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: {contract_sha256}
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:{"b" * 64}
INPUT_CLOSURE_SHA256: {input_sha256}
ACCEPTANCE_CHAIN_SHA256: {upgraded_chain_sha256}
ACCEPTANCE_CHAIN_JSON: {upgraded_chain_json}
BINDING_JSON: {binding_json}
TARGET: /root/work_n01_auth_routine_r01
RUN: run_n01_auth_r01
ATTEMPT: 1/2
FOLLOWUP: 1/1
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
ACCEPTANCE_IDS: [A01]
TYPE: correction
DELTA:
- Correct the bounded authentication behavior.
VERIFY:
- V01 [A01]: python -m unittest tests.test_auth => passed
"""


def review_delta_message() -> str:
    evidence = reviewer_evidence()
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    evidence_sha256 = protocol_hash("evidence", evidence)
    acceptance_chain = evidence["acceptance_chain"]
    acceptance_chain_sha256 = evidence["acceptance_chain_sha256"]
    graph_manifest = acceptance_chain["graph_manifest"]
    graph_manifest_sha256 = acceptance_chain["graph_manifest_sha256"]
    contract_record = graph_manifest["contracts"][0]
    preimage: dict[str, object] = {
        "acceptance_ids": ["A01"],
        "acceptance_chain_sha256": acceptance_chain_sha256,
        "attempt": {"current": 1, "limit": 2},
        "contract_status": "preserved",
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": contract_record["contract_sha256"],
                "node": "n01_auth",
            }
        ],
        "current_state": "sha256:" + "d" * 64,
        "delta": ["D02#sha256:" + "f" * 64],
        "epoch": "e01",
        "evidence_sha256": evidence_sha256,
        "followup": {"current": 1, "limit": 1},
        "graph_manifest_sha256": graph_manifest_sha256,
        "kind": "review_delta",
        "open_risks": [],
        "previous_input_closure_sha256": "sha256:" + "a" * 64,
        "prior_reviewed_state": "sha256:" + "c" * 64,
        "protocol": "cco.v4",
        "resolves": [{"id": "F01", "resolution": "Applied the bounded fix."}],
        "target": "/root/review_e01_r01",
    }
    input_sha256 = protocol_hash("input_closure", preimage)
    return f"""CCO_REVIEW_DELTA cco.v4
EPOCH: e01
MODE: delta
ATTEMPT: 1/2
FOLLOWUP: 1/1
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:{"a" * 64}
INPUT_CLOSURE_SHA256: {input_sha256}
TARGET: /root/review_e01_r01
PRIOR_REVIEWED_STATE: sha256:{"c" * 64}
CURRENT_STATE: sha256:{"d" * 64}
CONTRACT_STATUS: preserved
GRAPH_MANIFEST_SHA256: {graph_manifest_sha256}
ACCEPTANCE_CHAIN_SHA256: {acceptance_chain_sha256}
CONTRACTS:
- n01_auth@1#{contract_record["contract_sha256"]}
ACCEPTANCE_IDS: [A01]
EVIDENCE_SHA256: {evidence_sha256}
RESOLVES:
- F01: Applied the bounded fix.
DELTA:
- D02#sha256:{"f" * 64}
EVIDENCE_JSON: {evidence_json}
OPEN_RISKS:
- none
"""


def run_hook(value: str | dict[str, object], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    stdin = value if isinstance(value, str) else json.dumps(value)
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def run_hook_utf8(value: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(value, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )


class AgentPreflightBehaviorTests(unittest.TestCase):
    def test_counter_caps_fail_before_repository_preflight(self) -> None:
        tool_input = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message().replace("ATTEMPT: 1/2", "ATTEMPT: 1/4"),
        }
        with mock.patch.object(
            preflight_module,
            "repository_root",
            side_effect=AssertionError("repository preflight should not run"),
        ):
            result = preflight_module.evaluate(payload(tool_input=tool_input))

        self.assertEqual(result["decision"], "block")

    def test_worker_path_preflight_reuses_one_repository_context(self) -> None:
        tool_input = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message(),
        }
        with (
            mock.patch.object(
                preflight_module,
                "repository_root",
                wraps=preflight_module.repository_root,
            ) as root_probe,
            mock.patch.object(
                preflight_module,
                "repository_control_roots",
                wraps=preflight_module.repository_control_roots,
            ) as control_probe,
            mock.patch.object(
                preflight_module,
                "validate_repository_lease_path",
                wraps=preflight_module.validate_repository_lease_path,
            ) as path_probe,
        ):
            preflight_module.validate_worker(
                tool_input, "cost_orchestrator_routine_worker"
            )

        self.assertEqual(root_probe.call_count, 1)
        self.assertEqual(control_probe.call_count, 1)
        self.assertEqual(path_probe.call_count, 1)

    def test_hook_config_registers_one_agent_preflight(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        groups = config["hooks"]["PreToolUse"]

        self.assertEqual(len(groups), 2)
        spawn_group = next(
            group for group in groups if group["matcher"] == "^(Agent|spawn_agent)$"
        )
        control_group = next(
            group for group in groups if group["matcher"] == "send_message|followup_task"
        )
        self.assertEqual(len(control_group["hooks"]), 1)
        handlers = spawn_group["hooks"]
        self.assertEqual(len(handlers), 1)
        self.assertIn("${PLUGIN_ROOT}/hooks/agent_preflight.py", handlers[0]["command"])
        self.assertIn(
            "${PLUGIN_ROOT}/hooks/agent_preflight.py", handlers[0]["commandWindows"]
        )
        self.assertFalse(handlers[0]["async"])

    def test_live_worker_steer_and_reviewer_delta_are_hash_checked(self) -> None:
        valid_cases = (
            (
                "send_message",
                {
                    "target": "/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message(),
                },
            ),
            (
                "followup_task",
                {
                    "target": "/root/review_e01_r01",
                    "message": review_delta_message(),
                },
            ),
        )
        for tool_name, tool_input in valid_cases:
            with self.subTest(tool_name=tool_name):
                result = run_hook(payload(tool_name=tool_name, tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

        invalid_cases = (
            (
                "send_message",
                {
                    "target": "/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message().replace(
                        "FOLLOWUP: 1/1", "FOLLOWUP: 999/1"
                    ),
                },
            ),
            (
                "send_message",
                {
                    "target": "/root/work_n01_auth_routine_r001",
                    "message": worker_followup_message().replace("r01", "r001"),
                },
            ),
            (
                "send_message",
                {
                    "target": "/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message().replace(
                        "Correct the bounded authentication behavior.",
                        "Tampered live steer.",
                        1,
                    ),
                },
            ),
            (
                "followup_task",
                {
                    "target": "/root/review_e01_r01",
                    "message": review_delta_message().replace(
                        "D02#sha256:" + "f" * 64,
                        "D02#sha256:" + "0" * 64,
                        1,
                    ),
                },
            ),
            (
                "followup_task",
                {
                    "target": "/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message(),
                },
            ),
        )
        for tool_name, tool_input in invalid_cases:
            with self.subTest(tool_name=tool_name, target=tool_input["target"]):
                result = run_hook(payload(tool_name=tool_name, tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_continuations_bind_exact_task_path_and_acceptance_closure(self) -> None:
        invalid_cases = (
            (
                "send_message",
                {
                    "target": "/foreign/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message(),
                },
            ),
            (
                "followup_task",
                {
                    "target": "/foreign/root/review_e01_r01",
                    "message": review_delta_message(),
                },
            ),
            (
                "send_message",
                {
                    "target": "/root/work_n01_auth_routine_r01",
                    "message": worker_followup_message().replace(
                        "ACCEPTANCE_IDS: [A01]", "ACCEPTANCE_IDS: [A99]"
                    ),
                },
            ),
        )

        for tool_name, tool_input in invalid_cases:
            with self.subTest(tool_name=tool_name, target=tool_input["target"]):
                result = run_hook(payload(tool_name=tool_name, tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_unrelated_agent_spawn_is_allowed(self) -> None:
        for task_name in ("ordinary", "work_docs"):
            with self.subTest(task_name=task_name):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": task_name,
                            "message": "Do an ordinary task.",
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_reserved_cco_dispatch_cannot_bypass_with_a_missing_or_unknown_role(self) -> None:
        worker = {
            "task_name": "work_n01_auth_routine_r01",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message(),
        }
        reviewer = {
            "task_name": "review_e01_r01",
            "fork_turns": "none",
            "message": reviewer_message(),
        }
        cases = (
            worker,
            {**worker, "agent_type": "cost_orchestrator_routine_worke"},
            reviewer,
            {**reviewer, "agent_type": "cost_orchestrator_reviewe"},
            {
                "task_name": "work_n01_docs_routine_r01",
                "agent_type": "ordinary_worker",
                "fork_turns": "none",
                "message": "ordinary text",
            },
        )
        for tool_input in cases:
            with self.subTest(tool_input=tool_input):
                result = run_hook(payload(tool_input=tool_input))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_standalone_continuation_headers_are_reserved_on_spawn(self) -> None:
        for message in (worker_followup_message(), review_delta_message()):
            with self.subTest(header=message.splitlines()[0]):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": "ordinary",
                            "message": message,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_valid_explicit_and_native_worker_routes_are_allowed(self) -> None:
        cases = (
            (
                worker_message(),
                {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            ),
            (
                worker_message(
                    model_policy="native",
                    requested_model="none",
                    effort_policy="native",
                    requested_effort="none",
                ),
                {},
            ),
            (
                worker_message(
                    model_policy="user",
                    requested_model="gpt-5.6-terra",
                    effort_policy="native",
                    requested_effort="none",
                ),
                {"model": "gpt-5.6-terra"},
            ),
            (
                worker_message(
                    model_policy="native",
                    requested_model="none",
                    effort_policy="user",
                    requested_effort="high",
                ),
                {"reasoning_effort": "high"},
            ),
            (
                worker_message(requested_model="gpt-5.6-terra"),
                {"model": "gpt-5.6-terra", "reasoning_effort": "max"},
            ),
            (
                worker_message(
                    model_policy="user",
                    requested_model="gpt-5.6-terra",
                    effort_policy="route_default",
                    requested_effort="max",
                ),
                {"model": "gpt-5.6-terra", "reasoning_effort": "max"},
            ),
        )
        for message, overrides in cases:
            with self.subTest(overrides=overrides):
                tool_input = {
                    "task_name": "work_n01_auth_routine_r01",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "message": message,
                    **overrides,
                }
                result = run_hook(payload(tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_worker_contract_must_be_declared_by_the_full_graph_manifest(self) -> None:
        tool_input = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message(manifest_node="n02_other"),
        }

        result = run_hook(payload(tool_input=tool_input), cwd=REPO)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision":"block"', result.stdout)

    def test_worker_packet_preserves_hashed_prefix_scope_kind(self) -> None:
        tool_input = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message(
                write_path="generated",
                write_kind="prefix",
            ),
        }

        result = run_hook(payload(tool_input=tool_input))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_worker_packet_rejects_an_untyped_scope_line(self) -> None:
        tool_input = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message().replace(
                "- exact:src/auth.py", "- src/auth.py"
            ),
        }

        result = run_hook(payload(tool_input=tool_input))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_native_policy_requires_override_key_omission(self) -> None:
        message = worker_message(
            model_policy="native",
            requested_model="none",
            effort_policy="native",
            requested_effort="none",
        )
        for key in ("model", "reasoning_effort"):
            with self.subTest(key=key):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": "work_n01_auth_routine_r01",
                            "agent_type": "cost_orchestrator_routine_worker",
                            "fork_turns": "none",
                            "message": message,
                            key: None,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_adaptive_policy_cannot_be_mixed_with_a_native_dimension(self) -> None:
        result = run_hook(
            payload(
                tool_input={
                    "task_name": "work_n01_auth_routine_r01",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "reasoning_effort": "max",
                    "message": worker_message(
                        model_policy="native",
                        requested_model="none",
                        effort_policy="route_default",
                        requested_effort="max",
                    ),
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_adaptive_route_defaults_are_not_limited_to_hardcoded_models(self) -> None:
        cases = (
            {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "message": worker_message(requested_model="gpt-5.6-sol"),
            },
            {
                "task_name": "work_n01_auth_complex_r01",
                "agent_type": "cost_orchestrator_complex_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "message": worker_message(
                    role="cost_orchestrator_complex_worker",
                    lane="complex",
                    requested_model="gpt-5.6-luna",
                ),
            },
            {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
                "message": worker_message(requested_effort="high"),
            },
        )
        for tool_input in cases:
            with self.subTest(task_name=tool_input["task_name"], model=tool_input["model"]):
                result = run_hook(payload(tool_input=tool_input))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_adaptive_route_requires_a_matching_hash_bound_decision(self) -> None:
        valid = worker_message()
        routing_line = next(
            line for line in valid.splitlines() if line.startswith("ROUTING_DECISION_JSON:")
        )
        cases = (
            valid.replace(routing_line, "ROUTING_DECISION_JSON: none"),
            valid.replace("- routing_decision#sha256:", "- other_decision#sha256:"),
            valid.replace('"selection_method":"strict_pareto_fixed_anchor_mcda"',
                          '"selection_method":"cheapest_only"'),
        )
        for message in cases:
            with self.subTest(message_change=message[:80]):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": "work_n01_auth_routine_r01",
                            "agent_type": "cost_orchestrator_routine_worker",
                            "fork_turns": "none",
                            "model": "gpt-5.6-luna",
                            "reasoning_effort": "max",
                            "message": message,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_unicode_line_separators_remain_field_content(self) -> None:
        message = worker_message(
            objective="Implement\u2028the closed authentication behavior."
        )
        result = run_hook(
            payload(
                tool_input={
                    "task_name": "work_n01_auth_routine_r01",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "message": message,
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_worker_dispatch_is_blocked(self) -> None:
        base = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "message": worker_message(),
        }
        cases = (
            {**base, "fork_turns": "all"},
            {**base, "task_name": "wrong_name"},
            {**base, "model": "gpt-5.6-terra"},
            {**base, "sandbox_mode": "danger-full-access"},
            {**base, "message": worker_message(role="cost_orchestrator_complex_worker")},
            {
                **base,
                "message": worker_message().replace(
                    "CONTRACT_SHA256: sha256:", "CONTRACT_SHA256: invalid", 1
                ),
            },
            {**base, "message": worker_message().replace("FOLLOWUP: 0/1", "FOLLOWUP: 1/1")},
            {**base, "message": worker_message().replace("VERIFY:\n", "")},
            {**base, "message": worker_message().replace("CCO_WORK cco.v4", "CCO_WORK_FOLLOWUP cco.v4")},
            {
                **base,
                "message": worker_message().replace(
                    "CONTRACT_REV: 1", "CONTRACT_REV: " + "9" * 10_000
                ),
            },
        )
        for tool_input in cases:
            with self.subTest(tool_input=tool_input):
                result = run_hook(payload(tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn("CCO", output["reason"])

    def test_worker_preflight_recomputes_contract_input_and_fork_closure(self) -> None:
        base = {
            "task_name": "work_n01_auth_routine_r01",
            "agent_type": "cost_orchestrator_routine_worker",
            "fork_turns": "none",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
        }
        cases = (
            {
                **base,
                "fork_turns": "1",
                "message": worker_message(),
            },
            {
                **base,
                "message": worker_message().replace(
                    "Implement the closed authentication behavior.",
                    "Tampered objective.",
                ),
            },
            {
                **base,
                "message": worker_message().replace(
                    "I01#sha256:" + "d" * 64,
                    "I01#sha256:" + "e" * 64,
                ),
            },
        )
        for tool_input in cases:
            with self.subTest(fork=tool_input["fork_turns"]):
                result = run_hook(payload(tool_input=tool_input))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_worker_and_review_paths_are_canonical_repository_relative(self) -> None:
        cases = (
            {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "message": worker_message().replace(
                    "- exact:src/auth.py", "- exact:../escape.py"
                ),
            },
            {
                "task_name": "review_e01_r01",
                "agent_type": "cost_orchestrator_reviewer",
                "fork_turns": "none",
                "message": reviewer_message().replace(
                    "- exact:src/auth.py", "- exact:src/../escape.py"
                ),
            },
        )
        for tool_input in cases:
            with self.subTest(task_name=tool_input["task_name"]):
                result = run_hook(payload(tool_input=tool_input))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_preflight_treats_each_gitlink_as_one_atomic_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            initialized = subprocess.run(
                ["git", "init"], cwd=repo, text=True, capture_output=True, check=False
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            committed = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=CCO Tests",
                    "-c",
                    "user.email=cco-tests@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(head.returncode, 0, head.stderr)
            gitlink = subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{head.stdout.strip()},vendor/child",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(gitlink.returncode, 0, gitlink.stderr)

            base = {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
            }
            nested = run_hook(
                payload(
                    tool_input={
                        **base,
                        "message": worker_message(
                            write_path="vendor/child/src/owned.txt"
                        ),
                    }
                ),
                cwd=repo,
            )
            atomic = run_hook(
                payload(
                    tool_input={
                        **base,
                        "message": worker_message(write_path="vendor/child"),
                    }
                ),
                cwd=repo,
            )
            prefix = run_hook(
                payload(
                    tool_input={
                        **base,
                        "message": worker_message(
                            write_path="vendor/child",
                            write_kind="prefix",
                        ),
                    }
                ),
                cwd=repo,
            )

            self.assertEqual(nested.returncode, 0, nested.stderr)
            self.assertEqual(json.loads(nested.stdout)["decision"], "block")
            self.assertEqual(atomic.returncode, 0, atomic.stderr)
            self.assertEqual(atomic.stdout, "")
            self.assertEqual(prefix.returncode, 0, prefix.stderr)
            self.assertEqual(json.loads(prefix.stdout)["decision"], "block")

    @unittest.skipUnless(os.name == "nt", "case aliases are Windows-specific")
    def test_preflight_rejects_a_case_alias_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            self.assertEqual(
                subprocess.run(
                    ["git", "init"],
                    cwd=repo,
                    text=True,
                    capture_output=True,
                    check=False,
                ).returncode,
                0,
            )
            (repo / "src").mkdir()
            (repo / "src" / "auth.py").write_text("pass\n", encoding="utf-8")
            added = subprocess.run(
                ["git", "add", "src/auth.py"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            tool_input = {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "message": worker_message(write_path="SRC/AUTH.PY"),
            }

            result = run_hook(payload(tool_input=tool_input), cwd=repo)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    @unittest.skipUnless(os.name == "nt", "8.3 aliases are Windows-specific")
    def test_preflight_rejects_a_short_name_alias_to_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            initialized = subprocess.run(
                ["git", "init"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            buffer = ctypes.create_unicode_buffer(32_768)
            length = ctypes.windll.kernel32.GetShortPathNameW(
                str(repo / ".git"), buffer, len(buffer)
            )
            if length == 0 or length >= len(buffer):
                self.skipTest("the test volume did not expose an 8.3 alias for .git")
            short_name = Path(buffer.value).name
            if not short_name or short_name.lower() == ".git":
                self.skipTest("the test volume did not expose an 8.3 alias for .git")

            result = run_hook(
                payload(
                    tool_input={
                        "task_name": "work_n01_auth_routine_r01",
                        "agent_type": "cost_orchestrator_routine_worker",
                        "fork_turns": "none",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "max",
                        "message": worker_message(
                            write_path=f"{short_name}/config"
                        ),
                    }
                ),
                cwd=repo,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_preflight_rejects_a_junction_into_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            initialized = subprocess.run(
                ["git", "init"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            junction = repo / "leased"
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(repo / ".git")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)

            result = run_hook(
                payload(
                    tool_input={
                        "task_name": "work_n01_auth_routine_r01",
                        "agent_type": "cost_orchestrator_routine_worker",
                        "fork_turns": "none",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "max",
                        "message": worker_message(
                            write_path="leased/hooks/post-checkout"
                        ),
                    }
                ),
                cwd=repo,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_initial_worker_and_reviewer_attempts_start_at_one(self) -> None:
        cases = (
            {
                "task_name": "work_n01_auth_routine_r01",
                "agent_type": "cost_orchestrator_routine_worker",
                "fork_turns": "none",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
                "message": worker_message().replace("ATTEMPT: 1/2", "ATTEMPT: 0/2"),
            },
            {
                "task_name": "review_e01_r00",
                "agent_type": "cost_orchestrator_reviewer",
                "fork_turns": "none",
                "message": reviewer_message().replace("ATTEMPT: 1/2", "ATTEMPT: 0/2"),
            },
        )
        for tool_input in cases:
            with self.subTest(task_name=tool_input["task_name"]):
                result = run_hook(payload(tool_input=tool_input))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_initial_worker_rejects_a_noncanonical_run_alias(self) -> None:
        result = run_hook(
            payload(
                tool_input={
                    "task_name": "work_n01_auth_routine_r001",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "message": worker_message().replace("r01", "r001"),
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_valid_fresh_reviewer_is_allowed(self) -> None:
        cases = (
            ("review_e01_r01", reviewer_message()),
            ("review_e01_r02", reviewer_message(attempt=2)),
        )
        for task_name, message in cases:
            with self.subTest(task_name=task_name):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": task_name,
                            "agent_type": "cost_orchestrator_reviewer",
                            "fork_turns": "none",
                            "message": message,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_primary_acceptance_bundle_does_not_spawn_a_redundant_reviewer(self) -> None:
        evidence = reviewer_evidence()
        chain = evidence["acceptance_chain"]
        decision = chain["decisions"][0]["decision"]
        decision["mode"] = "primary"
        decision["reasons"] = []
        chain["decisions"][0]["decision_sha256"] = protocol_hash(
            "acceptance_decision", decision
        )
        evidence["acceptance_chain_sha256"] = protocol_hash(
            "acceptance_chain", chain
        )
        tool_input = {
            "task_name": "review_e01_r01",
            "agent_type": "cost_orchestrator_reviewer",
            "fork_turns": "none",
            "message": reviewer_message(evidence),
        }

        result = run_hook(payload(tool_input=tool_input))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_reviewer_override_or_delta_spawn_is_blocked(self) -> None:
        base = {
            "task_name": "review_e01_r01",
            "agent_type": "cost_orchestrator_reviewer",
            "fork_turns": "none",
            "message": reviewer_message(),
        }
        cases = (
            {**base, "model": "gpt-5.6-sol"},
            {**base, "reasoning_effort": "high"},
            {**base, "model": None},
            {**base, "reasoning_effort": None},
            {**base, "sandbox_mode": "danger-full-access"},
            {**base, "fork_turns": "1"},
            {**base, "message": reviewer_message().replace("MODE: fresh", "MODE: delta")},
            {**base, "message": reviewer_message().replace("EPOCH: e01", "EPOCH: e02")},
        )
        for tool_input in cases:
            with self.subTest(tool_input=tool_input):
                result = run_hook(payload(tool_input=tool_input))
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")

    def test_reviewer_evidence_preimage_must_match_hash_state_and_ids(self) -> None:
        base = {
            "task_name": "review_e01_r01",
            "agent_type": "cost_orchestrator_reviewer",
            "fork_turns": "none",
        }
        message = reviewer_message()
        cases = (
            message.replace(
                '"observed_outcome":"Authentication verification passed."',
                '"observed_outcome":"unverified"',
            ),
            message.replace(
                '"current_state":"sha256:' + "d" * 64 + '"',
                '"current_state":"sha256:' + "e" * 64 + '"',
            ),
            message.replace('"acceptance_ids":["A01"]', '"acceptance_ids":["A02"]', 1),
            message.replace('"acceptance_ids"', ' "acceptance_ids"', 1),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate[-160:]):
                result = run_hook(payload(tool_input={**base, "message": candidate}))
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")

    def test_fresh_review_requires_passing_primary_evidence(self) -> None:
        evidence = reviewer_evidence()
        records = evidence["records"]
        self.assertIsInstance(records, list)
        records[0]["outcome"] = "failed"
        records[0]["exit_status"] = 1
        records[0]["observed_outcome"] = "Authentication verification failed."

        result = run_hook(
            payload(
                tool_input={
                    "task_name": "review_e01_r01",
                    "agent_type": "cost_orchestrator_reviewer",
                    "fork_turns": "none",
                    "message": reviewer_message(evidence),
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_review_contract_and_acceptance_closure_is_exact(self) -> None:
        evidence = reviewer_evidence()
        contract_record = evidence["acceptance_chain"]["graph_manifest"][
            "contracts"
        ][0]
        contract = f"- n01_auth@1#{contract_record['contract_sha256']}"
        cases = (
            reviewer_message().replace(
                "- A01: Authentication behavior passes.",
                "- A01: Authentication behavior passes.\n- unbound criterion",
            ),
            reviewer_message().replace(contract, "- not-a-contract"),
            reviewer_message().replace(contract, f"{contract}\n{contract}"),
            reviewer_message().replace(
                contract,
                f"- n02_other@1#sha256:{'c' * 64}\n{contract}",
            ),
        )
        for message in cases:
            with self.subTest(message=message[:180]):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": "review_e01_r01",
                            "agent_type": "cost_orchestrator_reviewer",
                            "fork_turns": "none",
                            "message": message,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_reviewer_preflight_recomputes_the_review_input_closure(self) -> None:
        cases = (
            reviewer_message().replace(
                "Ship the fixed authentication behavior.",
                "Review a different goal.",
            ),
            reviewer_message().replace(
                "D01#sha256:" + "e" * 64,
                "D01#sha256:" + "f" * 64,
            ),
        )
        for message in cases:
            with self.subTest(message=message[:180]):
                result = run_hook(
                    payload(
                        tool_input={
                            "task_name": "review_e01_r01",
                            "agent_type": "cost_orchestrator_reviewer",
                            "fork_turns": "none",
                            "message": message,
                        }
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_oversized_cco_spawn_packet_is_blocked(self) -> None:
        message = worker_message().replace(
            "Implement the closed authentication behavior.", "x" * (1024 * 1024)
        )
        result = run_hook(
            payload(
                tool_input={
                    "task_name": "work_n01_auth_routine_r01",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "message": message,
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_malformed_hook_input_fails_open(self) -> None:
        result = run_hook("{not json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_hook_decodes_outer_json_as_utf8_before_validation(self) -> None:
        message = worker_message().replace(
            "Implement the closed authentication behavior.",
            "\u9a8c UTF-8 transport.",
        ).replace("ATTEMPT: 1/2", "ATTEMPT: 0/2")
        result = run_hook_utf8(
            payload(
                tool_input={
                    "task_name": "work_n01_auth_routine_r01",
                    "agent_type": "cost_orchestrator_routine_worker",
                    "fork_turns": "none",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "message": message,
                }
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["decision"], "block")

    def test_hook_does_not_write_to_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = run_hook(
                payload(
                    tool_input={
                        "agent_type": "cost_orchestrator_routine_worker",
                        "message": "invalid",
                    }
                ),
                cwd=workspace,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import subprocess
import sys
import unittest
from copy import deepcopy


REPO = Path(__file__).resolve().parents[1]
HELPER = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "scripts"
    / "protocol_hash.py"
)
sys.path.insert(0, str(HELPER.parent))
from protocol_hash import ProtocolHashError, digest  # noqa: E402


def valid_contract() -> dict[str, object]:
    return {
        "acceptance": [
            {
                "criterion": "CLI returns a domain-separated digest",
                "id": "A01",
            }
        ],
        "constraints": ["Use UTF-8 JSON"],
        "contract_rev": 1,
        "discretion": ["Use a standard SHA-256 implementation"],
        "exclusions": ["Do not change hooks"],
        "interfaces": ["protocol_hash.py hash --domain contract"],
        "lane": "routine",
        "node": "n01_protocol_hash",
        "objective": "Validate canonical CCO v4 preimages",
        "protocol": "cco.v4",
        "risk_flags": [],
        "verification": [
            {
                "acceptance_ids": ["A01"],
                "expected": "A sha256 digest",
                "id": "V01",
                "operation": "python -m unittest tests/test_protocol_hash.py",
            }
        ],
        "write": [
            {
                "kind": "exact",
                "path": "plugins/codex-cost-orchestrator/scripts/protocol_hash.py",
            },
            {"kind": "exact", "path": "tests/test_protocol_hash.py"},
        ],
    }


def valid_graph_manifest() -> dict[str, object]:
    contract = valid_contract()
    return {
        "acceptance_owners": [
            {
                "acceptance_id": "A01",
                "implementation_owner": "n01_protocol_hash",
            }
        ],
        "contracts": [
            {
                "contract": contract,
                "contract_sha256": digest("contract", contract),
            }
        ],
        "protocol": "cco.v4",
    }


def valid_acceptance_chain(*, mode: str = "primary") -> dict[str, object]:
    manifest = valid_graph_manifest()
    manifest_sha256 = digest("graph_manifest", manifest)
    reasons = [] if mode == "primary" else ["explicit_independent_review"]
    decision = {
        "graph_manifest_sha256": manifest_sha256,
        "mode": mode,
        "previous_decision_sha256": None,
        "protocol": "cco.v4",
        "reasons": reasons,
        "revision": 1,
    }
    return {
        "decisions": [
            {
                "decision": decision,
                "decision_sha256": digest("acceptance_decision", decision),
            }
        ],
        "graph_manifest": manifest,
        "graph_manifest_sha256": manifest_sha256,
        "protocol": "cco.v4",
    }


def valid_evidence() -> dict[str, object]:
    acceptance_chain = valid_acceptance_chain(mode="independent")
    return {
        "acceptance_ids": ["A01"],
        "acceptance_chain": acceptance_chain,
        "acceptance_chain_sha256": digest("acceptance_chain", acceptance_chain),
        "current_state": "sha256:" + "b" * 64,
        "protocol": "cco.v4",
        "records": [
            {
                "acceptance_ids": ["A01"],
                "artifact_sha256s": ["sha256:" + "c" * 64],
                "exit_status": 0,
                "implementation_owner": "n01_protocol_hash",
                "observed_outcome": "passed",
                "operation": "python -m unittest tests/test_protocol_hash.py",
                "outcome": "passed",
                "verification_id": "V01",
            }
        ],
    }


def valid_worker_initial() -> dict[str, object]:
    manifest = valid_graph_manifest()
    chain = valid_acceptance_chain()
    return {
        "acceptance_chain_sha256": digest("acceptance_chain", chain),
        "attempt": {"current": 1, "limit": 2},
        "acceptance_ids": ["A01"],
        "baseline": "sha256:" + "d" * 64,
        "content_anchors": [
            {"content_sha256": "sha256:" + "e" * 64, "id": "anchor.one"}
        ],
        "contract_rev": 1,
        "contract_sha256": "sha256:" + "f" * 64,
        "dependencies": [
            {"id": "dependency.one", "state_sha256": "sha256:" + "0" * 64}
        ],
        "effort_policy": "native",
        "fork_turns": "none",
        "followup": {"current": 0, "limit": 1},
        "graph_manifest_sha256": digest("graph_manifest", manifest),
        "kind": "worker_initial",
        "lease": "wl_n01_protocol_hash_r01",
        "lease_generation": 1,
        "model_policy": "user",
        "node": "n01_protocol_hash",
        "protocol": "cco.v4",
        "requested_effort": None,
        "requested_model": "gpt-5.6-luna",
        "role": "cost_orchestrator_routine_worker",
        "run": "run_n01_protocol_hash_r01",
        "stop_generation": 0,
    }


def valid_worker_followup() -> dict[str, object]:
    initial = valid_worker_initial()
    binding = {
        key: deepcopy(value)
        for key, value in initial.items()
        if key not in {"followup", "kind", "protocol"}
    }
    return {
        "acceptance_chain_sha256": initial["acceptance_chain_sha256"],
        "binding": binding,
        "delta": ["Run the focused protocol-hash test."],
        "followup": {"current": 1, "limit": 1},
        "kind": "worker_followup",
        "previous_input_closure_sha256": "sha256:" + "1" * 64,
        "protocol": "cco.v4",
        "target": "/root/work_n01_protocol_hash_r01",
        "type": "verification",
        "verify": [
            {
                "acceptance_ids": ["A01"],
                "expected": "Tests pass",
                "id": "V01",
                "operation": "python -m unittest tests/test_protocol_hash.py",
            }
        ],
    }


def valid_review_fresh() -> dict[str, object]:
    chain = valid_acceptance_chain(mode="independent")
    manifest = chain["graph_manifest"]
    contract_record = manifest["contracts"][0]
    contract = contract_record["contract"]
    return {
        "acceptance": deepcopy(contract["acceptance"]),
        "acceptance_ids": ["A01"],
        "accumulated_delta": ["protocol hash schema implementation"],
        "allowed_paths": deepcopy(contract["write"]),
        "attempt": {"current": 1, "limit": 2},
        "acceptance_chain_sha256": digest("acceptance_chain", chain),
        "baseline": "sha256:" + "3" * 64,
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": contract_record["contract_sha256"],
                "node": "n01_protocol_hash",
            }
        ],
        "current_state": "sha256:" + "5" * 64,
        "epoch": "e01",
        "evidence_sha256": "sha256:" + "6" * 64,
        "followup": {"current": 0, "limit": 1},
        "fork_turns": "none",
        "goal": "Validate canonical CCO v4 preimages",
        "graph_manifest_sha256": digest("graph_manifest", manifest),
        "interfaces": deepcopy(contract["interfaces"]),
        "kind": "review_fresh",
        "open_risks": [],
        "protocol": "cco.v4",
    }


def valid_review_delta() -> dict[str, object]:
    chain = valid_acceptance_chain(mode="independent")
    manifest = chain["graph_manifest"]
    contract_record = manifest["contracts"][0]
    return {
        "acceptance_ids": ["A01"],
        "acceptance_chain_sha256": digest("acceptance_chain", chain),
        "attempt": {"current": 1, "limit": 2},
        "contract_status": "preserved",
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": contract_record["contract_sha256"],
                "node": "n01_protocol_hash",
            }
        ],
        "current_state": "sha256:" + "7" * 64,
        "delta": ["Add exact input-preimage validation."],
        "epoch": "e01",
        "evidence_sha256": "sha256:" + "8" * 64,
        "followup": {"current": 1, "limit": 1},
        "graph_manifest_sha256": digest("graph_manifest", manifest),
        "kind": "review_delta",
        "open_risks": [],
        "previous_input_closure_sha256": "sha256:" + "9" * 64,
        "prior_reviewed_state": "sha256:" + "a" * 64,
        "protocol": "cco.v4",
        "resolves": [
            {"id": "F01", "resolution": "Validate review preimages."}
        ],
        "target": "/root/review_e01_r01",
    }


def valid_failure() -> dict[str, object]:
    return {
        "acceptance_or_verification_id": "V01",
        "contract_sha256": "sha256:" + "a" * 64,
        "diagnostic_ids": ["D_TEST_FAILED"],
        "exit_status": 1,
        "failure_class": "verification_failed",
        "node": "n01_protocol_hash",
        "protocol": "cco.v4",
    }


class ProtocolHashBehaviorTests(unittest.TestCase):
    def hash_text(
        self, value: str, domain: str = "contract"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), "hash", "--domain", domain],
            input=value,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def hash_value(self, value: object, domain: str = "contract") -> subprocess.CompletedProcess[str]:
        return self.hash_text(json.dumps(value, ensure_ascii=False), domain)

    def test_hash_is_canonical_across_object_key_order(self) -> None:
        value = valid_contract()
        first = self.hash_value(value)
        second = self.hash_value(dict(reversed(value.items())))

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertRegex(first.stdout.strip(), r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.stdout, second.stdout)

    def test_contract_hash_has_a_stable_cross_implementation_vector(self) -> None:
        result = self.hash_value(valid_contract())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "sha256:1e9af0d5512998e82580f308ab44220d382dd025fb5683d4b975d38ac64cea94",
        )

    def test_exact_and_prefix_scope_kinds_have_distinct_contract_identities(self) -> None:
        exact = valid_contract()
        exact["write"] = [{"kind": "exact", "path": "generated"}]
        prefix = valid_contract()
        prefix["write"] = [{"kind": "prefix", "path": "generated"}]

        exact_result = self.hash_value(exact)
        prefix_result = self.hash_value(prefix)

        self.assertEqual(exact_result.returncode, 0, exact_result.stderr)
        self.assertEqual(prefix_result.returncode, 0, prefix_result.stderr)
        self.assertNotEqual(exact_result.stdout, prefix_result.stdout)

    def test_contract_rejects_legacy_untyped_write_paths(self) -> None:
        contract = valid_contract()
        contract["write"] = ["src/auth.py"]

        result = self.hash_value(contract)

        self.assertEqual(result.returncode, 2)
        self.assertIn("scope", result.stderr)

    def test_contract_domain_rejects_an_object_that_is_not_a_v4_contract(self) -> None:
        result = self.hash_value({"objective": "ship"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("contract", result.stderr)

    def test_graph_manifest_recomputes_every_full_contract_hash(self) -> None:
        result = self.hash_value(valid_graph_manifest(), "graph_manifest")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^sha256:[0-9a-f]{64}$")

        tampered = valid_graph_manifest()
        tampered["contracts"][0]["contract"]["objective"] = "Tampered objective"
        result = self.hash_value(tampered, "graph_manifest")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match contract", result.stderr)

        duplicate_verification = valid_graph_manifest()
        second_contract = deepcopy(valid_contract())
        second_contract["node"] = "n02_other"
        second_contract["acceptance"][0]["id"] = "A02"
        second_contract["verification"][0]["acceptance_ids"] = ["A02"]
        duplicate_verification["contracts"].append(
            {
                "contract": second_contract,
                "contract_sha256": digest("contract", second_contract),
            }
        )
        duplicate_verification["acceptance_owners"].append(
            {
                "acceptance_id": "A02",
                "implementation_owner": "n02_other",
            }
        )
        result = self.hash_value(duplicate_verification, "graph_manifest")
        self.assertEqual(result.returncode, 2)
        self.assertIn("globally unique", result.stderr)

    def test_worker_initial_binds_the_full_graph_and_acceptance_chain(self) -> None:
        manifest = valid_graph_manifest()
        chain = valid_acceptance_chain()
        worker = valid_worker_initial()
        worker["graph_manifest_sha256"] = digest("graph_manifest", manifest)
        worker["acceptance_chain_sha256"] = digest("acceptance_chain", chain)

        self.assertTrue(digest("input_closure", worker).startswith("sha256:"))

        for missing in ("graph_manifest_sha256", "acceptance_chain_sha256"):
            with self.subTest(missing=missing):
                incomplete = deepcopy(worker)
                del incomplete[missing]
                with self.assertRaises(ProtocolHashError):
                    digest("input_closure", incomplete)

    def test_acceptance_chain_records_one_way_primary_to_independent_upgrade(self) -> None:
        chain = valid_acceptance_chain()
        initial_sha256 = chain["decisions"][0]["decision_sha256"]
        upgraded = {
            "graph_manifest_sha256": chain["graph_manifest_sha256"],
            "mode": "independent",
            "previous_decision_sha256": initial_sha256,
            "protocol": "cco.v4",
            "reasons": ["verification_failure", "worker_retry"],
            "revision": 2,
        }
        chain["decisions"].append(
            {
                "decision": upgraded,
                "decision_sha256": digest("acceptance_decision", upgraded),
            }
        )

        self.assertTrue(digest("acceptance_chain", chain).startswith("sha256:"))

        for mutation in ("drop_history", "downgrade", "break_link"):
            with self.subTest(mutation=mutation):
                tampered = deepcopy(chain)
                with self.assertRaises(ProtocolHashError):
                    if mutation == "drop_history":
                        tampered["decisions"] = tampered["decisions"][1:]
                    elif mutation == "downgrade":
                        tampered["decisions"][1]["decision"]["mode"] = "primary"
                        tampered["decisions"][1]["decision_sha256"] = digest(
                            "acceptance_decision",
                            tampered["decisions"][1]["decision"],
                        )
                    else:
                        tampered["decisions"][1]["decision"][
                            "previous_decision_sha256"
                        ] = "sha256:" + "0" * 64
                        tampered["decisions"][1]["decision_sha256"] = digest(
                            "acceptance_decision",
                            tampered["decisions"][1]["decision"],
                        )
                    digest("acceptance_chain", tampered)

    def test_primary_acceptance_rejects_a_declared_risk(self) -> None:
        chain = valid_acceptance_chain()
        manifest = chain["graph_manifest"]
        contract_record = manifest["contracts"][0]
        contract_record["contract"]["risk_flags"] = ["public_interface"]
        contract_record["contract_sha256"] = digest(
            "contract", contract_record["contract"]
        )
        manifest_sha256 = digest("graph_manifest", manifest)
        chain["graph_manifest_sha256"] = manifest_sha256
        decision = chain["decisions"][0]["decision"]
        decision["graph_manifest_sha256"] = manifest_sha256
        chain["decisions"][0]["decision_sha256"] = digest(
            "acceptance_decision", decision
        )

        with self.assertRaises(ProtocolHashError):
            digest("acceptance_chain", chain)

    def test_graph_manifest_owner_must_be_the_node_that_declares_acceptance(self) -> None:
        manifest = valid_graph_manifest()
        manifest["acceptance_owners"][0]["implementation_owner"] = "ghost"

        result = self.hash_value(manifest, "graph_manifest")

        self.assertEqual(result.returncode, 2)
        self.assertIn("declaring contract node", result.stderr)

    def test_single_routine_contract_can_bind_primary_sol_acceptance(self) -> None:
        result = self.hash_value(valid_acceptance_chain(), "acceptance_chain")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_primary_acceptance_rejects_complex_or_multi_node_graphs(self) -> None:
        complex_chain = valid_acceptance_chain()
        complex_manifest = complex_chain["graph_manifest"]
        complex_contract = complex_manifest["contracts"][0]["contract"]
        complex_contract["lane"] = "complex"
        complex_manifest["contracts"][0]["contract_sha256"] = digest(
            "contract", complex_contract
        )

        multi_chain = valid_acceptance_chain()
        multi_manifest = multi_chain["graph_manifest"]
        second = deepcopy(valid_contract())
        second["node"] = "n02_other"
        second["acceptance"][0]["id"] = "A02"
        second["verification"][0]["id"] = "V02"
        second["verification"][0]["acceptance_ids"] = ["A02"]
        second["write"] = [{"kind": "exact", "path": "src/other.py"}]
        multi_manifest["contracts"].append(
            {"contract": second, "contract_sha256": digest("contract", second)}
        )
        multi_manifest["acceptance_owners"].append(
            {"acceptance_id": "A02", "implementation_owner": "n02_other"}
        )

        for chain in (complex_chain, multi_chain):
            manifest_sha256 = digest("graph_manifest", chain["graph_manifest"])
            chain["graph_manifest_sha256"] = manifest_sha256
            decision = chain["decisions"][0]["decision"]
            decision["graph_manifest_sha256"] = manifest_sha256
            chain["decisions"][0]["decision_sha256"] = digest(
                "acceptance_decision", decision
            )
            with self.subTest(chain=chain):
                result = self.hash_value(chain, "acceptance_chain")
                self.assertEqual(result.returncode, 2)
                self.assertIn("ineligible", result.stderr)

    def test_sol_owned_change_set_is_an_explicit_contract_node(self) -> None:
        contract = valid_contract()
        contract["lane"] = "sol"
        contract["node"] = "sol_n01_protocol_hash"
        contract_result = self.hash_value(contract)
        self.assertEqual(contract_result.returncode, 0, contract_result.stderr)
        manifest = {
            "acceptance_owners": [
                {
                    "acceptance_id": "A01",
                    "implementation_owner": "sol_n01_protocol_hash",
                }
            ],
            "contracts": [
                {
                    "contract": contract,
                    "contract_sha256": contract_result.stdout.strip(),
                }
            ],
            "protocol": "cco.v4",
        }
        manifest_sha256 = digest("graph_manifest", manifest)
        decision = {
            "graph_manifest_sha256": manifest_sha256,
            "mode": "independent",
            "previous_decision_sha256": None,
            "protocol": "cco.v4",
            "reasons": ["sol_owned_change"],
            "revision": 1,
        }
        chain = {
            "decisions": [
                {
                    "decision": decision,
                    "decision_sha256": digest("acceptance_decision", decision),
                }
            ],
            "graph_manifest": manifest,
            "graph_manifest_sha256": manifest_sha256,
            "protocol": "cco.v4",
        }

        result = self.hash_value(chain, "acceptance_chain")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_graph_manifest_limits_the_graph_wide_write_scope(self) -> None:
        first = valid_contract()
        first["write"] = [
            {"kind": "exact", "path": f"generated/first_{index:03d}.py"}
            for index in range(65)
        ]
        second = deepcopy(valid_contract())
        second["node"] = "n02_other"
        second["acceptance"][0]["id"] = "A02"
        second["verification"][0]["id"] = "V02"
        second["verification"][0]["acceptance_ids"] = ["A02"]
        second["write"] = [
            {"kind": "exact", "path": f"generated/second_{index:03d}.py"}
            for index in range(65)
        ]
        manifest = {
            "acceptance_owners": [
                {"acceptance_id": "A01", "implementation_owner": "n01_protocol_hash"},
                {"acceptance_id": "A02", "implementation_owner": "n02_other"},
            ],
            "contracts": [
                {"contract": first, "contract_sha256": digest("contract", first)},
                {"contract": second, "contract_sha256": digest("contract", second)},
            ],
            "protocol": "cco.v4",
        }

        result = self.hash_value(manifest, "graph_manifest")

        self.assertEqual(result.returncode, 2)
        self.assertIn("write scope", result.stderr)

    def test_graph_manifest_rejects_overlapping_and_case_alias_write_scopes(self) -> None:
        cases = (
            (
                {"kind": "prefix", "path": "generated"},
                {"kind": "exact", "path": "generated/file.py"},
            ),
            (
                {"kind": "prefix", "path": "generated"},
                {"kind": "prefix", "path": "generated/nested"},
            ),
            (
                {"kind": "exact", "path": "Generated/File.py"},
                {"kind": "exact", "path": "generated/file.py"},
            ),
        )
        for first_scope, second_scope in cases:
            with self.subTest(first=first_scope, second=second_scope):
                first = valid_contract()
                first["write"] = [first_scope]
                second = deepcopy(valid_contract())
                second["node"] = "n02_other"
                second["acceptance"][0]["id"] = "A02"
                second["verification"][0]["id"] = "V02"
                second["verification"][0]["acceptance_ids"] = ["A02"]
                second["write"] = [second_scope]
                manifest = {
                    "acceptance_owners": [
                        {"acceptance_id": "A01", "implementation_owner": "n01_protocol_hash"},
                        {"acceptance_id": "A02", "implementation_owner": "n02_other"},
                    ],
                    "contracts": [
                        {"contract": first, "contract_sha256": digest("contract", first)},
                        {"contract": second, "contract_sha256": digest("contract", second)},
                    ],
                    "protocol": "cco.v4",
                }

                result = self.hash_value(manifest, "graph_manifest")

                self.assertEqual(result.returncode, 2)
                self.assertIn("overlapping write scopes", result.stderr)

    def test_worker_initial_requires_policy_null_pairings(self) -> None:
        valid = self.hash_value(valid_worker_initial(), "input_closure")
        self.assertEqual(valid.returncode, 0, valid.stderr)

        non_native_null = valid_worker_initial()
        non_native_null["requested_model"] = None
        result = self.hash_value(non_native_null, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requested_model", result.stderr)

        native_value = valid_worker_initial()
        native_value["requested_effort"] = "high"
        result = self.hash_value(native_value, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requested_effort", result.stderr)

    def test_worker_binding_closes_run_and_lease_identity(self) -> None:
        cases = (
            ("run_n02_other_r01", "wl_n01_protocol_hash_r01"),
            ("run_n01_protocol_hash_r01", "wl_n02_other_r01"),
            ("run_n01_protocol_hash_r01", "wl_n01_protocol_hash_r02"),
        )
        for run, lease in cases:
            with self.subTest(run=run, lease=lease):
                value = valid_worker_initial()
                value["run"] = run
                value["lease"] = lease

                result = self.hash_value(value, "input_closure")

                self.assertEqual(result.returncode, 2)
                self.assertIn("identity", result.stderr)

    def test_worker_run_suffix_matches_the_attempt_counter(self) -> None:
        value = valid_worker_initial()
        value["run"] = "run_n01_protocol_hash_r02"
        value["lease"] = "wl_n01_protocol_hash_r02"

        result = self.hash_value(value, "input_closure")

        self.assertEqual(result.returncode, 2)
        self.assertIn("attempt", result.stderr)

        aliased = valid_worker_initial()
        aliased["run"] = "run_n01_protocol_hash_r001"
        aliased["lease"] = "wl_n01_protocol_hash_r001"
        result = self.hash_value(aliased, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("canonical", result.stderr)

    def test_worker_attempt_limit_has_a_small_protocol_cap(self) -> None:
        value = valid_worker_initial()
        value["attempt"] = {"current": 1, "limit": 4}

        result = self.hash_value(value, "input_closure")

        self.assertEqual(result.returncode, 2)
        self.assertIn("limit", result.stderr)

        followups = valid_worker_initial()
        followups["followup"] = {"current": 0, "limit": 3}
        result = self.hash_value(followups, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("limit", result.stderr)

        reviewer_attempts = valid_review_fresh()
        reviewer_attempts["attempt"] = {"current": 1, "limit": 3}
        result = self.hash_value(reviewer_attempts, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("limit", result.stderr)

        reviewer_followups = valid_review_delta()
        reviewer_followups["followup"] = {"current": 1, "limit": 3}
        result = self.hash_value(reviewer_followups, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("limit", result.stderr)

    def test_input_closure_binds_the_native_fork_policy(self) -> None:
        no_fork = valid_worker_initial()
        partial_fork = valid_worker_initial()
        partial_fork["fork_turns"] = "3"

        first = self.hash_value(no_fork, "input_closure")
        second = self.hash_value(partial_fork, "input_closure")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(first.stdout, second.stdout)

        invalid = valid_worker_initial()
        invalid["fork_turns"] = "all"
        result = self.hash_value(invalid, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fork_turns", result.stderr)

        invalid_review = valid_review_fresh()
        invalid_review["fork_turns"] = "1"
        result = self.hash_value(invalid_review, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fork_turns", result.stderr)

    def test_worker_followup_requires_exact_sorted_binding_records(self) -> None:
        valid = self.hash_value(valid_worker_followup(), "input_closure")
        self.assertEqual(valid.returncode, 0, valid.stderr)

        unsorted = valid_worker_followup()
        binding = unsorted["binding"]
        self.assertIsInstance(binding, dict)
        binding["dependencies"] = [
            {"id": "dependency.two", "state_sha256": "sha256:" + "2" * 64},
            {"id": "dependency.one", "state_sha256": "sha256:" + "0" * 64},
        ]
        result = self.hash_value(unsorted, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("dependencies record IDs", result.stderr)

        unknown_nested_key = valid_worker_followup()
        unknown_binding = unknown_nested_key["binding"]
        self.assertIsInstance(unknown_binding, dict)
        unknown_binding["extra"] = "not part of the preimage"
        result = self.hash_value(unknown_nested_key, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown keys", result.stderr)

        outside_acceptance = valid_worker_followup()
        outside_acceptance["verify"][0]["acceptance_ids"] = ["A99"]
        result = self.hash_value(outside_acceptance, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("acceptance IDs", result.stderr)

        invalid_target = valid_worker_followup()
        invalid_target["target"] = "work_n01_protocol_hash_routine_r01"
        result = self.hash_value(invalid_target, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("task path", result.stderr)

    def test_review_input_schemas_require_epoch_and_preserved_contracts(self) -> None:
        fresh = self.hash_value(valid_review_fresh(), "input_closure")
        delta = self.hash_value(valid_review_delta(), "input_closure")
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(delta.returncode, 0, delta.stderr)

        missing_epoch = valid_review_fresh()
        del missing_epoch["epoch"]
        result = self.hash_value(missing_epoch, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing keys", result.stderr)

        invalid_status = valid_review_delta()
        invalid_status["contract_status"] = "changed"
        result = self.hash_value(invalid_status, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract_status", result.stderr)

    def test_failure_and_evidence_domains_require_complete_records(self) -> None:
        failure = self.hash_value(valid_failure(), "failure")
        evidence = self.hash_value(valid_evidence(), "evidence")
        self.assertEqual(failure.returncode, 0, failure.stderr)
        self.assertEqual(evidence.returncode, 0, evidence.stderr)

        unknown_failure_key = valid_failure()
        unknown_failure_key["traceback"] = "must not enter the signature"
        result = self.hash_value(unknown_failure_key, "failure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown keys", result.stderr)

        incomplete_evidence = valid_evidence()
        records = incomplete_evidence["records"]
        self.assertIsInstance(records, list)
        records[0].pop("observed_outcome")
        result = self.hash_value(incomplete_evidence, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing keys", result.stderr)

        wrong_owner = valid_evidence()
        records = wrong_owner["records"]
        self.assertIsInstance(records, list)
        records[0]["implementation_owner"] = "n02_other_owner"
        result = self.hash_value(wrong_owner, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("implementation owner", result.stderr)

        wrong_operation = valid_evidence()
        records = wrong_operation["records"]
        self.assertIsInstance(records, list)
        records[0]["operation"] = "python -m unittest tests.test_unrelated"
        result = self.hash_value(wrong_operation, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("operation", result.stderr)

    def test_unordered_values_and_record_ids_must_be_nfc_utf8_sorted(self) -> None:
        unsorted_paths = valid_contract()
        unsorted_paths["write"] = list(reversed(unsorted_paths["write"]))
        result = self.hash_value(unsorted_paths)
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract.write", result.stderr)

        duplicate_acceptance = valid_contract()
        acceptance = duplicate_acceptance["acceptance"]
        self.assertIsInstance(acceptance, list)
        acceptance.append(deepcopy(acceptance[0]))
        result = self.hash_value(duplicate_acceptance)
        self.assertEqual(result.returncode, 2)
        self.assertIn("acceptance record IDs", result.stderr)

        unsorted_contracts = valid_review_fresh()
        contracts = unsorted_contracts["contracts"]
        self.assertIsInstance(contracts, list)
        contracts.insert(
            0,
            {
                "contract_rev": 1,
                "contract_sha256": "sha256:" + "f" * 64,
                "node": "n02_protocol_hash",
            },
        )
        result = self.hash_value(unsorted_contracts, "input_closure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("contracts record IDs", result.stderr)

        unsorted_evidence = valid_evidence()
        evidence_records = unsorted_evidence["records"]
        self.assertIsInstance(evidence_records, list)
        evidence_records.insert(
            0,
            {
                "acceptance_ids": ["A01"],
                "artifact_sha256s": [],
                "exit_status": 0,
                "implementation_owner": "n01_protocol_hash",
                "observed_outcome": "passed",
                "operation": "re-run test",
                "outcome": "passed",
                "verification_id": "V02",
            },
        )
        result = self.hash_value(unsorted_evidence, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("records record IDs", result.stderr)

        unicode_values = valid_contract()
        unicode_values["constraints"] = ["alpha", "éclair"]
        result = self.hash_value(unicode_values)
        self.assertEqual(result.returncode, 0, result.stderr)
        unicode_values["constraints"] = ["éclair", "alpha"]
        result = self.hash_value(unicode_values)
        self.assertEqual(result.returncode, 2)
        self.assertIn("NFC UTF-8 byte order", result.stderr)

    def test_contract_write_paths_are_canonical_repository_relative_paths(self) -> None:
        invalid_paths = (
            "../escape.py",
            "src/../escape.py",
            "src/./auth.py",
            "/absolute/auth.py",
            "C:/absolute/auth.py",
            "C:relative/auth.py",
            "\\\\server\\share\\auth.py",
            "src\\auth.py",
            "src//auth.py",
            "src/auth.py/",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                value = valid_contract()
                value["write"] = [{"kind": "exact", "path": path}]

                result = self.hash_value(value)

                self.assertEqual(result.returncode, 2)
                self.assertIn("repository-relative path", result.stderr)

        unicode_path = valid_contract()
        unicode_path["write"] = [
            {"kind": "exact", "path": "src/\u9a8c\u8bc1.py"}
        ]
        result = self.hash_value(unicode_path)
        self.assertEqual(result.returncode, 0, result.stderr)

        too_many = valid_contract()
        too_many["write"] = [
            {"kind": "exact", "path": f"src/generated_{index:03d}.py"}
            for index in range(129)
        ]
        result = self.hash_value(too_many)
        self.assertEqual(result.returncode, 2)
        self.assertIn("at most 128", result.stderr)

    def test_repository_paths_never_authorize_git_metadata_segments(self) -> None:
        for path in (".git/config", ".GIT/config", "nested/.git/config"):
            with self.subTest(path=path):
                value = valid_contract()
                value["write"] = [{"kind": "exact", "path": path}]

                result = self.hash_value(value)

                self.assertEqual(result.returncode, 2)
                self.assertIn("repository-relative path", result.stderr)

    def test_repository_paths_reject_win32_alias_and_device_segments(self) -> None:
        invalid_paths = (
            ".git./config",
            "src/file.",
            "src/file ",
            "src/NUL",
            "src/con.txt",
            "src/COM1.log",
            "src/LPT9",
            'src/a<bad>.txt',
            'src/a"bad.txt',
            "src/a|bad.txt",
            "src/a?bad.txt",
            "src/a*bad.txt",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                value = valid_contract()
                value["write"] = [{"kind": "exact", "path": path}]

                result = self.hash_value(value)

                self.assertEqual(result.returncode, 2)
                self.assertIn("repository-relative path", result.stderr)

    def test_review_allowed_paths_use_the_same_canonical_path_identity(self) -> None:
        value = valid_review_fresh()
        value["allowed_paths"] = [
            {"kind": "exact", "path": "src/../escape.py"}
        ]

        result = self.hash_value(value, "input_closure")

        self.assertEqual(result.returncode, 2)
        self.assertIn("repository-relative path", result.stderr)

    def test_schema_types_and_enums_are_not_coerced(self) -> None:
        wrong_type = valid_contract()
        wrong_type["contract_rev"] = True
        result = self.hash_value(wrong_type)
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract_rev must be an integer", result.stderr)

        wrong_enum = valid_contract()
        wrong_enum["lane"] = "expedited"
        result = self.hash_value(wrong_enum)
        self.assertEqual(result.returncode, 2)
        self.assertIn("contract.lane", result.stderr)

        wrong_optional_type = valid_failure()
        wrong_optional_type["exit_status"] = "1"
        result = self.hash_value(wrong_optional_type, "failure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("failure.exit_status", result.stderr)

    def test_evidence_requires_every_contract_verification_record(self) -> None:
        evidence = valid_evidence()
        chain = evidence["acceptance_chain"]
        manifest = chain["graph_manifest"]
        contract_record = manifest["contracts"][0]
        contract = contract_record["contract"]
        contract["verification"].append(
            {
                "acceptance_ids": ["A01"],
                "expected": "A second independent passing observation",
                "id": "V02",
                "operation": "python -m unittest tests/test_project_contract.py",
            }
        )
        contract_record["contract_sha256"] = digest("contract", contract)
        manifest_sha256 = digest("graph_manifest", manifest)
        chain["graph_manifest_sha256"] = manifest_sha256
        decision = chain["decisions"][0]["decision"]
        decision["graph_manifest_sha256"] = manifest_sha256
        chain["decisions"][0]["decision_sha256"] = digest(
            "acceptance_decision", decision
        )
        evidence["acceptance_chain_sha256"] = digest("acceptance_chain", chain)

        result = self.hash_value(evidence, "evidence")

        self.assertEqual(result.returncode, 2)
        self.assertIn("every contract verification", result.stderr)

        complete_only_marker = valid_failure()
        complete_only_marker["failure_class"] = "none"
        result = self.hash_value(complete_only_marker, "failure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("failure.failure_class", result.stderr)

        invalid_outcome = valid_evidence()
        evidence_records = invalid_outcome["records"]
        self.assertIsInstance(evidence_records, list)
        evidence_records[0]["outcome"] = "maybe"
        result = self.hash_value(invalid_outcome, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("outcome", result.stderr)

        invalid_exit = valid_evidence()
        evidence_records = invalid_exit["records"]
        self.assertIsInstance(evidence_records, list)
        evidence_records[0]["exit_status"] = "0"
        result = self.hash_value(invalid_exit, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exit_status", result.stderr)

        contradictory_exit = valid_evidence()
        evidence_records = contradictory_exit["records"]
        self.assertIsInstance(evidence_records, list)
        evidence_records[0]["exit_status"] = 1
        result = self.hash_value(contradictory_exit, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("passed outcome", result.stderr)

    def test_duplicate_object_keys_are_rejected(self) -> None:
        result = self.hash_text('{"objective":"first","objective":"second"}')

        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate object key", result.stderr)

    def test_floating_point_numbers_are_rejected(self) -> None:
        result = self.hash_text('{"attempt_weight":1.25}')

        self.assertEqual(result.returncode, 2)
        self.assertIn("floating-point numbers are not supported", result.stderr)

    def test_integers_outside_the_interoperable_safe_range_are_rejected(self) -> None:
        result = self.hash_text('{"lease_generation":9007199254740992}')

        self.assertEqual(result.returncode, 2)
        self.assertIn("integer is outside the safe range", result.stderr)

    def test_extremely_long_integer_is_rejected_without_a_traceback(self) -> None:
        result = self.hash_text('{"contract_rev":' + "9" * 10_000 + "}")

        self.assertEqual(result.returncode, 2)
        self.assertIn("integer is outside the safe range", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_non_nfc_strings_are_rejected(self) -> None:
        decomposed = "e\u0301"
        result = self.hash_value({"objective": decomposed})

        self.assertEqual(result.returncode, 2)
        self.assertIn("strings must use NFC normalization", result.stderr)

    def test_hash_domains_are_cryptographically_separated(self) -> None:
        contract = self.hash_value(valid_contract(), "contract")
        evidence = self.hash_value(valid_evidence(), "evidence")

        self.assertEqual(contract.returncode, 0, contract.stderr)
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertNotEqual(contract.stdout, evidence.stdout)

    def test_non_json_numeric_constants_are_rejected(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                result = self.hash_text(f'{{"value":{constant}}}')
                self.assertEqual(result.returncode, 2)
                self.assertIn("non-JSON numeric constant", result.stderr)

    def test_object_keys_must_be_ascii_for_cross_language_ordering(self) -> None:
        result = self.hash_value({"目标": "ship"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("object keys must be ASCII", result.stderr)

    def test_top_level_value_must_be_an_object(self) -> None:
        result = self.hash_value(["not", "an", "object"])

        self.assertEqual(result.returncode, 2)
        self.assertIn("input must be a JSON object", result.stderr)

    def test_input_size_and_nesting_are_bounded(self) -> None:
        oversized = self.hash_text('{"value":"' + "x" * (1024 * 1024) + '"}')
        self.assertEqual(oversized.returncode, 2)
        self.assertIn("input exceeds 1048576 bytes", oversized.stderr)

        nested: object = "leaf"
        for _ in range(65):
            nested = [nested]
        too_deep = self.hash_value({"value": nested})
        self.assertEqual(too_deep.returncode, 2)
        self.assertIn("nesting exceeds 64 levels", too_deep.stderr)

        parser_depth = self.hash_text(
            '{"value":' + "[" * 1100 + "0" + "]" * 1100 + "}"
        )
        self.assertEqual(parser_depth.returncode, 2)
        self.assertIn("nesting exceeds 64 levels", parser_depth.stderr)
        self.assertNotIn("Traceback", parser_depth.stderr)

    def test_digest_rejects_an_oversized_canonical_preimage(self) -> None:
        value = valid_contract()
        value["objective"] = "\\" * 600_000

        with self.assertRaisesRegex(
            ProtocolHashError, "canonical input exceeds 1048576 bytes"
        ):
            digest("contract", value)


if __name__ == "__main__":
    unittest.main()

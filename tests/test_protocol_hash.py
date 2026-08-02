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
        "verification": [
            {
                "acceptance_ids": ["A01"],
                "expected": "A sha256 digest",
                "id": "V01",
                "operation": "python -m unittest tests/test_protocol_hash.py",
            }
        ],
        "write": [
            "plugins/codex-cost-orchestrator/scripts/protocol_hash.py",
            "tests/test_protocol_hash.py",
        ],
    }


def valid_evidence() -> dict[str, object]:
    return {
        "acceptance_ids": ["A01"],
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
    return {
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
    return {
        "acceptance": [
            {
                "criterion": "CLI returns a domain-separated digest",
                "id": "A01",
            }
        ],
        "acceptance_ids": ["A01"],
        "accumulated_delta": ["protocol hash schema implementation"],
        "allowed_paths": [
            "plugins/codex-cost-orchestrator/scripts/protocol_hash.py",
            "tests/test_protocol_hash.py",
        ],
        "attempt": {"current": 1, "limit": 2},
        "baseline": "sha256:" + "3" * 64,
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": "sha256:" + "4" * 64,
                "node": "n01_protocol_hash",
            }
        ],
        "current_state": "sha256:" + "5" * 64,
        "epoch": "e01",
        "evidence_sha256": "sha256:" + "6" * 64,
        "followup": {"current": 0, "limit": 1},
        "fork_turns": "none",
        "goal": "Validate canonical CCO v4 preimages",
        "interfaces": ["protocol_hash.py hash --domain input_closure"],
        "kind": "review_fresh",
        "open_risks": [],
        "protocol": "cco.v4",
    }


def valid_review_delta() -> dict[str, object]:
    return {
        "acceptance_ids": ["A01"],
        "attempt": {"current": 1, "limit": 2},
        "contract_status": "preserved",
        "contracts": [
            {
                "contract_rev": 1,
                "contract_sha256": "sha256:" + "4" * 64,
                "node": "n01_protocol_hash",
            }
        ],
        "current_state": "sha256:" + "7" * 64,
        "delta": ["Add exact input-preimage validation."],
        "epoch": "e01",
        "evidence_sha256": "sha256:" + "8" * 64,
        "followup": {"current": 1, "limit": 1},
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
            "sha256:1e8288d57193a008be6dfd0a60997ce62bf7e0c8ceb39014aa701062f8f12998",
        )

    def test_contract_domain_rejects_an_object_that_is_not_a_v4_contract(self) -> None:
        result = self.hash_value({"objective": "ship"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("contract", result.stderr)

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

        conflicting_owner = valid_evidence()
        records = conflicting_owner["records"]
        self.assertIsInstance(records, list)
        second = deepcopy(records[0])
        second["verification_id"] = "V02"
        second["implementation_owner"] = "n02_other_owner"
        records.append(second)
        result = self.hash_value(conflicting_owner, "evidence")
        self.assertEqual(result.returncode, 2)
        self.assertIn("implementation owner", result.stderr)

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
                value["write"] = [path]

                result = self.hash_value(value)

                self.assertEqual(result.returncode, 2)
                self.assertIn("repository-relative path", result.stderr)

        unicode_path = valid_contract()
        unicode_path["write"] = ["src/\u9a8c\u8bc1.py"]
        result = self.hash_value(unicode_path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_allowed_paths_use_the_same_canonical_path_identity(self) -> None:
        value = valid_review_fresh()
        value["allowed_paths"] = ["src/../escape.py"]

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


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
HOOK = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "hooks"
    / "subagent_stop.py"
)
HOOKS_CONFIG = HOOK.parent / "hooks.json"
HASH_HELPER = HOOK.parent.parent / "scripts" / "protocol_hash.py"


def subagent_stop_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "11111111-1111-7111-8111-111111111111",
        "turn_id": "turn-1",
        "transcript_path": None,
        "agent_transcript_path": None,
        "cwd": str(REPO),
        "hook_event_name": "SubagentStop",
        "model": "gpt-5.6-luna",
        "permission_mode": "default",
        "stop_hook_active": False,
        "agent_id": "22222222-2222-7222-8222-222222222222",
        "agent_type": "ordinary_worker",
        "last_assistant_message": "ordinary task complete",
    }
    payload.update(overrides)
    return payload


def run_hook(
    payload: str | dict[str, object], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def run_hook_utf8(payload: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )


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


def failure_fields(status: str) -> tuple[str, str, str, str, str]:
    if status == "complete":
        return "none", "none", "none", "[]", "none"
    value: dict[str, object] = {
        "acceptance_or_verification_id": "V01",
        "contract_sha256": "sha256:" + "a" * 64,
        "diagnostic_ids": ["D_TEST_FAILED"],
        "exit_status": 1,
        "failure_class": "verification_failed",
        "node": "n01_auth",
        "protocol": "cco.v4",
    }
    return (
        "V01",
        "verification_failed",
        "1",
        "[D_TEST_FAILED]",
        protocol_hash("failure", value),
    )


def complete_work_result(status: str = "complete") -> str:
    failure_id, failure_class, exit_status, diagnostics, signature = failure_fields(
        status
    )
    return f"""CCO_WORK_RESULT cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: sha256:{"a" * 64}
INPUT_CLOSURE_SHA256: sha256:{"b" * 64}
RUN: run_n01_auth_r01
ATTEMPT: 1/2
FOLLOWUP: 0/1
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
ACCEPTANCE_IDS: [A01]
STATUS: {status}
FAILURE_ACCEPTANCE_OR_VERIFICATION_ID: {failure_id}
FAILURE_CLASS: {failure_class}
FAILURE_EXIT_STATUS: {exit_status}
FAILURE_DIAGNOSTIC_IDS: {diagnostics}
FAILURE_SIGNATURE: {signature}
CHANGED:
- src/auth.py: implemented bounded authentication change
VERIFIED:
- V01 [A01]: python -m unittest tests.test_auth => passed
JUDGMENT:
- none
DEVIATIONS:
- none
BLOCKERS:
- none
"""


def complete_review_result(verdict: str = "ship") -> str:
    return f"""CCO_REVIEW_RESULT cco.v4
EPOCH: e01
MODE: fresh
ATTEMPT: 1/2
FOLLOWUP: 0/1
INPUT_CLOSURE_SHA256: sha256:{"c" * 64}
ACCEPTANCE_IDS: [A01]
EVIDENCE_SHA256: sha256:{"d" * 64}
REVIEWED_STATE: sha256:{"e" * 64}
VERDICT: {verdict}
REASON: Acceptance evidence matches the reviewed state.
FINDINGS:
- none
RESIDUAL_RISK:
- none
"""


class ReviewHookBehaviorTests(unittest.TestCase):
    def test_default_plugin_hook_config_exposes_subagent_stop_command(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        manifest = json.loads(
            (HOOK.parent.parent / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        groups = config["hooks"]["SubagentStop"]

        self.assertNotIn("hooks", manifest)
        self.assertEqual(set(config["hooks"]), {"PreToolUse", "SubagentStop"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            groups[0]["matcher"],
            "cost_orchestrator_routine_worker|cost_orchestrator_complex_worker|cost_orchestrator_reviewer",
        )
        handlers = groups[0]["hooks"]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0]["type"], "command")
        self.assertIn(
            "${PLUGIN_ROOT}/hooks/subagent_stop.py", handlers[0]["command"]
        )
        self.assertIn(
            "${PLUGIN_ROOT}/hooks/subagent_stop.py",
            handlers[0]["commandWindows"],
        )
        self.assertEqual(handlers[0]["timeout"], 5)
        self.assertFalse(handlers[0]["async"])

    def test_unrelated_subagent_is_allowed(self) -> None:
        result = run_hook(subagent_stop_payload())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_first_incomplete_cco_worker_result_requests_one_continuation(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message="Implemented the requested change.",
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("CCO_WORK_RESULT cco.v4", output["reason"])
        self.assertIn("do not redo completed work", output["reason"])

    def test_result_header_without_required_fields_is_incomplete(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=(
                    "CCO_WORK_RESULT cco.v4\n"
                    "NODE: n01\n"
                    "STATUS: complete\n"
                ),
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("missing:", output["reason"])
        self.assertIn("RUN", output["reason"])

    def test_worker_result_without_run_identity_is_incomplete(self) -> None:
        message = complete_work_result().replace("RUN: run_n01_auth_r01\n", "")
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("RUN", output["reason"])

    def test_cco_text_from_an_unpinned_agent_type_is_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="temporary_worker",
                last_assistant_message=(
                    "CCO_WORK_RESULT cco.v4\n"
                    "NODE: n01\n"
                ),
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_incomplete_reviewer_result_requests_reviewer_protocol(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_reviewer",
                model="gpt-5.6-sol",
                last_assistant_message="Review finished.",
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("CCO_REVIEW_RESULT cco.v4", output["reason"])

    def test_complete_worker_result_is_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=complete_work_result(),
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_hook_decodes_outer_json_as_utf8_before_result_validation(self) -> None:
        message = complete_work_result().replace(
            "JUDGMENT:\n- none",
            "JUDGMENT:\n- \u9a8c UTF-8 transport",
        ).replace("RUN: run_n01_auth_r01", "RUN: invalid")
        result = run_hook_utf8(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["decision"], "block")

    def test_unicode_line_separators_remain_result_field_content(self) -> None:
        message = complete_work_result().replace(
            "python -m unittest tests.test_auth => passed",
            "python -m unittest tests.test_auth => passed\u2029with details",
        )
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_worker_result_with_malformed_identity_hash_is_incomplete(self) -> None:
        message = complete_work_result().replace(
            "CONTRACT_SHA256: sha256:" + "a" * 64,
            "CONTRACT_SHA256: sha256:not-a-hash",
        )
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("CONTRACT_SHA256", output["reason"])

    def test_worker_result_with_invalid_counters_or_generations_is_incomplete(self) -> None:
        cases = (
            ("CONTRACT_REV: 1", "CONTRACT_REV: 0", "CONTRACT_REV"),
            ("ATTEMPT: 1/2", "ATTEMPT: 0/2", "ATTEMPT"),
            ("ATTEMPT: 1/2", "ATTEMPT: 3/2", "ATTEMPT"),
            ("FOLLOWUP: 0/1", "FOLLOWUP: 2/1", "FOLLOWUP"),
            ("LEASE_GENERATION: 1", "LEASE_GENERATION: 0", "LEASE_GENERATION"),
            ("STOP_GENERATION: 0", "STOP_GENERATION: -1", "STOP_GENERATION"),
            (
                "STOP_GENERATION: 0",
                "STOP_GENERATION: " + "9" * 10_000,
                "STOP_GENERATION",
            ),
        )
        for original, replacement, field in cases:
            with self.subTest(field=field, replacement=replacement):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_complex_worker",
                        last_assistant_message=complete_work_result().replace(
                            original, replacement
                        ),
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn(field, output["reason"])

    def test_acceptance_ids_must_be_nonempty_sorted_and_unique(self) -> None:
        for acceptance_ids in ("[]", "[A02,A01]", "[A01,A01]", "[acceptance]"):
            with self.subTest(acceptance_ids=acceptance_ids):
                message = complete_work_result().replace(
                    "ACCEPTANCE_IDS: [A01]",
                    f"ACCEPTANCE_IDS: {acceptance_ids}",
                )
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_routine_worker",
                        last_assistant_message=message,
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn("ACCEPTANCE_IDS", output["reason"])

    def test_failure_signature_matches_worker_status(self) -> None:
        failure_hash = failure_fields("partial")[-1]
        cases = (
            (
                complete_work_result(),
                "FAILURE_SIGNATURE: none",
                f"FAILURE_SIGNATURE: {failure_hash}",
            ),
            (
                complete_work_result("partial"),
                f"FAILURE_SIGNATURE: {failure_hash}",
                "FAILURE_SIGNATURE: sha256:" + "f" * 64,
            ),
            (
                complete_work_result("blocked"),
                "FAILURE_CLASS: verification_failed",
                "FAILURE_CLASS: none",
            ),
            (
                complete_work_result("blocked"),
                "FAILURE_DIAGNOSTIC_IDS: [D_TEST_FAILED]",
                "FAILURE_DIAGNOSTIC_IDS: [D02,D01]",
            ),
        )
        for message, original, replacement in cases:
            with self.subTest(replacement=replacement):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_routine_worker",
                        last_assistant_message=message.replace(original, replacement),
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn("FAILURE_SIGNATURE", output["reason"])

    def test_verified_requires_verification_and_acceptance_ids(self) -> None:
        cases = (
            "- python -m unittest tests.test_auth => passed",
            "- V01: python -m unittest tests.test_auth => passed",
            "- V01 [A02,A01]: python -m unittest tests.test_auth => passed",
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_routine_worker",
                        last_assistant_message=complete_work_result().replace(
                            "- V01 [A01]: python -m unittest tests.test_auth => passed",
                            replacement,
                        ),
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn("VERIFIED", output["reason"])

    def test_one_optional_text_fence_is_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=f"```text\n{complete_work_result()}```",
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_conflicting_or_trailing_envelopes_are_incomplete(self) -> None:
        cases = (
            complete_work_result() + "\ntrailing prose",
            complete_work_result() + "\nUNEXPECTED: value",
            complete_work_result() + "\nCCO_WORK_RESULT cco.v4\nSTATUS: blocked",
            f"```text\n{complete_work_result()}```\ntrailing prose",
            f"```text\n{complete_work_result()}```\n```text\n{complete_work_result()}```",
        )
        for message in cases:
            with self.subTest(message=message[-80:]):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_complex_worker",
                        last_assistant_message=message,
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")

    def test_content_before_the_first_result_field_is_incomplete(self) -> None:
        message = complete_work_result().replace(
            "CCO_WORK_RESULT cco.v4\n",
            "CCO_WORK_RESULT cco.v4\nleading prose must not be ignored\n",
            1,
        )
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")

    def test_oversized_cco_result_packet_is_incomplete(self) -> None:
        message = complete_work_result().replace(
            "- none\nDEVIATIONS:", "- " + "x" * (1024 * 1024) + "\nDEVIATIONS:", 1
        )
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_worker_result_requires_matching_run_and_lease_identity(self) -> None:
        cases = (
            complete_work_result().replace(
                "RUN: run_n01_auth_r01", "RUN: arbitrary_run"
            ),
            complete_work_result().replace(
                "LEASE: wl_n01_auth_r01", "LEASE: wl_n01_auth_r02"
            ),
        )
        for message in cases:
            with self.subTest(message=message.splitlines()[5:9]):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_complex_worker",
                        last_assistant_message=message,
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")

    def test_complete_reviewer_result_is_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_reviewer",
                model="gpt-5.6-sol",
                last_assistant_message=complete_review_result("fix-first"),
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_reviewer_result_requires_well_formed_closure_fields(self) -> None:
        cases = (
            (
                "EVIDENCE_SHA256: sha256:" + "d" * 64,
                "EVIDENCE_SHA256: invalid",
                "EVIDENCE_SHA256",
            ),
            ("ATTEMPT: 1/2", "ATTEMPT: 0/2", "ATTEMPT"),
            ("ACCEPTANCE_IDS: [A01]", "ACCEPTANCE_IDS: [A02,A01]", "ACCEPTANCE_IDS"),
        )
        for original, replacement, field in cases:
            with self.subTest(field=field):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_reviewer",
                        model="gpt-5.6-sol",
                        last_assistant_message=complete_review_result().replace(
                            original, replacement
                        ),
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")
                self.assertIn(field, output["reason"])

    def test_valid_nonfinal_protocol_outcomes_are_left_to_the_orchestrator(self) -> None:
        cases = (
            (
                "cost_orchestrator_routine_worker",
                complete_work_result("partial"),
            ),
            (
                "cost_orchestrator_complex_worker",
                complete_work_result("blocked"),
            ),
            (
                "cost_orchestrator_reviewer",
                complete_review_result("rethink"),
            ),
        )
        for agent_type, message in cases:
            with self.subTest(agent_type=agent_type):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type=agent_type,
                        last_assistant_message=message,
                    )
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_invalid_reviewer_mode_is_incomplete(self) -> None:
        message = complete_review_result().replace("MODE: fresh", "MODE: resumed")
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_reviewer",
                model="gpt-5.6-sol",
                last_assistant_message=message,
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("MODE", output["reason"])

    def test_reviewer_epoch_and_mode_counter_must_be_coherent(self) -> None:
        cases = (
            complete_review_result().replace("EPOCH: e01", "EPOCH: invalid"),
            complete_review_result().replace("FOLLOWUP: 0/1", "FOLLOWUP: 1/1"),
            complete_review_result()
            .replace("MODE: fresh", "MODE: delta")
            .replace("FOLLOWUP: 0/1", "FOLLOWUP: 0/1"),
        )
        for message in cases:
            with self.subTest(message=message.splitlines()[1:6]):
                result = run_hook(
                    subagent_stop_payload(
                        agent_type="cost_orchestrator_reviewer",
                        model="gpt-5.6-sol",
                        last_assistant_message=message,
                    )
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["decision"], "block")

    def test_active_second_stop_is_always_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_routine_worker",
                stop_hook_active=True,
                last_assistant_message="still missing the required result packet",
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_malformed_input_fails_open(self) -> None:
        result = run_hook("{not valid json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_hook_does_not_write_to_the_active_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            result = run_hook(
                subagent_stop_payload(
                    agent_type="cost_orchestrator_routine_worker",
                    last_assistant_message="incomplete",
                    cwd=str(workspace),
                ),
                cwd=workspace,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

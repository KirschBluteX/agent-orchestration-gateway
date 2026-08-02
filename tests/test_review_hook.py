import json
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


def complete_work_result(status: str = "complete") -> str:
    return f"""CCO_WORK_RESULT cco.v3
NODE: n01_auth
CONTRACT_REV: 1
RUN: run_n01_auth_r01
LEASE: wl_n01_auth_r01
STATUS: {status}
CHANGED:
- src/auth.py: implemented bounded authentication change
VERIFIED:
- python -m unittest tests.test_auth => passed
JUDGMENT:
- none
DEVIATIONS:
- none
BLOCKERS:
- none
"""


def complete_review_result(verdict: str = "ship") -> str:
    return f"""CCO_REVIEW_RESULT cco.v3
EPOCH: e01
MODE: fresh
REVIEWED_STATE: state-123
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
        self.assertEqual(set(config["hooks"]), {"SubagentStop"})
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
        self.assertIn("CCO_WORK_RESULT cco.v3", output["reason"])
        self.assertIn("do not redo completed work", output["reason"])

    def test_result_header_without_required_fields_is_incomplete(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=(
                    "CCO_WORK_RESULT cco.v3\n"
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
                    "CCO_WORK_RESULT cco.v3\n"
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
        self.assertIn("CCO_REVIEW_RESULT cco.v3", output["reason"])

    def test_complete_worker_result_is_allowed(self) -> None:
        result = run_hook(
            subagent_stop_payload(
                agent_type="cost_orchestrator_complex_worker",
                last_assistant_message=complete_work_result(),
            )
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

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

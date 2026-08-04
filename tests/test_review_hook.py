from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks" / "subagent_stop.py"
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from packet_compiler import compile_dispatch, compile_result, parse_message  # noqa: E402
from tests.v6_test_support import fixed_route_plan  # noqa: E402


def dispatch(*, review: bool = False) -> tuple[dict[str, object], dict[str, object]]:
    spec: dict[str, object] = {
        "baseline": "sha256:" + "b" * 64,
        "contract": {"node": "review_e01" if review else "n01_v6", "objective": "bounded result"},
        "judgment": "complex" if review else "routine",
        "kind": "review" if review else "work",
        "node": "review_e01" if review else "n01_v6",
        "purpose": "acceptance" if review else "implementation",
        "route_plan": fixed_route_plan(
            purpose="acceptance" if review else "implementation",
            judgment="complex" if review else "routine",
            model="gpt-5.6-terra" if review else "gpt-5.6-luna",
        ),
    }
    if review:
        spec.update(
            {
                "acceptance": {"mode": "independent"},
                "current_state": "sha256:" + "c" * 64,
                "epoch": "e01",
                "evidence": {"records": []},
                "mode": "fresh",
            }
        )
    native = compile_dispatch(spec)
    return native, parse_message(native["message"])


def payload(role: str, message: str, *, task_name: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": "session-v6-result",
        "cwd": str(ROOT),
        "hook_event_name": "SubagentStop",
        "stop_hook_active": False,
        "agent_type": role,
        "agent_id": "/root/" + task_name,
        "last_assistant_message": message,
    }
    value.update(overrides)
    return value


def run(value: object, ledger_dir: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
        env={**os.environ, "CCO_LEDGER_DIR": ledger_dir},
    )


class ReviewHookBehaviorTests(unittest.TestCase):
    def test_compact_worker_and_review_results_are_allowed(self) -> None:
        cases = []
        worker, worker_capsule = dispatch()
        cases.append(
            (
                worker,
                compile_result(
                    worker_capsule,
                    status="complete",
                    disposition="retire",
                    changed=["src/policy.py"],
                ),
            )
        )
        reviewer, review_capsule = dispatch(review=True)
        cases.append(
            (
                reviewer,
                compile_result(
                    review_capsule,
                    status="complete",
                    disposition="accept",
                    verdict="ship",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            for native, message in cases:
                with self.subTest(role=native["agent_type"]):
                    result = run(
                        payload(
                            str(native["agent_type"]),
                            message,
                            task_name=str(native["task_name"]),
                        ),
                        directory,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_malformed_v6_result_requests_one_envelope_repair(self) -> None:
        native, capsule = dispatch()
        message = compile_result(capsule, status="complete", disposition="retire")
        malformed = message.replace("RESULT_SHA256:", "BROKEN_SHA256:", 1)
        with tempfile.TemporaryDirectory() as directory:
            outcome = json.loads(
                run(
                    payload(
                        str(native["agent_type"]),
                        malformed,
                        task_name=str(native["task_name"]),
                    ),
                    directory,
                ).stdout
            )
        self.assertEqual(outcome["decision"], "block")
        self.assertIn("CCO_RESULT cco.v6", outcome["reason"])

    def test_second_stop_does_not_create_an_unbounded_repair_loop(self) -> None:
        native, _capsule = dispatch()
        with tempfile.TemporaryDirectory() as directory:
            result = run(
                payload(
                    str(native["agent_type"]),
                    "incomplete",
                    task_name=str(native["task_name"]),
                    stop_hook_active=True,
                ),
                directory,
            )
        self.assertEqual(result.stdout, "")

    def test_unrelated_agent_is_not_claimed_by_cco(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run(
                payload("ordinary_worker", "ordinary result", task_name="ordinary"),
                directory,
            )
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()

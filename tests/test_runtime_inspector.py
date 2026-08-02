import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
INSPECTOR = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "scripts"
    / "inspect_agent_runtime.py"
)


class RuntimeInspectorBehaviorTests(unittest.TestCase):
    def test_resolves_canonical_path_with_exact_parent_thread(self) -> None:
        parent_id = "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
        target = "/root/work_n01_auth_routine_r01"
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "08" / "02"
            sessions.mkdir(parents=True)
            for current_parent, current_child in (
                (parent_id, child_id),
                (
                    "cccccccc-cccc-7ccc-8ccc-cccccccccccc",
                    "dddddddd-dddd-7ddd-8ddd-dddddddddddd",
                ),
            ):
                rollout = sessions / f"rollout-path-{current_child}.jsonl"
                records = [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": current_child,
                            "parent_thread_id": current_parent,
                            "agent_path": target,
                            "agent_role": "cost_orchestrator_routine_worker",
                        },
                    },
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-worker", "effort": "max"},
                    },
                ]
                rollout.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(Path(temp_dir) / "sessions"),
                    "--parent-thread-id",
                    parent_id,
                    target,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["thread_id"], child_id)
            self.assertNotIn(parent_id, result.stdout)
            self.assertNotIn(target, result.stdout)

            environment = os.environ.copy()
            environment["CODEX_THREAD_ID"] = parent_id
            from_environment = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(Path(temp_dir) / "sessions"),
                    target,
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(from_environment.returncode, 0, from_environment.stderr)
            self.assertEqual(json.loads(from_environment.stdout)["thread_id"], child_id)

    def test_emits_only_allowlisted_consistent_routing_metadata(self) -> None:
        thread_id = "11111111-1111-7111-8111-111111111111"
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "08" / "02"
            sessions.mkdir(parents=True)
            rollout = sessions / f"rollout-2026-08-02T00-00-00-{thread_id}.jsonl"
            records = [
                {
                    "type": "response_item",
                    "payload": {"prompt": "DO_NOT_LEAK_PROMPT"},
                },
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "parent_thread_id": "DO_NOT_LEAK_PARENT",
                        "agent_role": "cost_orchestrator_routine_worker",
                        "agent_path": "DO_NOT_LEAK_AGENT_PATH",
                        "model_provider": "DO_NOT_LEAK_PROVIDER",
                        "base_instructions": "DO_NOT_LEAK_INSTRUCTIONS",
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {
                        "model": "gpt-5.6-luna",
                        "effort": "max",
                        "sandbox_policy": {
                            "type": "workspace-write",
                            "secret": "DO_NOT_LEAK_SANDBOX",
                        },
                        "permission_profile": {
                            "type": "disabled",
                            "secret": "DO_NOT_LEAK_PERMISSION",
                        },
                        "cwd": "DO_NOT_LEAK_CWD",
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(Path(temp_dir) / "sessions"),
                    thread_id,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(
                set(output),
                {
                    "thread_id",
                    "agent_role",
                    "model",
                    "effort",
                    "sandbox_policy_type",
                    "permission_profile_type",
                },
            )
            self.assertEqual(output["thread_id"], thread_id)
            self.assertEqual(output["model"], "gpt-5.6-luna")
            self.assertNotIn("DO_NOT_LEAK", result.stdout)

    def test_rejects_invalid_or_ambiguous_thread_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions"
            sessions.mkdir()
            invalid = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(sessions),
                    "../not-a-thread",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(invalid.stdout, "")

            thread_id = "22222222-2222-7222-8222-222222222222"
            for day in ("01", "02"):
                directory = sessions / "2026" / "08" / day
                directory.mkdir(parents=True)
                (directory / f"rollout-{day}-{thread_id}.jsonl").write_text(
                    "{}\n", encoding="utf-8"
                )
            ambiguous = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(sessions),
                    thread_id,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(ambiguous.returncode, 0)
            self.assertEqual(ambiguous.stdout, "")
            self.assertNotIn(str(sessions), ambiguous.stderr)

    def test_rejects_inconsistent_turn_metadata_without_echoing_values(self) -> None:
        thread_id = "33333333-3333-7333-8333-333333333333"
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "08" / "02"
            sessions.mkdir(parents=True)
            rollout = sessions / f"rollout-conflict-{thread_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "agent_role": "cost_orchestrator_complex_worker",
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"model": "DO_NOT_LEAK_MODEL_A", "effort": "max"},
                },
                {
                    "type": "turn_context",
                    "payload": {"model": "DO_NOT_LEAK_MODEL_B", "effort": "max"},
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--sessions-dir",
                    str(Path(temp_dir) / "sessions"),
                    thread_id,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("DO_NOT_LEAK", result.stderr)

    def test_requires_role_model_and_effort(self) -> None:
        cases = {
            "missing-role": ({"model": "gpt-5.6-luna", "effort": "max"}, None),
            "missing-model": ({"effort": "max"}, "cost_orchestrator_routine_worker"),
            "missing-effort": (
                {"model": "gpt-5.6-luna"},
                "cost_orchestrator_routine_worker",
            ),
        }
        for index, (label, (turn, role)) in enumerate(cases.items(), start=4):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                thread_id = f"{index}{index}{index}{index}{index}{index}{index}{index}-{index}{index}{index}{index}-7{index}{index}{index}-8{index}{index}{index}-{index}{index}{index}{index}{index}{index}{index}{index}{index}{index}{index}{index}"
                sessions = Path(temp_dir) / "sessions" / "2026" / "08" / "02"
                sessions.mkdir(parents=True)
                rollout = sessions / f"rollout-missing-{thread_id}.jsonl"
                rollout.write_text(
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": thread_id, "agent_role": role},
                        }
                    )
                    + "\n"
                    + json.dumps({"type": "turn_context", "payload": turn})
                    + "\n",
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(INSPECTOR),
                        "--sessions-dir",
                        str(Path(temp_dir) / "sessions"),
                        thread_id,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_expected_routing_values_fail_closed_without_fixed_worker_models(self) -> None:
        thread_id = "99999999-9999-7999-8999-999999999999"
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "sessions"
            sessions = sessions_root / "2026" / "08" / "02"
            sessions.mkdir(parents=True)
            rollout = sessions / f"rollout-user-choice-{thread_id}.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "agent_role": "cost_orchestrator_complex_worker",
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {
                        "model": "gpt-user-selected-worker",
                        "effort": "ultra",
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            base = [
                sys.executable,
                str(INSPECTOR),
                "--sessions-dir",
                str(sessions_root),
                "--expect-role",
                "cost_orchestrator_complex_worker",
                "--expect-model",
                "gpt-user-selected-worker",
                "--expect-effort",
                "ultra",
                thread_id,
            ]
            matched = subprocess.run(
                base, text=True, capture_output=True, check=False
            )
            self.assertEqual(matched.returncode, 0, matched.stderr)
            self.assertEqual(json.loads(matched.stdout)["effort"], "ultra")

            mismatched = subprocess.run(
                [
                    *base[:-2],
                    "high",
                    thread_id,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(mismatched.returncode, 0)
            self.assertEqual(mismatched.stdout, "")
            self.assertNotIn("gpt-user-selected-worker", mismatched.stderr)


if __name__ == "__main__":
    unittest.main()

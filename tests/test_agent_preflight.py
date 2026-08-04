from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "codex-cost-orchestrator" / "hooks" / "agent_preflight.py"
HOOKS_CONFIG = HOOK.parent / "hooks.json"
sys.path.insert(0, str(HOOK.parent))

import agent_preflight  # noqa: E402
sys.path.insert(0, str(HOOK.parent.parent / "scripts"))
from packet_compiler import compile_dispatch  # noqa: E402
from graph_compiler import prepare_dispatch_graph  # noqa: E402
from task_ledger import TaskLedger  # noqa: E402
from tests.v6_test_support import (  # noqa: E402
    closed_graph_node,
    dispatch_decision,
    fixed_route_plan,
)


def payload(tool_input: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "spawn_agent",
        "tool_input": tool_input,
    }
    value.update(overrides)
    return value


def worker_input() -> dict[str, object]:
    plan = fixed_route_plan()
    decision = dispatch_decision()
    return compile_dispatch(
        {
            "acceptance": decision["derived"]["acceptance"],
            "baseline": "sha256:" + "b" * 64,
            "contract": {"node": "n01_protocol", "objective": "bounded result"},
            "decision": decision,
            "graph_sha256": "sha256:" + "a" * 64,
            "judgment": "routine",
            "kind": "work",
            "node": "n01_protocol",
            "purpose": "implementation",
            "route_plan": plan,
        }
    )


def run_hook(value: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(HOOK)],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )


class AgentPreflightBehaviorTests(unittest.TestCase):
    def test_canonical_v6_worker_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "src").mkdir()
            (repo / "src" / "owned.txt").write_text("baseline\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "CCO_LEDGER_DIR": str(root / "ledger"),
                    "CODEX_THREAD_ID": "preflight-session",
                },
            ):
                prepared = prepare_dispatch_graph(
                    [closed_graph_node()],
                    route_plan=fixed_route_plan(judgment="complex"),
                    native_capacity=1,
                    repo=repo,
                )
                result = run_hook(
                    payload(
                        prepared["dispatches"][0],
                        cwd=str(repo),
                        session_id="preflight-session",
                        tool_use_id="spawn-preflight",
                    )
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_manual_labels_without_a_derived_decision_are_blocked(self) -> None:
        plan = fixed_route_plan()
        manual = compile_dispatch(
            {
                "baseline": "sha256:" + "b" * 64,
                "contract": {"node": "n01_manual", "objective": "manual labels"},
                "graph_sha256": "sha256:" + "a" * 64,
                "judgment": "routine",
                "kind": "work",
                "node": "n01_manual",
                "purpose": "implementation",
                "route_plan": plan,
            }
        )

        outcome = agent_preflight.evaluate(payload(manual))

        self.assertEqual(outcome["decision"], "block")

    def test_legacy_or_substituted_packet_is_blocked(self) -> None:
        canonical = worker_input()
        for message in (
            "CCO_WORK cco.v5\nNODE: n01_protocol",
            str(canonical["message"]).replace("bounded result", "substituted objective"),
        ):
            with self.subTest(message=message[:24]):
                outcome = json.loads(
                    run_hook(payload({**canonical, "message": message})).stdout
                )
                self.assertEqual(outcome["decision"], "block")

    def test_every_visible_agent_spawn_requires_a_declared_cco_role(self) -> None:
        for tool_input in (
            {"task_name": "ordinary", "message": "do work"},
            {**worker_input(), "agent_type": "default"},
        ):
            with self.subTest(tool_input=tool_input):
                outcome = agent_preflight.evaluate(payload(tool_input))
                self.assertEqual(outcome["decision"], "block")

    def test_spawn_override_and_native_policy_cannot_bypass_hashes(self) -> None:
        extra = {**worker_input(), "sandbox_mode": "danger-full-access"}
        substituted = {**worker_input(), "model": "gpt-5.6-sol"}
        for tool_input in (extra, substituted):
            outcome = agent_preflight.evaluate(payload(tool_input))
            self.assertEqual(outcome["decision"], "block")

    def test_plain_messages_to_managed_owners_are_blocked_but_ordinary_targets_pass(self) -> None:
        owner = "/root/work_n01_protocol_r01"
        identity = {
            "node": "n01_protocol",
            "contract_rev": 1,
            "contract_sha256": "sha256:" + "a" * 64,
            "input_sha256": "sha256:" + "b" * 64,
            "generation": 1,
            "cursor": 0,
            "role": "cost_orchestrator_write_leaf",
            "run": "work_n01_protocol_r01",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CCO_LEDGER_DIR": directory}
        ):
            ledger = TaskLedger(Path(directory), "session-managed")
            ledger.reserve("spawn-managed", identity)
            ledger.activate("spawn-managed", owner)
            for tool_name in ("send_message", "followup_task"):
                with self.subTest(tool_name=tool_name):
                    outcome = agent_preflight.evaluate(
                        payload(
                            {"target": owner, "message": "please continue"},
                            tool_name=tool_name,
                            session_id="session-managed",
                            cwd=str(ROOT),
                            tool_use_id=f"plain-{tool_name}",
                        )
                    )
                    self.assertEqual(outcome["decision"], "block")

            retired_owner = owner
            current_owner = "/root/work_n01_protocol_r02"
            ledger.retire(retired_owner)
            ledger.reserve(
                "spawn-current",
                {**identity, "generation": 2, "run": "work_n01_protocol_r02"},
            )
            ledger.activate("spawn-current", current_owner)
            for target in (retired_owner, current_owner):
                with self.subTest(target=target):
                    outcome = agent_preflight.evaluate(
                        payload(
                            {"target": target, "message": "please continue"},
                            tool_name="send_message",
                            session_id="session-managed",
                            cwd=str(ROOT),
                            tool_use_id=f"plain-{target.rsplit('/', 1)[-1]}",
                        )
                    )
                    self.assertEqual(outcome["decision"], "block")

            ordinary = agent_preflight.evaluate(
                payload(
                    {"target": "/root/ordinary_worker", "message": "please continue"},
                    tool_name="send_message",
                    session_id="session-managed",
                    cwd=str(ROOT),
                    tool_use_id="plain-ordinary",
                )
            )
            self.assertEqual(ordinary, {})

    def test_hook_config_covers_full_ledger_lifecycle(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(
            set(config["hooks"]),
            {"PreToolUse", "PostToolUse", "SubagentStop", "SessionEnd"},
        )
        serialized = json.dumps(config, sort_keys=True)
        for script in ("agent_preflight.py", "ledger_runtime.py", "subagent_stop.py"):
            self.assertIn(script, serialized)
        for obsolete in ("agent_postflight.py", "interrupt_preflight.py", "ledger_cleanup.py"):
            self.assertNotIn(obsolete, serialized)


if __name__ == "__main__":
    unittest.main()

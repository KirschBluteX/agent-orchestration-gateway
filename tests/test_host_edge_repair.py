from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
MAINTENANCE = ROOT / "plugins" / "codex-cost-orchestrator" / "maintenance"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(MAINTENANCE))

from control_plane import RESULT_HEADER, ControlPlane, parse_result, parse_task_message  # noqa: E402
from repair_host_edges import HostEdgeRepairError, _validate_lifecycle_result  # noqa: E402


CATALOG = {
    "models": [
        {
            "multi_agent_version": "v2",
            "slug": "gpt-5.6-terra",
            "supported_reasoning_levels": [{"effort": "max"}],
        }
    ]
}


def result_text(dispatch_id: str, *, paused: bool = False) -> str:
    value = {
        "blockers": ["need input"] if paused else [],
        "changed_paths": [] if paused else ["owned.txt"],
        "cursor": 0,
        "deviations": [],
        "dispatch_id": dispatch_id,
        "evidence": {} if paused else {"A01": "check passed"},
        "failure_signature": "need_input" if paused else None,
        "outcome": "pause" if paused else "retire",
        "status": "blocked" if paused else "complete",
        "summary": "bounded result",
    }
    return RESULT_HEADER + "\n" + json.dumps(value, sort_keys=True, separators=(",", ":"))


class HostEdgeRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "owned.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, session: str) -> tuple[ControlPlane, str, str]:
        control = ControlPlane(session, root=self.state)
        control.create_plan(
            self.repo,
            {
                "acceptance": {"A01": "owned file is correct"},
                "goal": "host proof",
                "nodes": [
                    {
                        "acceptance": ["A01"],
                        "id": "n01",
                        "objective": "update owned file",
                        "role": "worker",
                        "scopes": [{"kind": "file", "path": "owned.txt"}],
                    }
                ],
            },
        )
        native = control.next_wave(capacity=1, native_catalog=CATALOG)["dispatches"][0]
        control.preflight_spawn({"tool_input": native, "tool_use_id": "call"})
        owner = "/root/" + native["task_name"]
        control.postflight_tool(
            {"tool_response": {"agent_path": owner}, "tool_use_id": "call"}
        )
        dispatch_id = parse_task_message(native["message"])["dispatch_id"]
        return control, dispatch_id, owner

    def test_only_proof_backed_retired_v9_child_is_repairable(self) -> None:
        session = "11111111-1111-1111-1111-111111111111"
        control, dispatch_id, owner = self.prepare(session)
        (self.repo / "owned.txt").write_text("new\n", encoding="utf-8")
        raw = result_text(dispatch_id)
        control.record_result(owner, raw)
        _validate_lifecycle_result(
            agent_path=owner,
            agent_role="cost_orchestrator_write_leaf",
            parent_thread_id=session,
            state_root=self.state,
            result=parse_result(raw),
        )

    def test_paused_child_is_not_host_repair_proof(self) -> None:
        session = "22222222-2222-2222-2222-222222222222"
        control, dispatch_id, owner = self.prepare(session)
        raw = result_text(dispatch_id, paused=True)
        control.record_result(owner, raw)
        with self.assertRaisesRegex(HostEdgeRepairError, "terminal owner"):
            _validate_lifecycle_result(
                agent_path=owner,
                agent_role="cost_orchestrator_write_leaf",
                parent_thread_id=session,
                state_root=self.state,
                result=parse_result(raw),
            )


if __name__ == "__main__":
    unittest.main()

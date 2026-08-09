from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from operation_deadline import (  # noqa: E402
    OperationDeadlineExceeded,
    deadline_after,
)
import workspace_state  # noqa: E402


class FakeGitProcess:
    def __init__(self, output: object, *, time_out: bool = False) -> None:
        self.returncode: int | None = None
        self.time_out = time_out
        self.wait_timeouts: list[float | None] = []
        output.write(b"ok")  # type: ignore[attr-defined]
        output.flush()  # type: ignore[attr-defined]

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.time_out and timeout is not None:
            raise subprocess.TimeoutExpired(["git"], timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def poll(self) -> int | None:
        return self.returncode


class OperationDeadlineTests(unittest.TestCase):
    def test_context_exit_does_not_overturn_already_committed_work(self) -> None:
        with patch("operation_deadline.time.monotonic", side_effect=[100.0, 106.0]):
            with deadline_after(5):
                pass

    def test_git_subprocess_receives_the_active_hook_deadline(self) -> None:
        processes: list[FakeGitProcess] = []

        def start_process(*_args: object, **kwargs: object) -> FakeGitProcess:
            process = FakeGitProcess(kwargs["stdout"])
            processes.append(process)
            return process

        with (
            deadline_after(5),
            patch.object(
                workspace_state.subprocess,
                "Popen",
                side_effect=start_process,
            ),
        ):
            self.assertEqual(workspace_state.git(Path.cwd(), "status"), b"ok")
        timeout = processes[0].wait_timeouts[0]
        self.assertIsInstance(timeout, float)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 5)

    def test_git_timeout_preserves_deadline_identity(self) -> None:
        def start_process(*_args: object, **kwargs: object) -> FakeGitProcess:
            return FakeGitProcess(kwargs["stdout"], time_out=True)

        with (
            patch.object(
                workspace_state,
                "remaining_seconds",
                side_effect=[0.01, OperationDeadlineExceeded("deadline")],
            ),
            patch.object(
                workspace_state.subprocess,
                "Popen",
                side_effect=start_process,
            ),
            self.assertRaises(OperationDeadlineExceeded),
        ):
            workspace_state.git(Path.cwd(), "status")


if __name__ == "__main__":
    unittest.main()

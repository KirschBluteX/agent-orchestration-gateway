from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
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


class OperationDeadlineTests(unittest.TestCase):
    def test_context_rejects_an_unchecked_overrun_on_exit(self) -> None:
        with (
            patch("operation_deadline.time.monotonic", side_effect=[100.0, 106.0]),
            self.assertRaises(OperationDeadlineExceeded),
        ):
            with deadline_after(5):
                pass

    def test_git_subprocess_receives_the_active_hook_deadline(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, stdout=b"ok", stderr=b"")
        with (
            deadline_after(5),
            patch.object(workspace_state.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(workspace_state.git(Path.cwd(), "status"), b"ok")
        timeout = run.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, float)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 5)

    def test_git_timeout_preserves_deadline_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                deadline_after(5),
                patch.object(
                    workspace_state.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(["git"], 1),
                ),
                self.assertRaises(OperationDeadlineExceeded),
            ):
                workspace_state.git(Path(directory), "status")


if __name__ == "__main__":
    unittest.main()

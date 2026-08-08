from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from state_lock import StateLockBusy, acquire, lock_path  # noqa: E402


def is_locked(root: Path, identity: str) -> bool:
    try:
        with acquire(root, identity, timeout=0):
            return False
    except StateLockBusy:
        return True


class StateLockTests(unittest.TestCase):
    def test_nested_acquisition_is_reentrant_for_the_single_lifecycle_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with acquire(root, "session"):
                with acquire(root, "session"):
                    self.assertTrue(lock_path(root, "session").is_file())
            self.assertFalse(is_locked(root, "session"))

    def test_live_lock_is_observed_across_processes_without_deleting_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "from pathlib import Path; import sys; "
                f"sys.path.insert(0, {str(SCRIPTS)!r}); "
                "from state_lock import acquire; "
                f"lock=acquire(Path({str(root)!r}), 'session'); lock.__enter__(); "
                "print('ready', flush=True); input(); lock.__exit__(None,None,None)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                self.assertTrue(is_locked(root, "session"))
            finally:
                if process.stdin is not None:
                    process.stdin.write("\n")
                    process.stdin.flush()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            self.assertFalse(is_locked(root, "session"))

    def test_negative_timeout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                with acquire(Path(directory), "session", timeout=-1):
                    pass


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from rollout_io import RolloutError, iter_records, iter_tail_records  # noqa: E402


class RolloutIoTests(unittest.TestCase):
    def test_oversized_line_is_rejected_with_a_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b"{" + b"a" * (4 * 1024 * 1024) + b"}\n")

            with self.assertRaisesRegex(RolloutError, "record exceeds"):
                list(iter_records(path))

    def test_invalid_utf8_is_reported_as_a_rollout_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b"\xff\n")

            with self.assertRaisesRegex(RolloutError, "UTF-8"):
                list(iter_records(path))

    def test_total_decompressed_budget_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("{}\n{}\n", encoding="utf-8")

            with mock.patch("rollout_io.MAX_DECOMPRESSED_BYTES", 3):
                with self.assertRaisesRegex(RolloutError, "decompressed byte"):
                    list(iter_records(path))

    def test_plain_tail_reader_returns_only_the_bounded_terminal_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes((b'{"history":true}\n' * 10_000) + b'{"tail":true}\n')

            records = list(iter_tail_records(path, max_bytes=64))

            self.assertEqual(records[-1], {"tail": True})
            self.assertLess(len(records), 10)


if __name__ == "__main__":
    unittest.main()

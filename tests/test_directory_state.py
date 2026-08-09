from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from directory_state import (  # noqa: E402
    DirectoryStateError,
    capture_directory_state,
    normalize_directory_scope,
    verify_directory_state,
)
import directory_state as directory_state_module  # noqa: E402


class DirectoryStateTests(unittest.TestCase):
    def test_child_enumeration_stops_at_the_entry_budget(self) -> None:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name

        class Entries:
            def __init__(self) -> None:
                self.items = iter(Entry(str(index)) for index in range(100))
                self.yielded = 0

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def __iter__(self):  # type: ignore[no-untyped-def]
                return self

            def __next__(self):  # type: ignore[no-untyped-def]
                item = next(self.items)
                self.yielded += 1
                return item

        entries = Entries()
        with (
            mock.patch.object(directory_state_module.os, "scandir", return_value=entries),
            self.assertRaisesRegex(DirectoryStateError, "entry.*budget"),
        ):
            directory_state_module._child_names(Path("unused"), max_names=2)
        self.assertEqual(entries.yielded, 3)

    def test_directory_enumeration_checks_deadline_between_entries(self) -> None:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name

        class Entries:
            def __init__(self) -> None:
                self.items = iter([Entry("one"), Entry("two"), Entry("three")])
                self.yielded = 0

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def __iter__(self):  # type: ignore[no-untyped-def]
                return self

            def __next__(self):  # type: ignore[no-untyped-def]
                item = next(self.items)
                self.yielded += 1
                return item

        entries = Entries()

        def checkpoint() -> None:
            if entries.yielded:
                raise RuntimeError("deadline")

        with (
            mock.patch.object(directory_state_module.os, "scandir", return_value=entries),
            mock.patch.object(directory_state_module, "checkpoint", side_effect=checkpoint),
            self.assertRaisesRegex(RuntimeError, "deadline"),
        ):
            directory_state_module._child_names(
                Path("unused"),
                max_names=directory_state_module.DEFAULT_MAX_ENTRIES,
            )
        self.assertEqual(entries.yielded, 1)

    def test_scope_snapshot_detects_any_explorer_or_reviewer_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "docs").mkdir()
            (root / "docs" / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "outside.txt").write_text("outside\n", encoding="utf-8")
            scopes = [{"kind": "prefix", "path": "docs"}]

            snapshot = capture_directory_state(
                root, scopes=scopes, capture_mode="scope"
            )
            (root / "docs" / "one.txt").write_text("changed\n", encoding="utf-8")
            verification = verify_directory_state(
                root, snapshot, allowed_scopes=[]
            )

            self.assertEqual(verification["verdict"], "fail")
            self.assertIn("outside_scope:docs/one.txt", verification["violations"])

    def test_full_snapshot_allows_worker_scope_and_rejects_outside_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "work").mkdir()
            (root / "work" / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "outside.txt").write_text("outside\n", encoding="utf-8")
            scope = [{"kind": "prefix", "path": "work"}]
            snapshot = capture_directory_state(root, scopes=scope, capture_mode="full")

            (root / "work" / "one.txt").write_text("allowed\n", encoding="utf-8")
            allowed = verify_directory_state(root, snapshot, allowed_scopes=scope)
            self.assertEqual(allowed["verdict"], "pass")
            self.assertEqual(allowed["changed_paths"], ["work/one.txt"])

            (root / "outside.txt").write_text("not allowed\n", encoding="utf-8")
            rejected = verify_directory_state(root, snapshot, allowed_scopes=scope)
            self.assertEqual(rejected["verdict"], "fail")
            self.assertIn("outside_scope:outside.txt", rejected["violations"])

    def test_budget_preflight_does_not_open_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.bin").write_bytes(b"x" * 32)
            with mock.patch.object(Path, "open", side_effect=AssertionError("content read")):
                with self.assertRaisesRegex(DirectoryStateError, "budget"):
                    capture_directory_state(
                        root,
                        scopes=[{"kind": "exact", "path": "one.bin"}],
                        capture_mode="full",
                        max_bytes=16,
                    )

    def test_entry_budget_counts_directories_and_stops_during_walk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root
            for index in range(6):
                current = current / f"d{index}"
                current.mkdir()

            with mock.patch(
                "directory_state._inspect_entry",
                wraps=__import__("directory_state")._inspect_entry,
            ) as inspect_entry:
                with self.assertRaisesRegex(DirectoryStateError, "entry.*budget"):
                    capture_directory_state(
                        root,
                        scopes=[{"kind": "prefix", "path": "d0"}],
                        capture_mode="full",
                        max_entries=3,
                    )
            self.assertLessEqual(inspect_entry.call_count, 4)

    def test_repeated_scope_segment_cannot_skip_a_non_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "same").write_text("file\n", encoding="utf-8")

            with self.assertRaisesRegex(DirectoryStateError, "non-directory"):
                normalize_directory_scope(
                    root,
                    {"kind": "exact", "path": "same/same"},
                )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_reparse_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError:
                self.skipTest("symlink privilege unavailable")
            with self.assertRaisesRegex(DirectoryStateError, "reparse"):
                capture_directory_state(
                    root,
                    scopes=[{"kind": "prefix", "path": "link"}],
                    capture_mode="full",
                )


if __name__ == "__main__":
    unittest.main()

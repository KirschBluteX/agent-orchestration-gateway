from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from protocol_hash import (  # noqa: E402
    ProtocolHashError,
    canonical_bytes,
    object_from_pairs,
    parse_repository_scope_text,
    repository_scopes_overlap,
    require_canonical_task_path,
    require_repository_path,
)


class CanonicalHelperTests(unittest.TestCase):
    def test_canonical_json_is_stable_utf8_and_rejects_ambiguous_values(self) -> None:
        self.assertEqual(
            canonical_bytes({"z": "验证", "a": 1}),
            '{"a":1,"z":"验证"}'.encode(),
        )
        with self.assertRaises(ProtocolHashError):
            canonical_bytes({"value": 1.5})
        with self.assertRaises(ProtocolHashError):
            canonical_bytes({"value": "e\u0301"})
        with self.assertRaises(ProtocolHashError):
            json.loads('{"a":1,"a":2}', object_pairs_hook=object_from_pairs)

    def test_repository_paths_are_portable_and_explicitly_typed(self) -> None:
        self.assertEqual(require_repository_path("src/验证.py", "path"), "src/验证.py")
        for value in (
            "/src/a.py",
            "src\\a.py",
            "src/../a.py",
            ".git/config",
            "src/CON.txt",
            "src/trailing. ",
        ):
            with self.subTest(value=value), self.assertRaises(ProtocolHashError):
                require_repository_path(value, "path")
        self.assertEqual(
            parse_repository_scope_text("prefix:src", "scope"),
            {"kind": "prefix", "path": "src"},
        )

    def test_scope_overlap_is_case_portable(self) -> None:
        self.assertTrue(
            repository_scopes_overlap(
                {"kind": "prefix", "path": "src"},
                {"kind": "exact", "path": "src/module.py"},
            )
        )
        self.assertTrue(
            repository_scopes_overlap(
                {"kind": "exact", "path": "SRC/module.py"},
                {"kind": "exact", "path": "src/module.py"},
            )
        )
        self.assertFalse(
            repository_scopes_overlap(
                {"kind": "exact", "path": "src/a.py"},
                {"kind": "exact", "path": "tests/a.py"},
            )
        )

    def test_native_task_path_is_exact(self) -> None:
        self.assertEqual(
            require_canonical_task_path("/root/work_n01_routine_g01", "target"),
            "/root/work_n01_routine_g01",
        )
        for value in ("work_n01", "/root/../work", "/root/Work"):
            with self.subTest(value=value), self.assertRaises(ProtocolHashError):
                require_canonical_task_path(value, "target")


if __name__ == "__main__":
    unittest.main()

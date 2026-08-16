from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "agent-orchestration-gateway"
    / "skills"
    / "orchestrate"
    / "scripts"
    / "validate_plan.py"
)


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_plan", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load plan validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def acceptance(identifier: str) -> dict[str, str]:
    return {"id": identifier, "criterion": f"Prove {identifier}"}


def module(
    identifier: str,
    *,
    depends_on: list[str] | None = None,
    writes: list[dict[str, str]] | None = None,
    kind: str = "work",
) -> dict[str, object]:
    return {
        "id": identifier,
        "type": kind,
        "objective": f"Complete {identifier}",
        "depends_on": depends_on or [],
        "writes": writes or [],
        "acceptance": [acceptance(f"accept-{identifier}")],
    }


def plan(
    *modules: dict[str, object], base_sha: str | None = "a" * 40
) -> dict[str, object]:
    return {
        "goal": "Ship the approved change",
        "base_sha": base_sha,
        "modules": list(modules),
    }


class ValidatePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_validator()

    def assert_invalid(self, payload: dict[str, object], fragment: str) -> None:
        with self.assertRaisesRegex(self.validator.ValidationError, fragment):
            self.validator.validate_plan(payload)

    def test_normalizes_valid_dag_topologically(self) -> None:
        payload = plan(
            module(
                "join",
                kind="integration",
                depends_on=["docs", "core"],
                writes=[{"kind": "exact", "path": "src/app.py"}],
            ),
            module("docs", writes=[{"kind": "prefix", "path": "docs"}]),
            module("core", writes=[{"kind": "prefix", "path": "src/core"}]),
        )

        normalized = self.validator.validate_plan(payload)

        self.assertEqual(
            [item["id"] for item in normalized["modules"]], ["core", "docs", "join"]
        )
        self.assertEqual(normalized["modules"][2]["depends_on"], ["core", "docs"])
        reordered = plan(
            payload["modules"][2], payload["modules"][1], payload["modules"][0]
        )
        reordered["modules"][2]["depends_on"].reverse()
        self.assertEqual(self.validator.validate_plan(reordered), normalized)

    def test_rejects_unknown_dependencies_cycles_and_invalid_integration(self) -> None:
        cases = [
            (plan(module("a", depends_on=["missing"])), "unknown module"),
            (
                plan(module("a", depends_on=["b"]), module("b", depends_on=["a"])),
                "cycle",
            ),
            (
                plan(module("join", kind="integration", depends_on=["a"]), module("a")),
                "at least two",
            ),
        ]
        for payload, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assert_invalid(payload, fragment)

    def test_rejects_duplicate_acceptance_and_module_ids(self) -> None:
        duplicate_acceptance = plan(module("a"), module("b"))
        duplicate_acceptance["modules"][1]["acceptance"][0]["id"] = "accept-a"
        self.assert_invalid(duplicate_acceptance, "acceptance id")
        self.assert_invalid(plan(module("a"), module("a")), "module id")

    def test_rejects_ambiguous_or_unsafe_paths(self) -> None:
        bad_paths = [
            "../src",
            "/src",
            "C:/src",
            "src\\file.py",
            "src//file.py",
            ".git/config",
            "src/COM1.txt",
            "src/COM\N{SUPERSCRIPT ONE}.txt",
            "src/COM1 .txt",
            "src/\N{RIGHT-TO-LEFT OVERRIDE}evil.py",
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                payload = plan(module("a", writes=[{"kind": "exact", "path": path}]))
                self.assert_invalid(payload, "path")
        self.assert_invalid(
            plan(module("a", writes=[{"kind": "exact", "path": "."}])),
            "repository root",
        )

    def test_rejects_redundant_and_cross_module_write_overlap(self) -> None:
        self.assert_invalid(
            plan(
                module(
                    "a",
                    writes=[
                        {"kind": "prefix", "path": "src"},
                        {"kind": "exact", "path": "src/a.py"},
                    ],
                )
            ),
            "overlap within module",
        )
        self.assert_invalid(
            plan(
                module("a", writes=[{"kind": "prefix", "path": "src"}]),
                module("b", writes=[{"kind": "exact", "path": "src/a.py"}]),
            ),
            "overlap across modules",
        )
        self.assert_invalid(
            plan(
                module("a", writes=[{"kind": "exact", "path": "docs/café.md"}]),
                module("b", writes=[{"kind": "exact", "path": "docs/café.md"}]),
            ),
            "overlap across modules",
        )

    def test_non_git_plan_allows_readers_or_exactly_one_writer(self) -> None:
        self.validator.validate_plan(plan(module("a"), module("b"), base_sha=None))
        self.validator.validate_plan(
            plan(module("a", writes=[{"kind": "prefix", "path": "."}]), base_sha=None)
        )
        self.assert_invalid(
            plan(
                module("a", writes=[{"kind": "exact", "path": "a.txt"}]),
                module("b"),
                base_sha=None,
            ),
            "single module",
        )

    def test_bounds_input_and_module_count(self) -> None:
        self.validator.validate_plan(
            plan(*(module(f"m-{index}") for index in range(8)))
        )
        self.assert_invalid(
            plan(*(module(f"m-{index}") for index in range(9))),
            "between 1 and 8",
        )
        raw = json.dumps(plan(module("a")), separators=(",", ":")).encode()
        exact = raw + b" " * (self.validator.MAX_INPUT_BYTES - len(raw))
        self.validator.validate_plan(self.validator.parse_plan(exact))
        with self.assertRaisesRegex(self.validator.ValidationError, "input exceeds"):
            self.validator.parse_plan(exact + b" ")

    def test_rejects_duplicate_json_keys(self) -> None:
        with self.assertRaisesRegex(
            self.validator.ValidationError, "duplicate JSON key"
        ):
            self.validator.parse_plan(b'{"goal":"a","goal":"b"}')

    def test_rejects_schema_extensions_and_nonstandard_json(self) -> None:
        payload = plan(module("a"))
        payload["extra"] = True
        self.assert_invalid(payload, "unknown field")
        with self.assertRaisesRegex(
            self.validator.ValidationError, "non-standard JSON constant"
        ):
            self.validator.parse_plan(b'{"goal":NaN}')

    def test_rejects_invalid_unicode_scalar_without_traceback(self) -> None:
        payload = plan(module("a"))
        payload["goal"] = "bad\ud800value"
        self.assert_invalid(payload, "invalid Unicode scalar")

        raw = json.dumps(payload).encode()
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_cli_emits_compact_json_and_clean_errors(self) -> None:
        payload = plan(module("a"))
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)],
            input=json.dumps(payload).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(
            json.loads(result.stdout), self.validator.validate_plan(payload)
        )
        self.assertNotIn(b"\n  ", result.stdout)


if __name__ == "__main__":
    unittest.main()

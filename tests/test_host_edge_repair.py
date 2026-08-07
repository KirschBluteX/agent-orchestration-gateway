from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from packet_compiler import RESULT_HEADER, result_sha256  # noqa: E402
from protocol_hash import canonical_bytes  # noqa: E402
from task_ledger import TaskLedger  # noqa: E402

TOOL = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "maintenance"
    / "repair_host_edges.py"
)
PARENT_ID = "019fc182-69b7-7421-8dba-0b49338f57d3"
CHILD_ID = "019fd29e-6784-7e43-8b3b-c448fb738654"
AGENT_PATH = "/root/review_e02_review_final_delta_sol_max_g01"
AGENT_ROLE = "cost_orchestrator_read_leaf"
DISPATCH_SHA256 = "sha256:" + "a" * 64


def cco_result(*, status: str = "complete", disposition: str = "retire") -> tuple[str, dict[str, object]]:
    failed = status != "complete"
    payload = {
        "blockers": ["needs input"] if status == "blocked" else [],
        "changed_paths": [],
        "deviations": [],
        "evidence": {"A01": "bounded result"} if status == "complete" else {},
        "failure_signature": "blocked:needs-input" if failed else None,
        "summary": "bounded CCO result",
    }
    result: dict[str, object] = {
        "dispatch_sha256": DISPATCH_SHA256,
        "disposition": disposition,
        "payload": payload,
        "protocol": "cco.v8",
        "status": status,
    }
    result["result_sha256"] = result_sha256(result)
    message = (
        f"{RESULT_HEADER}\nRESULT_SHA256: {result['result_sha256']}\n"
        f"RESULT_JSON: {canonical_bytes(result).decode('utf-8')}"
    )
    return message, result


def create_fixture(
    codex_home: Path,
    ledger_root: Path,
    *,
    compressed: bool = False,
    disposition: str = "retire",
    status: str = "complete",
    extended_path: bool = False,
) -> tuple[Path, Path]:
    suffix = ".jsonl.zst" if compressed else ".jsonl"
    rollout = codex_home / "sessions" / "2026" / "08" / "06" / f"rollout-{CHILD_ID}{suffix}"
    rollout.parent.mkdir(parents=True)
    message, result = cco_result(status=status, disposition=disposition)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": CHILD_ID,
                "parent_thread_id": PARENT_ID,
                "agent_path": AGENT_PATH,
                "agent_role": AGENT_ROLE,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "agent_path": AGENT_PATH,
                            "agent_role": AGENT_ROLE,
                        }
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": message},
        },
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    serialized = "".join(
        json.dumps(record, separators=(",", ":")) + "\n" for record in records
    )
    if compressed:
        from compression import zstd

        with zstd.open(rollout, "wt", encoding="utf-8") as stream:
            stream.write(serialized)
    else:
        rollout.write_text(serialized, encoding="utf-8")

    ledger = TaskLedger(ledger_root, PARENT_ID)
    identity = {
        "acceptance_ids": ["A01"],
        "assurance": "guarded",
        "contract_rev": 1,
        "contract_sha256": "sha256:" + "b" * 64,
        "cursor": 0,
        "generation": 1,
        "input_sha256": DISPATCH_SHA256,
        "node": "n01_host_edge",
        "role": "explorer",
        "route": {
            "assurance": "guarded",
            "constraints": {
                "fixed_effort": None,
                "fixed_model": None,
                "source": "automatic",
            },
            "decision_sha256": "sha256:" + "c" * 64,
            "plan_sha256": "sha256:" + "d" * 64,
            "rank": 1,
            "selected": {"effort": "max", "model": "gpt-5.6-terra"},
        },
        "run": AGENT_PATH.removeprefix("/root/"),
    }
    ledger.reserve("spawn", identity)
    ledger.activate("spawn", AGENT_PATH)
    ledger.record_result(
        node="n01_host_edge",
        contract_rev=1,
        run=AGENT_PATH.removeprefix("/root/"),
        generation=1,
        input_sha256=DISPATCH_SHA256,
        owner=AGENT_PATH,
        disposition="continuable" if disposition == "continue" else "retired",
        require_guarded=status != "complete",
        review_seed={
            "disposition": disposition,
            "payload": result["payload"],
            "status": status,
        },
    )

    database = codex_home / "state_5.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                agent_path TEXT,
                agent_role TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO threads (id, rollout_path, agent_path, agent_role) VALUES (?, ?, ?, ?)",
            (
                CHILD_ID,
                "\\\\?\\" + str(rollout) if extended_path and os.name == "nt" else str(rollout),
                AGENT_PATH,
                AGENT_ROLE,
            ),
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, 'open')",
            (PARENT_ID, CHILD_ID),
        )
        connection.commit()
    return database, rollout


class HostEdgeRepairCliTests(unittest.TestCase):
    def test_check_reports_terminal_open_cco_edge_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            database, _rollout = create_fixture(codex_home, ledger_root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--check",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["protocol"], "cco.host-edge-repair.v1")
            self.assertEqual(payload["mode"], "check")
            self.assertEqual(payload["examined"], 1)
            self.assertEqual(payload["repairable"], 1)
            self.assertEqual(payload["repaired"], 0)
            self.assertIsNone(payload["backup"])
            self.assertEqual(payload["edges"][0]["child_thread_id"], CHILD_ID)
            self.assertEqual(payload["edges"][0]["verdict"], "repairable")
            self.assertEqual(
                payload["edges"][0]["evidence"],
                "CCO_RESULT+TaskLedger+event_msg/task_complete",
            )
            with closing(sqlite3.connect(database)) as connection:
                status = connection.execute(
                    "SELECT status FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (CHILD_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "open")

    def test_repair_backs_up_then_closes_only_the_proven_parent_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            database, _rollout = create_fixture(codex_home, ledger_root)
            backup_root = codex_home / "backups" / "cco-host-edge-repair"
            backup_root.mkdir(parents=True)
            for index in range(4):
                stale = backup_root / f"state_5-old-{index}.rollback.json"
                stale.write_text("{}\n", encoding="utf-8")
                os.utime(stale, (1_000_000 + index, 1_000_000 + index))
            foreign = backup_root / "state_50-keep.rollback.json"
            foreign.write_text("{}\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--child-thread-id",
                    CHILD_ID,
                    "--repair",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["mode"], "repair")
            self.assertEqual(payload["repairable"], 1)
            self.assertEqual(payload["repaired"], 1)
            backup = Path(payload["backup"])
            self.assertTrue(backup.is_file())
            rollback = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(rollback["protocol"], "cco.host-edge-rollback.v1")
            self.assertEqual(
                rollback["edges"],
                [
                    {
                        "child_thread_id": CHILD_ID,
                        "parent_thread_id": PARENT_ID,
                        "prior_status": "open",
                    }
                ],
            )
            self.assertNotIn("rollout_path", rollback)
            self.assertEqual(
                len(list(backup_root.glob("state_5-*.rollback.json"))),
                3,
            )
            self.assertTrue(foreign.is_file())
            with closing(sqlite3.connect(database)) as connection:
                current_status = connection.execute(
                    "SELECT status FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (CHILD_ID,),
                ).fetchone()[0]
            self.assertEqual(current_status, "closed")

    def test_repair_rejects_an_unproven_exact_child_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            database, rollout = create_fixture(codex_home, ledger_root)
            records = rollout.read_text(encoding="utf-8").splitlines()
            records[-1] = json.dumps(
                {"type": "event_msg", "payload": {"type": "agent_message"}},
                separators=(",", ":"),
            )
            rollout.write_text("\n".join(records) + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--child-thread-id",
                    CHILD_ID,
                    "--repair",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("not proof-backed", result.stderr)
            self.assertFalse((codex_home / "backups").exists())
            with closing(sqlite3.connect(database)) as connection:
                status = connection.execute(
                    "SELECT status FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (CHILD_ID,),
                ).fetchone()[0]
            self.assertEqual(status, "open")

    def test_check_never_repairs_a_blocked_continuable_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            create_fixture(
                codex_home,
                ledger_root,
                disposition="continue",
                status="blocked",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--check",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            edge = json.loads(result.stdout)["edges"][0]
            self.assertEqual(edge["verdict"], "skipped")
            self.assertIn("not proof-backed terminal", edge["reason"])

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are Windows-only")
    def test_windows_extended_rollout_path_maps_to_the_trusted_sessions_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            create_fixture(codex_home, ledger_root, extended_path=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--check",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["repairable"], 1)

    @unittest.skipUnless(sys.version_info >= (3, 14), "stdlib zstd requires Python 3.14+")
    def test_compressed_rollout_is_audited_with_the_same_terminal_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            ledger_root = Path(directory) / "ledger"
            create_fixture(codex_home, ledger_root, compressed=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
                    "--ledger-root",
                    str(ledger_root),
                    "--parent-thread-id",
                    PARENT_ID,
                    "--check",
                    "--json",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["repairable"], 1)


if __name__ == "__main__":
    unittest.main()

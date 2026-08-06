from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
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


def create_fixture(codex_home: Path) -> Path:
    rollout = codex_home / "sessions" / "2026" / "08" / "06" / f"rollout-{CHILD_ID}.jsonl"
    rollout.parent.mkdir(parents=True)
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
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    rollout.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
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
            (CHILD_ID, str(rollout), AGENT_PATH, AGENT_ROLE),
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, 'open')",
            (PARENT_ID, CHILD_ID),
        )
        connection.commit()
    return database


class HostEdgeRepairCliTests(unittest.TestCase):
    def test_check_reports_terminal_open_cco_edge_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            database = create_fixture(codex_home)

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
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
            self.assertEqual(payload["edges"][0]["evidence"], "event_msg/task_complete")
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
            database = create_fixture(codex_home)

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--codex-home",
                    str(codex_home),
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
            with closing(sqlite3.connect(database)) as connection:
                current_status = connection.execute(
                    "SELECT status FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (CHILD_ID,),
                ).fetchone()[0]
            with closing(sqlite3.connect(backup)) as connection:
                backup_status = connection.execute(
                    "SELECT status FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (CHILD_ID,),
                ).fetchone()[0]
            self.assertEqual(current_status, "closed")
            self.assertEqual(backup_status, "open")

    def test_repair_rejects_an_unproven_exact_child_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / ".codex"
            codex_home.mkdir()
            database = create_fixture(codex_home)
            rollout = next((codex_home / "sessions").rglob(f"*{CHILD_ID}.jsonl"))
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


if __name__ == "__main__":
    unittest.main()

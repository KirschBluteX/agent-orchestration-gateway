from __future__ import annotations

try:
    from compression import zstd
except ImportError:  # pragma: no cover - Python < 3.14 needs the declared wheel.
    zstd = None
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
MAINTENANCE = ROOT / "plugins" / "codex-cost-orchestrator" / "maintenance"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(MAINTENANCE))

import repair_host_edges  # noqa: E402
from repair_host_edges import (  # noqa: E402
    CCO_ROLES,
    JOURNAL_PREFIX,
    JOURNAL_SUFFIX,
    ROLLBACK_RETENTION,
    HostEdgeRepairError,
    _post_commit_warnings,
    _prune_rollback_journals,
    audit_edges,
    repair_edges,
)


PARENT = "10000000-0000-4000-8000-000000000001"
CCO_ROLE = "cost_orchestrator_write_leaf"
AGENT_PATH = "/root/worker_host_edge_repair"


def child_id(index: int) -> str:
    return f"20000000-0000-4000-8000-{index:012d}"


def event(event_type: str, **extra: object) -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": event_type, **extra}}


class HostEdgeRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.sessions = self.home / "sessions"
        self.sessions.mkdir(parents=True)
        self.database = self.home / "state_7.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    agent_path TEXT,
                    agent_role TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (parent_thread_id, child_thread_id)
                )
                """
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def metadata(
        self,
        child: str,
        *,
        parent: str = PARENT,
        agent_path: str = AGENT_PATH,
        agent_role: str = CCO_ROLE,
    ) -> dict[str, object]:
        return {
            "id": child,
            "parent_thread_id": parent,
            "agent_path": agent_path,
            "agent_role": agent_role,
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "agent_path": agent_path,
                        "agent_role": agent_role,
                        "child_thread_id": child,
                        "parent_thread_id": parent,
                    }
                }
            },
        }

    def write_rollout(
        self,
        child: str,
        records: list[dict[str, object]],
        *,
        parent: str = PARENT,
        agent_path: str = AGENT_PATH,
        agent_role: str = CCO_ROLE,
        metadata: dict[str, object] | None = None,
        suffix: str = ".jsonl",
    ) -> Path:
        path = self.sessions / f"rollout-{child}{suffix}"
        payload = metadata or self.metadata(
            child,
            parent=parent,
            agent_path=agent_path,
            agent_role=agent_role,
        )
        encoded = "".join(
            json.dumps(record, sort_keys=True) + "\n"
            for record in [{"type": "session_meta", "payload": payload}, *records]
        ).encode("utf-8")
        if suffix == ".jsonl.zst":
            if zstd is None:  # pragma: no cover - guarded by the test decorator.
                self.fail("the zstd test needs a supported local codec")
            with zstd.open(path, "wb") as stream:
                stream.write(encoded)
        else:
            path.write_bytes(encoded)
        return path

    def add_edge(
        self,
        child: str,
        records: list[dict[str, object]],
        *,
        parent: str = PARENT,
        agent_path: str = AGENT_PATH,
        agent_role: str = CCO_ROLE,
        metadata: dict[str, object] | None = None,
        suffix: str = ".jsonl",
    ) -> Path:
        rollout = self.write_rollout(
            child,
            records,
            parent=parent,
            agent_path=agent_path,
            agent_role=agent_role,
            metadata=metadata,
            suffix=suffix,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (child, str(rollout), agent_path, agent_role),
            )
            connection.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')",
                (parent, child),
            )
            connection.commit()
        return rollout

    def status(self, child: str, *, parent: str = PARENT) -> str:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                """
                SELECT status FROM thread_spawn_edges
                WHERE parent_thread_id = ? AND child_thread_id = ?
                """,
                (parent, child),
            ).fetchone()
        self.assertIsNotNone(row)
        return str(row[0])

    def audit(
        self,
        *,
        parent: str | None = PARENT,
        children: list[str] | None = None,
        all_native: bool = False,
    ) -> dict[str, object]:
        return audit_edges(
            codex_home=self.home,
            parent_thread_id=parent,
            child_thread_ids=children,
            all_native=all_native,
        )

    def repair(
        self,
        children: list[str],
        *,
        parent: str = PARENT,
        all_native: bool = False,
    ) -> dict[str, object]:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            return repair_edges(
                codex_home=self.home,
                parent_thread_id=parent,
                child_thread_ids=children,
                all_native=all_native,
                offline_confirmed=True,
            )

    def edge(self, child: str, **kwargs: object) -> dict[str, object]:
        report = self.audit(children=[child], **kwargs)
        edges = report["edges"]
        self.assertEqual(len(edges), 1)
        return edges[0]

    def test_success_needs_only_host_metadata_and_task_complete(self) -> None:
        child = child_id(1)
        self.add_edge(child, [event("task_started"), event("task_complete")])

        report = audit_edges(
            codex_home=self.home,
            parent_thread_id=PARENT,
            child_thread_ids=[child],
            state_root=self.root / "missing-cco-lifecycle-state",
        )

        self.assertEqual(report["repairable"], 1)
        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "repairable")
        self.assertEqual(edge["evidence"], "session_meta+host_lifecycle/task_complete")
        self.assertTrue(str(edge["proof_digest"]).startswith("sha256:"))
        self.assertEqual(self.status(child), "open")
        self.assertFalse((self.home / "backups").exists())

    def test_ordinary_error_before_task_complete_is_terminal(self) -> None:
        child = child_id(2)
        self.add_edge(
            child,
            [
                event("task_started"),
                event("error", message="native tool failed"),
                event("task_complete", error="native tool failed"),
            ],
        )

        self.assertEqual(self.edge(child)["verdict"], "repairable")

    def test_usage_limit_error_is_terminal(self) -> None:
        child = child_id(3)
        self.add_edge(
            child,
            [
                event("task_started"),
                event("task_complete", error={"kind": "usage_limit"}),
            ],
        )

        self.assertEqual(self.edge(child)["verdict"], "repairable")

    def test_token_count_after_task_complete_does_not_reopen_edge(self) -> None:
        child = child_id(4)
        self.add_edge(
            child,
            [
                event("task_started"),
                event("task_complete"),
                event("token_count", info={"total_token_usage": {}}),
            ],
        )

        self.assertEqual(self.edge(child)["verdict"], "repairable")

    def test_later_start_after_completion_is_unproven(self) -> None:
        child = child_id(5)
        self.add_edge(
            child,
            [
                event("task_complete"),
                event("token_count"),
                event("task_started"),
            ],
        )

        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "skipped")
        self.assertIn("starts after task_complete", str(edge["reason"]))

    def test_turn_aborted_is_unproven_even_if_completion_follows(self) -> None:
        child = child_id(6)
        self.add_edge(
            child,
            [event("task_started"), event("turn_aborted"), event("task_complete")],
        )

        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "skipped")
        self.assertIn("interrupted or aborted", str(edge["reason"]))

    def test_interruption_before_a_large_terminal_window_is_still_unproven(self) -> None:
        child = child_id(61)
        self.add_edge(
            child,
            [
                event("task_started"),
                event("turn_aborted"),
                {"type": "token_count", "padding": "x" * 1_100_000},
                event("task_complete"),
            ],
        )

        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "skipped")
        self.assertIn("interrupted or aborted", str(edge["reason"]))

    def test_nonterminal_rollout_is_unproven(self) -> None:
        child = child_id(7)
        self.add_edge(child, [event("task_started"), event("token_count")])

        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "skipped")
        self.assertIn("no authoritative task_complete", str(edge["reason"]))

    def test_metadata_mismatch_is_unproven(self) -> None:
        child = child_id(8)
        metadata = self.metadata(child)
        spawn = metadata["source"]["subagent"]["thread_spawn"]
        spawn["parent_thread_id"] = child_id(99)
        self.add_edge(child, [event("task_complete")], metadata=metadata)

        edge = self.edge(child)
        self.assertEqual(edge["verdict"], "skipped")
        self.assertIn("metadata does not match", str(edge["reason"]))

    def test_unknown_and_malformed_tail_are_unproven(self) -> None:
        unknown = child_id(9)
        malformed = child_id(10)
        self.add_edge(unknown, [event("task_complete"), event("unexpected_tail")])
        rollout = self.add_edge(malformed, [event("task_complete")])
        with rollout.open("ab") as stream:
            stream.write(b'{"type":"event_msg"')

        unknown_edge = self.edge(unknown)
        malformed_edge = self.edge(malformed)
        self.assertEqual(unknown_edge["verdict"], "skipped")
        self.assertIn("unknown tail", str(unknown_edge["reason"]))
        self.assertEqual(malformed_edge["verdict"], "skipped")
        self.assertIn("cannot be read", str(malformed_edge["reason"]))

    def test_non_cco_roles_need_explicit_all_native_and_only_selected_edge_closes(self) -> None:
        cco_child = child_id(11)
        native_child = child_id(12)
        native_role = "native_worker"
        self.add_edge(cco_child, [event("task_complete")])
        self.add_edge(
            native_child,
            [event("task_complete")],
            agent_role=native_role,
        )

        default_report = self.audit(parent=None)
        self.assertEqual(default_report["examined"], 1)
        self.assertEqual(default_report["edges"][0]["child_thread_id"], cco_child)
        all_native_report = self.audit(parent=None, all_native=True)
        self.assertEqual(all_native_report["examined"], 2)
        with self.assertRaisesRegex(HostEdgeRepairError, "absent or not open"):
            self.repair([native_child])
        self.repair([native_child], all_native=True)

        self.assertEqual(self.status(native_child), "closed")
        self.assertEqual(self.status(cco_child), "open")

    def test_repair_requires_offline_confirmation_and_no_active_task(self) -> None:
        child = child_id(13)
        self.add_edge(child, [event("task_complete")])
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "active-task"}):
            with self.assertRaisesRegex(HostEdgeRepairError, "active Codex task"):
                repair_edges(
                    codex_home=self.home,
                    parent_thread_id=PARENT,
                    child_thread_ids=[child],
                    offline_confirmed=True,
                )
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            with self.assertRaisesRegex(HostEdgeRepairError, "offline-confirm"):
                repair_edges(
                    codex_home=self.home,
                    parent_thread_id=PARENT,
                    child_thread_ids=[child],
                )
        self.assertEqual(self.status(child), "open")

    def test_repair_revalidates_rollout_inside_immediate_transaction(self) -> None:
        child = child_id(14)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        real_audit = repair_host_edges.audit_edges

        def mutate_after_audit(*args: object, **kwargs: object) -> dict[str, object]:
            report = real_audit(*args, **kwargs)
            self.write_rollout(child, [event("task_started")])
            return report

        with patch.object(repair_host_edges, "audit_edges", side_effect=mutate_after_audit):
            with self.assertRaisesRegex(HostEdgeRepairError, "changed before repair"):
                self.repair([child])

        self.assertEqual(self.status(child), "open")
        self.assertFalse((self.home / "backups").exists())

    def test_repair_binds_the_journalled_rollout_proof_before_commit(self) -> None:
        child = child_id(141)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        real_write = repair_host_edges._write_rollback_journal

        def mutate_after_journal(*args: object, **kwargs: object) -> Path:
            journal = real_write(*args, **kwargs)
            self.write_rollout(child, [event("task_started")])
            return journal

        with patch.object(
            repair_host_edges,
            "_write_rollback_journal",
            side_effect=mutate_after_journal,
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "proof changed before repair commit"):
                self.repair([child])

        self.assertEqual(self.status(child), "open")
        journals = self.home / "backups" / "cco-host-edge-repair"
        self.assertFalse(list(journals.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")))

    def test_precommit_proof_mutation_is_durably_compensated_at_finalization(self) -> None:
        child = child_id(144)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        real_verify = repair_host_edges._verify_commit_proofs
        calls = 0

        def mutate_after_first_verify(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            real_verify(*args, **kwargs)
            if calls == 1:
                self.write_rollout(child, [event("task_started")])

        with patch.object(
            repair_host_edges,
            "_verify_commit_proofs",
            side_effect=mutate_after_first_verify,
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "proof changed before repair commit"):
                self.repair([child])

        self.assertEqual(calls, 2)
        self.assertEqual(self.status(child), "open")
        journals = self.home / "backups" / "cco-host-edge-repair"
        self.assertFalse(list(journals.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")))

    def test_post_commit_proof_mutation_is_durably_compensated(self) -> None:
        child = child_id(146)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        real_commit = repair_host_edges._commit_transaction
        commits = 0

        def mutate_after_repair_commit(connection: sqlite3.Connection) -> None:
            nonlocal commits
            real_commit(connection)
            commits += 1
            if commits == 1:
                self.write_rollout(child, [event("task_started")])

        with patch.object(
            repair_host_edges,
            "_commit_transaction",
            side_effect=mutate_after_repair_commit,
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "proof changed before repair commit"):
                self.repair([child])

        self.assertEqual(commits, 2)
        self.assertEqual(self.status(child), "open")
        journals = self.home / "backups" / "cco-host-edge-repair"
        self.assertFalse(list(journals.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")))

    def test_rollback_and_journal_cleanup_keep_the_original_failure(self) -> None:
        child = child_id(147)
        self.add_edge(child, [event("task_complete")])
        primary = HostEdgeRepairError("primary proof failure")

        with (
            patch.object(
                repair_host_edges,
                "_verify_commit_proofs",
                side_effect=primary,
            ),
            patch.object(
                repair_host_edges,
                "_rollback_transaction",
                side_effect=OSError("rollback failed"),
            ) as rollback,
            patch.object(
                repair_host_edges,
                "_discard_uncommitted_journal",
                side_effect=OSError("journal cleanup failed"),
            ) as cleanup,
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "primary proof failure") as raised:
                self.repair([child])

        self.assertIs(raised.exception, primary)
        self.assertEqual(rollback.call_count, 1)
        self.assertEqual(cleanup.call_count, 1)
        notes = getattr(raised.exception, "__notes__", [])
        self.assertTrue(any("rollback failed" in note for note in notes))
        self.assertTrue(any("journal cleanup failed" in note for note in notes))

    def test_uncommitted_journal_is_deleted_while_publication_lock_is_held(self) -> None:
        child = child_id(150)
        self.add_edge(child, [event("task_complete")])
        real_discard = repair_host_edges._discard_uncommitted_journal
        contenders: list[bool] = []

        def discard_under_lock(journal: Path | None) -> None:
            self.assertIsNotNone(journal)

            def contend() -> None:
                try:
                    with repair_host_edges.acquire_state_lock(
                        Path(journal).parent,
                        repair_host_edges.JOURNAL_LOCK_IDENTITY,
                        timeout=0,
                    ):
                        contenders.append(False)
                except repair_host_edges.StateLockBusy:
                    contenders.append(True)

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            real_discard(journal)

        with (
            patch.object(
                repair_host_edges,
                "_verify_commit_proofs",
                side_effect=HostEdgeRepairError("proof failure"),
            ),
            patch.object(
                repair_host_edges,
                "_discard_uncommitted_journal",
                side_effect=discard_under_lock,
            ),
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "proof failure"):
                self.repair([child])

        self.assertEqual(contenders, [True])
        self.assertEqual(self.status(child), "open")

    def test_journal_directory_creation_is_published_before_repair(self) -> None:
        child = child_id(148)
        self.add_edge(child, [event("task_complete")])
        real_sync = repair_host_edges._sync_directory
        calls: list[Path] = []

        def observe_sync(path: Path, *, label: str) -> None:
            calls.append(path)
            real_sync(path, label=label)

        with patch.object(
            repair_host_edges,
            "_sync_directory",
            side_effect=observe_sync,
        ):
            self.repair([child])

        journal_root = self.home / "backups" / "cco-host-edge-repair"
        normalized_calls = {path.resolve(strict=False) for path in calls}
        self.assertIn(journal_root.resolve(strict=False), normalized_calls)
        self.assertIn(journal_root.parent.resolve(strict=False), normalized_calls)

    @unittest.skipUnless(os.name == "nt", "requires Windows write-through publication")
    def test_windows_uncommitted_journal_deletion_uses_write_through_rename(self) -> None:
        journal_dir = self.home / "backups" / "cco-host-edge-repair"
        journal_dir.mkdir(parents=True)
        journal = journal_dir / f"{JOURNAL_PREFIX}abort{JOURNAL_SUFFIX}"
        journal.write_text("{}\n", encoding="utf-8")
        replacements: list[tuple[Path, Path]] = []

        def replace(source: Path, target: Path) -> None:
            replacements.append((source, target))
            os.replace(source, target)

        with patch.object(repair_host_edges, "_replace_journal", side_effect=replace):
            repair_host_edges._discard_uncommitted_journal(journal)

        self.assertEqual(replacements[0][0], journal)
        self.assertFalse(journal.exists())
        self.assertFalse(list(journal_dir.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")))

    def test_published_journal_cleanup_syncs_parent_directory(self) -> None:
        child = child_id(145)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        edge = self.edge(child)
        journals = self.home / "backups" / "cco-host-edge-repair"
        repair_host_edges._journal_root(self.home)
        real_sync = repair_host_edges._sync_directory
        sync_calls: list[Path] = []

        def fail_publication_sync(path: Path, *, label: str) -> None:
            del label
            sync_calls.append(path)
            if len(sync_calls) == 1:
                raise HostEdgeRepairError("publication sync failed")
            real_sync(path, label="host-edge rollback journal directory")

        with patch.object(
            repair_host_edges,
            "_sync_directory",
            side_effect=fail_publication_sync,
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "publication sync failed"):
                repair_host_edges._write_rollback_journal(
                    self.database,
                    codex_home=self.home,
                    edges=[edge],
                    parent_thread_id=PARENT,
                )

        # The Windows publication path itself is write-through.  POSIX must
        # additionally synchronize the journal directory after unlinking the
        # failed publication.
        if os.name != "nt":
            self.assertGreaterEqual(len(sync_calls), 2)
            self.assertEqual(sync_calls[0], sync_calls[1])
        else:
            self.assertEqual(len(sync_calls), 1)
        self.assertFalse(list(journals.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")))

    def test_journal_publication_failure_survives_cleanup_failure(self) -> None:
        child = child_id(151)
        self.add_edge(child, [event("task_complete")])
        edge = self.edge(child)
        journal_root = repair_host_edges._journal_root(self.home)
        primary = HostEdgeRepairError("journal publication sync failed")

        with (
            patch.object(repair_host_edges, "_sync_directory", side_effect=primary),
            patch.object(
                repair_host_edges,
                "_remove_journal_durably",
                side_effect=OSError("journal cleanup failed"),
            ) as cleanup,
        ):
            with self.assertRaisesRegex(
                HostEdgeRepairError, "journal publication sync failed"
            ) as raised:
                repair_host_edges._write_rollback_journal(
                    self.database,
                    codex_home=self.home,
                    edges=[edge],
                    parent_thread_id=PARENT,
                    journal_root=journal_root,
                )

        self.assertIs(raised.exception, primary)
        self.assertEqual(cleanup.call_count, 1)
        notes = getattr(raised.exception, "__notes__", [])
        self.assertTrue(any("journal cleanup failed" in note for note in notes))

    def test_first_use_journal_directory_publication_failure_leaves_edge_open(self) -> None:
        child = child_id(149)
        self.add_edge(child, [event("task_complete")])

        with patch.object(
            repair_host_edges,
            "_replace_journal",
            side_effect=OSError("directory publication failed"),
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "parent directory failed"):
                self.repair([child])

        self.assertEqual(self.status(child), "open")
        self.assertFalse((self.home / "backups").exists())

    def test_failed_repair_preserves_prior_rollback_journals(self) -> None:
        child = child_id(142)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        journals = self.home / "backups" / "cco-host-edge-repair"
        journals.mkdir(parents=True)
        prior = []
        for index in range(ROLLBACK_RETENTION + 2):
            journal = journals / (
                f"{JOURNAL_PREFIX}20000101T00000{index}.000000Z-prior{index}{JOURNAL_SUFFIX}"
            )
            journal.write_text(f'{{"prior":{index}}}\n', encoding="utf-8")
            prior.append(journal)
        expected = {path.name: path.read_bytes() for path in prior}

        with patch.object(
            repair_host_edges,
            "_verify_commit_proofs",
            side_effect=HostEdgeRepairError("proof changed before repair commit"),
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "proof changed before repair commit"):
                self.repair([child])

        self.assertEqual(self.status(child), "open")
        self.assertEqual(
            {path.name: path.read_bytes() for path in journals.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")},
            expected,
        )

    def test_retention_runs_only_after_committed_repair(self) -> None:
        child = child_id(143)
        self.add_edge(child, [event("task_started"), event("task_complete")])
        real_prune = repair_host_edges._prune_rollback_journals
        observed: list[str] = []

        def prune_after_commit(journal: Path) -> None:
            observed.append(self.status(child))
            self.assertEqual(observed[-1], "closed")
            real_prune(journal)

        with patch.object(
            repair_host_edges,
            "_prune_rollback_journals",
            side_effect=prune_after_commit,
        ):
            result = self.repair([child])

        self.assertEqual(self.status(child), "closed")
        self.assertEqual(observed, ["closed"])
        self.assertEqual(result["warnings"], [])

    def test_journal_is_minimal_private_retained_and_journal_failure_rolls_back(self) -> None:
        child = child_id(15)
        self.add_edge(child, [event("task_complete")])
        result = self.repair([child])
        journal = Path(str(result["journal"]))
        document = json.loads(journal.read_text(encoding="utf-8"))

        self.assertEqual(self.status(child), "closed")
        self.assertEqual(document["protocol"], "cco.host-edge-rollback.v2")
        self.assertEqual(
            set(document["edges"][0]),
            {
                "child_thread_id",
                "parent_thread_id",
                "prior_status",
                "proof_digest",
                "rollout_path",
            },
        )
        self.assertEqual(document["edges"][0]["prior_status"], "open")
        self.assertTrue(document["edges"][0]["proof_digest"].startswith("sha256:"))
        self.assertNotIn("agent_path", document["edges"][0])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode) & 0o077, 0)
            self.assertEqual(stat.S_IMODE(journal.parent.stat().st_mode) & 0o077, 0)

        for index in range(ROLLBACK_RETENTION + 2):
            stale = journal.parent / (
                f"{JOURNAL_PREFIX}20990101T00000{index}.000000Z-test{index}{JOURNAL_SUFFIX}"
            )
            stale.write_text("{}\n", encoding="utf-8")
            os.utime(stale, (2_000_000_000 + index, 2_000_000_000 + index))
        _prune_rollback_journals(journal)
        retained = [
            path
            for path in journal.parent.iterdir()
            if path.name.startswith(JOURNAL_PREFIX) and path.name.endswith(JOURNAL_SUFFIX)
        ]
        self.assertEqual(len(retained), ROLLBACK_RETENTION)
        self.assertTrue(journal.exists())
        self.assertIn(journal, retained)

        rollback_child = child_id(16)
        self.add_edge(rollback_child, [event("task_complete")])
        with patch.object(
            repair_host_edges,
            "_write_rollback_journal",
            side_effect=HostEdgeRepairError("journal unavailable"),
        ):
            with self.assertRaisesRegex(HostEdgeRepairError, "journal unavailable"):
                self.repair([rollback_child])
        self.assertEqual(self.status(rollback_child), "open")

    def test_post_commit_prune_failure_is_only_a_warning(self) -> None:
        journal = self.root / "journal.rollback.json"
        with patch.object(
            repair_host_edges,
            "_prune_rollback_journals",
            side_effect=OSError("locked"),
        ):
            warnings = _post_commit_warnings(journal)
        self.assertEqual(
            warnings,
            ["repair committed, but old rollback journals could not be pruned"],
        )

    @unittest.skipIf(zstd is None, "requires Python 3.14 zstd support")
    def test_compressed_jsonl_zst_rollout_is_supported(self) -> None:
        child = child_id(17)
        self.add_edge(child, [event("task_complete")], suffix=".jsonl.zst")

        self.assertEqual(self.edge(child)["verdict"], "repairable")

    @unittest.skipUnless(os.name == "nt", "requires Windows extended paths")
    def test_windows_extended_paths_are_normalized_for_home_and_rollout(self) -> None:
        child = child_id(18)
        rollout = self.add_edge(child, [event("task_complete")])
        extended_rollout = "\\\\?\\" + str(rollout)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (extended_rollout, child),
            )
            connection.commit()

        report = audit_edges(
            codex_home=Path("\\\\?\\" + str(self.home)),
            parent_thread_id=PARENT,
            child_thread_ids=[child],
        )

        self.assertEqual(report["edges"][0]["verdict"], "repairable")
        self.assertIn(CCO_ROLE, CCO_ROLES)


if __name__ == "__main__":
    unittest.main()

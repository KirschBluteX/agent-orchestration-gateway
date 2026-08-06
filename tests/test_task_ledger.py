from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from task_ledger import LedgerConflict, TaskLedger  # noqa: E402


def identity(
    *,
    generation: int = 1,
    assurance: str = "mechanical",
    model: str = "gpt-5.6-luna",
    rank: int = 1,
) -> dict[str, object]:
    return {
        "acceptance_ids": ["A01"],
        "assurance": assurance,
        "contract_rev": 1,
        "contract_sha256": "sha256:" + "a" * 64,
        "cursor": 0,
        "generation": generation,
        "input_sha256": "sha256:" + chr(96 + generation + rank) * 64,
        "node": "n01_worker",
        "role": "worker",
        "route": {
            "assurance": assurance,
            "constraints": {
                "fixed_effort": None,
                "fixed_model": None,
                "source": "automatic",
            },
            "decision_sha256": "sha256:" + "c" * 64,
            "plan_sha256": "sha256:" + chr(99 + rank) * 64,
            "rank": rank,
            "selected": {"effort": "max", "model": model},
        },
        "run": f"worker_n01_worker_g{generation:02d}",
    }


class TaskLedgerTests(unittest.TestCase):
    def test_prethread_fallback_requires_each_confirmed_rank_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            with self.assertRaisesRegex(LedgerConflict, "rank 1"):
                ledger.reserve(
                    "skip-initial",
                    identity(rank=2, model="gpt-5.6-terra"),
                )

            ledger.reserve("spawn-r1", identity())
            ledger.release("spawn-r1")
            self.assertEqual(ledger.read_rows()[0]["state"], "rejected")
            with self.assertRaisesRegex(LedgerConflict, "next fallback rank"):
                ledger.reserve(
                    "skip-r2",
                    identity(rank=3, model="gpt-5.6-terra"),
                )
            fallback = ledger.reserve(
                "spawn-r2",
                identity(rank=2, model="gpt-5.6-terra"),
            )
            self.assertEqual(fallback["route"]["rank"], 2)
            self.assertEqual(fallback["state"], "reserved")

    def test_concurrent_reservations_leave_one_valid_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            outcomes: list[str] = []

            def reserve(call_id: str) -> None:
                try:
                    ledger.reserve(call_id, identity())
                except LedgerConflict:
                    outcomes.append("conflict")
                else:
                    outcomes.append("reserved")

            threads = [threading.Thread(target=reserve, args=(f"call-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(outcomes), ["conflict", "reserved"])
            self.assertEqual(len(ledger.read_rows()), 1)

    def test_continuation_is_single_flight_and_rejection_preserves_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            owner = "/root/worker_n01_worker_g01"
            ledger.reserve("spawn", identity())
            ledger.activate("spawn", owner)
            ledger.prepare_continuation(
                "continue-1",
                owner,
                previous_input_sha256="sha256:" + "b" * 64,
                next_input_sha256="sha256:" + "e" * 64,
                cursor=1,
            )
            with self.assertRaisesRegex(LedgerConflict, "pending"):
                ledger.prepare_continuation(
                    "continue-2",
                    owner,
                    previous_input_sha256="sha256:" + "b" * 64,
                    next_input_sha256="sha256:" + "f" * 64,
                    cursor=1,
                )
            ledger.settle_pending_continuation("continue-1", accepted=False)
            row = ledger.read_rows()[0]
            self.assertEqual(row["cursor"], 0)
            self.assertEqual(row["input_sha256"], "sha256:" + "b" * 64)

    def test_continuation_and_unseeded_result_clear_stale_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            owner = "/root/worker_n01_worker_g01"
            ledger.reserve("spawn", identity())
            ledger.activate("spawn", owner)
            seed = {
                "disposition": "continue",
                "payload": {"evidence": "old"},
                "status": "complete",
            }
            ledger.record_result(
                node="n01_worker",
                contract_rev=1,
                run="worker_n01_worker_g01",
                generation=1,
                input_sha256="sha256:" + "b" * 64,
                owner=owner,
                disposition="continuable",
                review_seed=seed,
            )
            self.assertEqual(ledger.read_rows()[0]["review_seed"], seed)

            ledger.prepare_continuation(
                "continue-1",
                owner,
                previous_input_sha256="sha256:" + "b" * 64,
                next_input_sha256="sha256:" + "e" * 64,
                cursor=1,
            )
            ledger.settle_pending_continuation("continue-1", accepted=True)
            row = ledger.read_rows()[0]
            self.assertEqual(row["input_sha256"], "sha256:" + "e" * 64)
            self.assertNotIn("review_seed", row)

            ledger.record_result(
                node="n01_worker",
                contract_rev=1,
                run="worker_n01_worker_g01",
                generation=1,
                input_sha256="sha256:" + "e" * 64,
                owner=owner,
                disposition="continuable",
                cursor=1,
                review_seed=seed,
            )
            self.assertIn("review_seed", ledger.read_rows()[0])
            ledger.record_result(
                node="n01_worker",
                contract_rev=1,
                run="worker_n01_worker_g01",
                generation=1,
                input_sha256="sha256:" + "e" * 64,
                owner=owner,
                disposition="continuable",
                cursor=1,
                review_seed=None,
            )
            self.assertNotIn("review_seed", ledger.read_rows()[0])

    def test_retired_and_superseded_owners_remain_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            first_owner = "/root/worker_n01_worker_g01"
            ledger.reserve("spawn-1", identity())
            ledger.activate("spawn-1", first_owner)
            ledger.retire(first_owner)
            self.assertTrue(ledger.is_managed_owner(first_owner))

            replacement = ledger.reserve(
                "spawn-2",
                identity(generation=2, assurance="guarded", model="gpt-5.6-terra"),
            )
            self.assertEqual(replacement["generation"], 2)
            with self.assertRaisesRegex(LedgerConflict, "stale"):
                ledger.record_result(
                    node="n01_worker",
                    contract_rev=1,
                    run="worker_n01_worker_g01",
                    generation=1,
                    input_sha256="sha256:" + "b" * 64,
                    owner=first_owner,
                    disposition="retired",
                )

    def test_host_restart_retires_owned_rows_and_records_a_guarded_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            owner = "/root/worker_n01_worker_g01"
            ledger.reserve("spawn", identity())
            ledger.activate("spawn", owner)

            self.assertEqual(ledger.retire_for_host_restart(), [owner])
            row = ledger.read_rows()[0]
            self.assertEqual(row["state"], "retired")
            self.assertEqual(row["retire_reason"], "host_restart")
            self.assertTrue(ledger.is_managed_owner(owner))
            self.assertEqual(ledger.read_rows()[0]["state"], "retired")
            self.assertEqual(ledger.retire_for_host_restart(), [])

            pending = TaskLedger(Path(directory), "session-b")
            pending.reserve("pending", identity())
            self.assertEqual(pending.retire_for_host_restart(), [])
            self.assertEqual(pending.read_rows()[0]["state"], "retired")
            self.assertEqual(pending.read_rows()[0]["retire_reason"], "host_restart")

    def test_cleanup_removes_only_terminal_or_expired_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = TaskLedger(root, "current")
            owner = "/root/worker_n01_worker_g01"
            ledger.reserve("spawn", identity())
            self.assertFalse(ledger.cleanup_if_terminal())
            ledger.activate("spawn", owner)
            ledger.retire(owner)
            self.assertTrue(ledger.cleanup_if_terminal())
            self.assertFalse(ledger.path.exists())

            stale = root / "stale.json"
            stale.write_text('{"fenced_owners":[],"guarded_floors":[],"rows":{}}', encoding="utf-8")
            old = time.time() - 120
            os.utime(stale, (old, old))
            fresh = root / "fresh.json"
            fresh.write_text('{"fenced_owners":[],"guarded_floors":[],"rows":{}}', encoding="utf-8")
            active = TaskLedger(root, "active")
            active.reserve("spawn-active", identity())
            os.utime(active.path, (old, old))
            removed = TaskLedger.cleanup_stale(
                root,
                keep_session_id="current",
                max_age_seconds=60,
                live_max_age_seconds=180,
            )
            self.assertEqual(removed, [stale])
            self.assertTrue(fresh.exists())
            self.assertTrue(active.path.exists())

            very_old = time.time() - 240
            os.utime(active.path, (very_old, very_old))
            removed = TaskLedger.cleanup_stale(
                root,
                keep_session_id="current",
                max_age_seconds=60,
                live_max_age_seconds=180,
            )
            self.assertEqual(removed, [active.path])


if __name__ == "__main__":
    unittest.main()

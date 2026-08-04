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


def identity(
    *, run: str = "run_n01_policy_r01", generation: int | None = None
) -> dict[str, object]:
    suffix = run.rsplit("_", 1)[-1]
    return {
        "contract_rev": 1,
        "contract_sha256": "sha256:" + "a" * 64,
        "input_sha256": "sha256:" + "b" * 64,
        "generation": generation if generation is not None else int(suffix[1:]),
        "cursor": 0,
        "node": "n01_policy",
        "role": "cost_orchestrator_write_leaf",
        "run": run,
    }


class TaskLedgerBehaviorTests(unittest.TestCase):
    def test_worker_result_is_retired_without_primary_acceptance(self) -> None:
        from task_ledger import TaskLedger

        owner = "/root/work_n01_policy_complex_r01"
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", owner)

            returned = ledger.record_result(
                node="n01_policy",
                contract_rev=1,
                run="run_n01_policy_r01",
                generation=1,
                input_sha256="sha256:" + "b" * 64,
                owner=owner,
                disposition="retired",
            )

            self.assertEqual(returned["state"], "retired")
            self.assertNotIn("result_status", returned)
            self.assertNotIn("complete", {returned["state"]})

    def test_fix_first_delta_ship_uses_one_prepared_continuation(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        owner = "/root/review_e01_r01"
        review = {
            **identity(),
            "node": "review_e01",
            "run": "run_review_e01_r01",
            "role": "cost_orchestrator_read_leaf",
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("spawn", review)
            ledger.activate("spawn", owner)
            fix_first = ledger.record_result(
                node="review_e01",
                contract_rev=1,
                run="run_review_e01_r01",
                generation=1,
                input_sha256="sha256:" + "b" * 64,
                owner=owner,
                disposition="continuable",
            )
            self.assertEqual(fix_first["state"], "continuable")
            repeated = ledger.record_result(
                node="review_e01",
                contract_rev=1,
                run="run_review_e01_r01",
                generation=1,
                input_sha256="sha256:" + "b" * 64,
                owner=owner,
                disposition="continuable",
            )
            self.assertEqual(repeated, fix_first)

            ledger.prepare_continuation(
                "delta-1",
                owner,
                previous_input_sha256="sha256:" + "b" * 64,
                next_input_sha256="sha256:" + "c" * 64,
                cursor=1,
            )
            with self.assertRaises(LedgerConflict):
                ledger.prepare_continuation(
                    "delta-racing",
                    owner,
                    previous_input_sha256="sha256:" + "b" * 64,
                    next_input_sha256="sha256:" + "d" * 64,
                    cursor=1,
                )
            settled = ledger.settle_continuation("delta-1", accepted=True)
            self.assertEqual(settled["state"], "owned")
            self.assertEqual(settled["input_sha256"], "sha256:" + "c" * 64)

            shipped = ledger.record_result(
                node="review_e01",
                contract_rev=1,
                run="run_review_e01_r01",
                generation=1,
                input_sha256="sha256:" + "c" * 64,
                owner=owner,
                disposition="retired",
                cursor=1,
            )
            self.assertEqual(shipped["state"], "retired")

    def test_new_generation_replaces_retired_owner_and_rejects_late_result(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        old_owner = "/root/work_n01_policy_complex_r01"
        new_owner = "/root/work_n01_policy_complex_r02"
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("spawn-1", identity())
            ledger.activate("spawn-1", old_owner)
            ledger.retire(old_owner)
            ledger.reserve("spawn-2", identity(run="run_n01_policy_r02"))
            ledger.activate("spawn-2", new_owner)

            with self.assertRaises(LedgerConflict):
                ledger.record_result(
                    node="n01_policy",
                    contract_rev=1,
                    run="run_n01_policy_r01",
                    generation=1,
                    input_sha256="sha256:" + "b" * 64,
                    owner=old_owner,
                    disposition="retired",
                )
            current = ledger.read_rows()[0]
            self.assertEqual(current["owner"], new_owner)
            self.assertEqual(current["generation"], 2)

    def test_retired_and_superseded_owners_remain_fenced_until_cleanup(self) -> None:
        from task_ledger import TaskLedger

        old_owner = "/root/work_n01_policy_complex_r01"
        new_owner = "/root/work_n01_policy_complex_r02"
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("spawn-1", identity())
            ledger.activate("spawn-1", old_owner)
            ledger.retire(old_owner)
            self.assertTrue(ledger.is_managed_owner(old_owner))

            ledger.reserve("spawn-2", identity(run="run_n01_policy_r02"))
            ledger.activate("spawn-2", new_owner)
            self.assertTrue(ledger.is_managed_owner(old_owner))
            self.assertTrue(ledger.is_managed_owner(new_owner))

            ledger.cleanup_if_terminal(force=True)
            self.assertFalse(ledger.is_managed_owner(old_owner))

    def test_generation_is_the_only_takeover_fence(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        owner = "/root/work_n01_policy_complex_r01"
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("spawn-1", identity())
            ledger.activate("spawn-1", owner)
            ledger.retire(owner)

            with self.assertRaises(LedgerConflict):
                ledger.reserve(
                    "spawn-stale",
                    identity(run="run_n01_policy_r02", generation=1),
                )
            replacement = ledger.reserve(
                "spawn-current",
                identity(run="run_n01_policy_r02", generation=2),
            )
            self.assertEqual(replacement["generation"], 2)

    def test_one_node_revision_can_have_only_one_reserved_owner(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            first = ledger.reserve("tool-1", identity())

            self.assertEqual(first["state"], "reserved")
            self.assertNotIn("identity_sha256", first)
            self.assertNotIn("entry_sha256", first)
            self.assertNotIn("transition_sha256", first)
            with self.assertRaises(LedgerConflict):
                ledger.reserve("tool-2", identity(run="run_n01_policy_r02"))

    def test_repeating_the_same_reservation_is_idempotent(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            first = ledger.reserve("tool-1", identity())
            repeated = ledger.reserve("tool-1", identity())

            self.assertEqual(repeated, first)

    def test_postflight_activates_the_reserved_native_owner(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())

            active = ledger.activate(
                "tool-1", "/root/work_n01_policy_complex_r01"
            )

            self.assertEqual(active["state"], "owned")
            self.assertEqual(
                active["owner"], "/root/work_n01_policy_complex_r01"
            )

    def test_failed_native_spawn_releases_its_reservation(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())

            ledger.release("tool-1")
            replacement = ledger.reserve(
                "tool-2", identity(run="run_n01_policy_r02")
            )

            self.assertEqual(replacement["state"], "reserved")

    def test_live_continuation_must_match_owner_and_current_input(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")

            ledger.prepare_continuation(
                "continuation-1",
                "/root/work_n01_policy_complex_r01",
                previous_input_sha256="sha256:" + "b" * 64,
                next_input_sha256="sha256:" + "c" * 64,
                cursor=1,
            )
            continued = ledger.settle_continuation("continuation-1", accepted=True)
            self.assertEqual(continued["input_sha256"], "sha256:" + "c" * 64)
            self.assertEqual(continued["cursor"], 1)
            with self.assertRaises(LedgerConflict):
                ledger.prepare_continuation(
                    "continuation-stale",
                    "/root/work_n01_policy_complex_r01",
                    previous_input_sha256="sha256:" + "b" * 64,
                    next_input_sha256="sha256:" + "d" * 64,
                    cursor=2,
                )

    def test_rejected_continuation_restores_the_previous_cursor(self) -> None:
        from task_ledger import TaskLedger

        owner = "/root/work_n01_policy_complex_r01"
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", owner)
            ledger.prepare_continuation(
                "continuation-rejected",
                owner,
                previous_input_sha256="sha256:" + "b" * 64,
                next_input_sha256="sha256:" + "c" * 64,
                cursor=1,
            )

            restored = ledger.settle_continuation("continuation-rejected", accepted=False)

            self.assertEqual(restored["state"], "owned")
            self.assertEqual(restored["input_sha256"], "sha256:" + "b" * 64)
            self.assertEqual(restored["cursor"], 0)

    def test_retire_rejects_a_late_result(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")

            retired = ledger.retire("/root/work_n01_policy_complex_r01")

            self.assertEqual(retired["state"], "retired")
            with self.assertRaises(LedgerConflict):
                ledger.record_result(
                    node="n01_policy",
                    contract_rev=1,
                    run="run_n01_policy_r01",
                    generation=1,
                    input_sha256="sha256:" + "b" * 64,
                    owner="/root/work_n01_policy_complex_r01",
                    disposition="retired",
                )

    def test_returned_result_retires_owner_but_allows_a_new_generation(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")

            returned = ledger.record_result(
                node="n01_policy",
                contract_rev=1,
                run="run_n01_policy_r01",
                generation=1,
                input_sha256="sha256:" + "b" * 64,
                owner="/root/work_n01_policy_complex_r01",
                disposition="retired",
            )
            replacement = ledger.reserve("tool-2", identity(run="run_n01_policy_r02"))

            self.assertEqual(returned["state"], "retired")
            self.assertEqual(replacement["generation"], 2)

    def test_fenced_revision_requires_a_new_generation(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")
            ledger.retire("/root/work_n01_policy_complex_r01")
            with self.assertRaises(LedgerConflict):
                ledger.reserve("tool-2", identity(run="run_n01_policy_r01"))
            replacement = ledger.reserve("tool-3", identity(run="run_n01_policy_r02"))
            self.assertEqual(replacement["run"], "run_n01_policy_r02")

            ledger.release("tool-3")
            restored = ledger.read_rows()[0]
            self.assertEqual(restored["state"], "retired")
            self.assertEqual(restored["run"], "run_n01_policy_r01")

    def test_concurrent_reservations_leave_valid_json_and_one_owner(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            outcomes: list[str] = []

            def reserve(tool_id: str) -> None:
                try:
                    ledger.reserve(tool_id, identity())
                except LedgerConflict:
                    outcomes.append("conflict")
                else:
                    outcomes.append("reserved")

            threads = [
                threading.Thread(target=reserve, args=(f"tool-{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(outcomes), ["conflict", "reserved"])
            self.assertEqual(len(ledger.read_rows()), 1)

    def test_task_stop_removes_ledger_and_atomic_staging_files(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = TaskLedger(root, "session-a")
            ledger.reserve("tool-1", identity())

            ledger.cleanup_if_terminal(force=True)

            self.assertFalse(ledger.path.exists())
            self.assertEqual(list(root.glob(".*.tmp-*.json")), [])

    def test_terminal_cleanup_does_not_remove_a_live_row(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())

            self.assertFalse(ledger.cleanup_if_terminal())
            self.assertTrue(ledger.path.exists())

            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")
            ledger.retire("/root/work_n01_policy_complex_r01")
            self.assertTrue(ledger.cleanup_if_terminal())
            self.assertFalse(ledger.path.exists())

    def test_partial_worker_result_retires_its_exact_owner(self) -> None:
        from task_ledger import LedgerConflict, TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), "session-a")
            ledger.reserve("tool-1", identity())
            ledger.activate("tool-1", "/root/work_n01_policy_complex_r01")

            row = ledger.record_result(
                node="n01_policy",
                contract_rev=1,
                run="run_n01_policy_r01",
                input_sha256="sha256:" + "b" * 64,
                generation=1,
                owner="/root/work_n01_policy_complex_r01",
                disposition="retired",
            )

            self.assertEqual(row["state"], "retired")
            with self.assertRaises(LedgerConflict):
                ledger.record_result(
                    node="n01_policy",
                    contract_rev=1,
                    run="run_n01_policy_r01",
                    input_sha256="sha256:" + "b" * 64,
                    generation=1,
                    owner="/root/work_n01_policy_complex_r01",
                    disposition="retired",
                )

    def test_lazy_cleanup_removes_only_expired_ledger_residue(self) -> None:
        from task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = TaskLedger(root, "session-old")
            current = TaskLedger(root, "session-current")
            stale.reserve("tool-old", identity())
            current.reserve("tool-current", identity())
            expired = time.time() - 120
            os.utime(stale.path, (expired, expired))

            removed = TaskLedger.cleanup_stale(
                root, keep_session_id="session-current", max_age_seconds=60
            )

            self.assertEqual(removed, [stale.path])
            self.assertFalse(stale.path.exists())
            self.assertTrue(current.path.exists())


if __name__ == "__main__":
    unittest.main()

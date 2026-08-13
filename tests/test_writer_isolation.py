from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import control_plane as control_plane_module  # noqa: E402
from control_plane import (  # noqa: E402
    RESULT_HEADER,
    ControlPlane,
    ControlPlaneError,
    ControlPlaneUnavailable,
    parse_task_message,
)
from delegation_compiler import compile_delegation_request  # noqa: E402
import writer_isolation as isolation_module  # noqa: E402
from operation_deadline import deadline_after  # noqa: E402


def catalog() -> dict[str, object]:
    return {
        "models": [
            {
                "multi_agent_version": "v2",
                "slug": "gpt-5.6-terra",
                "supported_reasoning_levels": [{"effort": "max"}],
            }
        ]
    }


class CooperativeWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_workspace(self, kind: str = "clean") -> Path:
        repo = self.root / f"repo-{kind}"
        repo.mkdir()
        (repo / "a.txt").write_text("a0\n", encoding="utf-8")
        (repo / "b.txt").write_text("b0\n", encoding="utf-8")
        if kind != "directory":
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=CCO Tests",
                    "-c",
                    "user.email=cco-tests@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repo,
                check=True,
            )
            if kind == "dirty":
                (repo / "a.txt").write_text("dirty baseline\n", encoding="utf-8")
        return repo

    @staticmethod
    def plan(*, cooperative: bool = True, overlap: bool = False) -> dict[str, object]:
        right_scope = {"kind": "exact", "path": "a.txt"} if overlap else {
            "kind": "exact",
            "path": "b.txt",
        }
        plan: dict[str, object] = {
            "acceptance": {"A01": "left writer", "A02": "right writer"},
            "goal": "cooperative writer exercise",
            "nodes": [
                {
                    "acceptance": ["A01"],
                    "id": "left",
                    "objective": "change left",
                    "role": "worker",
                    "scopes": [{"kind": "exact", "path": "a.txt"}],
                },
                {
                    "acceptance": ["A02"],
                    "id": "right",
                    "objective": "change right",
                    "role": "worker",
                    "scopes": [right_scope],
                },
            ],
        }
        if cooperative:
            plan["writer_isolation"] = "cooperative"
        return plan

    def control(self, name: str) -> ControlPlane:
        return ControlPlane(name, root=self.root / f"state-{name}")

    def dispatch(self, control: ControlPlane, repo: Path, *, capacity: int = 2) -> list[dict[str, object]]:
        control.create_plan(repo, self.plan())
        return control.next_wave(capacity=capacity, native_catalog=catalog())["dispatches"]

    def start(self, control: ControlPlane, action: dict[str, object], index: int) -> tuple[dict[str, object], str]:
        native = action["tool_input"]
        assert isinstance(native, dict)
        task = parse_task_message(native["message"])
        control.preflight_spawn({"tool_input": native, "tool_use_id": f"call-{index}"})
        owner = "/root/" + str(native["task_name"])
        control.postflight_tool(
            {
                "tool_input": native,
                "tool_response": {"task_name": owner},
                "tool_use_id": f"call-{index}",
            }
        )
        return task, owner

    @staticmethod
    def result(task: dict[str, object], acceptance: str, path: str | None) -> str:
        return RESULT_HEADER + "\n" + json.dumps(
            {
                "blockers": [],
                "changed_paths": [] if path is None else [path],
                "cursor": 0,
                "deviations": [],
                "dispatch_id": task["dispatch_id"],
                "evidence": {acceptance: "deterministic cooperative check"},
                "failure_signature": None,
                "outcome": "retire",
                "status": "complete",
                "summary": "cooperative writer complete",
            },
            separators=(",", ":"),
        )

    def test_default_serial_behavior_still_has_one_writer(self) -> None:
        repo = self.make_workspace()
        control = self.control("serial")
        control.create_plan(repo, self.plan(cooperative=False))
        batch = control.next_wave(capacity=2, native_catalog=catalog())
        self.assertEqual(len(batch["dispatches"]), 1)
        task = parse_task_message(batch["dispatches"][0]["tool_input"]["message"])
        self.assertNotIn("writer_isolation", task)
        self.assertTrue(Path(task["workspace_root"]).samefile(repo))

    def test_clean_git_uses_detached_worktrees_and_explicit_task_notice(self) -> None:
        repo = self.make_workspace("clean")
        with (
            patch.object(
                isolation_module,
                "_walk_visible",
                side_effect=AssertionError("clean Git worktrees must not scan the full tree"),
            ),
            patch.object(
                isolation_module,
                "_git",
                wraps=isolation_module._git,
            ) as git_calls,
        ):
            actions = self.dispatch(self.control("clean"), repo)
        status_calls = [
            call.args[1:]
            for call in git_calls.call_args_list
            if len(call.args) > 1 and call.args[1] == "status"
        ]
        self.assertEqual(
            status_calls,
            [
                (
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=normal",
                    "--ignored=matching",
                    "--",
                    ":(top,literal)a.txt",
                    ":(top,literal)b.txt",
                )
            ],
        )
        self.assertEqual(len(actions), 2)
        for action in actions:
            task = parse_task_message(action["tool_input"]["message"])
            isolate = Path(task["workspace_root"])
            self.assertNotEqual(isolate, repo)
            self.assertTrue((isolate / ".git").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=isolate,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "true",
            )
            notice = task["writer_isolation"]["notice"]
            self.assertIn("not a sandbox", notice)
            self.assertIn("Work only", notice)

    def test_ignored_writer_content_selects_a_bounded_copy(self) -> None:
        repo = self.make_workspace("clean")
        (repo / ".gitignore").write_text("a.cache\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-qm",
                "ignore cache",
            ],
            cwd=repo,
            check=True,
        )
        (repo / "a.cache").write_text("required ignored input\n", encoding="utf-8")

        records = isolation_module.prepare_isolates(
            self.root / "ignored-state",
            repo,
            backend="git",
            session_id="ignored-copy",
            batch_id="sha256:" + "9" * 64,
            members=[
                {
                    "id": "left",
                    "scopes": [{"kind": "exact", "path": "a.cache"}],
                }
            ],
        )

        self.assertEqual(records[0]["mode"], isolation_module.COPY)
        isolate = Path(records[0]["isolate_root"])
        self.assertEqual(
            (isolate / "a.cache").read_text(encoding="utf-8"),
            "required ignored input\n",
        )
        self.assertEqual(isolation_module.cleanup_isolates(self.root / "ignored-state", records), 1)

    def test_guarded_cooperative_writers_run_before_the_final_reviewer(self) -> None:
        repo = self.make_workspace("clean")
        control = self.control("guarded-review")
        plan = self.plan()
        for node in plan["nodes"]:
            node["verification"] = "semantic"
        control.create_plan(repo, plan)
        with control._coordinated_state() as state:
            normalized = control._read_plan(state)
            self.assertEqual(
                [node["role"] for node in normalized["nodes"]],
                ["reviewer", "worker", "worker"],
            )

        actions = control.next_wave(capacity=2, native_catalog=catalog())["dispatches"]
        self.assertEqual(len(actions), 2)
        tasks_and_owners = [
            self.start(control, action, index) for index, action in enumerate(actions)
        ]
        for task, _owner in tasks_and_owners:
            self.assertIn("writer_isolation", task)
            self.assertFalse(Path(task["workspace_root"]).samefile(repo))
        control.record_result(
            tasks_and_owners[0][1],
            self.result(tasks_and_owners[0][0], "A01", None),
        )
        control.record_result(
            tasks_and_owners[1][1],
            self.result(tasks_and_owners[1][0], "A02", None),
        )

        review = control.next_wave(capacity=2, native_catalog=catalog())["dispatches"]
        self.assertEqual(len(review), 1)
        review_task = parse_task_message(review[0]["tool_input"]["message"])
        self.assertEqual(review_task["role"], "reviewer")
        self.assertNotIn("writer_isolation", review_task)
        self.assertTrue(Path(review_task["workspace_root"]).samefile(repo))
        control.restart()
        self.assertGreater(control.cleanup(), 0)

    def test_dirty_git_and_non_git_use_reparse_free_copies_without_git_control(self) -> None:
        for kind in ("dirty", "directory"):
            with self.subTest(kind=kind):
                repo = self.make_workspace(kind)
                if kind == "dirty":
                    # A nested worktree/submodule can use either a .git
                    # directory or file. It is not task-visible content in a
                    # cooperative copy.
                    marker = repo / "nested" / ".git"
                    marker.mkdir(parents=True)
                    (marker / "control").write_text("not copied\n", encoding="utf-8")
                actions = self.dispatch(self.control(kind), repo)
                self.assertEqual(len(actions), 2)
                for action in actions:
                    task = parse_task_message(action["tool_input"]["message"])
                    isolate = Path(task["workspace_root"])
                    self.assertFalse((isolate / ".git").exists())
                    if kind == "dirty":
                        self.assertFalse((isolate / "nested" / ".git").exists())
                    self.assertEqual((isolate / "a.txt").read_text(encoding="utf-8"), (repo / "a.txt").read_text(encoding="utf-8"))

    def test_copy_never_carries_nested_git_control_for_a_directory_workspace(self) -> None:
        source = self.root / "copy-source"
        source.mkdir()
        (source / "visible.txt").write_text("visible\n", encoding="utf-8")
        marker = source / "nested" / ".git"
        marker.mkdir(parents=True)
        (marker / "control").write_text("not copied\n", encoding="utf-8")
        records = isolation_module.prepare_isolates(
            self.root / "copy-state",
            source,
            backend="directory",
            session_id="directory-copy",
            batch_id="sha256:" + "c" * 64,
            members=[{"id": "left", "scopes": [{"kind": "exact", "path": "visible.txt"}]}],
        )
        isolate = Path(records[0]["isolate_root"])
        self.assertEqual((isolate / "visible.txt").read_text(encoding="utf-8"), "visible\n")
        self.assertFalse((isolate / "nested" / ".git").exists())

    def test_scope_overlap_and_capacity_fall_back_to_serial_before_spawn(self) -> None:
        repo = self.make_workspace()
        overlap = self.control("overlap")
        overlap.create_plan(repo, self.plan(overlap=True))
        overlap_batch = overlap.next_wave(capacity=2, native_catalog=catalog())
        self.assertEqual(len(overlap_batch["dispatches"]), 1)
        overlap_task = parse_task_message(overlap_batch["dispatches"][0]["tool_input"]["message"])
        self.assertNotIn("writer_isolation", overlap_task)

        limited = self.control("capacity")
        limited.create_plan(repo, self.plan())
        limited_batch = limited.next_wave(capacity=1, native_catalog=catalog())
        self.assertEqual(len(limited_batch["dispatches"]), 1)
        limited_task = parse_task_message(limited_batch["dispatches"][0]["tool_input"]["message"])
        self.assertNotIn("writer_isolation", limited_task)

    def test_prebaseline_canonical_race_is_rejected_before_spawn(self) -> None:
        repo = self.make_workspace()
        control = self.control("prebaseline-race")
        control.create_plan(repo, self.plan())
        original = control_plane_module.prepare_isolates

        def prepare_then_change(*args: object, **kwargs: object) -> list[dict[str, object]]:
            records = original(*args, **kwargs)
            (repo / "a.txt").write_text("external race\n", encoding="utf-8")
            return records

        with patch.object(
            control_plane_module,
            "prepare_isolates",
            side_effect=prepare_then_change,
        ):
            with self.assertRaisesRegex(
                ControlPlaneUnavailable,
                "does not match the canonical baseline",
            ):
                control.next_wave(capacity=2, native_catalog=catalog())
        self.assertEqual(control.status()["counts"]["starting"], 0)

    def test_all_required_results_hold_then_apply_together(self) -> None:
        repo = self.make_workspace()
        control = self.control("all-required")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")

        held = control.record_result(first_owner, self.result(first, "A01", "a.txt"))
        self.assertEqual(held["state"], "ready_to_apply")
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")

        applied = control.record_result(second_owner, self.result(second, "A02", "b.txt"))
        self.assertEqual(applied["state"], "retired")
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a1\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b1\n")
        self.assertEqual(control.status()["state"], "complete")

    def test_canonical_drift_fences_the_whole_batch_without_repair(self) -> None:
        repo = self.make_workspace()
        control = self.control("drift")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("isolated\n", encoding="utf-8")
        (repo / "a.txt").write_text("external canonical drift\n", encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "canonical workspace drift"):
            control.record_result(first_owner, self.result(first, "A01", "a.txt"))
        status = control.status()
        self.assertEqual(status["counts"]["fenced"], 2)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "external canonical drift\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")

    def test_ready_result_replay_rechecks_canonical_drift(self) -> None:
        repo = self.make_workspace()
        control = self.control("ready-replay-drift")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("isolated\n", encoding="utf-8")
        result = self.result(first, "A01", "a.txt")
        self.assertEqual(control.record_result(first_owner, result)["state"], "ready_to_apply")
        (repo / "a.txt").write_text("external canonical drift\n", encoding="utf-8")

        with self.assertRaisesRegex(ControlPlaneError, "canonical workspace drift"):
            control.record_result(first_owner, result)
        self.assertEqual(control.status()["counts"]["fenced"], 2)

    def test_apply_failure_rolls_back_exact_staged_content(self) -> None:
        canonical = self.root / "canonical"
        source = self.root / "source"
        canonical.mkdir()
        source.mkdir()
        (canonical / "a.txt").write_text("a0\n", encoding="utf-8")
        (canonical / "b.txt").write_text("b0\n", encoding="utf-8")
        (source / "a.txt").write_text("a1\n", encoding="utf-8")
        (source / "b.txt").write_text("b1\n", encoding="utf-8")
        journal = isolation_module.stage_apply_journal(
            self.root / "journal-state",
            wave_id="sha256:" + "1" * 64,
            canonical_root=canonical,
            changes={
                "a.txt": {"source_root": str(source)},
                "b.txt": {"source_root": str(source)},
            },
        )
        original = isolation_module._copy_node

        def fail_second(source_path: Path, target_path: Path) -> None:
            if target_path.name == "b.txt":
                raise isolation_module.WriterIsolationError("injected apply failure")
            original(source_path, target_path)

        with patch.object(isolation_module, "_copy_node", side_effect=fail_second):
            with self.assertRaises(isolation_module.WriterIsolationError):
                isolation_module.apply_journal(journal, self.root / "journal-state")
        isolation_module.rollback_journal(journal, self.root / "journal-state")
        self.assertEqual((canonical / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((canonical / "b.txt").read_text(encoding="utf-8"), "b0\n")

    def test_apply_copy_failures_remove_owned_partial_file_before_recovery(self) -> None:
        canonical = self.root / "deadline-canonical"
        source = self.root / "deadline-source"
        canonical.mkdir()
        source.mkdir()
        target = canonical / "a.bin"
        replacement = source / "a.bin"
        before = b"before\n"
        target.write_bytes(before)
        replacement.write_bytes(b"x" * (isolation_module._CHUNK_BYTES * 2 + 1))
        replacement_stat = replacement.stat()
        replacement_identity = (replacement_stat.st_dev, replacement_stat.st_ino)
        original_read = os.read

        def interrupt_during_replacement() -> object:
            matching_reads = 0

            def interrupted_read(descriptor: int, size: int) -> bytes:
                nonlocal matching_reads
                current = os.fstat(descriptor)
                if (
                    (current.st_dev, current.st_ino) == replacement_identity
                    and target.exists()
                    and target.stat().st_size != len(before)
                ):
                    matching_reads += 1
                    if matching_reads == 2:
                        raise KeyboardInterrupt("simulated process exit during copy")
                return original_read(descriptor, size)

            return interrupted_read

        def deadline_during_replacement() -> object:
            matching_reads = 0

            def expired_read(descriptor: int, size: int) -> bytes:
                nonlocal matching_reads
                current = os.fstat(descriptor)
                if (
                    (current.st_dev, current.st_ino) == replacement_identity
                    and target.exists()
                    and target.stat().st_size != len(before)
                ):
                    matching_reads += 1
                    if matching_reads == 2:
                        raise control_plane_module.OperationDeadlineExceeded(
                            "simulated copy deadline"
                        )
                return original_read(descriptor, size)

            return expired_read

        for failure, injected_read in (
            (KeyboardInterrupt, interrupt_during_replacement),
            (control_plane_module.OperationDeadlineExceeded, deadline_during_replacement),
        ):
            with self.subTest(failure=failure.__name__):
                target.write_bytes(before)
                journal = isolation_module.stage_apply_journal(
                    self.root / f"{failure.__name__}-state",
                    wave_id=(
                        "sha256:" + ("a" if failure is KeyboardInterrupt else "b") * 64
                    ),
                    canonical_root=canonical,
                    changes={"a.bin": {"source_root": str(source)}},
                )
                with patch.object(
                    isolation_module.os,
                    "read",
                    side_effect=injected_read(),
                ):
                    with self.assertRaises(failure):
                        isolation_module.apply_journal(
                            journal,
                            self.root / f"{failure.__name__}-state",
                        )

                self.assertEqual(journal["entries"][0]["phase"], "applying")
                self.assertFalse(target.exists())
                isolation_module.rollback_journal(
                    journal,
                    self.root / f"{failure.__name__}-state",
                )
                self.assertEqual(target.read_bytes(), before)

    def test_control_plane_apply_failure_fences_without_partial_success(self) -> None:
        repo = self.make_workspace()
        control = self.control("apply-failure")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        control.record_result(first_owner, self.result(first, "A01", "a.txt"))
        original = isolation_module._copy_node
        failed = False

        def fail_once(source_path: Path, target_path: Path) -> None:
            nonlocal failed
            if target_path.name == "b.txt" and not failed:
                failed = True
                raise isolation_module.WriterIsolationError("injected apply failure")
            original(source_path, target_path)

        with patch.object(isolation_module, "_copy_node", side_effect=fail_once):
            with self.assertRaisesRegex(ControlPlaneError, "cooperative apply rolled back"):
                control.record_result(second_owner, self.result(second, "A02", "b.txt"))
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(control.status()["counts"]["fenced"], 2)
        state = json.loads(control.state_path.read_text(encoding="utf-8"))
        backup = Path(state["cooperative_journal"]["backup_root"])
        roots = [Path(first["workspace_root"]), Path(second["workspace_root"])]
        self.assertTrue(backup.exists())
        self.assertTrue(all(root.exists() for root in roots))

        self.assertGreater(control.cleanup(), 0)

        self.assertFalse(backup.exists())
        self.assertTrue(all(not root.exists() for root in roots))

    def test_terminal_cleanup_persists_detachment_before_deleting_files(self) -> None:
        repo = self.make_workspace()
        control = self.control("cleanup-crash")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        control.record_result(first_owner, self.result(first, "A01", None))
        control.record_result(second_owner, self.result(second, "A02", None))
        roots = [Path(first["workspace_root"]), Path(second["workspace_root"])]

        with patch.object(
            control_plane_module,
            "cleanup_isolates",
            side_effect=KeyboardInterrupt("simulated process exit after state commit"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.cleanup()

        persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
        self.assertTrue(
            all(item.get("isolation") is None for item in persisted["dispatches"].values())
        )
        self.assertNotIn("cooperative_journal", persisted)
        self.assertTrue(all(root.exists() for root in roots))

        # A later cleanup derives these files as unreferenced and safely
        # completes deletion without reconstructing a second ownership ledger.
        self.assertGreater(control.cleanup(), 0)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_restart_recovers_an_interrupted_apply_journal(self) -> None:
        repo = self.make_workspace()
        control = self.control("recovery")
        actions = self.dispatch(control, repo)
        tasks = [parse_task_message(action["tool_input"]["message"]) for action in actions]
        (Path(tasks[0]["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(tasks[1]["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        with control._coordinated_state() as state:
            wave = control._read_wave(state)
            journal = isolation_module.stage_apply_journal(
                control.root,
                wave_id=wave["wave_id"],
                canonical_root=repo,
                changes={
                    "a.txt": {"source_root": tasks[0]["workspace_root"]},
                    "b.txt": {"source_root": tasks[1]["workspace_root"]},
                },
            )
            isolation_module.apply_journal(journal, control.root)
            journal["phase"] = "applying"
            state["cooperative_journal"] = journal
            control._write_state(state)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a1\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b1\n")

        self.assertGreaterEqual(control.restart(), 1)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(control.status()["counts"]["fenced"], 2)

    def test_current_cleanup_cleans_unreferenced_isolates_and_journals(self) -> None:
        state_root = self.root / "orphan-state"
        canonical = self.root / "orphan-canonical"
        source = self.root / "orphan-source"
        canonical.mkdir()
        source.mkdir()
        (canonical / "a.txt").write_text("a0\n", encoding="utf-8")
        (source / "a.txt").write_text("a1\n", encoding="utf-8")
        records = isolation_module.prepare_isolates(
            state_root,
            canonical,
            backend="directory",
            session_id="orphan-cleanup",
            batch_id="sha256:" + "d" * 64,
            members=[{"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]}],
        )
        journal = isolation_module.stage_apply_journal(
            state_root,
            wave_id="sha256:" + "e" * 64,
            canonical_root=canonical,
            changes={"a.txt": {"source_root": str(source)}},
        )
        isolate = Path(records[0]["isolate_root"])
        backup = Path(journal["backup_root"])
        self.assertTrue(isolate.exists())
        self.assertTrue(backup.exists())

        self.assertEqual(
            ControlPlane("orphan-cleanup", root=state_root)._cleanup_unused_cooperative_isolates(),
            2,
        )

        self.assertFalse(isolate.exists())
        self.assertFalse(backup.exists())

    def test_restart_recovers_an_incomplete_cooperative_apply(self) -> None:
        repo = self.make_workspace()
        control = self.control("mig")
        actions = self.dispatch(control, repo)
        tasks = [parse_task_message(action["tool_input"]["message"]) for action in actions]
        (Path(tasks[0]["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(tasks[1]["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        with control._coordinated_state() as state:
            wave = control._read_wave(state)
            journal = isolation_module.stage_apply_journal(
                control.root,
                wave_id=wave["wave_id"],
                canonical_root=repo,
                changes={
                    "a.txt": {"source_root": tasks[0]["workspace_root"]},
                    "b.txt": {"source_root": tasks[1]["workspace_root"]},
                },
            )
            isolation_module.apply_journal(journal, control.root)
            journal["phase"] = "applying"
            state["cooperative_journal"] = journal
            control._write_state(state)

        self.assertGreaterEqual(control.restart(), 1)
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(control.status()["counts"]["fenced"], 2)

    def test_cleanup_removes_successful_isolates_and_compiler_requires_opt_in(self) -> None:
        repo = self.make_workspace()
        control = self.control("cleanup")
        actions = self.dispatch(control, repo)
        tasks_and_owners = [self.start(control, action, index) for index, action in enumerate(actions)]
        for task, _owner in tasks_and_owners:
            name = "a.txt" if task["dispatch_id"] == tasks_and_owners[0][0]["dispatch_id"] else "b.txt"
            (Path(task["workspace_root"]) / name).write_text("changed\n", encoding="utf-8")
        control.record_result(tasks_and_owners[0][1], self.result(tasks_and_owners[0][0], "A01", "a.txt"))
        control.record_result(tasks_and_owners[1][1], self.result(tasks_and_owners[1][0], "A02", "b.txt"))
        roots = [Path(task["workspace_root"]) for task, _owner in tasks_and_owners]
        self.assertGreater(control.cleanup(), 0)
        self.assertTrue(all(not root.exists() for root in roots))

        request = {
            "authority": "delegated",
            "clarification_required": False,
            "closed": True,
            "declared_tools": [],
            "direct": False,
            "protocol": "cco.delegation.v1",
            "upper_bound_seconds": 60,
            "work": {"kind": "dag", "plan": self.plan()},
            "writer_isolation": "cooperative",
        }
        compiled = compile_delegation_request(request)
        self.assertEqual(compiled["plan"]["writer_isolation"], "cooperative")

    def test_cleanup_removes_no_delta_cooperative_isolates(self) -> None:
        repo = self.make_workspace()
        control = self.control("empty-cleanup")
        actions = self.dispatch(control, repo)
        tasks_and_owners = [self.start(control, action, index) for index, action in enumerate(actions)]
        control.record_result(
            tasks_and_owners[0][1],
            self.result(tasks_and_owners[0][0], "A01", None),
        )
        control.record_result(
            tasks_and_owners[1][1],
            self.result(tasks_and_owners[1][0], "A02", None),
        )
        roots = [Path(task["workspace_root"]) for task, _owner in tasks_and_owners]
        self.assertGreater(control.cleanup(), 0)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_cleanup_cleans_terminal_success_artifacts(self) -> None:
        repo = self.make_workspace()
        control = self.control("terminal")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        control.record_result(first_owner, self.result(first, "A01", "a.txt"))
        control.record_result(second_owner, self.result(second, "A02", "b.txt"))
        roots = [Path(first["workspace_root"]), Path(second["workspace_root"])]
        self.assertTrue(all(root.exists() for root in roots))

        self.assertGreater(control.cleanup(), 0)

        self.assertTrue(all(not root.exists() for root in roots))

    def test_preparation_reservation_blocks_competing_reader_and_writer(self) -> None:
        repo = self.make_workspace()
        state_root = self.root / "shared-state"
        control = ControlPlane("reservation-owner", root=state_root)
        control.create_plan(repo, self.plan())
        original = control_plane_module.prepare_isolates
        checked = False

        def interleave(*args: object, **kwargs: object) -> list[dict[str, object]]:
            nonlocal checked
            contender = ControlPlane("reservation-contender", root=state_root)
            scopes = [{"kind": "exact", "path": "a.txt"}]
            with control._coordinated_state() as state:
                self.assertIsNotNone(state.get("cooperative_preparing"))
            with self.assertRaisesRegex(ControlPlaneError, "preparation is already reserved"):
                contender._assert_cross_task_compatible(
                    repo,
                    role="worker",
                    scopes=scopes,
                )
            with self.assertRaisesRegex(ControlPlaneError, "preparation is already reserved"):
                contender._assert_cross_task_compatible(
                    repo,
                    role="explorer",
                    scopes=scopes,
                )
            checked = True
            return original(*args, **kwargs)

        with patch.object(control_plane_module, "prepare_isolates", side_effect=interleave):
            actions = control.next_wave(capacity=2, native_catalog=catalog())["dispatches"]
        self.assertTrue(checked)
        self.assertEqual(len(actions), 2)
        with control._coordinated_state() as state:
            self.assertNotIn("cooperative_preparing", state)

    def test_orphan_cleanup_cannot_delete_a_concurrently_prepared_batch(self) -> None:
        repo = self.make_workspace()
        state_root = self.root / "namespace-race-state"
        control = ControlPlane("namespace-race-owner", root=state_root)
        control.create_plan(repo, self.plan())
        original = control_plane_module.prepare_isolates
        preparation_entered = threading.Event()
        allow_preparation = threading.Event()
        cleanup_finished = threading.Event()
        prepared_roots: list[Path] = []
        errors: list[BaseException] = []

        def paused_prepare(*args: object, **kwargs: object) -> list[dict[str, object]]:
            preparation_entered.set()
            if not allow_preparation.wait(timeout=10):
                raise AssertionError("cleanup race barrier timed out")
            records = original(*args, **kwargs)
            prepared_roots.extend(Path(item["isolate_root"]) for item in records)
            return records

        def dispatch_wave() -> None:
            try:
                control.next_wave(capacity=2, native_catalog=catalog())
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        def cleanup_orphans() -> None:
            try:
                ControlPlane("namespace-race-cleaner", root=state_root)._cleanup_unused_cooperative_isolates()
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)
            finally:
                cleanup_finished.set()

        with patch.object(
            control_plane_module,
            "prepare_isolates",
            side_effect=paused_prepare,
        ):
            dispatch_thread = threading.Thread(target=dispatch_wave)
            dispatch_thread.start()
            self.assertTrue(preparation_entered.wait(timeout=10))
            cleanup_thread = threading.Thread(target=cleanup_orphans)
            cleanup_thread.start()
            self.assertFalse(cleanup_finished.wait(timeout=0.2))
            allow_preparation.set()
            dispatch_thread.join(timeout=20)
            cleanup_thread.join(timeout=20)

        self.assertFalse(dispatch_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(prepared_roots), 2)
        self.assertTrue(all(root.exists() for root in prepared_roots))

    def test_orphan_cleanup_cannot_delete_a_journal_before_publication(self) -> None:
        repo = self.make_workspace()
        state_root = self.root / "journal-race-state"
        control = ControlPlane("journal-race-owner", root=state_root)
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        control.record_result(first_owner, self.result(first, "A01", "a.txt"))

        original_stage = control_plane_module.stage_apply_journal
        journal_staged = threading.Event()
        allow_publication = threading.Event()
        cleanup_finished = threading.Event()
        backups: list[Path] = []
        removed: list[int] = []
        errors: list[BaseException] = []

        def paused_stage(*args: object, **kwargs: object) -> dict[str, object]:
            journal = original_stage(*args, **kwargs)
            backups.append(Path(journal["backup_root"]))
            journal_staged.set()
            if not allow_publication.wait(timeout=10):
                raise AssertionError("journal publication race barrier timed out")
            return journal

        def finish_wave() -> None:
            try:
                control.record_result(second_owner, self.result(second, "A02", "b.txt"))
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        def cleanup_orphans() -> None:
            try:
                removed.append(
                    ControlPlane(
                        "journal-race-cleaner", root=state_root
                    )._cleanup_unused_cooperative_isolates()
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)
            finally:
                cleanup_finished.set()

        with patch.object(
            control_plane_module,
            "stage_apply_journal",
            side_effect=paused_stage,
        ):
            result_thread = threading.Thread(target=finish_wave)
            result_thread.start()
            self.assertTrue(journal_staged.wait(timeout=10))
            cleanup_thread = threading.Thread(target=cleanup_orphans)
            cleanup_thread.start()
            self.assertFalse(cleanup_finished.wait(timeout=0.2))
            allow_publication.set()
            result_thread.join(timeout=20)
            cleanup_thread.join(timeout=20)

        self.assertFalse(result_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(removed, [0])
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].exists())
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a1\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b1\n")
        self.assertGreater(control.cleanup(), 0)
        self.assertFalse(backups[0].exists())

    def test_orphan_cleanup_skips_an_over_budget_liveness_scan(self) -> None:
        repo = self.make_workspace()
        state_root = self.root / "bounded-orphan-state"
        control = ControlPlane("bounded-orphan", root=state_root)
        control.create_plan(repo, self.plan())
        original_acquire = control_plane_module.acquire
        lock_identities: list[str] = []

        def traced_acquire(*args: object, **kwargs: object) -> object:
            lock_identities.append(str(args[1]))
            return original_acquire(*args, **kwargs)

        with (
            patch.object(
                control_plane_module,
                "MAX_ISOLATION_LIVENESS_SCAN_BYTES",
                1,
            ),
            patch.object(control_plane_module, "acquire", side_effect=traced_acquire),
            patch.object(
                control_plane_module, "cleanup_unused_isolate_batches"
            ) as isolate_cleanup,
            patch.object(
                control_plane_module, "cleanup_unused_journal_batches"
            ) as journal_cleanup,
        ):
            self.assertEqual(control._cleanup_unused_cooperative_isolates(), 0)

        self.assertEqual(
            lock_identities,
            [control_plane_module.ISOLATION_NAMESPACE_LOCK],
        )
        isolate_cleanup.assert_not_called()
        journal_cleanup.assert_not_called()

    def test_orphan_cleanup_keeps_files_when_hook_deadline_expires(self) -> None:
        control = ControlPlane("bounded-orphan-deadline", root=self.root / "deadline-state")
        with patch.object(
            control_plane_module,
            "_state_json_paths",
            side_effect=control_plane_module.OperationDeadlineExceeded(
                "injected deadline"
            ),
        ):
            self.assertEqual(control._cleanup_unused_cooperative_isolates(), 0)

    def test_cooperative_wave_has_one_union_baseline_without_state_mirrors(self) -> None:
        repo = self.make_workspace()
        control = self.control("union-baseline")
        actions = self.dispatch(control, repo)
        self.assertEqual(len(actions), 2)
        with control._coordinated_state() as state:
            wave = control._read_wave(state)
            self.assertNotIn("cooperative_isolates", state)
            self.assertNotIn("baselines", wave)
            self.assertNotIn("isolate_baselines", wave)
            self.assertNotIn("canonical_snapshot_id", wave)
            self.assertNotIn("isolate_snapshot_ids", wave)
            canonical = wave["canonical_baseline"]
            self.assertEqual(
                canonical["scopes"],
                [
                    {"kind": "exact", "path": "a.txt"},
                    {"kind": "exact", "path": "b.txt"},
                ],
            )
            self.assertEqual(
                {unit["baseline_id"] for unit in wave["units"]},
                {canonical["state_id"]},
            )
            self.assertEqual(set(wave["isolate_snapshots"]), {"left", "right"})

    def test_wave_digest_binds_each_persisted_isolate_snapshot_identity(self) -> None:
        repo = self.make_workspace()
        control = self.control("snapshot-digest")
        self.dispatch(control, repo)
        with control._coordinated_state() as state:
            wave = control._read_wave(state)
            isolate = Path(wave["isolate_snapshots"]["left"]["root"])
            (isolate / "a.txt").write_text("replacement\n", encoding="utf-8")
            replacement = control_plane_module.capture_workspace(
                isolate,
                scopes=[{"kind": "exact", "path": "a.txt"}],
                writable=True,
            )
            tampered = dict(wave)
            tampered["isolate_snapshots"] = dict(wave["isolate_snapshots"])
            tampered["isolate_snapshots"]["left"] = replacement
            control_plane_module._atomic_write(
                control._artifact_path("wave", wave["wave_id"]),
                tampered,
            )
            with self.assertRaisesRegex(ControlPlaneError, "wave artifact digest"):
                control._read_wave(state)

    def test_ready_isolate_snapshot_change_fences_the_whole_batch(self) -> None:
        repo = self.make_workspace()
        control = self.control("isolate-ready-cas")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        self.assertEqual(
            control.record_result(first_owner, self.result(first, "A01", "a.txt"))["state"],
            "ready_to_apply",
        )
        # This mutation happens after the persisted ready snapshot, not during
        # the child result check. The second result must not apply either delta.
        (Path(first["workspace_root"]) / "a.txt").write_text("tampered\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        with self.assertRaisesRegex(ControlPlaneError, "changed after its ready snapshot"):
            control.record_result(second_owner, self.result(second, "A02", "b.txt"))
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(control.status()["counts"]["fenced"], 2)

    def test_apply_rechecks_full_ready_snapshot_before_each_canonical_mutation(self) -> None:
        repo = self.make_workspace()
        control = self.control("apply-ready-cas")
        actions = self.dispatch(control, repo)
        first, first_owner = self.start(control, actions[0], 0)
        second, second_owner = self.start(control, actions[1], 1)
        (Path(first["workspace_root"]) / "a.txt").write_text("a1\n", encoding="utf-8")
        (Path(second["workspace_root"]) / "b.txt").write_text("b1\n", encoding="utf-8")
        control.record_result(first_owner, self.result(first, "A01", "a.txt"))
        original_apply = control_plane_module.apply_isolation_journal

        def mutate_after_stage(*args: object, **kwargs: object) -> None:
            # This path is not a staged source entry. Only a full persisted
            # ready-snapshot recheck can catch it at the mutation boundary.
            (Path(first["workspace_root"]) / "late.txt").write_text(
                "late\n", encoding="utf-8"
            )
            original_apply(*args, **kwargs)

        with patch.object(
            control_plane_module,
            "apply_isolation_journal",
            side_effect=mutate_after_stage,
        ):
            with self.assertRaisesRegex(ControlPlaneError, "cooperative apply rolled back"):
                control.record_result(second_owner, self.result(second, "A02", "b.txt"))
        self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "b0\n")

    def test_namespace_and_unowned_state_source_are_rejected(self) -> None:
        canonical = self.root / "canonical-namespace"
        canonical.mkdir()
        (canonical / "a.txt").write_text("a0\n", encoding="utf-8")
        state_root = self.root / "unmarked-state"
        (state_root / "isolates").mkdir(parents=True)
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "unmarked"):
            isolation_module.prepare_isolates(
                state_root,
                canonical,
                backend="directory",
                session_id="namespace",
                batch_id="sha256:" + "1" * 64,
                members=[{"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]}],
            )
        self.assertFalse((state_root / "isolates" / ".cco-writer-isolation-owned-v1").exists())
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "inside, above"):
            isolation_module.prepare_isolates(
                canonical / "state",
                canonical,
                backend="directory",
                session_id="inside",
                batch_id="sha256:" + "2" * 64,
                members=[{"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]}],
            )

        source_state = self.root / "source-state"
        source_state.mkdir()
        (source_state / "a.txt").write_text("external\n", encoding="utf-8")
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "unowned source"):
            isolation_module.stage_apply_journal(
                source_state,
                wave_id="sha256:" + "3" * 64,
                canonical_root=canonical,
                changes={"a.txt": {"source_root": str(source_state)}},
            )

    def test_reparse_namespace_is_rejected_before_materialization(self) -> None:
        canonical = self.root / "reparse-canonical"
        canonical.mkdir()
        (canonical / "a.txt").write_text("a0\n", encoding="utf-8")
        state_root = self.root / "reparse-state"
        namespace = state_root / "isolates"
        namespace.mkdir(parents=True)
        original_lstat = isolation_module._lstat
        reparse = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        def reparse_namespace(
            path: Path, label: str, *, missing: bool = False
        ) -> os.stat_result | None:
            if Path(path) == namespace:
                return reparse
            return original_lstat(path, label, missing=missing)

        with patch.object(isolation_module, "_lstat", side_effect=reparse_namespace):
            with self.assertRaisesRegex(isolation_module.WriterIsolationError, "reparse"):
                isolation_module.prepare_isolates(
                    state_root,
                    canonical,
                    backend="directory",
                    session_id="reparse",
                    batch_id="sha256:" + "a" * 64,
                    members=[{"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]}],
                )

    def test_bounded_copy_rejects_growth_reparse_swap_and_high_cardinality(self) -> None:
        source = self.root / "growth-source"
        target = self.root / "growth-target"
        source.mkdir()
        source_file = source / "a.txt"
        source_file.write_text("initial\n", encoding="utf-8")
        original_copy = isolation_module._copy_file

        def copy_then_grow(
            source_path: Path, target_path: Path, **kwargs: object
        ) -> int:
            copied = original_copy(source_path, target_path, **kwargs)
            source_file.write_text("grown\n", encoding="utf-8")
            return copied

        with patch.object(isolation_module, "_copy_file", side_effect=copy_then_grow):
            with self.assertRaisesRegex(isolation_module.WriterIsolationError, "changed"):
                isolation_module._copy_tree(source, target)
        self.assertFalse(target.exists())

        # A reparse replacement after the copy is rejected by the final stable
        # source walk. Windows installations without symlink privilege skip only
        # this host capability check; the bounded growth case above still runs.
        source_file.write_text("initial\n", encoding="utf-8")
        external = self.root / "outside.txt"
        external.write_text("outside\n", encoding="utf-8")
        try:
            probe = self.root / "symlink-probe"
            os.symlink(external, probe)
            probe.unlink()
        except OSError:
            reparse_available = False
        else:
            reparse_available = True
        if reparse_available:
            swapped_target = self.root / "reparse-target"

            def copy_then_swap(
                source_path: Path, target_path: Path, **kwargs: object
            ) -> int:
                copied = original_copy(source_path, target_path, **kwargs)
                source_file.unlink()
                os.symlink(external, source_file)
                return copied

            with patch.object(isolation_module, "_copy_file", side_effect=copy_then_swap):
                with self.assertRaisesRegex(isolation_module.WriterIsolationError, "reparse"):
                    isolation_module._copy_tree(source, swapped_target)
            self.assertFalse(swapped_target.exists())
            source_file.unlink()

        crowded = self.root / "crowded-source"
        crowded.mkdir()
        for index in range(isolation_module.MAX_FILES + 1):
            (crowded / f"f{index:04d}.txt").touch()
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "file capacity"):
            isolation_module.prepare_isolates(
                self.root / "crowded-state",
                crowded,
                backend="directory",
                session_id="crowded",
                batch_id="sha256:" + "4" * 64,
                members=[{"id": "left", "scopes": [{"kind": "exact", "path": "f0000.txt"}]}],
            )

    def test_apply_cas_refuses_to_overwrite_external_canonical_change(self) -> None:
        canonical = self.root / "cas-canonical"
        source = self.root / "cas-source"
        canonical.mkdir()
        source.mkdir()
        (canonical / "a.txt").write_text("before\n", encoding="utf-8")
        (source / "a.txt").write_text("after\n", encoding="utf-8")
        journal = isolation_module.stage_apply_journal(
            self.root / "cas-state",
            wave_id="sha256:" + "5" * 64,
            canonical_root=canonical,
            changes={"a.txt": {"source_root": str(source)}},
        )
        (canonical / "a.txt").write_text("external\n", encoding="utf-8")
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "canonical content changed"):
            isolation_module.apply_journal(journal, self.root / "cas-state")
        self.assertEqual((canonical / "a.txt").read_text(encoding="utf-8"), "external\n")

    def test_apply_cas_rechecks_after_ready_snapshot_verification(self) -> None:
        canonical = self.root / "late-cas-canonical"
        source = self.root / "late-cas-source"
        canonical.mkdir()
        source.mkdir()
        (canonical / "a.txt").write_text("before\n", encoding="utf-8")
        (source / "a.txt").write_text("after\n", encoding="utf-8")
        journal = isolation_module.stage_apply_journal(
            self.root / "late-cas-state",
            wave_id="sha256:" + "9" * 64,
            canonical_root=canonical,
            changes={"a.txt": {"source_root": str(source)}},
        )

        def mutate_during_ready_proof(*args: object, **kwargs: object) -> None:
            (canonical / "a.txt").write_text("external\n", encoding="utf-8")

        with patch.object(
            isolation_module,
            "_verify_ready_isolates",
            side_effect=mutate_during_ready_proof,
        ):
            with self.assertRaisesRegex(isolation_module.WriterIsolationError, "canonical content changed"):
                isolation_module.apply_journal(
                    journal,
                    self.root / "late-cas-state",
                    ready_isolates=[{}],
                )
        self.assertEqual((canonical / "a.txt").read_text(encoding="utf-8"), "external\n")

    def test_apply_cas_rejects_unstaged_canonical_scope_drift(self) -> None:
        canonical = self.root / "scope-cas-canonical"
        source = self.root / "scope-cas-source"
        (canonical / "owned").mkdir(parents=True)
        (source / "owned").mkdir(parents=True)
        (canonical / "owned" / "a.txt").write_text("before\n", encoding="utf-8")
        (canonical / "owned" / "untouched.txt").write_text("before\n", encoding="utf-8")
        (source / "owned" / "a.txt").write_text("after\n", encoding="utf-8")
        scopes = [{"kind": "prefix", "path": "owned"}]
        identity = isolation_module.scoped_content_identity(canonical, scopes)
        journal = isolation_module.stage_apply_journal(
            self.root / "scope-cas-state",
            wave_id="sha256:" + "b" * 64,
            canonical_root=canonical,
            changes={"owned/a.txt": {"source_root": str(source)}},
        )
        (canonical / "owned" / "untouched.txt").write_text(
            "external\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "workspace changed"):
            isolation_module.apply_journal(
                journal,
                self.root / "scope-cas-state",
                canonical_identity=identity,
                canonical_scopes=scopes,
            )
        self.assertEqual(
            (canonical / "owned" / "a.txt").read_text(encoding="utf-8"),
            "before\n",
        )
        self.assertEqual(
            (canonical / "owned" / "untouched.txt").read_text(encoding="utf-8"),
            "external\n",
        )

    def test_orphan_worktree_cleanup_removes_git_registry_before_tree(self) -> None:
        canonical = self.make_workspace()
        state_root = self.root / "orphan-worktree-state"
        records = isolation_module.prepare_isolates(
            state_root,
            canonical,
            backend="git",
            session_id="orphan-worktree",
            batch_id="sha256:" + "6" * 64,
            members=[{"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]}],
        )
        isolate = Path(records[0]["isolate_root"])
        self.assertTrue((isolate / ".git").is_file())
        before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=canonical,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(
            sum(line.startswith("worktree ") for line in before.splitlines()),
            2,
        )
        self.assertEqual(
            isolation_module.cleanup_unused_isolate_batches(
                state_root,
                [],
            ),
            1,
        )
        self.assertFalse(isolate.exists())
        after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=canonical,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(
            sum(line.startswith("worktree ") for line in after.splitlines()),
            1,
        )

    def test_partial_preparation_cleanup_and_restart_reservation_recovery(self) -> None:
        canonical = self.make_workspace()
        state_root = self.root / "partial-preparation-state"
        original = isolation_module._copy_tree
        calls = 0

        def fail_second_copy(source_path: Path, target_path: Path, **kwargs: object) -> tuple[int, int]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise isolation_module.WriterIsolationError("injected copy failure")
            return original(source_path, target_path, **kwargs)

        # A dirty canonical workspace selects the copy backend deterministically.
        (canonical / "a.txt").write_text("dirty\n", encoding="utf-8")
        batch_id = "sha256:" + "7" * 64
        with patch.object(isolation_module, "_copy_tree", side_effect=fail_second_copy):
            with self.assertRaisesRegex(isolation_module.WriterIsolationError, "injected"):
                isolation_module.prepare_isolates(
                    state_root,
                    canonical,
                    backend="git",
                    session_id="partial-preparation",
                    batch_id=batch_id,
                    members=[
                        {"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]},
                        {"id": "right", "scopes": [{"kind": "exact", "path": "b.txt"}]},
                    ],
                )
        roots = isolation_module.preparing_isolate_roots(
            state_root,
            canonical_root=canonical,
            session_id="partial-preparation",
            batch_id=batch_id,
            count=2,
        )
        self.assertTrue(all(not Path(root).exists() for root in roots))

        control = ControlPlane("restart-preparation", root=state_root)
        control.create_plan(canonical, self.plan())
        reservation_batch = "sha256:" + "8" * 64
        records = isolation_module.prepare_isolates(
            state_root,
            canonical,
            backend="git",
            session_id=control.session_id,
            batch_id=reservation_batch,
            members=[
                {"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]},
                {"id": "right", "scopes": [{"kind": "exact", "path": "b.txt"}]},
            ],
        )
        with control._coordinated_state() as state:
            state["cooperative_preparing"] = {
                "batch_id": reservation_batch,
                "members": [
                    {"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]},
                    {"id": "right", "scopes": [{"kind": "exact", "path": "b.txt"}]},
                ],
                "plan_id": state["plan_id"],
            }
            control._write_state(state)
        self.assertTrue(all(Path(record["isolate_root"]).exists() for record in records))
        self.assertGreaterEqual(control.restart(), 1)
        self.assertTrue(all(not Path(record["isolate_root"]).exists() for record in records))
        with control._coordinated_state() as state:
            self.assertNotIn("cooperative_preparing", state)

    def test_failed_preparation_without_returned_records_uses_reservation_cleanup(self) -> None:
        canonical = self.make_workspace()
        control = self.control("lost-preparation-records")
        control.create_plan(canonical, self.plan())
        original = control_plane_module.prepare_isolates
        roots: list[Path] = []

        def materialize_then_lose_records(
            *args: object, **kwargs: object
        ) -> list[dict[str, object]]:
            records = original(*args, **kwargs)
            roots.extend(Path(record["isolate_root"]) for record in records)
            raise isolation_module.WriterIsolationError("injected lost isolate records")

        with patch.object(
            control_plane_module,
            "prepare_isolates",
            side_effect=materialize_then_lose_records,
        ):
            with self.assertRaisesRegex(ControlPlaneUnavailable, "lost isolate records"):
                control.next_wave(capacity=2, native_catalog=catalog())
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(not root.exists() for root in roots))
        with control._coordinated_state() as state:
            self.assertNotIn("cooperative_preparing", state)

    def test_git_isolation_clears_routing_overrides_and_bounds_active_deadlines(self) -> None:
        canonical = self.make_workspace()
        redirected = self.make_workspace("redirected")
        with patch.dict(
            os.environ,
            {
                "GIT_COMMON_DIR": str(redirected / ".git"),
                "GIT_DIR": str(redirected / ".git"),
                "GIT_INDEX_FILE": str(redirected / ".git" / "index"),
                "GIT_WORK_TREE": str(redirected),
            },
        ):
            result = isolation_module._git(canonical, "rev-parse", "--show-toplevel")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.decode().strip()), canonical.resolve())

        completed = subprocess.CompletedProcess(["git"], 0, stdout=b"", stderr=b"")
        with (
            deadline_after(5),
            patch.object(isolation_module.subprocess, "run", return_value=completed) as run,
        ):
            isolation_module._git(canonical, "status")
        kwargs = run.call_args.kwargs
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 5)
        self.assertNotIn("GIT_DIR", kwargs["env"])
        self.assertNotIn("GIT_WORK_TREE", kwargs["env"])

        with (
            patch.object(isolation_module, "MAX_GIT_OUTPUT_BYTES", 1),
            self.assertRaisesRegex(
                isolation_module.WriterIsolationUnavailable,
                "output capacity",
            ),
        ):
            isolation_module._git(canonical, "rev-parse", "--show-toplevel")

    def test_three_writer_group_is_bounded_and_uses_distinct_slots(self) -> None:
        canonical = self.make_workspace("directory")
        (canonical / "c.txt").write_text("c0\n", encoding="utf-8")
        state_root = self.root / "three-writer-state"
        members = [
            {"id": "left", "scopes": [{"kind": "exact", "path": "a.txt"}]},
            {"id": "middle", "scopes": [{"kind": "exact", "path": "b.txt"}]},
            {"id": "right", "scopes": [{"kind": "exact", "path": "c.txt"}]},
        ]

        records = isolation_module.prepare_isolates(
            state_root,
            canonical,
            backend="directory",
            session_id="three-writers",
            batch_id="sha256:" + "f" * 64,
            members=members,
        )

        self.assertEqual(len(records), 3)
        self.assertEqual(
            [Path(record["isolate_root"]).name for record in records],
            ["n00", "n01", "n02"],
        )
        self.assertTrue(all(Path(record["isolate_root"]).is_dir() for record in records))
        self.assertEqual(isolation_module.cleanup_isolates(state_root, records), 3)

        # The group budget is not multiplied by its member count.
        with patch.object(isolation_module, "MAX_GROUP_FILES", 8):
            with self.assertRaisesRegex(
                isolation_module.WriterIsolationError,
                "aggregate copy capacity",
            ):
                isolation_module.prepare_isolates(
                    self.root / "three-writer-over-budget",
                    canonical,
                    backend="directory",
                    session_id="three-writers-over-budget",
                    batch_id="sha256:" + "0" * 64,
                    members=members,
                )
        with patch.object(isolation_module, "MAX_GROUP_BYTES", 8):
            with self.assertRaisesRegex(
                isolation_module.WriterIsolationError,
                "aggregate copy capacity",
            ):
                isolation_module.prepare_isolates(
                    self.root / "three-writer-byte-over-budget",
                    canonical,
                    backend="directory",
                    session_id="three-writers-byte-over-budget",
                    batch_id="sha256:" + "3" * 64,
                    members=members,
                )

    def test_preparation_baseexception_reclaims_only_owned_copy_tree(self) -> None:
        canonical = self.make_workspace("directory")
        state_root = self.root / "preparation-baseexception-state"
        batch_id = "sha256:" + "1" * 64
        original_copy_tree = isolation_module._copy_tree

        def copy_then_exit(
            source_path: Path, target_path: Path, **kwargs: object
        ) -> tuple[int, int]:
            original_copy_tree(source_path, target_path, **kwargs)
            raise KeyboardInterrupt("simulated preparation exit")

        with patch.object(
            isolation_module,
            "_copy_tree",
            side_effect=copy_then_exit,
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "preparation exit"):
                isolation_module.prepare_isolates(
                    state_root,
                    canonical,
                    backend="directory",
                    session_id="preparation-baseexception",
                    batch_id=batch_id,
                    members=[
                        {
                            "id": "writer",
                            "scopes": [{"kind": "exact", "path": "a.txt"}],
                        }
                    ],
                )

        roots = isolation_module.preparing_isolate_roots(
            state_root,
            canonical_root=canonical,
            session_id="preparation-baseexception",
            batch_id=batch_id,
            count=1,
        )
        self.assertFalse(Path(roots[0]).exists())

    def test_backup_staging_baseexception_preserves_external_replacement(self) -> None:
        canonical = self.root / "backup-canonical"
        source = self.root / "backup-source"
        canonical.mkdir()
        source.mkdir()
        for root, suffix in ((canonical, "0"), (source, "1")):
            (root / "a.txt").write_text(f"a{suffix}\n", encoding="utf-8")
            (root / "b.txt").write_text(f"b{suffix}\n", encoding="utf-8")
        state_root = self.root / "backup-baseexception-state"
        original_copy_node = isolation_module._copy_node

        def copy_then_replace(source_path: Path, target_path: Path) -> None:
            original_copy_node(source_path, target_path)
            if target_path.name == "b0001":
                target_path.write_text("external replacement\n", encoding="utf-8")
                raise SystemExit("simulated staging exit")

        with patch.object(
            isolation_module,
            "_copy_node",
            side_effect=copy_then_replace,
        ):
            with self.assertRaisesRegex(SystemExit, "staging exit"):
                isolation_module.stage_apply_journal(
                    state_root,
                    wave_id="sha256:" + "2" * 64,
                    canonical_root=canonical,
                    changes={
                        "a.txt": {"source_root": str(source)},
                        "b.txt": {"source_root": str(source)},
                    },
                )

        backups = list((state_root / "isolate-journals").rglob("b000*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].name, "b0001")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "external replacement\n")

    def test_journal_file_budget_is_aggregate_across_all_writer_changes(self) -> None:
        canonical = self.root / "journal-file-canonical"
        source = self.root / "journal-file-source"
        canonical.mkdir()
        source.mkdir()
        for root, suffix in ((canonical, "0"), (source, "1")):
            (root / "a.txt").write_text(f"a{suffix}\n", encoding="utf-8")
            (root / "b.txt").write_text(f"b{suffix}\n", encoding="utf-8")

        with patch.object(isolation_module, "MAX_JOURNAL_FILES", 3):
            with self.assertRaisesRegex(
                isolation_module.WriterIsolationError,
                "journal exceeds its file capacity",
            ):
                isolation_module.stage_apply_journal(
                    self.root / "journal-file-state",
                    wave_id="sha256:" + "4" * 64,
                    canonical_root=canonical,
                    changes={
                        "a.txt": {"source_root": str(source)},
                        "b.txt": {"source_root": str(source)},
                    },
                )

    def test_copy_failure_preserves_an_in_place_external_edit(self) -> None:
        source = self.root / "copy-race-source.bin"
        target = self.root / "copy-race-target.bin"
        source.write_bytes(b"x" * (isolation_module._CHUNK_BYTES * 2 + 1))
        source_stat = source.stat()
        original_read = os.read
        external = b"external in-place edit\n"

        def edit_then_interrupt(descriptor: int, size: int) -> bytes:
            current = os.fstat(descriptor)
            if (
                (current.st_dev, current.st_ino)
                == (source_stat.st_dev, source_stat.st_ino)
                and target.exists()
                and target.stat().st_size > 0
            ):
                target.write_bytes(external)
                raise KeyboardInterrupt("simulated external in-place edit")
            return original_read(descriptor, size)

        with patch.object(isolation_module.os, "read", side_effect=edit_then_interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "in-place edit"):
                isolation_module._copy_file(source, target)

        self.assertEqual(target.read_bytes(), external)

    def test_copy_permissions_are_applied_through_the_open_descriptor(self) -> None:
        source = self.root / "permission-source.txt"
        target = self.root / "permission-target.txt"
        source.write_text("content\n", encoding="utf-8")

        with patch.object(
            isolation_module.os,
            "chmod",
            side_effect=AssertionError("pathname chmod must not be used"),
        ):
            self.assertEqual(
                isolation_module._copy_file(source, target),
                len(source.read_bytes()),
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "content\n")

    def test_identity_and_empty_scope_apis_fail_before_isolate_materialization(self) -> None:
        canonical = self.make_workspace("directory")
        state_root = self.root / "empty-scope-state"
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "member scopes"):
            isolation_module.prepare_isolates(
                state_root,
                canonical,
                backend="directory",
                session_id="empty-scope",
                batch_id="sha256:" + "e" * 64,
                members=[{"id": "writer", "scopes": []}],
            )
        self.assertFalse(state_root.exists())
        with self.assertRaisesRegex(isolation_module.WriterIsolationError, "ambiguous"):
            isolation_module.scoped_content_identity(
                canonical,
                [
                    {"kind": "exact", "path": "a.txt"},
                    {"kind": "exact", "path": "a.txt"},
                ],
            )


if __name__ == "__main__":
    unittest.main()

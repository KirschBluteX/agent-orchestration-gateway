import json
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "codex-cost-orchestrator"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import workspace_state as workspace_state_module  # noqa: E402
from workspace_state import StateUnavailable  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
STATE_TOOL = (
    REPO / "plugins" / "codex-cost-orchestrator" / "scripts" / "workspace_state.py"
)


class WorkspaceStateBehaviorTests(unittest.TestCase):
    def test_git_output_is_rejected_before_it_exceeds_the_memory_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            with (
                mock.patch.object(
                    workspace_state_module,
                    "MAX_GIT_OUTPUT_BYTES",
                    4,
                ),
                self.assertRaisesRegex(StateUnavailable, "output byte limit"),
            ):
                workspace_state_module.git(repo, "rev-parse", "--show-toplevel")

    def test_git_subprocess_is_terminated_and_reaped_on_process_level_exit(self) -> None:
        repo = Path("unused")
        for exit_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exit_type=exit_type.__name__):
                process = mock.Mock()
                process.poll.return_value = None
                process.wait.side_effect = [exit_type("simulated exit"), None]
                with mock.patch.object(
                    workspace_state_module.subprocess,
                    "Popen",
                    return_value=process,
                ):
                    with self.assertRaises(exit_type):
                        workspace_state_module._run_git(repo, "status", "--porcelain=v1")
                process.kill.assert_called_once_with()
                self.assertEqual(process.wait.call_count, 2)

    def test_git_subprocess_is_terminated_and_reaped_on_deadline(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["git"], 0.01),
            None,
        ]
        expired = workspace_state_module.OperationDeadlineExceeded("simulated deadline")
        with (
            mock.patch.object(
                workspace_state_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(workspace_state_module, "checkpoint"),
            mock.patch.object(
                workspace_state_module,
                "remaining_seconds",
                side_effect=[0.01, expired],
            ),
            self.assertRaisesRegex(
                workspace_state_module.OperationDeadlineExceeded,
                "workspace inspection exceeded",
            ),
        ):
            workspace_state_module._run_git(Path("unused"), "status")
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_git_record_count_is_bounded_before_records_are_materialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            with (
                mock.patch.object(
                    workspace_state_module,
                    "MAX_GIT_RECORDS",
                    1,
                ),
                self.assertRaisesRegex(StateUnavailable, "record limit"),
            ):
                workspace_state_module.repository_index_records(repo)

    def test_git_control_directory_entry_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one").write_text("1", encoding="utf-8")
            (root / "two").write_text("2", encoding="utf-8")
            with (
                mock.patch.object(
                    workspace_state_module,
                    "MAX_GIT_CONTROL_ENTRIES",
                    1,
                ),
                self.assertRaisesRegex(StateUnavailable, "control entry limit"),
            ):
                workspace_state_module.directory_digest(root)

    def test_git_control_reparse_targets_share_the_outer_entry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            scanned = base / "scanned"
            target = base / "target"
            scanned.mkdir()
            target.mkdir()
            (target / "nested").write_text("bounded", encoding="utf-8")
            link = scanned / "linked"
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
            else:
                link.symlink_to(target, target_is_directory=True)

            with (
                mock.patch.object(
                    workspace_state_module,
                    "MAX_GIT_CONTROL_ENTRIES",
                    1,
                ),
                self.assertRaisesRegex(StateUnavailable, "control entry limit"),
            ):
                workspace_state_module.directory_digest(scanned)

    def test_prefix_scope_reparse_checks_share_one_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            (repo / "other").mkdir()
            (repo / "other" / "entry.txt").write_text("entry\n", encoding="utf-8")
            with (
                mock.patch.object(
                    workspace_state_module,
                    "MAX_SCOPE_REPARSE_ENTRIES",
                    1,
                ),
                self.assertRaisesRegex(StateUnavailable, "reparse entry limit"),
            ):
                budget = workspace_state_module.ScopeReparseBudget()
                workspace_state_module.normalize_allow(
                    repo,
                    "prefix:src",
                    reparse_budget=budget,
                )
                workspace_state_module.normalize_allow(
                    repo,
                    "prefix:other",
                    reparse_budget=budget,
                )

    def test_complete_git_control_inspection_uses_one_shared_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            observed: list[object | None] = []

            def admin_digest(
                _root: Path,
                *,
                _budget: object | None = None,
            ) -> str:
                observed.append(_budget)
                return "admin"

            def entry_digest(
                _path: Path,
                *,
                _budget: object | None = None,
            ) -> str:
                observed.append(_budget)
                return "entry"

            with (
                mock.patch.object(
                    workspace_state_module,
                    "git_admin_digest",
                    side_effect=admin_digest,
                ),
                mock.patch.object(
                    workspace_state_module,
                    "control_entry_digest",
                    side_effect=entry_digest,
                ),
            ):
                workspace_state_module.state_payload(repo)

            self.assertEqual(len(observed), 3)
            self.assertIsNotNone(observed[0])
            self.assertTrue(all(item is observed[0] for item in observed))

    def test_head_oid_rejects_an_unexpected_git_failure(self) -> None:
        failed = subprocess.CompletedProcess(
            ["git", "rev-parse"],
            128,
            stdout=b"",
            stderr=b"fatal: temporary failure",
        )
        with (
            mock.patch.object(workspace_state_module, "_run_git", return_value=failed),
            self.assertRaises(StateUnavailable),
        ):
            workspace_state_module.head_oid(Path("unused"))

    def test_head_oid_accepts_only_an_explicit_unborn_symbolic_ref(self) -> None:
        results = [
            subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(
                ["git"], 0, stdout=b"refs/heads/main\n", stderr=b""
            ),
            subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b""),
        ]
        with mock.patch.object(
            workspace_state_module,
            "_run_git",
            side_effect=results,
        ):
            self.assertIsNone(workspace_state_module.head_oid(Path("unused")))

    def test_head_oid_accepts_a_real_unborn_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "unborn"
            repo.mkdir()
            self.assertEqual(self.git(repo, "init").returncode, 0)

            self.assertIsNone(workspace_state_module.head_oid(repo))

    def test_git_environment_cannot_redirect_an_explicit_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            redirected_root = root / "redirected-root"
            redirected_root.mkdir()
            redirected = self.make_repo(redirected_root)
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_COMMON_DIR": str(redirected / ".git"),
                    "GIT_DIR": str(redirected / ".git"),
                    "GIT_INDEX_FILE": str(redirected / ".git" / "index"),
                    "GIT_WORK_TREE": str(redirected),
                },
            ):
                self.assertEqual(
                    workspace_state_module.repository_root(repo), repo.resolve()
                )

    def test_empty_snapshot_scopes_and_malformed_identities_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            with self.assertRaisesRegex(workspace_state_module.StateError, "non-empty"):
                workspace_state_module.state_payload(repo, scopes=[])
            with self.assertRaisesRegex(workspace_state_module.StateError, "non-empty"):
                workspace_state_module.ignored_entries(
                    repo,
                    max_files=1,
                    max_bytes=1,
                    scopes=[],
                )

            snapshot = workspace_state_module.state_payload(repo)
            snapshot["repo_identity"] = {"device": True, "inode": 1}
            unsigned = {key: value for key, value in snapshot.items() if key != "state_id"}
            snapshot["state_id"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaisesRegex(workspace_state_module.StateError, "identity"):
                workspace_state_module.validate_snapshot(snapshot)

    def git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        self.assertEqual(self.git(repo, "init").returncode, 0)
        (repo / "src").mkdir()
        (repo / "src" / "owned.txt").write_text("owned baseline\n", encoding="utf-8")
        (repo / "notes.txt").write_text("notes baseline\n", encoding="utf-8")
        self.assertEqual(self.git(repo, "add", ".").returncode, 0)
        commit = self.git(
            repo,
            "-c",
            "user.name=CCO Tests",
            "-c",
            "user.email=cco-tests@example.invalid",
            "commit",
            "-m",
            "initial",
        )
        self.assertEqual(commit.returncode, 0, commit.stderr)
        return repo

    def test_strict_mode_observes_an_ignored_file_outside_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
            (repo / "secret.txt").write_text("baseline\n", encoding="utf-8")
            baseline = root / "baseline.json"

            capture = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--mode",
                    "strict",
                    "--output",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            (repo / "secret.txt").write_text("changed\n", encoding="utf-8")

            checked = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(checked.returncode, 1, checked.stderr)
            result = json.loads(checked.stdout)
            self.assertEqual(result["changed_paths"], ["secret.txt"])
            self.assertEqual(result["violations"], ["outside_lease:secret.txt"])

    def test_scoped_reader_policy_uses_literal_pathspecs_and_scoped_budgets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            reader = repo / "reader"
            generated = repo / "generated"
            reader.mkdir()
            generated.mkdir()
            (repo / ".gitignore").write_text(
                "reader/*.cache\ngenerated/\n",
                encoding="utf-8",
            )
            (reader / "snapshot.cache").write_text("reader\n", encoding="utf-8")
            (generated / "dependency.cache").write_text(
                "outside\n",
                encoding="utf-8",
            )
            self.assertEqual(self.git(repo, "add", ".gitignore").returncode, 0)
            committed = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-m",
                "ignore generated trees",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            scopes = [{"kind": "prefix", "path": "reader"}]
            original_git = workspace_state_module.git
            ignored_calls: list[tuple[str, ...]] = []

            def observed_git(root_path: Path, *args: str) -> bytes:
                if args[:1] == ("ls-files",) and "--ignored" in args:
                    ignored_calls.append(args)
                return original_git(root_path, *args)

            with mock.patch.object(
                workspace_state_module,
                "git",
                side_effect=observed_git,
            ):
                snapshot = workspace_state_module.state_payload(
                    repo,
                    ignored_policy=(
                        workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1
                    ),
                    scopes=scopes,
                )

            self.assertEqual(
                snapshot["ignored_policy"],
                workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1,
            )
            self.assertEqual(
                snapshot["ignored_scope_digest"],
                workspace_state_module.ignored_scope_digest(scopes),
            )
            self.assertIn("reader/snapshot.cache", snapshot["entries"])
            self.assertIn(
                "sha256",
                snapshot["entries"]["reader/snapshot.cache"]["ignored"]["fingerprint"],
            )
            self.assertNotIn("generated/dependency.cache", snapshot["entries"])
            self.assertEqual(
                ignored_calls,
                [
                    (
                        "ls-files",
                        "--others",
                        "--ignored",
                        "--exclude-standard",
                        "-z",
                        "--",
                        ":(top,literal)reader",
                    )
                ],
            )

            with self.assertRaisesRegex(
                workspace_state_module.StateError,
                "ignored scan exceeds the 0 file limit",
            ):
                workspace_state_module.state_payload(
                    repo,
                    ignored_max_files=0,
                    ignored_policy=(
                        workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1
                    ),
                    scopes=scopes,
                )
            with self.assertRaisesRegex(
                workspace_state_module.StateError,
                "ignored scan exceeds the 1 byte limit",
            ):
                workspace_state_module.state_payload(
                    repo,
                    ignored_max_bytes=1,
                    ignored_policy=(
                        workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1
                    ),
                    scopes=scopes,
                )

    def test_scoped_reader_policy_keeps_hidden_tracked_paths_authoritative(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / "reader").mkdir()
            scopes = [{"kind": "prefix", "path": "reader"}]
            marked = self.git(
                repo,
                "update-index",
                "--assume-unchanged",
                "notes.txt",
            )
            self.assertEqual(marked.returncode, 0, marked.stderr)
            baseline = workspace_state_module.state_payload(
                repo,
                ignored_policy=workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1,
                scopes=scopes,
            )
            (repo / "notes.txt").write_text("hidden change\n", encoding="utf-8")
            (repo / "src" / "owned.txt").write_text(
                "ordinary outside change\n",
                encoding="utf-8",
            )

            code, result, _current = workspace_state_module.verify(
                repo,
                baseline,
                [],
                entry_scopes=scopes,
            )

            self.assertEqual(code, 1)
            self.assertEqual(result["changed_paths"], ["notes.txt", "src/owned.txt"])
            self.assertEqual(
                result["violations"],
                ["outside_lease:notes.txt", "outside_lease:src/owned.txt"],
            )

    def test_scoped_reader_policy_binds_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            reader = repo / "reader"
            generated = repo / "generated"
            reader.mkdir()
            generated.mkdir()
            (repo / ".gitignore").write_text(
                "reader/*.cache\ngenerated/\n",
                encoding="utf-8",
            )
            (reader / "snapshot.cache").write_text("reader\n", encoding="utf-8")
            (generated / "dependency.cache").write_text(
                "outside\n",
                encoding="utf-8",
            )
            self.assertEqual(self.git(repo, "add", ".gitignore").returncode, 0)
            committed = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-m",
                "ignore generated trees",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            scopes = [{"kind": "prefix", "path": "reader"}]
            scoped = workspace_state_module.state_payload(
                repo,
                ignored_policy=workspace_state_module.IGNORED_POLICY_SCOPED_READER_V1,
                scopes=scopes,
            )
            with self.assertRaisesRegex(
                workspace_state_module.StateError,
                "reader scopes do not match",
            ):
                workspace_state_module.verify(
                    repo,
                    scoped,
                    [],
                    entry_scopes=[{"kind": "prefix", "path": "src"}],
                )


    def test_verifies_only_the_allowed_delta_from_a_dirty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / "notes.txt").write_text(
                "pre-existing user edit\n", encoding="utf-8"
            )
            (repo / "scratch.tmp").write_text(
                "pre-existing untracked\n", encoding="utf-8"
            )

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            (repo / "src" / "owned.txt").write_text("worker edit\n", encoding="utf-8")
            status_before = self.git(repo, "status", "--porcelain=v2", "-z").stdout
            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:src/owned.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 0, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(result["changed_paths"], ["src/owned.txt"])
            self.assertEqual(result["violations"], [])
            self.assertEqual(
                self.git(repo, "status", "--porcelain=v2", "-z").stdout,
                status_before,
            )
            self.assertEqual(
                (repo / "notes.txt").read_text(encoding="utf-8"),
                "pre-existing user edit\n",
            )
            self.assertEqual(
                (repo / "scratch.tmp").read_text(encoding="utf-8"),
                "pre-existing untracked\n",
            )

    def test_explicit_exact_and_prefix_scopes_have_distinct_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "src" / "owned.txt").write_text("changed\n", encoding="utf-8")

            exact = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:src",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            prefix = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "prefix:src",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(exact.returncode, 2, exact.stderr)
            self.assertIn("invalid lease path: src", exact.stderr)
            self.assertEqual(prefix.returncode, 0, prefix.stderr)
            self.assertEqual(json.loads(prefix.stdout)["violations"], [])

    def test_passing_verify_can_emit_the_next_serial_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            baseline = root / "baseline.json"
            capture = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--output",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            (repo / "src" / "owned.txt").write_text("next state\n", encoding="utf-8")
            next_baseline = root / "next.json"
            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:src/owned.txt",
                    "--next-baseline",
                    str(next_baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 0, verify.stderr)
            result = json.loads(verify.stdout)
            emitted = json.loads(next_baseline.read_text(encoding="utf-8"))
            self.assertEqual(emitted["state_id"], result["current_state"])

    def test_verify_rejects_an_untyped_allow_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "src/owned.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("exact:<path> or prefix:<path>", verify.stderr)

    def test_capture_writes_utf8_baseline_only_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            baseline = root / "state" / "baseline.json"
            capture = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--output",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(capture.returncode, 0, capture.stderr)
            self.assertEqual(capture.stdout, "")
            self.assertEqual(
                json.loads(baseline.read_text(encoding="utf-8"))["schema"],
                workspace_state_module.SCHEMA,
            )

            inside = repo / "baseline.json"
            refused = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--output",
                    str(inside),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertFalse(inside.exists())

    @unittest.skipUnless(os.name == "nt", "Win32 device paths are Windows-specific")
    def test_capture_rejects_device_aliases_that_resolve_inside_the_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            direct = repo / ".git" / "cco-device-baseline.json"
            device_alias = "\\\\?\\" + str(direct)

            capture = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--output",
                    device_alias,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(capture.returncode, 2)
            self.assertIn("outside the repository", capture.stderr)
            self.assertFalse(direct.exists())

    @unittest.skipUnless(os.name == "nt", "UNC aliases are Windows-specific")
    def test_capture_rejects_unc_aliases_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            direct = repo / ".git" / "cco-unc-baseline.json"
            drive = direct.drive.rstrip(":")
            if not drive:
                self.skipTest("the temporary directory is not on a drive-letter volume")
            relative = str(direct)[len(direct.anchor) :].replace("/", "\\")
            unc_alias = f"\\\\localhost\\{drive}$\\{relative}"

            capture = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "capture",
                    "--repo",
                    str(repo),
                    "--output",
                    unc_alias,
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(capture.returncode, 2)
            self.assertIn("outside the repository", capture.stderr)
            self.assertFalse(direct.exists())

    def test_rejects_an_out_of_lease_path_without_cleaning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            (repo / "notes.txt").write_text("outside lease\n", encoding="utf-8")
            status_before = self.git(repo, "status", "--porcelain=v2", "-z").stdout
            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "prefix:src",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["verdict"], "violation")
            self.assertEqual(result["changed_paths"], ["notes.txt"])
            self.assertEqual(result["violations"], ["outside_lease:notes.txt"])
            self.assertEqual(
                self.git(repo, "status", "--porcelain=v2", "-z").stdout,
                status_before,
            )
            self.assertEqual(
                (repo / "notes.txt").read_text(encoding="utf-8"),
                "outside lease\n",
            )

    def test_refuses_a_lease_inside_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:.git/config",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_refuses_a_junction_that_resolves_into_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            junction = repo / "leased"
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(repo / ".git")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "prefix:leased",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)
            self.assertEqual(verify.stdout, "")

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_prefix_scope_rejects_a_reparse_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            junction = repo / "src" / "control"
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(repo / ".git")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "prefix:src",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_git_control_reparse_topology_changes_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            hooks = repo / ".git" / "hooks"
            outside = root / "outside-hooks"
            shutil.copytree(hooks, outside)
            shutil.rmtree(hooks)
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(hooks), str(outside)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("hooks_changed", json.loads(verify.stdout)["violations"])

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_git_control_junction_target_change_updates_workspace_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            hooks = repo / ".git" / "hooks"
            target_a = root / "hooks-a"
            target_b = root / "hooks-b"
            shutil.copytree(hooks, target_a)
            shutil.copytree(hooks, target_b)
            shutil.rmtree(hooks)
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(hooks), str(target_a)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            os.rmdir(hooks)
            relinked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(hooks), str(target_b)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(relinked.returncode, 0, relinked.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("hooks_changed", json.loads(verify.stdout)["violations"])

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_git_control_junction_target_content_change_updates_workspace_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            hooks = repo / ".git" / "hooks"
            target = root / "hooks-target"
            shutil.copytree(hooks, target)
            marker = target / "cco-marker"
            marker.write_text("baseline\n", encoding="utf-8")
            shutil.rmtree(hooks)
            linked = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(hooks), str(target)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            marker.write_text("changed\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("hooks_changed", json.loads(verify.stdout)["violations"])

    @unittest.skipUnless(os.name == "nt", "8.3 aliases are Windows-specific")
    def test_refuses_an_existing_short_name_alias_to_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            buffer = ctypes.create_unicode_buffer(32_768)
            length = ctypes.windll.kernel32.GetShortPathNameW(
                str(repo / ".git"), buffer, len(buffer)
            )
            if length == 0 or length >= len(buffer):
                self.skipTest("the test volume did not expose an 8.3 alias for .git")
            short_name = Path(buffer.value).name
            if not short_name or short_name.lower() == ".git":
                self.skipTest("the test volume did not expose an 8.3 alias for .git")

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    f"exact:{short_name}/config",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)

    def test_rejects_noncanonical_lease_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            for path in (
                "../escape.txt",
                "/absolute.txt",
                "C:/absolute.txt",
                "C:relative.txt",
                "\\\\server\\share\\owned.txt",
                "src\\owned.txt",
                "src//owned.txt",
                "src/./owned.txt",
                "src/../owned.txt",
                "src//",
            ):
                with self.subTest(path=path):
                    verify = subprocess.run(
                        [
                            sys.executable,
                            str(STATE_TOOL),
                            "verify",
                            "--repo",
                            str(repo),
                            "--baseline",
                            str(baseline),
                            "--allow",
                            f"exact:{path}",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(verify.returncode, 2)
                    self.assertIn("invalid lease path", verify.stderr)

    def test_every_host_requires_exact_existing_git_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "src" / "owned.txt").write_text("changed\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:SRC/OWNED.TXT",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)
            self.assertEqual(verify.stdout, "")

    @unittest.skipUnless(
        os.path.normcase("A") == os.path.normcase("a"),
        "Unicode lease comparison regression is Windows-specific",
    )
    def test_windows_lease_comparison_preserves_distinct_unicode_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / "strasse.txt").write_text("ascii baseline\n", encoding="utf-8")
            (repo / "straße.txt").write_text("unicode baseline\n", encoding="utf-8")
            self.assertEqual(self.git(repo, "add", ".").returncode, 0)
            commit = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-m",
                "add distinct names",
            )
            self.assertEqual(commit.returncode, 0, commit.stderr)

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "strasse.txt").write_text("changed\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:straße.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertEqual(
                json.loads(verify.stdout)["violations"],
                ["outside_lease:strasse.txt"],
            )

    @unittest.skipIf(os.name == "nt", "backslash filenames are unavailable on Windows")
    def test_posix_backslash_filename_never_aliases_a_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / "a").mkdir()
            (repo / "a" / "b.txt").write_text("slash baseline\n", encoding="utf-8")
            (repo / "a\\b.txt").write_text("backslash baseline\n", encoding="utf-8")
            self.assertEqual(self.git(repo, "add", ".").returncode, 0)
            committed = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-m",
                "add distinct slash spellings",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "a\\b.txt").write_text("changed\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:a/b.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["changed_paths"], ["a\\b.txt"])
            self.assertEqual(result["violations"], ["outside_lease:a\\b.txt"])

    def test_rejects_staging_even_when_the_file_is_in_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            (repo / "src" / "owned.txt").write_text(
                "staged worker edit\n", encoding="utf-8"
            )
            self.assertEqual(self.git(repo, "add", "src/owned.txt").returncode, 0)
            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:src/owned.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn("index_changed", result["violations"])
            self.assertNotIn("outside_lease:src/owned.txt", result["violations"])

    def test_symbolic_head_is_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            self.assertEqual(self.git(repo, "branch", "same-commit").returncode, 0)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            switched = self.git(repo, "switch", "same-commit")
            self.assertEqual(switched.returncode, 0, switched.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn("symbolic_head_changed", result["violations"])
            self.assertNotEqual(result["baseline_state"], result["current_state"])

    def test_git_config_is_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            configured = self.git(repo, "config", "core.hooksPath", ".cco-hooks")
            self.assertEqual(configured.returncode, 0, configured.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("git_config_changed", json.loads(verify.stdout)["violations"])

    def test_git_refs_are_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            branched = self.git(repo, "branch", "unreferenced-by-head")
            self.assertEqual(branched.returncode, 0, branched.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("refs_changed", json.loads(verify.stdout)["violations"])

    def test_git_hooks_are_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            hook = repo / ".git" / "hooks" / "post-checkout"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("hooks_changed", json.loads(verify.stdout)["violations"])

    def test_git_info_metadata_is_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            attributes = repo / ".git" / "info" / "attributes"
            attributes.write_text("*.txt -diff\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("git_info_changed", json.loads(verify.stdout)["violations"])

    def test_git_administrative_state_is_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            head = self.git(repo, "rev-parse", "HEAD")
            self.assertEqual(head.returncode, 0, head.stderr)
            (repo / ".git" / "shallow").write_text(head.stdout, encoding="ascii")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn("git_admin_changed", result["violations"])
            self.assertNotEqual(result["baseline_state"], result["current_state"])

    def test_git_lock_files_are_part_of_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / ".git" / "index.lock").write_bytes(b"")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertIn("git_admin_changed", json.loads(verify.stdout)["violations"])

    def test_git_admin_digest_covers_alternates_sequences_and_worktree_registry(
        self,
    ) -> None:
        mutations = (
            ("objects/info/alternates", "{alternate}\n"),
            ("sequencer/todo", "pick deadbeef test\n"),
            ("worktrees/ghost/gitdir", "{repo}/ghost/.git\n"),
        )
        for relative, template in mutations:
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                repo = self.make_repo(root)
                alternate = root / "alternate-objects"
                alternate.mkdir()
                capture = subprocess.run(
                    [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(capture.returncode, 0, capture.stderr)
                baseline = root / "baseline.json"
                baseline.write_text(capture.stdout, encoding="utf-8")
                target = repo / ".git" / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    template.format(alternate=alternate, repo=repo), encoding="utf-8"
                )

                verify = subprocess.run(
                    [
                        sys.executable,
                        str(STATE_TOOL),
                        "verify",
                        "--repo",
                        str(repo),
                        "--baseline",
                        str(baseline),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(verify.returncode, 1, verify.stderr)
                self.assertIn(
                    "git_admin_changed", json.loads(verify.stdout)["violations"]
                )

    def test_physical_repository_identity_is_bound_to_the_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")

            original = root / "original-repo"
            repo.rename(original)
            shutil.copytree(original, repo, symlinks=True)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn("repository_identity_changed", result["violations"])
            self.assertIn("git_control_identity_changed", result["violations"])

    def test_tracked_content_is_observed_despite_assume_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            marked = self.git(
                repo, "update-index", "--assume-unchanged", "src/owned.txt"
            )
            self.assertEqual(marked.returncode, 0, marked.stderr)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "src" / "owned.txt").write_text("hidden change\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["changed_paths"], ["src/owned.txt"])
            self.assertEqual(result["violations"], ["outside_lease:src/owned.txt"])

    def test_tracked_content_is_observed_despite_skip_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            marked = self.git(repo, "update-index", "--skip-worktree", "src/owned.txt")
            self.assertEqual(marked.returncode, 0, marked.stderr)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "src" / "owned.txt").write_text(
                "skip-worktree hidden change\n", encoding="utf-8"
            )

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["changed_paths"], ["src/owned.txt"])
            self.assertEqual(result["violations"], ["outside_lease:src/owned.txt"])

    def test_snapshot_requires_every_security_identity_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            snapshot = json.loads(capture.stdout)
            snapshot.pop("repo_identity")
            unsigned = {
                key: value for key, value in snapshot.items() if key != "state_id"
            }
            canonical = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
            snapshot["state_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
            baseline = root / "baseline.json"
            baseline.write_text(json.dumps(snapshot), encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("exact v4 required fields", verify.stderr)

    def test_already_dirty_submodule_content_remains_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_container = root / "child-container"
            parent_container = root / "parent-container"
            child_container.mkdir()
            parent_container.mkdir()
            child = self.make_repo(child_container)
            repo = self.make_repo(parent_container)
            added = self.git(
                repo,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(child),
                "vendor/child",
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            committed = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-am",
                "add submodule",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            nested_file = repo / "vendor" / "child" / "src" / "owned.txt"
            nested_file.write_text("first dirty state\n", encoding="utf-8")

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            nested_file.write_text("second dirty state\n", encoding="utf-8")

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["changed_paths"], ["vendor/child"])
            self.assertEqual(result["violations"], ["outside_lease:vendor/child"])

            child_lease = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:vendor/child/src/owned.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(child_lease.returncode, 2)
            self.assertIn("invalid lease path", child_lease.stderr)

            if os.path.normcase("A") == os.path.normcase("a"):
                case_alias = subprocess.run(
                    [
                        sys.executable,
                        str(STATE_TOOL),
                        "verify",
                        "--repo",
                        str(repo),
                        "--baseline",
                        str(baseline),
                        "--allow",
                        "exact:VENDOR/CHILD/src/owned.txt",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(case_alias.returncode, 2)
                self.assertIn("invalid lease path", case_alias.stderr)

            atomic_lease = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:vendor/child",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(atomic_lease.returncode, 0, atomic_lease.stderr)
            self.assertEqual(json.loads(atomic_lease.stdout)["violations"], [])

            ancestor_prefix = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "prefix:vendor",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ancestor_prefix.returncode, 2)
            self.assertIn("invalid lease path", ancestor_prefix.stderr)

            staged = self.git(repo / "vendor" / "child", "add", "src/owned.txt")
            self.assertEqual(staged.returncode, 0, staged.stderr)
            protected = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:vendor/child",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(protected.returncode, 1, protected.stderr)
            self.assertTrue(
                any(
                    item.startswith("submodule_control_changed:vendor/child:")
                    for item in json.loads(protected.stdout)["violations"]
                )
            )

    def test_exact_submodule_lease_rejects_removing_its_git_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_container = root / "child-container"
            parent_container = root / "parent-container"
            child_container.mkdir()
            parent_container.mkdir()
            child = self.make_repo(child_container)
            repo = self.make_repo(parent_container)
            added = self.git(
                repo,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(child),
                "vendor/child",
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            committed = self.git(
                repo,
                "-c",
                "user.name=CCO Tests",
                "-c",
                "user.email=cco-tests@example.invalid",
                "commit",
                "-am",
                "add submodule",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)

            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "vendor" / "child" / ".git").unlink()

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--allow",
                    "exact:vendor/child",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn(
                "submodule_control_changed:vendor/child:kind",
                result["violations"],
            )

    def test_empty_lease_rejects_an_observed_tracked_workspace_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            capture = subprocess.run(
                [sys.executable, str(STATE_TOOL), "capture", "--repo", str(repo)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(capture.returncode, 0, capture.stderr)
            baseline = root / "baseline.json"
            baseline.write_text(capture.stdout, encoding="utf-8")
            (repo / "src" / "owned.txt").write_text(
                "unexpected reviewer edit\n", encoding="utf-8"
            )

            verify = subprocess.run(
                [
                    sys.executable,
                    str(STATE_TOOL),
                    "verify",
                    "--repo",
                    str(repo),
                    "--baseline",
                    str(baseline),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertEqual(result["schema"], "cco.workspace-verification.v3")
            self.assertEqual(result["allowed_scopes"], [])
            self.assertEqual(result["violations"], ["outside_lease:src/owned.txt"])


if __name__ == "__main__":
    unittest.main()

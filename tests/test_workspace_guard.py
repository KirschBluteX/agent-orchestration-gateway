from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "codex-cost-orchestrator"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import workspace_guard  # noqa: E402
import workspace_state  # noqa: E402


class WorkspaceGuardIgnoredPolicyTests(unittest.TestCase):
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
        (repo / "reader").mkdir()
        (repo / "generated").mkdir()
        (repo / "reader" / "owned.txt").write_text("reader\n", encoding="utf-8")
        (repo / "notes.txt").write_text("notes\n", encoding="utf-8")
        (repo / ".gitignore").write_text(
            "reader/*.cache\ngenerated/\n",
            encoding="utf-8",
        )
        (repo / "reader" / "snapshot.cache").write_text(
            "reader cache\n",
            encoding="utf-8",
        )
        (repo / "generated" / "dependency.cache").write_text(
            "generated cache\n",
            encoding="utf-8",
        )
        self.assertEqual(self.git(repo, "add", ".").returncode, 0)
        committed = self.git(
            repo,
            "-c",
            "user.name=CCO Tests",
            "-c",
            "user.email=cco-tests@example.invalid",
            "commit",
            "-m",
            "initial",
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        return repo

    def test_read_only_git_capture_uses_the_scoped_reader_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            scopes = [{"kind": "prefix", "path": "reader"}]

            baseline = workspace_guard.capture(
                repo,
                scopes=scopes,
                writable=False,
            )

            snapshot = baseline["snapshot"]
            self.assertEqual(
                snapshot["ignored_policy"],
                workspace_state.IGNORED_POLICY_SCOPED_READER_V1,
            )
            self.assertEqual(
                snapshot["ignored_scope_digest"],
                workspace_state.ignored_scope_digest(scopes),
            )
            self.assertIn("reader/snapshot.cache", snapshot["entries"])
            self.assertNotIn("generated/dependency.cache", snapshot["entries"])
            self.assertEqual(
                workspace_guard.verify_state(
                    repo,
                    baseline,
                    allowed_scopes=[],
                    owner_scopes=scopes,
                )["verdict"],
                "pass",
            )

    def test_writable_or_mixed_capture_keeps_the_global_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            scopes = [{"kind": "prefix", "path": "reader"}]

            baseline = workspace_guard.capture(
                repo,
                scopes=scopes,
                writable=True,
            )

            snapshot = baseline["snapshot"]
            self.assertEqual(
                snapshot["ignored_policy"],
                workspace_state.IGNORED_POLICY_GLOBAL_V1,
            )
            self.assertIsNone(snapshot["ignored_scope_digest"])
            self.assertIn("generated/dependency.cache", snapshot["entries"])

    def test_exact_ordinary_directory_and_rebound_root_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            with self.assertRaisesRegex(workspace_guard.WorkspaceGuardError, "invalid lease path"):
                workspace_guard.capture(
                    repo,
                    scopes=[{"kind": "exact", "path": "reader"}],
                    writable=False,
                )

            baseline = workspace_guard.capture(
                repo,
                scopes=[{"kind": "prefix", "path": "reader"}],
                writable=False,
            )
            rebound = deepcopy(baseline)
            rebound["root"] = str(root)
            with self.assertRaisesRegex(
                workspace_guard.WorkspaceGuardError,
                "snapshot root does not match",
            ):
                workspace_guard.validate_baseline(rebound)

    def test_scoped_policy_is_bound_to_read_only_wave_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = self.make_repo(Path(temp_dir))
            scopes = [{"kind": "prefix", "path": "reader"}]
            baseline = workspace_guard.capture(
                repo,
                scopes=scopes,
                writable=False,
            )

            writable = deepcopy(baseline)
            writable["writable"] = True
            with self.assertRaisesRegex(
                workspace_guard.WorkspaceGuardError,
                "writable wave cannot use",
            ):
                workspace_guard.validate_baseline(writable)

            rebound = deepcopy(baseline)
            rebound["scopes"] = [{"kind": "prefix", "path": "notes.txt"}]
            with self.assertRaisesRegex(
                workspace_guard.WorkspaceGuardError,
                "does not match wave scopes",
            ):
                workspace_guard.validate_baseline(rebound)

            predecessor = deepcopy(baseline)
            predecessor["snapshot"]["schema"] = "cco.workspace-state.v3"
            with self.assertRaisesRegex(
                workspace_guard.WorkspaceGuardError,
                "unsupported schema",
            ):
                workspace_guard.validate_baseline(predecessor)

    def test_exact_submodule_reader_observes_bounded_ignored_content(self) -> None:
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
            ignored = repo / "vendor" / "child" / "reader" / "runtime.cache"
            ignored.write_text("before\n", encoding="utf-8")
            scopes = [{"kind": "exact", "path": "vendor/child"}]

            baseline = workspace_guard.capture(repo, scopes=scopes, writable=False)
            nested = baseline["snapshot"]["entries"]["vendor/child"]["tracked"][
                "fingerprint"
            ]["state"]
            self.assertEqual(nested["ignored_mode"], "strict")
            self.assertIn("reader/runtime.cache", nested["entries"])

            ignored.write_text("after\n", encoding="utf-8")
            with self.assertRaisesRegex(
                workspace_guard.WorkspaceGuardError,
                "workspace verification failed",
            ):
                workspace_guard.verify_state(
                    repo,
                    baseline,
                    allowed_scopes=[],
                    owner_scopes=scopes,
                )

    def test_hidden_unrelated_submodule_does_not_consume_reader_ignored_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            containers = [root / name for name in ("owned", "other", "parent")]
            for container in containers:
                container.mkdir()
            owned = self.make_repo(containers[0])
            other = self.make_repo(containers[1])
            repo = self.make_repo(containers[2])
            for source, target in (
                (owned, "vendor/owned"),
                (other, "vendor/other"),
            ):
                added = self.git(
                    repo,
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(source),
                    target,
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
                "add submodules",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            unrelated_ignored = (
                repo / "vendor" / "other" / "reader" / "unrelated.cache"
            )
            unrelated_ignored.write_text("unrelated\n", encoding="utf-8")
            hidden = self.git(
                repo,
                "update-index",
                "--skip-worktree",
                "vendor/other",
            )
            self.assertEqual(hidden.returncode, 0, hidden.stderr)
            scopes = [{"kind": "exact", "path": "vendor/owned"}]

            snapshot = workspace_state.state_payload(
                repo,
                ignored_mode="light",
                ignored_max_files=0,
                ignored_max_bytes=0,
                ignored_policy=workspace_state.IGNORED_POLICY_SCOPED_READER_V1,
                scopes=scopes,
            )

            owned_state = snapshot["entries"]["vendor/owned"]["tracked"][
                "fingerprint"
            ]["state"]
            other_state = snapshot["entries"]["vendor/other"]["tracked"][
                "fingerprint"
            ]["state"]
            self.assertEqual(owned_state["ignored_mode"], "strict")
            self.assertEqual(other_state["ignored_mode"], "light")


if __name__ == "__main__":
    unittest.main()

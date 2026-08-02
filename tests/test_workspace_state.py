import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
STATE_TOOL = (
    REPO
    / "plugins"
    / "codex-cost-orchestrator"
    / "scripts"
    / "workspace_state.py"
)


class WorkspaceStateBehaviorTests(unittest.TestCase):
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

    def test_verifies_only_the_allowed_delta_from_a_dirty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = self.make_repo(root)
            (repo / "notes.txt").write_text("pre-existing user edit\n", encoding="utf-8")
            (repo / "scratch.tmp").write_text("pre-existing untracked\n", encoding="utf-8")

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
                    "src/owned.txt",
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
            self.assertEqual(json.loads(baseline.read_text(encoding="utf-8"))["schema"], "cco.workspace-state.v1")

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
                    "src/",
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
                    ".git/config",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 2)
            self.assertIn("invalid lease path", verify.stderr)
            self.assertEqual(verify.stdout, "")

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

            (repo / "src" / "owned.txt").write_text("staged worker edit\n", encoding="utf-8")
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
                    "src/owned.txt",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(verify.returncode, 1, verify.stderr)
            result = json.loads(verify.stdout)
            self.assertIn("index_changed", result["violations"])
            self.assertNotIn("outside_lease:src/owned.txt", result["violations"])

    def test_empty_lease_rejects_every_workspace_mutation(self) -> None:
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
            self.assertEqual(result["allowed_paths"], [])
            self.assertEqual(
                result["violations"], ["outside_lease:src/owned.txt"]
            )


if __name__ == "__main__":
    unittest.main()

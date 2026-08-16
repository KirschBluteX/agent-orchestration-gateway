from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from benchmarks.aog_benchmark import BenchmarkError, _validate_result


REPO = Path(__file__).resolve().parents[1]
PILOT = REPO / "benchmarks" / "manifests" / "featurebench-pilot-v1.json"


class BenchmarkHarnessTests(unittest.TestCase):
    @staticmethod
    def _manifest() -> dict[str, object]:
        return {
            "protocol": "aog.benchmark-manifest.v1",
            "study_id": "featurebench-pilot-v1",
            "dataset": {
                "name": "LiberCoders/FeatureBench",
                "revision": "e99d6efdfe511ea832c1b5735c536129561ec96a",
                "split": "fast",
            },
            "repetitions": 1,
            "arms": [
                {
                    "id": "primary-sol-max",
                    "mode": "primary_only",
                    "primary_model": "gpt-5.6-sol",
                    "primary_effort": "max",
                },
                {
                    "id": "aog-static",
                    "mode": "aog_static",
                    "primary_model": "gpt-5.6-sol",
                    "primary_effort": "max",
                },
            ],
            "tasks": [
                {
                    "id": "owner__repo.task.lv1",
                    "repo": "owner/repo",
                    "base_commit": "1" * 40,
                    "image": "example/image",
                    "partition": "development",
                    "task_sha256": "sha256:" + "2" * 64,
                    "acceptance_sha256": "sha256:" + "3" * 64,
                }
            ],
        }

    def test_plan_binds_every_arm_to_the_same_task_and_acceptance(self) -> None:
        manifest = self._manifest()

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "plan",
                    "--manifest",
                    str(path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["protocol"], "aog.benchmark-plan.v1")
        self.assertEqual(len(plan["runs"]), 2)
        bindings = {
            (run["task_sha256"], run["acceptance_sha256"])
            for run in plan["runs"]
        }
        self.assertEqual(
            bindings,
            {("sha256:" + "2" * 64, "sha256:" + "3" * 64)},
        )
        self.assertEqual(
            {run["arm_id"] for run in plan["runs"]},
            {"primary-sol-max", "aog-static"},
        )
        self.assertEqual(len({run["run_id"] for run in plan["runs"]}), 2)

    def test_validate_rejects_duplicate_task_ids_and_invalid_evidence_hashes(self) -> None:
        manifest = self._manifest()
        tasks = manifest["tasks"]
        assert isinstance(tasks, list)
        tasks.append(dict(tasks[0]))
        tasks[1]["task_sha256"] = "not-a-sha"

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "validate",
                    "--manifest",
                    str(path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("duplicate task id", completed.stderr)

    def test_plan_is_deterministic_and_balances_arm_order(self) -> None:
        manifest = self._manifest()
        template = manifest["tasks"][0]
        assert isinstance(template, dict)
        manifest["tasks"] = [
            {
                **template,
                "id": f"owner__repo.task-{index}.lv1",
                "base_commit": f"{index:x}" * 40,
                "task_sha256": "sha256:" + f"{index + 1:x}" * 64,
                "acceptance_sha256": "sha256:" + f"{index + 7:x}" * 64,
            }
            for index in range(1, 7)
        ]

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            command = [
                sys.executable,
                "-m",
                "benchmarks.aog_benchmark",
                "plan",
                "--manifest",
                str(path),
            ]
            first = subprocess.run(
                command,
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            second = subprocess.run(
                command,
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        runs = json.loads(first.stdout)["runs"]
        first_by_task: dict[str, str] = {}
        for run in runs:
            first_by_task.setdefault(run["task_id"], run["arm_id"])
        counts = {
            arm: list(first_by_task.values()).count(arm)
            for arm in {"primary-sol-max", "aog-static"}
        }
        self.assertEqual(counts, {"primary-sol-max": 3, "aog-static": 3})
        self.assertEqual([run["sequence"] for run in runs], list(range(1, 13)))

    def test_usage_sums_each_model_and_deduplicates_cumulative_events(self) -> None:
        def context(model: str) -> dict[str, object]:
            return {
                "type": "turn_context",
                "payload": {"model": model, "effort": "max"},
            }

        def token(
            *,
            total_input: int,
            cached: int,
            output: int,
            last_input: int,
            last_cached: int,
            last_output: int,
        ) -> dict[str, object]:
            return {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total_input,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output,
                            "reasoning_output_tokens": output // 2,
                            "total_tokens": total_input + output,
                        },
                        "last_token_usage": {
                            "input_tokens": last_input,
                            "cached_input_tokens": last_cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": last_output,
                            "reasoning_output_tokens": last_output // 2,
                            "total_tokens": last_input + last_output,
                        },
                    },
                },
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollouts = {
                "sol.jsonl": [
                    context("gpt-5.6-sol"),
                    token(
                        total_input=100,
                        cached=60,
                        output=10,
                        last_input=100,
                        last_cached=60,
                        last_output=10,
                    ),
                    token(
                        total_input=100,
                        cached=60,
                        output=10,
                        last_input=100,
                        last_cached=60,
                        last_output=10,
                    ),
                    token(
                        total_input=160,
                        cached=100,
                        output=20,
                        last_input=60,
                        last_cached=40,
                        last_output=10,
                    ),
                ],
                "terra.jsonl": [
                    context("gpt-5.6-terra"),
                    token(
                        total_input=50,
                        cached=10,
                        output=8,
                        last_input=50,
                        last_cached=10,
                        last_output=8,
                    ),
                ],
                "luna.jsonl": [
                    context("gpt-5.6-luna"),
                    token(
                        total_input=20,
                        cached=15,
                        output=4,
                        last_input=20,
                        last_cached=15,
                        last_output=4,
                    ),
                ],
            }
            paths: list[Path] = []
            for name, events in rollouts.items():
                path = root / name
                path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8",
                )
                paths.append(path)
            command = [
                sys.executable,
                "-m",
                "benchmarks.aog_benchmark",
                "usage",
            ]
            for path in paths:
                command.extend(["--rollout", str(path)])
            completed = subprocess.run(
                command,
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        usage = json.loads(completed.stdout)
        self.assertEqual(usage["protocol"], "aog.benchmark-usage.v1")
        self.assertEqual(
            usage["families"]["sol"],
            {
                "cache_read_input_tokens": 100,
                "cache_write_input_tokens": 0,
                "input_tokens": 160,
                "output_tokens": 20,
                "reasoning_output_tokens": 10,
                "requests": 2,
                "total_tokens": 180,
                "uncached_input_tokens": 60,
            },
        )
        self.assertEqual(usage["families"]["terra"]["input_tokens"], 50)
        self.assertEqual(usage["families"]["luna"]["input_tokens"], 20)

    def test_usage_reads_zstd_rollout_through_hardened_reader(self) -> None:
        try:
            from compression import zstd
        except ImportError:
            try:
                import zstandard
            except ImportError:
                self.skipTest("zstandard support is unavailable")
            raw_compress = zstandard.ZstdCompressor().compress
        else:
            raw_compress = zstd.compress

        usage = {
            "input_tokens": 20,
            "cached_input_tokens": 5,
            "cache_write_input_tokens": 0,
            "output_tokens": 4,
            "reasoning_output_tokens": 2,
            "total_tokens": 24,
        }
        records = [
            {
                "type": "turn_context",
                "payload": {"effort": "max", "model": "gpt-5.6-luna"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": usage,
                        "total_token_usage": usage,
                    },
                },
            },
        ]
        raw = ("\n".join(json.dumps(item) for item in records) + "\n").encode()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "luna.jsonl.zst"
            path.write_bytes(raw_compress(raw))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "usage",
                    "--rollout",
                    str(path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["families"]["luna"]["input_tokens"], 20)
        self.assertEqual(report["families"]["luna"]["cache_read_input_tokens"], 5)

    def test_validate_requires_sol_max_as_primary_for_every_arm(self) -> None:
        manifest = self._manifest()
        arms = manifest["arms"]
        assert isinstance(arms, list)
        arms[1]["primary_model"] = "gpt-5.6-terra"

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "validate",
                    "--manifest",
                    str(path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Primary must be gpt-5.6-sol/max", completed.stderr)

    def test_summary_reports_paired_outcomes_and_tokens_by_model_family(self) -> None:
        def counters(
            *, input_tokens: int = 0, cached: int = 0, output: int = 0, requests: int = 0
        ) -> dict[str, int]:
            return {
                "cache_read_input_tokens": cached,
                "cache_write_input_tokens": 0,
                "input_tokens": input_tokens,
                "output_tokens": output,
                "reasoning_output_tokens": 0,
                "requests": requests,
                "total_tokens": input_tokens + output,
                "uncached_input_tokens": input_tokens - cached,
            }

        manifest = self._manifest()
        arms = manifest["arms"]
        assert isinstance(arms, list)
        arms.append(
            {
                "id": "aog-static-alt",
                "mode": "aog_static",
                "primary_model": "gpt-5.6-sol",
                "primary_effort": "max",
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            plan_path = root / "plan.json"
            results = root / "results"
            results.mkdir()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            planned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "plan",
                    "--manifest",
                    str(manifest_path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan_path.write_text(planned.stdout, encoding="utf-8")
            plan = json.loads(planned.stdout)
            verdicts = {
                "primary-sol-max": "fail",
                "aog-static": "pass",
                "aog-static-alt": "pass",
            }
            for index, run in enumerate(plan["runs"], start=1):
                is_aog = run["arm_mode"] == "aog_static"
                root_thread_id = f"00000000-0000-4000-8000-{index:012d}"
                usage = {
                    "protocol": "aog.benchmark-usage.v1",
                    "root_thread_id": root_thread_id,
                    "rollouts": 2 if is_aog else 1,
                    "unexpected_models": [],
                    "models": {
                        "gpt-5.6-sol/max": counters(
                            input_tokens=100, cached=40, output=10, requests=1
                        ),
                        **(
                            {
                                "gpt-5.6-terra/max": counters(
                                    input_tokens=50, cached=20, output=5, requests=1
                                )
                            }
                            if is_aog
                            else {}
                        ),
                    },
                    "families": {
                        "sol": counters(
                            input_tokens=100, cached=40, output=10, requests=1
                        ),
                        "terra": counters(
                            input_tokens=50 if is_aog else 0,
                            cached=20 if is_aog else 0,
                            output=5 if is_aog else 0,
                            requests=1 if is_aog else 0,
                        ),
                        "luna": counters(),
                    },
                }
                result = {
                    "protocol": "aog.benchmark-result.v1",
                    "run_id": run["run_id"],
                    "manifest_sha256": run["manifest_sha256"],
                    "task_id": run["task_id"],
                    "arm_id": run["arm_id"],
                    "root_thread_id": root_thread_id,
                    "repetition": run["repetition"],
                    "verdict": verdicts[run["arm_id"]],
                    "wall_time_seconds": 120 if is_aog else 90,
                    "usage": usage,
                }
                (results / f"{run['run_id']}.json").write_text(
                    json.dumps(result), encoding="utf-8"
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "summarize",
                    "--plan",
                    str(plan_path),
                    "--results-dir",
                    str(results),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["protocol"], "aog.benchmark-summary.v1")
        self.assertEqual(summary["expected_runs"], 3)
        self.assertEqual(summary["recorded_runs"], 3)
        self.assertEqual(summary["missing_run_ids"], [])
        self.assertEqual(summary["paired"]["aog_static_wins"], 2)
        self.assertEqual(summary["paired"]["pairs"], 2)
        self.assertEqual(
            set(summary["paired"]["by_pair"]),
            {
                "primary-sol-max__vs__aog-static",
                "primary-sol-max__vs__aog-static-alt",
            },
        )
        self.assertEqual(
            summary["arms"]["aog-static"]["tokens"]["terra"]["input_tokens"],
            50,
        )
        self.assertEqual(
            summary["arms"]["primary-sol-max"]["tokens"]["sol"][
                "cache_read_input_tokens"
            ],
            40,
        )

    def test_result_rejects_inconsistent_model_and_family_usage(self) -> None:
        def counter(*, input_tokens: int, requests: int) -> dict[str, int]:
            return {
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "requests": requests,
                "total_tokens": input_tokens,
                "uncached_input_tokens": input_tokens,
            }

        expected = {
            "arm_id": "aog-static",
            "arm_mode": "aog_static",
            "manifest_sha256": "sha256:" + "a" * 64,
            "repetition": 1,
            "run_id": "run-accounting",
            "task_id": "owner__repo.task.lv1",
        }
        root_thread_id = "11111111-1111-4111-8111-111111111111"
        result = {
            **expected,
            "protocol": "aog.benchmark-result.v1",
            "root_thread_id": root_thread_id,
            "verdict": "pass",
            "wall_time_seconds": 1,
            "usage": {
                "families": {
                    "sol": counter(input_tokens=10, requests=1),
                    "terra": counter(input_tokens=0, requests=0),
                    "luna": counter(input_tokens=0, requests=0),
                },
                "models": {
                    "gpt-5.6-sol/max": counter(input_tokens=10, requests=1),
                    "gpt-5.6-terra/max": counter(input_tokens=5, requests=1),
                },
                "protocol": "aog.benchmark-usage.v1",
                "root_thread_id": root_thread_id,
                "unexpected_models": [],
            },
        }

        with self.assertRaisesRegex(BenchmarkError, "model/family usage"):
            _validate_result(result, expected=expected)

    def test_summary_cli_fails_closed_when_a_planned_result_is_missing(self) -> None:
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            plan_path = root / "plan.json"
            results = root / "results"
            results.mkdir()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            planned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "plan",
                    "--manifest",
                    str(manifest_path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan_path.write_text(planned.stdout, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "summarize",
                    "--plan",
                    str(plan_path),
                    "--results-dir",
                    str(results),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["recorded_runs"], 0)
        self.assertEqual(len(summary["missing_run_ids"]), 2)

    def test_published_pilot_is_pinned_stratified_and_has_twelve_runs(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.aog_benchmark",
                "plan",
                "--manifest",
                str(PILOT),
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads(PILOT.read_text(encoding="utf-8"))
        plan = json.loads(completed.stdout)
        self.assertEqual(manifest["dataset"]["split"], "fast")
        self.assertRegex(manifest["dataset"]["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(manifest["runner"]["revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(len(manifest["tasks"]), 6)
        self.assertEqual(len({task["repo"] for task in manifest["tasks"]}), 6)
        self.assertEqual(
            sorted(task["selection"]["bucket"] for task in manifest["tasks"]),
            ["large", "large", "medium", "medium", "small", "small"],
        )
        self.assertTrue(all("patch" not in task for task in manifest["tasks"]))
        self.assertEqual(len(plan["runs"]), 12)

    def test_usage_discovers_exact_aog_children_from_a_primary_thread(self) -> None:
        root_id = "11111111-1111-4111-8111-111111111111"
        child_id = "22222222-2222-4222-8222-222222222222"

        def events(
            thread_id: str,
            *,
            parent_id: str | None,
            model: str,
            input_tokens: int,
            cached: int,
            output: int,
        ) -> str:
            records = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "parent_thread_id": parent_id,
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"model": model, "effort": "max"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": input_tokens,
                                "cached_input_tokens": cached,
                                "cache_write_input_tokens": 0,
                                "output_tokens": output,
                                "reasoning_output_tokens": 0,
                                "total_tokens": input_tokens + output,
                            },
                            "last_token_usage": {
                                "input_tokens": input_tokens,
                                "cached_input_tokens": cached,
                                "cache_write_input_tokens": 0,
                                "output_tokens": output,
                                "reasoning_output_tokens": 0,
                                "total_tokens": input_tokens + output,
                            },
                        },
                    },
                },
            ]
            return "\n".join(json.dumps(record) for record in records) + "\n"

        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp)
            sessions = codex_home / "sessions"
            sessions.mkdir()
            root_rollout = sessions / "root.jsonl"
            child_rollout = sessions / "child.jsonl"
            root_rollout.write_text(
                events(
                    root_id,
                    parent_id=None,
                    model="gpt-5.6-sol",
                    input_tokens=100,
                    cached=40,
                    output=10,
                ),
                encoding="utf-8",
            )
            child_rollout.write_text(
                events(
                    child_id,
                    parent_id=root_id,
                    model="gpt-5.6-luna",
                    input_tokens=50,
                    cached=20,
                    output=5,
                ),
                encoding="utf-8",
            )
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        rollout_path TEXT,
                        agent_path TEXT,
                        agent_role TEXT
                    );
                    CREATE TABLE thread_spawn_edges (
                        parent_thread_id TEXT,
                        child_thread_id TEXT,
                        status TEXT
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, NULL, NULL)",
                    (root_id, str(root_rollout)),
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?)",
                    (
                        child_id,
                        str(child_rollout),
                        "/root/worker_n01_luna_max_g01",
                        "aog_write_leaf",
                    ),
                )
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
                    (root_id, child_id),
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "usage",
                    "--codex-home",
                    str(codex_home),
                    "--root-thread-id",
                    root_id,
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        usage = json.loads(completed.stdout)
        self.assertEqual(usage["rollouts"], 2)
        self.assertEqual(usage["families"]["sol"]["input_tokens"], 100)
        self.assertEqual(usage["families"]["luna"]["input_tokens"], 50)

    def test_record_creates_one_plan_bound_result_without_overwrite(self) -> None:
        zero = {
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "requests": 0,
            "total_tokens": 0,
            "uncached_input_tokens": 0,
        }
        sol = {
            **zero,
            "input_tokens": 100,
            "output_tokens": 10,
            "requests": 1,
            "total_tokens": 110,
            "uncached_input_tokens": 100,
        }
        usage = {
            "protocol": "aog.benchmark-usage.v1",
            "root_thread_id": "11111111-1111-4111-8111-111111111111",
            "rollouts": 1,
            "unexpected_models": [],
            "models": {"gpt-5.6-sol/max": sol},
            "families": {"sol": sol, "terra": zero, "luna": zero},
        }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "manifest.json"
            plan_path = root / "plan.json"
            usage_path = root / "usage.json"
            results = root / "results"
            manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
            planned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "plan",
                    "--manifest",
                    str(manifest_path),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan_path.write_text(planned.stdout, encoding="utf-8")
            plan = json.loads(planned.stdout)
            run = next(item for item in plan["runs"] if item["arm_mode"] == "primary_only")
            usage_path.write_text(json.dumps(usage), encoding="utf-8")
            command = [
                sys.executable,
                "-m",
                "benchmarks.aog_benchmark",
                "record",
                "--plan",
                str(plan_path),
                "--run-id",
                run["run_id"],
                "--usage",
                str(usage_path),
                "--verdict",
                "pass",
                "--wall-time-seconds",
                "12.5",
                "--results-dir",
                str(results),
            ]
            first = subprocess.run(
                command,
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            changed = list(command)
            changed[changed.index("--verdict") + 1] = "fail"
            second = subprocess.run(
                changed,
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            result_path = results / f"{run['run_id']}.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)
        self.assertEqual(result["run_id"], run["run_id"])
        self.assertEqual(result["task_id"], run["task_id"])
        self.assertEqual(result["verdict"], "pass")

    def test_usage_rejects_duplicate_rollout_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            rollout = Path(temp) / "rollout.jsonl"
            rollout.write_text("", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "usage",
                    "--rollout",
                    str(rollout),
                    "--rollout",
                    str(rollout),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("duplicate rollout", completed.stderr)

    def test_preflight_returns_structured_blockers_without_starting_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = Path(temp) / "manifest.json"
            manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.aog_benchmark",
                    "preflight",
                    "--manifest",
                    str(manifest_path),
                    "--featurebench-root",
                    str(REPO),
                ],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertIn(completed.returncode, {0, 3})
        report = json.loads(completed.stdout)
        self.assertEqual(report["protocol"], "aog.benchmark-preflight.v1")
        self.assertIn("docker", report["checks"])
        self.assertIn("featurebench_revision", report["checks"])
        self.assertIn("blockers", report)
        self.assertFalse(report["started_run"])


if __name__ == "__main__":
    unittest.main()

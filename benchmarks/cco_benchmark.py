#!/usr/bin/env python3
"""Build reproducible CCO benchmark plans without invoking a model."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import statistics
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping


MANIFEST_PROTOCOL = "cco.benchmark-manifest.v1"
PLAN_PROTOCOL = "cco.benchmark-plan.v1"
USAGE_PROTOCOL = "cco.benchmark-usage.v1"
RESULT_PROTOCOL = "cco.benchmark-result.v1"
SUMMARY_PROTOCOL = "cco.benchmark-summary.v1"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
AGENT_PATH = re.compile(r"^/root(?:/[a-z0-9][a-z0-9_]{0,127})+$")
CCO_ROLES = {
    "cost_orchestrator_read_leaf",
    "cost_orchestrator_write_leaf",
}
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class BenchmarkError(ValueError):
    """Raised when a benchmark artifact is unsafe or ambiguous."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkError("manifest is not readable canonical JSON") from error
    if not isinstance(value, Mapping):
        raise BenchmarkError("manifest must be a JSON object")
    if value.get("protocol") != MANIFEST_PROTOCOL:
        raise BenchmarkError(f"manifest protocol must be {MANIFEST_PROTOCOL}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _required_string(value: Any, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise BenchmarkError(f"{label} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise BenchmarkError(f"{label} has an invalid format")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if manifest.get("protocol") != MANIFEST_PROTOCOL:
        raise BenchmarkError(f"manifest protocol must be {MANIFEST_PROTOCOL}")
    study_id = _required_string(manifest.get("study_id"), label="study id", pattern=SLUG)

    dataset = manifest.get("dataset")
    if not isinstance(dataset, Mapping):
        raise BenchmarkError("dataset must be an object")
    _required_string(dataset.get("name"), label="dataset name")
    _required_string(dataset.get("revision"), label="dataset revision", pattern=HEX40)
    if dataset.get("split") not in {"fast", "lite", "full"}:
        raise BenchmarkError("dataset split must be fast, lite, or full")

    repetitions = manifest.get("repetitions")
    if (
        not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or not 1 <= repetitions <= 10
    ):
        raise BenchmarkError("manifest repetitions must be an integer from 1 to 10")

    arms = manifest.get("arms")
    if not isinstance(arms, list) or not arms:
        raise BenchmarkError("manifest arms must be a non-empty array")
    arm_ids: set[str] = set()
    for arm in arms:
        if not isinstance(arm, Mapping):
            raise BenchmarkError("every arm must be an object")
        arm_id = _required_string(arm.get("id"), label="arm id", pattern=SLUG)
        if arm_id in arm_ids:
            raise BenchmarkError(f"duplicate arm id: {arm_id}")
        arm_ids.add(arm_id)
        if arm.get("mode") not in {"primary_only", "cco_static"}:
            raise BenchmarkError(f"arm {arm_id} has an invalid mode")
        primary_model = _required_string(
            arm.get("primary_model"), label=f"arm {arm_id} primary model"
        )
        primary_effort = arm.get("primary_effort")
        if primary_model != "gpt-5.6-sol" or primary_effort != "max":
            raise BenchmarkError(
                f"arm {arm_id} Primary must be gpt-5.6-sol/max"
            )

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise BenchmarkError("manifest tasks must be a non-empty array")
    task_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            raise BenchmarkError("every task must be an object")
        task_id = _required_string(task.get("id"), label="task id")
        if task_id in task_ids:
            raise BenchmarkError(f"duplicate task id: {task_id}")
        task_ids.add(task_id)
        _required_string(task.get("repo"), label=f"task {task_id} repo", pattern=REPOSITORY)
        _required_string(
            task.get("base_commit"), label=f"task {task_id} base commit", pattern=HEX40
        )
        _required_string(task.get("image"), label=f"task {task_id} image")
        if task.get("partition") not in {"development", "holdout"}:
            raise BenchmarkError(f"task {task_id} has an invalid partition")
        _required_string(
            task.get("task_sha256"), label=f"task {task_id} task hash", pattern=SHA256
        )
        _required_string(
            task.get("acceptance_sha256"),
            label=f"task {task_id} acceptance hash",
            pattern=SHA256,
        )

    return {
        "arms": len(arms),
        "manifest_sha256": _sha256(manifest),
        "runs": len(arms) * len(tasks) * repetitions,
        "study_id": study_id,
        "tasks": len(tasks),
    }


def build_plan(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validation = validate_manifest(manifest)
    arms = manifest.get("arms")
    tasks = manifest.get("tasks")
    repetitions = manifest.get("repetitions")
    assert isinstance(arms, list)
    assert isinstance(tasks, list)
    assert isinstance(repetitions, int)

    manifest_sha256 = validation["manifest_sha256"]
    blocks: list[tuple[str, int, Mapping[str, Any]]] = []
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            assert isinstance(task, Mapping)
            block_key = _sha256(
                {
                    "manifest_sha256": manifest_sha256,
                    "repetition": repetition,
                    "task_id": task["id"],
                }
            )
            blocks.append((block_key, repetition, task))

    runs: list[dict[str, Any]] = []
    for block_index, (_, repetition, task) in enumerate(sorted(blocks)):
        rotation = block_index % len(arms)
        ordered_arms = arms[rotation:] + arms[:rotation]
        for arm in ordered_arms:
            assert isinstance(arm, Mapping)
            identity = {
                "arm_id": arm.get("id"),
                "manifest_sha256": manifest_sha256,
                "repetition": repetition,
                "task_id": task.get("id"),
            }
            runs.append(
                {
                    **identity,
                    "acceptance_sha256": task.get("acceptance_sha256"),
                    "arm_mode": arm.get("mode"),
                    "primary_effort": arm.get("primary_effort"),
                    "primary_model": arm.get("primary_model"),
                    "run_id": "run-" + _sha256(identity).split(":", 1)[1][:20],
                    "sequence": len(runs) + 1,
                    "task_sha256": task.get("task_sha256"),
                }
            )

    return {
        "manifest_sha256": manifest_sha256,
        "protocol": PLAN_PROTOCOL,
        "runs": runs,
        "study_id": manifest.get("study_id"),
    }


def _load_json(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"{label} is not readable JSON") from error
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"{label} must be a JSON object")
    return value


def _result_counter(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"{label} must be an object")
    fields = tuple(_empty_usage())
    result: dict[str, int] = {}
    for field in fields:
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise BenchmarkError(f"{label}.{field} must be a non-negative integer")
        result[field] = item
    if result["uncached_input_tokens"] != (
        result["input_tokens"] - result["cache_read_input_tokens"]
    ):
        raise BenchmarkError(f"{label} has inconsistent uncached input tokens")
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise BenchmarkError(f"{label} has inconsistent total tokens")
    return result


def _validate_result(
    value: Mapping[str, Any], *, expected: Mapping[str, Any]
) -> dict[str, Any]:
    if value.get("protocol") != RESULT_PROTOCOL:
        raise BenchmarkError(f"result protocol must be {RESULT_PROTOCOL}")
    for field in (
        "run_id",
        "manifest_sha256",
        "task_id",
        "arm_id",
        "repetition",
    ):
        if value.get(field) != expected.get(field):
            raise BenchmarkError(f"result {expected.get('run_id')} has a mismatched {field}")
    verdict = value.get("verdict")
    if verdict not in {"pass", "fail", "error"}:
        raise BenchmarkError(f"result {expected.get('run_id')} has an invalid verdict")
    wall_time = value.get("wall_time_seconds")
    if (
        not isinstance(wall_time, (int, float))
        or isinstance(wall_time, bool)
        or not math.isfinite(wall_time)
        or wall_time <= 0
    ):
        raise BenchmarkError(f"result {expected.get('run_id')} has invalid wall time")

    usage = value.get("usage")
    if not isinstance(usage, Mapping) or usage.get("protocol") != USAGE_PROTOCOL:
        raise BenchmarkError(f"result {expected.get('run_id')} has invalid usage")
    if usage.get("unexpected_models") != []:
        raise BenchmarkError(f"result {expected.get('run_id')} used an unexpected model")
    models = usage.get("models")
    if not isinstance(models, Mapping) or "gpt-5.6-sol/max" not in models:
        raise BenchmarkError(f"result {expected.get('run_id')} lacks Sol/max Primary usage")
    families = usage.get("families")
    if not isinstance(families, Mapping) or set(families) != {"sol", "terra", "luna"}:
        raise BenchmarkError(f"result {expected.get('run_id')} has invalid model families")
    checked_families = {
        family: _result_counter(families[family], label=f"usage family {family}")
        for family in ("sol", "terra", "luna")
    }
    if checked_families["sol"]["requests"] < 1:
        raise BenchmarkError(f"result {expected.get('run_id')} has no Primary request")
    if expected.get("arm_mode") == "primary_only":
        for family in ("terra", "luna"):
            if any(checked_families[family].values()):
                raise BenchmarkError(
                    f"result {expected.get('run_id')} delegated in primary-only mode"
                )

    return {
        "arm_id": expected["arm_id"],
        "arm_mode": expected["arm_mode"],
        "repetition": expected["repetition"],
        "run_id": expected["run_id"],
        "task_id": expected["task_id"],
        "tokens": checked_families,
        "verdict": verdict,
        "wall_time_seconds": float(wall_time),
    }


def _merge_counter(target: dict[str, int], source: Mapping[str, int]) -> None:
    for field in target:
        target[field] += source[field]


def summarize(plan: Mapping[str, Any], results_dir: Path) -> dict[str, Any]:
    if plan.get("protocol") != PLAN_PROTOCOL:
        raise BenchmarkError(f"plan protocol must be {PLAN_PROTOCOL}")
    runs = plan.get("runs")
    if not isinstance(runs, list) or not runs:
        raise BenchmarkError("plan runs must be a non-empty array")
    expected: dict[str, Mapping[str, Any]] = {}
    arm_modes: dict[str, str] = {}
    for run in runs:
        if not isinstance(run, Mapping):
            raise BenchmarkError("every plan run must be an object")
        run_id = _required_string(run.get("run_id"), label="plan run id")
        if run_id in expected:
            raise BenchmarkError(f"duplicate plan run id: {run_id}")
        expected[run_id] = run
        arm_id = _required_string(run.get("arm_id"), label="plan arm id")
        arm_mode = run.get("arm_mode")
        if arm_mode not in {"primary_only", "cco_static"}:
            raise BenchmarkError(f"plan run {run_id} has invalid arm mode")
        prior = arm_modes.setdefault(arm_id, arm_mode)
        if prior != arm_mode:
            raise BenchmarkError(f"plan arm {arm_id} changes mode")

    try:
        result_paths = sorted(results_dir.glob("*.json"))
    except OSError as error:
        raise BenchmarkError("results directory is unavailable") from error
    observed: dict[str, dict[str, Any]] = {}
    for path in result_paths:
        value = _load_json(path, label=f"result {path.name}")
        run_id = value.get("run_id")
        if not isinstance(run_id, str) or run_id not in expected:
            raise BenchmarkError(f"result {path.name} is not in the plan")
        if run_id in observed:
            raise BenchmarkError(f"duplicate result for run {run_id}")
        observed[run_id] = _validate_result(value, expected=expected[run_id])

    arms: dict[str, dict[str, Any]] = {}
    for arm_id, mode in sorted(arm_modes.items()):
        arms[arm_id] = {
            "errors": 0,
            "failed": 0,
            "mode": mode,
            "pass_rate": None,
            "passed": 0,
            "recorded": 0,
            "tokens": {
                family: _empty_usage() for family in ("sol", "terra", "luna")
            },
            "wall_time_seconds": {"median": None, "total": 0.0},
        }
    wall_times: dict[str, list[float]] = {arm_id: [] for arm_id in arms}
    for result in observed.values():
        arm = arms[result["arm_id"]]
        arm["recorded"] += 1
        arm[{"pass": "passed", "fail": "failed", "error": "errors"}[result["verdict"]]] += 1
        wall_times[result["arm_id"]].append(result["wall_time_seconds"])
        for family in ("sol", "terra", "luna"):
            _merge_counter(arm["tokens"][family], result["tokens"][family])
    for arm_id, arm in arms.items():
        count = arm["recorded"]
        if count:
            arm["pass_rate"] = arm["passed"] / count
            arm["wall_time_seconds"] = {
                "median": statistics.median(wall_times[arm_id]),
                "total": sum(wall_times[arm_id]),
            }

    paired = {"cco_static_wins": 0, "pairs": 0, "primary_only_wins": 0, "ties": 0}
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for result in observed.values():
        key = (result["task_id"], result["repetition"])
        grouped.setdefault(key, {})[result["arm_mode"]] = result
    for pair in grouped.values():
        if set(pair) != {"primary_only", "cco_static"}:
            continue
        paired["pairs"] += 1
        primary_pass = pair["primary_only"]["verdict"] == "pass"
        cco_pass = pair["cco_static"]["verdict"] == "pass"
        if cco_pass and not primary_pass:
            paired["cco_static_wins"] += 1
        elif primary_pass and not cco_pass:
            paired["primary_only_wins"] += 1
        else:
            paired["ties"] += 1

    missing = sorted(set(expected) - set(observed))
    return {
        "arms": arms,
        "expected_runs": len(expected),
        "missing_run_ids": missing,
        "paired": paired,
        "plan_sha256": _sha256(plan),
        "protocol": SUMMARY_PROTOCOL,
        "recorded_runs": len(observed),
        "study_id": plan.get("study_id"),
    }


def record_result(
    plan: Mapping[str, Any],
    *,
    run_id: str,
    usage: Mapping[str, Any],
    verdict: str,
    wall_time_seconds: float,
    results_dir: Path,
) -> dict[str, Any]:
    if plan.get("protocol") != PLAN_PROTOCOL:
        raise BenchmarkError(f"plan protocol must be {PLAN_PROTOCOL}")
    runs = plan.get("runs")
    if not isinstance(runs, list):
        raise BenchmarkError("plan runs must be an array")
    expected = next(
        (run for run in runs if isinstance(run, Mapping) and run.get("run_id") == run_id),
        None,
    )
    if expected is None:
        raise BenchmarkError(f"run id is not present in the plan: {run_id}")
    result = {
        "arm_id": expected["arm_id"],
        "manifest_sha256": expected["manifest_sha256"],
        "protocol": RESULT_PROTOCOL,
        "repetition": expected["repetition"],
        "run_id": run_id,
        "task_id": expected["task_id"],
        "usage": usage,
        "verdict": verdict,
        "wall_time_seconds": wall_time_seconds,
    }
    _validate_result(result, expected=expected)
    serialized = (
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        target = results_dir / f"{run_id}.json"
        if target.exists():
            existing = target.read_bytes()
            if existing != serialized:
                raise BenchmarkError(f"result already exists with different content: {run_id}")
            reused = True
        else:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{run_id}.", suffix=".tmp", dir=results_dir
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
            reused = False
    except OSError as error:
        raise BenchmarkError("result cannot be written") from error
    return {
        "protocol": "cco.benchmark-receipt.v1",
        "result_sha256": "sha256:" + hashlib.sha256(serialized).hexdigest(),
        "run_id": run_id,
        "reused": reused,
    }


def _executable(explicit: Path | None, name: str) -> str | None:
    if explicit is not None:
        return str(explicit)
    return shutil.which(name)


def _run_command(executable: str | None, arguments: list[str]) -> tuple[bool, str]:
    if not executable:
        return False, "not found"
    command = [executable, *arguments]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".bat", ".cmd"}:
        command = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([executable, *arguments]),
        ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, type(error).__name__
    raw = completed.stdout or completed.stderr or b""
    encoding = "utf-16-le" if b"\x00" in raw[:32] else "utf-8"
    output = raw.decode(encoding, errors="replace").strip().splitlines()
    detail = output[-1] if output else f"exit {completed.returncode}"
    detail = detail.encode("ascii", errors="backslashreplace").decode("ascii")
    return completed.returncode == 0, detail


def preflight(
    manifest: Mapping[str, Any],
    *,
    featurebench_root: Path,
    uv: Path | None = None,
    docker: Path | None = None,
    codex: Path | None = None,
) -> dict[str, Any]:
    validation = validate_manifest(manifest)
    checks: dict[str, dict[str, Any]] = {
        "manifest": {"ok": True, "runs": validation["runs"]},
        "featurebench_revision": {"ok": False, "expected": None, "actual": None},
        "uv": {"ok": False, "detail": "not checked"},
        "codex": {"ok": False, "detail": "not checked"},
        "docker": {"ok": False, "detail": "not checked"},
        "wsl": {"ok": os.name != "nt", "detail": "native POSIX"},
    }
    blockers: list[str] = []

    runner = manifest.get("runner")
    expected_revision = runner.get("revision") if isinstance(runner, Mapping) else None
    checks["featurebench_revision"]["expected"] = expected_revision
    if isinstance(expected_revision, str) and featurebench_root.is_dir():
        ok, actual = _run_command(
            shutil.which("git"), ["-C", str(featurebench_root), "rev-parse", "HEAD"]
        )
        checks["featurebench_revision"].update(
            {"actual": actual, "ok": ok and actual == expected_revision}
        )
    if not checks["featurebench_revision"]["ok"]:
        blockers.append("featurebench_revision_mismatch")

    uv_ok, uv_detail = _run_command(_executable(uv, "uv"), ["--version"])
    checks["uv"].update({"ok": uv_ok, "detail": uv_detail})
    if not uv_ok:
        blockers.append("uv_unavailable")
    codex_ok, codex_detail = _run_command(_executable(codex, "codex"), ["--version"])
    checks["codex"].update({"ok": codex_ok, "detail": codex_detail})
    if not codex_ok:
        blockers.append("codex_unavailable")
    docker_ok, docker_detail = _run_command(
        _executable(docker, "docker"), ["version", "--format", "{{.Server.Version}}"]
    )
    checks["docker"].update({"ok": docker_ok, "detail": docker_detail})
    if not docker_ok:
        blockers.append("docker_daemon_unavailable")

    if os.name == "nt":
        wsl_ok, wsl_detail = _run_command(
            _executable(None, "wsl.exe"), ["--list", "--quiet"]
        )
        checks["wsl"].update({"ok": wsl_ok and bool(wsl_detail), "detail": wsl_detail})
        if not checks["wsl"]["ok"]:
            blockers.append("wsl_distribution_required")

    return {
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "protocol": "cco.benchmark-preflight.v1",
        "ready": not blockers,
        "started_run": False,
        "study_id": manifest.get("study_id"),
    }


def _token_values(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"{label} must be an object")
    result: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        item = value.get(field, 0)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise BenchmarkError(f"{label}.{field} must be a non-negative integer")
        result[field] = item
    return result


def _empty_usage() -> dict[str, int]:
    return {
        "cache_read_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "requests": 0,
        "total_tokens": 0,
        "uncached_input_tokens": 0,
    }


def _model_family(model: str) -> str:
    lowered = model.casefold()
    for family in ("sol", "terra", "luna"):
        if re.search(rf"(?:^|[-_/]){family}(?:$|[-_/])", lowered):
            return family
    return "other"


def _add_usage(target: dict[str, int], delta: Mapping[str, int]) -> None:
    target["input_tokens"] += delta["input_tokens"]
    target["cache_read_input_tokens"] += delta["cached_input_tokens"]
    target["cache_write_input_tokens"] += delta["cache_write_input_tokens"]
    target["output_tokens"] += delta["output_tokens"]
    target["reasoning_output_tokens"] += delta["reasoning_output_tokens"]
    target["total_tokens"] += delta["total_tokens"]
    target["requests"] += 1
    target["uncached_input_tokens"] = (
        target["input_tokens"] - target["cache_read_input_tokens"]
    )
    if target["uncached_input_tokens"] < 0:
        raise BenchmarkError("cached input tokens exceed total input tokens")


def collect_usage(paths: list[Path]) -> dict[str, Any]:
    families = {family: _empty_usage() for family in ("sol", "terra", "luna")}
    models: dict[str, dict[str, int]] = {}
    unexpected_models: set[str] = set()

    for path in paths:
        current_model: str | None = None
        current_effort: str | None = None
        previous_total: dict[str, int] | None = None
        try:
            stream = path.open("r", encoding="utf-8")
        except OSError as error:
            raise BenchmarkError(f"rollout is not readable: {path.name}") from error
        with stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line, object_pairs_hook=_unique_object)
                except (json.JSONDecodeError, BenchmarkError) as error:
                    raise BenchmarkError(
                        f"rollout {path.name} has invalid JSON at line {number}"
                    ) from error
                if not isinstance(record, Mapping):
                    continue
                payload = record.get("payload")
                if record.get("type") == "turn_context" and isinstance(payload, Mapping):
                    model = payload.get("model")
                    effort = payload.get("effort")
                    if isinstance(model, str) and model:
                        current_model = model
                    if isinstance(effort, str) and effort:
                        current_effort = effort
                    continue
                if (
                    record.get("type") != "event_msg"
                    or not isinstance(payload, Mapping)
                    or payload.get("type") != "token_count"
                ):
                    continue
                if current_model is None:
                    raise BenchmarkError(
                        f"rollout {path.name} reports usage before its model context"
                    )
                info = payload.get("info")
                if not isinstance(info, Mapping):
                    raise BenchmarkError(f"rollout {path.name} has invalid token info")
                total = _token_values(
                    info.get("total_token_usage"), label="total token usage"
                )
                last = _token_values(info.get("last_token_usage"), label="last token usage")
                if previous_total is None:
                    delta = last
                else:
                    delta = {
                        field: total[field] - previous_total[field]
                        for field in TOKEN_FIELDS
                    }
                    if any(value < 0 for value in delta.values()):
                        delta = last
                previous_total = total
                if not any(delta.values()):
                    continue

                route = f"{current_model}/{current_effort or 'unknown'}"
                model_usage = models.setdefault(route, _empty_usage())
                _add_usage(model_usage, delta)
                family = _model_family(current_model)
                if family == "other":
                    unexpected_models.add(current_model)
                else:
                    _add_usage(families[family], delta)

    return {
        "families": families,
        "models": dict(sorted(models.items())),
        "protocol": USAGE_PROTOCOL,
        "rollouts": len(paths),
        "unexpected_models": sorted(unexpected_models),
    }


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _plain_file_inside(path: Path, *, root: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    if any(_is_reparse(candidate) for candidate in (absolute, *absolute.parents)):
        raise BenchmarkError(f"{label} cannot use a reparse ancestor")
    try:
        resolved = absolute.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        inside = os.path.commonpath(
            (os.path.normcase(str(resolved_root)), os.path.normcase(str(resolved)))
        ) == os.path.normcase(str(resolved_root))
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"{label} is outside its trusted root") from error
    if not inside or not resolved.is_file():
        raise BenchmarkError(f"{label} is outside its trusted root")
    return resolved


def _first_session_meta(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            line = stream.readline(64 * 1024 + 1)
    except OSError as error:
        raise BenchmarkError(f"rollout metadata is unavailable: {path.name}") from error
    if not line or len(line) > 64 * 1024:
        raise BenchmarkError(f"rollout metadata is missing or oversized: {path.name}")
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, BenchmarkError) as error:
        raise BenchmarkError(f"rollout metadata is invalid: {path.name}") from error
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"rollout metadata is invalid: {path.name}")
    return value


def discover_rollouts(codex_home: Path, root_thread_id: str) -> list[Path]:
    if THREAD_ID.fullmatch(root_thread_id) is None:
        raise BenchmarkError("root thread id is not a canonical UUID")
    try:
        home = codex_home.expanduser().resolve(strict=True)
        sessions = (home / "sessions").resolve(strict=True)
    except OSError as error:
        raise BenchmarkError("Codex home or sessions root is unavailable") from error
    if any(_is_reparse(candidate) for candidate in (home, sessions)):
        raise BenchmarkError("Codex home cannot use a reparse root")

    databases: list[tuple[int, Path]] = []
    for path in home.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_([0-9]+)\.sqlite", path.name)
        if match is not None:
            databases.append((int(match.group(1)), path))
    if not databases:
        raise BenchmarkError("Codex state database was not found")
    databases.sort(reverse=True)
    database = _plain_file_inside(databases[0][1], root=home, label="state database")

    uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        required = {
            "threads": {"id", "rollout_path", "agent_path", "agent_role"},
            "thread_spawn_edges": {"parent_thread_id", "child_thread_id"},
        }
        for table, columns in required.items():
            present = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns <= present:
                raise BenchmarkError(f"Codex state database lacks {table} columns")

        rows: list[tuple[str, str | None, sqlite3.Row]] = []
        root = connection.execute(
            "SELECT id, rollout_path, agent_path, agent_role FROM threads WHERE id = ?",
            (root_thread_id,),
        ).fetchone()
        if root is None:
            raise BenchmarkError("root thread is absent from Codex state")
        rows.append((root_thread_id, None, root))

        children = connection.execute(
            """
            SELECT t.id, t.rollout_path, t.agent_path, t.agent_role
            FROM thread_spawn_edges AS e
            JOIN threads AS t ON t.id = e.child_thread_id
            WHERE e.parent_thread_id = ?
            ORDER BY t.id
            """,
            (root_thread_id,),
        ).fetchall()
        if len(children) > 64:
            raise BenchmarkError("benchmark thread has too many direct children")
        child_ids: set[str] = set()
        for row in children:
            child_id = str(row["id"])
            if child_id in child_ids:
                raise BenchmarkError(f"duplicate child thread: {child_id}")
            child_ids.add(child_id)
            if THREAD_ID.fullmatch(child_id) is None:
                raise BenchmarkError("child thread id is not a canonical UUID")
            if row["agent_role"] not in CCO_ROLES:
                raise BenchmarkError(f"child {child_id} is not CCO-owned")
            agent_path = row["agent_path"]
            if not isinstance(agent_path, str) or AGENT_PATH.fullmatch(agent_path) is None:
                raise BenchmarkError(f"child {child_id} has an invalid Agent path")
            nested = connection.execute(
                "SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id = ? LIMIT 1",
                (child_id,),
            ).fetchone()
            if nested is not None:
                raise BenchmarkError(f"CCO child {child_id} delegated another Agent")
            rows.append((child_id, root_thread_id, row))

    paths: list[Path] = []
    for thread_id, parent_id, row in rows:
        rollout_value = row["rollout_path"]
        if not isinstance(rollout_value, str):
            raise BenchmarkError(f"thread {thread_id} has no rollout path")
        rollout = _plain_file_inside(
            Path(rollout_value), root=sessions, label=f"thread {thread_id} rollout"
        )
        first = _first_session_meta(rollout)
        payload = first.get("payload")
        if (
            first.get("type") != "session_meta"
            or not isinstance(payload, Mapping)
            or payload.get("id") != thread_id
        ):
            raise BenchmarkError(f"thread {thread_id} rollout identity does not match")
        if parent_id is not None and payload.get("parent_thread_id") != parent_id:
            raise BenchmarkError(f"child {thread_id} parent identity does not match")
        paths.append(rollout)
    return paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="emit a deterministic paired run plan")
    plan.add_argument("--manifest", type=Path, required=True)
    validate = commands.add_parser("validate", help="validate a benchmark manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    usage = commands.add_parser("usage", help="sum token usage from exact Codex rollouts")
    source = usage.add_mutually_exclusive_group(required=True)
    source.add_argument("--rollout", type=Path, action="append")
    source.add_argument("--root-thread-id")
    usage.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    summary = commands.add_parser("summarize", help="summarize exact plan-bound results")
    summary.add_argument("--plan", type=Path, required=True)
    summary.add_argument("--results-dir", type=Path, required=True)
    record = commands.add_parser("record", help="write one plan-bound benchmark result")
    record.add_argument("--plan", type=Path, required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--usage", type=Path, required=True)
    record.add_argument("--verdict", choices=("pass", "fail", "error"), required=True)
    record.add_argument("--wall-time-seconds", type=float, required=True)
    record.add_argument("--results-dir", type=Path, required=True)
    pre = commands.add_parser("preflight", help="check benchmark runtime prerequisites")
    pre.add_argument("--manifest", type=Path, required=True)
    pre.add_argument("--featurebench-root", type=Path, required=True)
    pre.add_argument("--uv", type=Path)
    pre.add_argument("--docker", type=Path)
    pre.add_argument("--codex", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            manifest = _load_manifest(args.manifest)
            print(json.dumps(build_plan(manifest), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "validate":
            manifest = _load_manifest(args.manifest)
            print(json.dumps(validate_manifest(manifest), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "usage":
            paths = args.rollout
            if args.root_thread_id is not None:
                paths = discover_rollouts(args.codex_home, args.root_thread_id)
            assert paths is not None
            report = collect_usage(paths)
            if args.root_thread_id is not None:
                report["root_thread_id"] = args.root_thread_id
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "summarize":
            plan = _load_json(args.plan, label="plan")
            print(
                json.dumps(
                    summarize(plan, args.results_dir),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "record":
            plan = _load_json(args.plan, label="plan")
            usage = _load_json(args.usage, label="usage")
            print(
                json.dumps(
                    record_result(
                        plan,
                        run_id=args.run_id,
                        usage=usage,
                        verdict=args.verdict,
                        wall_time_seconds=args.wall_time_seconds,
                        results_dir=args.results_dir,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "preflight":
            manifest = _load_manifest(args.manifest)
            report = preflight(
                manifest,
                featurebench_root=args.featurebench_root,
                uv=args.uv,
                docker=args.docker,
                codex=args.codex,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["ready"] else 3
    except BenchmarkError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

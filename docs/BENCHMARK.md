# Reproducible benchmark protocol

Benchmarking is an explicit development activity outside the CCO runtime. A benchmark
run may read local Codex JSONL events for that run and store a user-approved result
file; it is not a runtime reporting service.

## Current pilot

[`benchmarks/manifests/featurebench-pilot-v1.json`](../benchmarks/manifests/featurebench-pilot-v1.json)
pins FeatureBench's `fast` split, its dataset revision, and its runner revision. It
selects six tasks from the 100-task split using a declared stratified procedure:
two small, two medium, and two large tasks, from six different repositories. The
selection uses only dataset metadata and a deterministic SHA-256 rank; gold patches
are not included in the manifest.

Every task is run in two paired arms:

1. `primary-sol-max`: Primary only, fixed to `gpt-5.6-sol/max`;
2. `cco-static`: the same `gpt-5.6-sol/max` Primary with CCO static routing enabled.

The task statement, base commit, and acceptance hash are identical in each pair.
The plan generator balances which arm runs first and produces 12 immutable run IDs.
The pilot is a development set for finding orchestration defects; its score is not a
public model-performance or economic claim.

## Local workflow

Validate and create a deterministic plan without invoking a model:

```text
python -m benchmarks.cco_benchmark validate \
  --manifest benchmarks/manifests/featurebench-pilot-v1.json
python -m benchmarks.cco_benchmark plan \
  --manifest benchmarks/manifests/featurebench-pilot-v1.json \
  > benchmarks/runs/featurebench-pilot-v1.plan.json
```

Check the pinned FeatureBench checkout and host prerequisites before starting:

```text
python -m benchmarks.cco_benchmark preflight \
  --manifest benchmarks/manifests/featurebench-pilot-v1.json \
  --featurebench-root <FEATUREBENCH_CHECKOUT>
```

FeatureBench currently uses POSIX `fcntl` and Docker. On Windows, run its evaluator
through a WSL/Linux distribution after Docker Desktop's Linux engine is ready. Codex
and CCO may remain on the Windows host while task workspaces are bind-mounted for
evaluation.

After a run, collect usage from the Primary thread UUID. The command follows only
the exact root and its direct CCO leaves, rejects nested or non-CCO children, and
uses `last_token_usage` deltas to avoid counting repeated cumulative events:

```text
python -m benchmarks.cco_benchmark usage \
  --root-thread-id <PRIMARY_THREAD_UUID> \
  --codex-home <CODEX_HOME> \
  > <RUN_ID>.usage.json
```

The report separates `sol`, `terra`, and `luna` and records input, uncached input,
cached input reads, cache writes, output, reasoning output, total tokens, and request
count. It is a benchmark artifact, not a runtime accounting ledger.

Bind the usage and evaluator verdict to the planned run, then summarize only complete
pairs:

```text
python -m benchmarks.cco_benchmark record \
  --plan benchmarks/runs/featurebench-pilot-v1.plan.json \
  --run-id <RUN_ID> --usage <RUN_ID>.usage.json \
  --verdict pass --wall-time-seconds <SECONDS> \
  --results-dir benchmarks/results/featurebench-pilot-v1

python -m benchmarks.cco_benchmark summarize \
  --plan benchmarks/runs/featurebench-pilot-v1.plan.json \
  --results-dir benchmarks/results/featurebench-pilot-v1
```

Missing, mismatched, unexpected-model, or overwritten results fail closed. `summarize`
still prints the machine-readable missing IDs but exits non-zero when any planned result
is absent, so automation cannot publish an incomplete study. The report contains only
host-provided usage values and never infers a price.

## Comparison discipline

Use the pilot to classify failures as CCO placement/lifecycle/scope defects, model
capability failures, evaluator failures, or infrastructure failures. Fix only defects
attributable to CCO, turn each confirmed defect into a regression test, and reserve a
held-out task set for the final report. Do not repeatedly tune against the held-out
set or combine runs from different Codex/model revisions.

Record the exact task and acceptance IDs, repository and Codex revisions, model and
effort, wall time, Primary/child generations, route rejections, reviewer findings,
and scope/late-result violations. Publish raw manifests and exclusions alongside any
future public result; do not advertise a fixed outcome from one pilot.

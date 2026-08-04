# Reproducible benchmark protocol

CCO does not collect billing, token, or telemetry data. Benchmarking is an explicit
development activity outside the plugin runtime.

## Comparison groups

Run the same closed task set with:

1. Primary-only execution;
2. fixed Luna/max;
3. fixed Terra/max;
4. CCO static defaults and guarded escalation;
5. fixed Sol/max as a quality reference when supported.

Use at least mechanical implementation, bounded implementation, and guarded/reviewer
tasks. Freeze the repository commit, Codex version, model/effort availability,
acceptance criteria, commands, and starting workspace for every group.

## Record

- exact task and acceptance IDs;
- model and effort actually observed;
- completion and deterministic test verdict;
- wall-clock duration;
- number of fresh generations and continuations;
- reviewer intervention and findings;
- route rejection or guarded escalation;
- scope, ownership, or late-result violation;
- externally observed token/cost only when the host exposes a trustworthy value.

Do not infer missing cost, mix results from different model releases, or advertise a
single percentage as a permanent guarantee. Publish the raw task manifest, result
records, exclusions, and aggregation script together with any promotional claim.

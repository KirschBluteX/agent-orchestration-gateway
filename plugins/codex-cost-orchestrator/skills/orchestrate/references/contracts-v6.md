# CCO v6 capsule contract

Codex native Agent tools remain the only runtime. CCO adds one canonical dispatch
capsule and one compact result envelope.

Capsule and result hashes are integrity identities, not encryption, authorization,
credentials, or secrecy controls.

## Dispatch

`graph_compiler.prepare_dispatch_graph(...)` is the production entry for initial
dispatch. It derives labels from facts, captures one real workspace snapshot, writes
one task-local prepared artifact outside the repository, applies the deterministic
ready-node selector, and returns active plus precompiled fallback native spawn inputs
without spawning them:

```text
agent_type / task_name / fork_turns / model / reasoning_effort / message
```

The three-line message is:

```text
CCO_DISPATCH cco.v6
CAPSULE_SHA256: sha256:<identity>
CAPSULE_JSON: <canonical compact object>
```

The capsule binds:

- logical `kind`, `purpose`, `judgment`, derived assurance, and light/strict/fresh/delta `mode`;
- node and optional review epoch;
- one closed contract plus sorted typed exact/prefix scopes;
- selected model/effort, route rank, and optional compact plan identity;
- baseline, optional graph identity, and review acceptance/evidence/current state;
- execution `task_name`, `fork_turns`, one `generation`, and one `cursor`.

Initial capsules have cursor zero. A continuation includes the previous capsule hash,
one nonempty canonical delta, the same task name/generation, and cursor +1. Material
contract or ownership changes use a fresh capsule and newer generation.

Every prepared graph receives one complete canonical `cco.route-plan.v2`. Its route
key is purpose/judgment/assurance. The graph compiler re-derives assurance from the
node acceptance facts, validates the plan hash, active candidate/rank, route key, and placement, then
stores only `plan_sha256`, rank, and selected pair in each capsule. A caller-supplied
pair or detached plan hash is invalid. The prepared artifact binds the graph manifest,
route-plan identity, all node decisions/contracts/scopes, workspace mode, and exact
snapshot under `graph_sha256`. Each node also binds the finite route identities
derived from the original ranked plan. PreToolUse requires that artifact, exact node,
and one bound route identity match; a rejection therefore advances to an
already-compiled request without changing the graph or baseline.
This detects accidental or tampered substitution on the normal resolver path; it is
not authentication against a malicious Primary, which remains CCO's trusted control
plane.

## Result

The leaf returns exactly:

```text
CCO_RESULT cco.v6
RESULT_SHA256: sha256:<identity>
RESULT_JSON: <canonical compact object>
```

The result binds `dispatch_sha256`, `status`, `disposition`, and a bounded payload.
Status is `complete`, `partial`, or `blocked`. Disposition is `continue`, `retire`, or
`accept`; only a review capsule may use `accept`. A write leaf's complete/retire result
means its turn ended, not that Primary accepted the state.

## Fencing and acceptance

The active ledger owner, generation, cursor/current dispatch identity, and canonical
native task path must all match. A retired or superseded owner cannot become current
again. A workspace-bound ledger row also retains the prepared artifact path, baseline,
graph identity, node scopes, whole-graph scopes, and light/strict mode. SubagentStop
verifies the current state against the whole-graph scope union before accepting a
result envelope; this tolerates disjoint parallel graph changes without authorizing
Primary to misattribute them. SessionEnd removes the task ledger and every prepared
artifact for that session.

Primary acceptance is separate: inspect actual state and produce complete evidence.
A reviewer may return `accept` only for its exact evidence/current state; `continue`
represents a contract-preserving `fix-first`; `retire` represents a closed
non-accepting review such as `rethink`.

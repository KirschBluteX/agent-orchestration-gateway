# CCO v6 capsule contract

Codex native Agent tools remain the only runtime. CCO adds one canonical dispatch
capsule and one compact result envelope.

Capsule and result hashes are integrity identities, not encryption, authorization,
credentials, or secrecy controls.

## Dispatch

`packet_compiler.compile_dispatch(spec)` returns one native spawn input;
`compile_dispatch_batch(nodes, route_plan, native_capacity)` applies the deterministic
ready-node selector and returns several such inputs without spawning them:

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

- logical `kind`, `purpose`, `judgment`, and light/strict/fresh/delta `mode`;
- node and optional review epoch;
- one closed contract plus sorted typed exact/prefix scopes;
- selected model/effort, route rank, and optional compact plan identity;
- baseline, optional graph identity, and review acceptance/evidence/current state;
- execution `task_name`, `fork_turns`, one `generation`, and one `cursor`.

Initial capsules have cursor zero. A continuation includes the previous capsule hash,
one nonempty canonical delta, the same task name/generation, and cursor +1. Material
contract or ownership changes use a fresh capsule and newer generation.

Every initial compiler call receives one complete canonical `cco.route-plan.v1`.
The compiler validates its hash, active candidate/rank, route key, and placement, then
stores only `plan_sha256`, rank, and selected pair in the capsule. A caller-supplied
pair or detached plan hash is invalid. This detects accidental or tampered
substitution on the normal resolver path; it is not authentication against a
malicious Primary, which remains CCO's trusted control plane.

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
again. Primary acceptance is separate: inspect actual state and produce complete
evidence. A reviewer may return `accept` only for its exact evidence/current state;
`continue` represents a contract-preserving `fix-first`; `retire` represents a closed
non-accepting review such as `rethink`.

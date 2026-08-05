# cco.v7 contract reference

`cco.v7` is a clean protocol break. A task that still owns a v6 capsule must start a
new task and prepare a new graph; v6 and v7 owners never share a ledger.

## Prepared graph

`graph_compiler.py` is the only initial preparation entry. For every node it receives:

- logical `role`: explorer, worker, or reviewer;
- complete closure facts;
- acceptance IDs, deterministic coverage, verification strengths, lifecycle events,
  and all risk answers;
- evidenced placement benefits and direct action/check counts;
- contract, generation, typed repository scopes, dependency readiness,
  responsibility, explicit dependencies, and completed graph nodes;
- optional current user model/effort constraints.

It derives assurance, acceptance mode, placement, the dependency-ready frontier,
downstream priority, safe compatible microtask aggregation, static routes, fallback
requests, one whole-graph baseline, and native spawn inputs. The normal CLI commits a
`cco.dispatch-batch.v2` transaction and returns only short spawn references;
`--full` exposes the diagnostic `cco.graph.v4` manifest, full capsules, and route
plan without committing a transaction. No caller supplies a selected route pair or
graph hash.

The prepared-workspace layer has two adapters without changing the capsule: `git`
uses Git/control-state identity; `directory` binds the exact non-Git root. Directory
workers capture the full root, while explorers/reviewers capture declared scopes.
Directory preparation never runs `git init` and fails before content hashing when the
default 20,000-file / 1 GiB budget is exceeded.

## Dispatch transaction

Each short reference binds one immutable full spawn input stored outside the
repository. While a transaction has undispatched work, PreToolUse permits only its
current exact references or one exact abort command. The hook expands a reference,
verifies the prepared workspace and sibling leases, reserves the owner, and then lets
Codex perform the native spawn. A successful sibling remains active if another node
gets a confirmed pre-thread rejection; only that node may use its next precompiled
fallback. Graph-level identity or workspace failure fences the remaining batch.

## Dispatch envelope

The exact wire format is three lines:

```text
CCO_DISPATCH cco.v7
CAPSULE_SHA256: sha256:<64 lowercase hex>
CAPSULE_JSON: <canonical JSON object>
```

The capsule binds:

- protocol, logical role, assurance, node, generation, optional review epoch;
- acceptance mode/reasons and sorted acceptance IDs;
- canonical contract and typed scopes;
- baseline, graph identity, and light/strict/fresh/delta mode;
- selected model/effort, route constraints, route decision identity, plan identity,
  and fallback rank;
- task name, positive partial fork or `none`, and continuation cursor;
- optional evidence/current state; continuation delta and previous capsule identity.

The native spawn arguments must exactly match capsule task name, physical read/write
profile, fork, explicit model, and effort. Both physical profiles are model-neutral,
so current host-supported Luna/Terra/user-pinned routes use the same two profiles.
The writable profile is valid only for worker;
explorer and reviewer use the read-only profile. Reviewer requires an epoch,
independent acceptance, and `fork_turns=none`.

A reviewer normally receives the freshly captured workspace identity as `baseline`.
For a known worker delta, Primary may supply that worker's previously verified state
identity as node `review_baseline`. The compiler emits it as capsule `baseline` and
automatically binds the fresh review snapshot as `current_state`. The artifact and
ledger continue to use the fresh snapshot for pre-spawn and result-time read-only
verification. `review_baseline` is reviewer-only and is an identity reference, not a
stored source copy or proof supplied by an untrusted party.

`CCO_NATIVE_BYPASS v1` is not a capsule. It is a user-authorized one-shot prefix for
an unmanaged native spawn. PreToolUse removes the prefix before Codex sees the task.

## Continuation

A continuation keeps node, contract, generation, route, baseline, graph, scopes,
task name, and owner. It changes mode to `delta`, increments cursor by exactly one,
binds the previous capsule hash, and supplies a non-empty evidence delta. Raw messages
to a live or fenced CCO owner are rejected.

## Result envelope

The exact wire format is:

```text
CCO_RESULT cco.v7
RESULT_SHA256: sha256:<64 lowercase hex>
RESULT_JSON: <canonical JSON object>
```

The result binds the latest dispatch hash, status (`complete`, `partial`, `blocked`),
disposition (`continue`, `retire`, `accept`), and an exact payload:

```json
{
  "blockers": [],
  "changed_paths": ["src/example.py"],
  "deviations": [],
  "evidence": {"A01": "deterministic observation"},
  "failure_signature": null,
  "summary": "bounded outcome"
}
```

Lists are sorted and duplicate-free. Paths are canonical repository-relative paths.
A complete result covers every acceptance ID; partial/blocked results may cover a
subset. Read-only leaves declare no changed paths. A blocked result has a blocker.
Any non-complete, blocked, or deviating result has one stable lowercase canonical
failure signature; a successful exact result uses `null`.

SubagentStop matches the result to the current owner, capsule, role, cursor, graph,
workspace, and node scopes. Declared worker paths must equal the real delta inside
that node's scopes. Only a complete reviewer may return `accept`; that is still a
review claim until Primary confirms the exact state.

## Ledger and cleanup

PreToolUse claims one exact transaction reference and reserves one
`node@contract_rev`; PostToolUse activates exactly one canonical native owner or
releases a confirmed pre-thread rejection. Continuations
reserve and settle one next cursor. Interrupt retires and fences before native
interruption. Terminal results leave small owner tombstones so late results and raw
follow-ups remain fenced across turns.

Full transaction bundles are deleted as their candidates settle. An exhausted route
chain leaves a terminal tombstone so a higher generation may restart at rank one.
Completed sibling scopes retain their lease while any batch reference remains
pending. A graph artifact is deleted only when its transaction has no pending,
dispatching, or active node. Capacity pruning removes only validated terminal
transactions whose native call tombstones have also settled.
Small tombstones are retained across turns until the next SessionStart confirms and
removes terminal prior-session state. Live, unknown, locked, or malformed abandoned
state remains subject to bounded stale cleanup of up to seven days. Incomplete,
blocked, deviating, or retired Luna results set the `node + role` guarded floor for a
newer generation.

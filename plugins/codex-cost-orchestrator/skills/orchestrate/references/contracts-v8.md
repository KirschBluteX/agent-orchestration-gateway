# cco.v8 contract reference

`cco.v8` is a clean protocol break. A task that still owns a v6 or v7 capsule must
start a new task and prepare a new graph; older owners never share a v8 ledger.

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
requests, one whole-graph baseline, and native spawn inputs. Closure and placement
are derived once for the ready graph; callers never run a per-node model-classifier
request. A one-action context-partition node is child-eligible only with both
`capsule:self-contained` and `context:history-not-required` evidence. The normal CLI commits a
`cco.dispatch-batch.v2` transaction and returns only short spawn references;
`--full` exposes the diagnostic `cco.graph.v5` manifest, full capsules, and route
plan without committing a transaction. No caller supplies a selected route pair or
graph hash.

A CLI reviewer node may replace repeated worker-state fields with `review_source`
(`node` plus `contract_rev`). It must identify one terminal worker row in the current
task ledger. The compiler derives reviewer role, guarded acceptance facts, exact
scopes, `review_baseline`, source evidence, no-history fork, and default selection;
Primary still supplies the reviewer node, epoch, and closed semantic review contract.

The prepared-workspace layer has two adapters without changing the capsule: `git`
uses Git/control-state identity; `directory` binds the exact non-Git root. Directory
workers capture the full root, while explorers/reviewers capture declared scopes.
Directory preparation never runs `git init` and fails before content hashing when the
default 20,000-entry / 1 GiB budget is exceeded.

## Dispatch transaction

Each short reference binds one immutable full spawn input stored outside the
repository. While a transaction has undispatched work, PreToolUse permits only its
current exact references or one exact abort command. The hook expands a reference,
verifies the prepared workspace and sibling leases, reserves the owner, and then lets
Codex perform the native spawn. A successful sibling remains active if another node
gets a confirmed pre-thread rejection; only that node may use its next precompiled
fallback. Graph-level identity or workspace failure fences the remaining batch.

The dispatch fast path is one compiler call followed by all ready native spawns in the
same model turn. Primary then waits for a completion, user message, blocking input, or
the protection timeout; progress-only turns are not part of the protocol.

Normal completion, failure, and interruption are established only by authoritative
native terminal events. A Codex Desktop restart is the explicit host interruption
boundary handled by `SessionStart`, which retires and fences active or dispatching
children as `host_restart`. Protected or unreadable progress content, no workspace
delta, no commentary, and elapsed time do not settle a node. A protection timeout
wakes Primary for recovery but preserves the owner unless the host also reports a
terminal state. Opaque protected content, including `reasoning` objects carrying
`encrypted_content`, remains in the host's typed field and must never be copied into
a plain `send_message` or `followup_task` string.

## Dispatch envelope

The exact wire format is three lines:

```text
CCO_DISPATCH cco.v8
CAPSULE_SHA256: sha256:<64 lowercase hex>
CAPSULE_JSON: <canonical JSON object>
```

The capsule binds:

- protocol, logical role, assurance, node, generation, optional review epoch;
- the canonical absolute `workspace_root` captured at graph preparation;
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
workspace root, task name, and owner. It changes mode to `delta`, increments cursor by exactly one,
binds the previous capsule hash, and supplies a non-empty evidence delta. Raw messages
to a live or fenced CCO owner are rejected.

The capsule `workspace_root` must exactly match the prepared graph artifact and
dispatch transaction repository. It never comes from a host cwd or SubagentStop
event. A leaf works from that root or returns blocked if the root is unavailable.

## Result envelope

The exact wire format is:

```text
CCO_RESULT cco.v8
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

SubagentStop maps a current native thread UUID to the canonical owner using the
bounded first `session_meta` record from `agent_transcript_path`; older hosts that
send the canonical owner directly remain compatible. It then matches the result to
the current owner, capsule, role, cursor, graph, workspace, and node scopes. Declared worker paths must equal the real delta inside
that node's scopes. Workspace verification uses the prepare-time repository bound in
the task claim, not the event's potentially parent-directory `cwd`. Only a complete reviewer may return `accept`; that is still a
review claim until Primary confirms the exact state.

SubagentStop is authoritative native terminality. Every valid result retires the
dispatch transaction, including `continue`; the TaskLedger alone retains a
continuable owner for an explicit later capsule. A structurally or semantically
invalid result is retired and fenced in the same callback, so validation never forces
the leaf to generate a second formatting-only response.

## Ledger and cleanup

PreToolUse claims one exact transaction reference and reserves one
`node@contract_rev`; PostToolUse activates exactly one canonical native owner or
releases a confirmed pre-thread rejection. Continuations
reserve and settle one next cursor. Interrupt retires and fences before native
interruption. A validated result keeps a bounded review seed (status, disposition,
payload, and evidence) in the task row so `review_source` can bind it without reading
the repository again. Terminal results leave owner tombstones so late results and raw
follow-ups remain fenced across turns.

Full transaction bundles are deleted as their candidates settle. An exhausted route
chain leaves a terminal tombstone so a higher generation may restart at rank one.
Completed sibling scopes retain their lease while any batch reference remains
pending. A graph artifact is deleted only when its transaction has no pending,
dispatching, or active node. Capacity pruning removes only validated terminal
transactions whose native call tombstones have also settled and never prunes a
fenced transaction that still contains an active sibling. Cross-session cleanup
checks the corresponding transaction before removing a terminal TaskLedger or graph
artifact.
Small tombstones are retained across turns. On a Codex Desktop restart, the next
SessionStart retires and fences active or dispatching children as `host_restart`
interruptions before removing terminal prior-session state. Unknown, locked, or
malformed abandoned state remains subject to bounded stale cleanup of up to seven days. Incomplete,
blocked, deviating, or retired Luna results set the `node + role` guarded floor for a
newer generation.

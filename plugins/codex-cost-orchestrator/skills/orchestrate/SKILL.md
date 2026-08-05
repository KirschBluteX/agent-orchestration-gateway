---
name: orchestrate
description: >-
  Default role-aware router for medium or large Codex analysis, implementation,
  refactoring, fixes, and acceptance. Keeps Primary authoritative, dispatches only
  closed work through native Agents, prefers eligible Luna/Terra static routes, and
  enforces compact cco.v7 lifecycle and exact-state evidence without runtime network
  requests, billing history, or a second Agent runtime.
---

# Codex Cost Orchestrator

CCO is implicit when installed. The user does not need to name this skill. Primary
owns user intent, unresolved choices, graph conflicts, final integration decisions,
and acceptance. A closed integration patch may be delegated like any other worker
contract; authority does not move with the patch.

Every ordinary native Agent spawn must use a prepared `cco.v7` request. Only an
explicit user instruction to use native behavior authorizes a message beginning with
`CCO_NATIVE_BYPASS v1`; the hook removes that marker before dispatch, and Codex then
uses native inheritance. Never infer bypass permission.

Read `references/runtime-gates.md` for installation, trust, configuration, capacity,
or recovery detail. Read `references/contracts-v7.md` before a continuation or an
independent review. Keep reference prose out of leaf messages.

## Close, place, route

Make **one graph-level decision** after the user goal, repository facts, and
acceptance contract are closed. The graph decision derives assurance, placement,
and acceptance for the ready set once; it is the only classifier pass. There is
**no per-node model classifier** and no repeated model request merely to choose a child.
The compiler validates and derives every node locally inside that one graph pass.
Put facts shared by several nodes in graph `defaults`, then describe each node only
by its contract, typed scopes, dependencies, and real responsibility differences.

Assign one logical role:

- `explorer`: bounded read-only inspection or probe;
- `worker`: bounded workspace mutation;
- `reviewer`: fresh independent acceptance.

Derive one assurance from facts, never from model reputation:

- `mechanical`: every permitted choice is acceptance-equivalent and verification is deterministic;
- `bounded`: the contract is closed, but permitted implementation choices can affect the result;
- `guarded`: risk, manual/nondeterministic evidence, failure, deviation, scope surprise, routing mismatch, Primary-owned change, or review work is present.

An unresolved objective, interface, owner, criterion, acceptance condition, or
decision space stays in Primary.

Choose a child only when the contract is closed and at least one evidenced structural
benefit exists: `parallel_ready`, `context_partition`, `closed_chain`,
`runtime_isolation`, `independent_evidence`, `context_recovery`, or
`explicit_delegation`. A task with at most one direct action and one direct check stays
in Primary when `closed_chain` is its only benefit. File count, price, model name,
estimated tokens, or a vague claim that delegation is helpful never proves placement.

The single-action fast path is deliberately narrow. A one-action/one-check worker may
use `context_partition` only when its capsule carries both
`capsule:self-contained` and `context:history-not-required`; otherwise it remains in
Primary. A low-risk open-ended document task may first be closed by Primary with a
minimal outline and deterministic acceptance, then send only the document contract to
the same fast path. This keeps long history out of the leaf without handing unresolved
content choices to it.

Re-evaluate closure and ownership only after a material fact changes. Reclaim
duplicate, overlapping, or no-longer-useful work before routing.

## Static local route

Use the current native Agent capability set as authority. Prefer capability metadata
already exposed by the host; use the PATH Codex CLI catalogue only as a fallback.
Resolve the whole ready graph locally through `graph_compiler.py`; routing invokes no
model, Radar service, network request, route cache, token meter, or billing store.

Built-in order:

| Role / assurance | Automatic candidates |
| --- | --- |
| explorer or worker / mechanical | Luna, then Terra |
| explorer or worker / bounded | Terra, then Luna |
| explorer or worker / guarded | Terra only |
| reviewer / any assurance | Terra only |

For an automatic model, choose the first supported effort in `max → xhigh → high`.
Never choose `ultra` automatically. Never choose Sol automatically. A current user pin
wins over project configuration, global configuration, and built-in defaults. Model-
only pins leave effort adaptive; effort-only pins leave model order adaptive; a full
pair has no fallback. A user may explicitly pin Sol or guarded Luna. An unsupported
pin remains in Primary and is reported; it is never silently replaced.

Project policy is read from `.codex/cco.toml` only when the workspace root appears in
`~/.codex/cco.toml` under `trusted_project_roots`. A malformed higher-priority policy
or a route with no supported candidate returns affected nodes to Primary. Automatic
configuration cannot contain Sol, and guarded/reviewer automatic configuration cannot
contain Luna.

Call the graph compiler once after facts are closed. Prefer its compact CLI output so
the route plan and manifest do not enter model context:

```text
python -B <PLUGIN_ROOT>/scripts/graph_compiler.py --repo <REPO> --native-capacity <OBSERVED_CAPACITY>
```

Pass the JSON object on stdin. It contains `nodes` and may contain host `native_catalog` and route
`policy`. A reviewer node may use `review_source` to bind a terminal worker ledger
row; the compiler then reuses its baseline, acceptance IDs, exact scopes, changed
paths, and result evidence without another repository inspection. The compiler
derives decisions, captures one scope-limited workspace
baseline, maximizes useful dependency-ready non-conflicting work up to native
capacity, and returns exact spawn inputs plus precompiled fallbacks. It never spawns.
Git and bounded non-Git directory roots use the same entry and capsule; CCO never
runs `git init`.
Use the write leaf only for `worker`; explorer and reviewer share the read leaf. Use
`fork_turns: none` when repository anchors and the capsule close context; otherwise
use the smallest positive partial fork, never full history.

After a confirmed pre-thread native rejection, use only the next returned fallback.
Do not rescore, recapture the baseline, rebuild the contract, or contact a service.

## Fast dispatch path

Use already-available user, repository-policy, and task facts before acquiring more
context. If they close the graph, do not read the repository again before preparing
it. If one material fact is missing, dispatch one narrow explorer for that fact; do
not make Primary perform an open-ended inspection first.

All three roles use this same preparation entry. Give an explorer one exact question,
repository anchors, and required evidence. Give a worker the already-closed plan node
and exact write scopes; never infer a worker scope. Give a reviewer a closed review
contract plus `review_source` when a terminal worker ledger row is available. Do not
reconstruct facts that the compiler can bind from that row.

For one ready graph, close once, compile once, dispatch every returned ready reference
in the same model turn, then enter one long event wait. Do not insert route
explanations, status checks, file reads, tests, edits, or baseline recaptures between
the compiler result and those spawn calls. The transaction gate makes this sequence
fail closed. A confirmed pre-thread rejection may consume only its already-prepared
fallback in that same dispatch turn.

Use graph-level `defaults` for facts shared by nodes. Do not construct separate graphs
merely to use different models; routing is a compiler output. The compiler performs
one combined route resolution on the normal path. It probes individual nodes only
after that combined operation fails, so a malformed node can be isolated without
making every successful graph pay for duplicate local work.

## Dispatch and wait

CCO adds no concurrency ceiling. Fill observed native capacity only with nodes whose
responsibilities differ and whose typed scopes do not conflict. Merge artificial
splits. A CCO leaf never delegates; Primary owns the complete graph.

After dispatch, Primary may continue only a node already proven unsuitable for a
child when it cannot overlap, conflict with, or depend on active leaf work. Otherwise
call `wait_agent` once and enter one long event wait. Do not poll status, emit progress-only model turns, or
duplicate a leaf. Wake only for completion, blocking input, user input, or the long
native protection timeout.

Only an **authoritative native terminal event** proves that an Agent completed,
failed, or was interrupted. An explicit blocking-input event may wake Primary without
retiring its owner. Unreadable or protected progress, an unchanged workspace, elapsed
time, and missing commentary are not completion evidence. Preserve the owner and wait
again after a protection timeout unless the native runtime reports a terminal state.
Never forward opaque protected payloads through plain-text `send_message` or
`followup_task`; the PreToolUse gate blocks such a type-boundary violation.

## Lifecycle and recovery

The capsule binds role, assurance, acceptance IDs, contract, route, baseline, graph,
typed scopes, context fork, generation, and continuation cursor. PreToolUse verifies
the prepared artifact and reserves ownership; PostToolUse activates exactly one
canonical task path or releases a confirmed pre-thread rejection. A continuation
increments only the cursor and preserves owner, generation, contract, route, and
baseline. Interrupt retires and fences the owner before native interruption.

Current Codex hosts identify SubagentStop with a native thread UUID. CCO maps it to
the reserved canonical owner through the bounded session metadata record and exact
dispatch identity; older canonical owner IDs remain compatible. Every valid
SubagentStop retires the native dispatch transaction. A `continue` disposition keeps
only the TaskLedger owner continuable for an explicit later delta.

Treat every result as a claim. `CCO_RESULT cco.v7` must cover acceptance IDs, declare
exact changed paths, and match the current workspace delta inside that node's scopes.
Only a complete reviewer may return `accept`; explorer and worker never claim Primary
acceptance. Large terminal graph artifacts are deleted immediately. Small owner
tombstones remain across turns for fencing. The next SessionStart immediately removes
validated terminal state from prior sessions; live, unknown, locked, or malformed
abandoned state remains subject to bounded stale cleanup of up to seven days.

Leaf profiles receive the exact three-line result shape before execution. If a result
still fails structural, identity, evidence, scope, or workspace validation, the hook
retires and fences that child in the same terminal callback and returns control to
Primary. It never blocks SubagentStop merely to purchase a second formatting response
from the same child.

Normalize each failure into one stable failure signature. Retry only when new evidence
changes the intervention. A confirmed pre-thread rejection uses a precompiled route
fallback. A transient evidence delta may continue the same owner. Luna quality or
scope failure creates a newer Terra guarded generation. Terra quality failure returns
to Primary for replanning. Never escalate automatically to Sol. Any incomplete,
blocked, or deviating terminal result sets a ledger-enforced guarded floor for the next
generation, even if Primary forgets to record the event.

## Verify and accept

Primary inspects the actual delta, verifies scope attribution, preserves pre-existing
work, and reruns acceptance-critical checks when evidence is semantic, risky,
nondeterministic, incomplete, or state-sensitive. Deterministic low-risk evidence may
be reused only at the same exact workspace and graph state.

Primary acceptance is allowed when every acceptance ID has deterministic evidence at
one unchanged state and no review trigger exists. Use a fresh reviewer only for a real
risk, manual/semantic evidence, failure/deviation, contract or scope surprise,
unresolved integration choice, Primary-owned implementation, or an explicit user
request. Ordinary Primary micro-edits do not trigger review, and a routine child with
complete deterministic evidence **does not require a reviewer**.

A reviewer is a fresh read leaf with `fork_turns: none`. A contract-preserving evidence
delta may continue that reviewer. Architecture, interface, ownership, schema, safety,
or acceptance changes require a fresh review generation. Finish only for the exact
accepted state. When reviewing a known worker delta, use `review_baseline` only for
that worker's previously verified baseline; the compiler captures and binds the
reviewer's current workspace state separately.

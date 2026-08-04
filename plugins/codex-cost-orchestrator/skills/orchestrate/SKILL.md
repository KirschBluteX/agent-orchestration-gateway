---
name: orchestrate
description: >-
  Default purpose-aware router for medium or large Codex analysis, implementation,
  refactoring, fixes, and acceptance. Keeps Primary authoritative, sends only closed
  work to model-neutral native leaves, prefers eligible Luna/Terra routes, and uses
  compact cco.v6 lifecycle and exact-state evidence without billing history.
---

# Codex Cost Orchestrator

CCO is implicit when this skill is installed; the user need not name it. Primary
retains intent, architecture, graph ownership, context policy, integration,
verification, and final acceptance. Codex native Agent tools remain the only runtime.
Integration here means authority: a closed integration patch is ordinary delegated
implementation, while Primary keeps conflict decisions and exact-state acceptance.

Read `references/runtime-gates.md` only for install, route, permission, isolation,
cache, or recovery detail. Read `references/contracts-v6.md` before a continuation
or an independent review. Do not copy reference prose into leaf messages.

## Classify, place, then route

Classify `PURPOSE` as `analysis_inspect`, `analysis_probe`, `implementation`, or
`acceptance`. Derive `JUDGMENT` from closure facts: `routine` means permitted choices
are acceptance-equivalent; `complex` means bounded choices can affect the result but
their criteria and verification are closed; unresolved work stays in Primary.

Choose a child only for at least one concrete benefit: closed execution,
dependency-ready disjoint parallel work, observed context compaction, a self-contained
source partition, runtime isolation, independent evidence, or explicit delegation.
Reclaim duplicate work. Price, files, estimated tokens, and model labels never prove
placement value.

Re-derive closure and placement whenever new facts can change ownership: after a
user resolves a material trade-off, after the first RED test freezes a public
interface, and after architecture plus typed write scopes are frozen. Skip an
inapplicable checkpoint; never repeat it when its facts are unchanged. If the work is
still unresolved or has lost its structural child benefit, keep or reclaim it in
Primary before routing.

Keep an atomic deterministic low-risk edit in Primary. For children, call
`scripts/graph_compiler.py:prepare_dispatch_graph()` once after the graph is closed;
it derives decisions, captures the real task-local workspace artifact, applies
native capacity/conflicts, and returns every native request. Do not construct an
initial capsule through `compile_dispatch()` or `compile_dispatch_batch()` directly.
Use the writable leaf only for implementation; use the read leaf for inspection,
bounded probes, and acceptance. Use `fork_turns: none` when the capsule and repository
anchors close the task; otherwise use the smallest positive partial fork, never full
history.
Before a profile's first use in a task, check only that exact installed read/write
profile with `scripts/install_agents.py --check`; never check unused logical kinds.

## Adaptive route

Exact user model/effort values always win. Otherwise derive one `assurance` value
from each node's existing acceptance facts with `derive_route_assurance()` and call
`routing_catalog.py resolve-plan` once for the whole graph. Every route request must
carry that derived value; do not invent a subjective model-suitability label.
Routing is local and invokes no model or Agent. It intersects native capabilities
with Radar IQ strictly above 90, validates sample/cohort/coverage, and applies a
Wilson-aware Pareto utility over quality, resource use, time, and uncertainty. When
backend metadata is present, explicitly known `multi_agent_version=v1` and `v2`
entries are eligible; unmarked or unknown versions are not guessed to be spawnable.
Deterministic routine assurance permits Luna/Terra competition. A deterministic
complex route admits Luna only when its Wilson lower bound is also strictly above
the IQ floor. Guarded assurance excludes Luna from automatic selection; an exact
user-fixed pair remains authoritative.

Prefer eligible Luna/Terra for workers and reviewers. Admit Sol as the automatic
leader only when no eligible Luna/Terra exists or Sol's Wilson lower bound is strictly
above the best eligible Luna/Terra upper bound. A user-fixed Sol route remains exact.
Bind only the compact plan identity and selected pair into the capsule; do not pass
the metric table to a leaf.

Pass the complete validated plan and all closed nodes to `prepare_dispatch_graph()`.
A caller may not supply a selected pair or plan hash separately. The graph compiler
applies observed native capacity plus responsibility/access/scope conflicts and
returns native spawn inputs; it never spawns Agents itself. Placement is decided
first, so only child-eligible contracts enter dispatch selection.

The Radar TTL is one hour. A source-age-valid LKG up to 72 hours old can dispatch
immediately with `needs_refresh`; refresh serves a later graph. Fully fixed pairs skip
Radar. The prepared graph returns active requests plus every node's complete legal
`fallback_dispatches`; after a confirmed adaptive pre-thread rejection, take only the
next precompiled request. Never rescore, recompile the capsule, recapture the baseline,
or rebuild a contract. Explanations are hidden unless the user requests routing
diagnosis. Only one refresh process may be reserved for an exact stale snapshot;
success removes its request artifact, while a failed launch or refresh is bounded by
a short retry lease.

## Parallelism and quiescence

Do not impose a CCO concurrency cap. Use the host's observed native capacity and fill
it with dependency-ready nodes whose responsibilities differ and whose typed scopes
do not overlap. Merge artificial splits. Concurrent dispatch is not itself an
independent-review trigger.

After dispatch, continue only useful Primary work that cannot duplicate or conflict
with a leaf. Otherwise wait event-first. Do not poll lists/status or issue short waits,
and suppress ordinary leaf progress. Wake only for blocking, completion, user input,
or a long native protection timeout.

## Capsule and lifecycle

The capsule binds purpose, judgment, derived assurance, mode, contract, route, baseline, graph identity,
typed scopes, acceptance/evidence when applicable, context fork, one `generation`,
and one continuation `cursor`. PreToolUse also resolves the exact prepared workspace
artifact and binds its whole-graph scopes into the ledger. SubagentStop verifies the
current workspace against that graph union so disjoint concurrent nodes do not
produce false positives; Primary still attributes each actual delta to its owner.
Use `compile_continuation()` for a same-owner evidence-bearing delta; it increments
only the cursor and keeps the generation.

The external task-local ledger is a small single-flight cursor. PreToolUse reserves;
PostToolUse activates or releases a confirmed pre-thread rejection; continuation
preflight reserves its next cursor; interrupt retires the current owner; SubagentStop
rejects stale results; SessionEnd removes residue. It is not acceptance or a second
runtime.

Do not use fixed retry/follow-up limits. Continue or create a newer generation only
when new actionable evidence changes the intervention. Normalize failure signatures;
never repeat an unchanged failed prompt. Retire/fence the old owner before transfer.
An evidence-backed Luna execution failure, deviation, or scope surprise must add the
corresponding acceptance event and use a newer guarded generation; do not continue or
retry another Luna effort for that failure. Do not waive the Sol advantage gate.

## Verify and accept

Treat every leaf result as a claim. Match its exact owner and dispatch identity,
inspect the real delta, verify the scope subset, preserve pre-existing work, and rerun
acceptance-critical operations in Primary. A writable leaf cannot return an
acceptance disposition.

Primary acceptance is eligible when all declared risks are absent and deterministic
evidence covers every acceptance ID at one unchanged state, including complex and
multi-node graphs. Require an independent review for real risk, manual/semantic
evidence, a contract gap, scope/routing surprise, failure/deviation, unresolved
integration judgment, Primary-owned implementation, or an explicit request.

A fresh reviewer is a read leaf with `fork_turns: none`. `fix-first` may continue the
same exact reviewer only for a contract-preserving evidence delta; material contract,
architecture, interface, ownership, safety, schema, or acceptance changes create a
new fresh generation. Report read-only isolation only when runtime metadata proves
it. Finish only for the exact accepted state.

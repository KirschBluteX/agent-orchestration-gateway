---
name: orchestrate
description: "Default cost-aware implementation router for Codex. Use implicitly for medium or large features, bug fixes, refactors, multi-file or cross-module changes, risky code changes, or work needing delegated implementation. Keep read-only requests and confidently atomic low-risk edits in Sol, and upgrade as soon as scope, uncertainty, or verification risk expands. Uses a Sol control plane, user-selectable or IQ-gated adaptive worker model and effort, hash-bound contracts and inputs, bounded native subagents, generation-fenced leases, primary evidence, and structurally gated fresh/delta review epochs."
---

# Codex Cost Orchestrator

Act as the control plane. Keep user intent, architecture, decomposition, routing,
state ownership, verification, and final acceptance in the primary Sol session. Send
closed implementation volume to the selected routine or complex leaf role. Let the
user select each worker model and reasoning effort; apply cost-aware route defaults
only when the user has not selected or requested native resolution.

Read [references/worker-core.md](references/worker-core.md) before the first
orchestrated spawn. Read [references/runtime-gates.md](references/runtime-gates.md)
only for runtime/profile mismatch, route mismatch, permission/isolation concern, or
workspace recovery. Read [references/contracts-v4.md](references/contracts-v4.md) only
before concurrent Multi, a retry/live follow-up, or an independent review epoch. Do not
repeat reference prose in worker messages.

## Default routing decision

Apply this decision whether the skill was selected implicitly or invoked explicitly.
Skill selection makes the router active; it does not by itself require a spawn.
Classify by uncertainty, coupling, and impact. File count and diff size are signals,
not decisive rules.

Use the no-write route for read-only analysis, explanation, planning, status checks,
and diagnosis when the user did not ask for a fix. Answer in Sol without opening a
work graph, runtime preflight, or review epoch.

### Direct fast path

Keep an implementation in the primary Sol session only when every condition holds:

- the desired result is unambiguous and mechanically determined;
- the change is atomic, confined to one bounded area, and expected to stay small;
- it does not alter a public interface, schema, migration, dependency boundary,
  authentication, authorization, security control, concurrency behavior, build,
  release, or destructive data path;
- deterministic verification is contract-defined;
- no enumerated `RISK_FLAGS` apply: `authentication_authorization`, `build_release`,
  `concurrency`, `dependency_boundary`, `destructive_data`, `external_side_effect`,
  `migration`, `nondeterministic_verification`, `public_interface`, `schema`, or
  `security`;
- no independent parallel node or specialist implementation judgment is useful;
- one deterministic focused verification can provide proportionate acceptance evidence;
  and
- the worktree state and ownership are clear enough to avoid lease coordination.

State the direct-route reason briefly, inspect the actual delta, and run focused
verification. Before the first write, record an exact `DIRECT_BASELINE` and the
pre-existing changed paths so later task edits remain distinguishable from user work.
Use the workspace-state helper when available. Do not create workers or a review epoch
merely to satisfy ceremony.

### Mandatory orchestration

Use the full work-graph path when any direct-path condition is false or uncertain.
This is the default for medium or large implementation, multiple independently
verifiable nodes, cross-module work, public contracts, security-sensitive or
concurrent behavior, uncertain bug causes, broad regression surfaces, parallelizable
work, or changes that benefit from independent acceptance.

### Upgrade before continuing

Reclassify a direct task before expanding it. Upgrade to the full orchestration path
when scope reaches another bounded area, a material interface or ownership decision
appears, the first verification fails for a non-trivial reason, diagnosis becomes
systemic, the regression surface grows, or independent review becomes valuable.

Retain the original `DIRECT_BASELINE`. Freeze and inspect the existing Sol delta,
register it as an explicit `sol` contract node with hashed exact/prefix write scopes
and a state identifier, then use the current state as each new worker lease baseline.
The final accumulated review
or primary acceptance closure must compare the finished state with `DIRECT_BASELINE`
and include both the frozen Sol delta and every later worker delta. A Sol-owned node
forces independent acceptance. Do not let already-written work disappear behind a
rebased lease or quietly continue under the old direct-route assumption.

### User override

Naming or invoking `$codex-cost-orchestrator:orchestrate` selects this router; it does
not alone force delegation. A request for the "full CCO flow", delegated worker lanes,
or a review epoch forces the orchestrated path. An explicit no-delegation,
single-agent, or direct-execution constraint keeps work in Sol and overrides mere
skill selection. If the same effective instruction simultaneously requires full CCO
and forbids delegation, stop before writing and ask the user to resolve the conflict.
A user override does not waive higher-priority instructions or authorize false claims
about review, isolation, tests, or acceptance evidence.

## Gate the session once

This gate applies only to the orchestrated path. Require a primary Sol session, each
worker lane actually used by the work graph, and the reviewer only when the latest
acceptance decision is `independent`:

- `cost_orchestrator_routine_worker`
- `cost_orchestrator_complex_worker`
- `cost_orchestrator_reviewer` when independent acceptance is required

Run the companion-profile exactness check for the active workspace once per Codex task,
not before every node. It must reject visible differing same-name roles in user or
project config layers.
Cache successful profile checks in a task-local checked set unless a role is missing,
runtime evidence conflicts, or the installed profiles change. Worker templates must be
model-neutral; the reviewer remains pinned to Sol High and requests read-only. Require
native spawn to expose the exact `agent_type` and every model/effort override field
needed by the selected policy.

When a primary-to-independent upgrade occurs, check whether
`cost_orchestrator_reviewer` is already in that cached checked set. If it is not, run
the non-mutating
`python scripts/install_agents.py --workspace <repo> --check --profile reviewer`
check and record success before any fix or review. A failed reviewer profile check stops
both actions until the reviewer profile is available; do not defer this check to the
fresh reviewer spawn.

Record `MODEL_POLICY` and `EFFORT_POLICY` independently as `user`, `route_default`, or
`native`. User values win and are never replaced by the adaptive selector. A native
dimension is omitted from spawn. At each new work-graph creation, resolve every lane
that still has a `route_default` dimension with the shipped routing helper; do not
refresh or change that decision while the graph is running:

```text
python scripts/routing_catalog.py resolve --lane <routine|complex> --packet \
  [--fixed-model <user-model>] [--fixed-effort <user-effort>]
```

The helper intersects the current bundled Codex capability catalog with a validated
CodexRadar snapshot, requires observed IQ strictly above 90 plus sample/cohort
coverage, computes a Wilson-aware strict Pareto frontier, and selects by fixed-anchor
quality/cost/time utility. Cost and time penalties remain monotonic above their
anchors. Routine favors cost; complex favors quality. The default refresh TTL is one
hour (minimum configurable value ten minutes). Raw responses remain in memory; disk
keeps only one normalized LKG for at most 72 source-hours and one small hysteresis
state, with no history. A new winner must persist across two distinct measurement
snapshots unless the active route becomes ineligible or policy changes. Treat the
compact score explanation as internal; do not show it unless the user asks or routing
diagnosis requires it.

Bind the complete canonical route decision in `ROUTING_DECISION_JSON` and its
`decision_sha256` as the `routing_decision` content anchor in `INPUTS`. If one
dimension is `user`, pass it as the matching fixed constraint. Do not mix a
`route_default` dimension with a `native` dimension because the pair cannot be
validated before spawn. When both dimensions are `user` or `native`, carry
`ROUTING_DECISION_JSON: none` and no routing-decision anchor. Native spawn validation
is still final.

Observe every usable worker's role/model/effort before accepting its work. Exact user
or selected route-default values must match; native values must be observable and
stable. Never silently substitute a role, model, or effort.

A native spawn rejection before it returns a usable canonical task path creates no
owner and consumes no worker attempt or lease generation. An explicit user selection
stops there. A route default may use `routing_catalog.py advance` only for the next
candidate in the hash-bound fallback order and must build a newly hashed input closure.
If Radar and a source-age-valid LKG are both unavailable, fail closed instead of using
an unverified static default. Once a usable worker exists, any
routing mismatch is fenced, consumes that run, and cannot fall back in place.

If a required role is missing or mismatched, fail closed before delegated writes. Name
the failed role and the install/check action, but do not edit `CODEX_HOME` or downgrade
to an ordinary subagent automatically. Resume in a new task after installation, or use
Sol alone only after an explicit user route override.

## Build the work graph

Resolve material ambiguity before delegation. Create the coarsest independently
verifiable nodes, each with:

- a stable `NODE`, `CONTRACT_REV`, and canonical `CONTRACT_SHA256`;
- a chained `INPUT_CLOSURE_SHA256` covering dispatch identity and bounded inputs;
- dependencies and one `routine`, `complex`, or explicit Sol-owned lane;
- a baseline-bound behavioral `LEASE`, `LEASE_GENERATION`, and `STOP_GENERATION`;
- explicit hashed `exact` or `prefix` write scopes, interfaces, discretion,
  exclusions, enumerated `RISK_FLAGS`, and stable acceptance IDs;
- one implementation owner per acceptance ID and primary-Sol verification IDs;
- finite attempt and follow-up limits fixed before dispatch; and
- focused verification with concrete expected evidence.

Cap each worker contract revision at three runs; cap each individual worker run at two
live follow-ups. Cap each review epoch at two fresh attempts; cap each reviewer thread
at two delta follow-ups. Smaller limits are valid; never raise a cap to continue.

Before the first worker spawn, build and validate an immutable full graph manifest and
an append-only acceptance chain. Recompute every contract and manifest hash, require
each acceptance owner to equal the node that declares it, reject duplicate global
acceptance or verification IDs, reject overlapping or portable case-alias scopes, and
limit the graph-wide distinct scope union to 128. Bind `GRAPH_MANIFEST_SHA256`, the
canonical `ACCEPTANCE_CHAIN_JSON`, and `ACCEPTANCE_CHAIN_SHA256` into every worker
initial closure. Use a `prefix` scope for one bounded
generated directory instead of enumerating more than 128 files. Never derive exact
versus prefix authority after hashing.

Construct each canonical JSON preimage in Sol before formatting the readable packet;
`protocol_hash.py` validates and hashes the submitted preimage, but does not construct
it. The trusted PreToolUse guardrail will rebuild the canonical contract and initial
input preimages from the readable packet and recompute every applicable protocol hash
before native spawn. Include the exact `fork_turns` value in the input preimage; a
different context fork is a different authority closure.

Treat a lease as a control-plane promise, not an OS filesystem lock. Do not issue
overlapping active leases. Preserve pre-existing dirty and untracked work. If an owned
path changes unexpectedly, stop the node and re-establish its baseline rather than
merging concurrent edits by guesswork.

Maintain one single-flight ledger row per `NODE@CONTRACT_REV`: hashes, status, lane,
run, counters, canonical task path, routing request and observation, lease and
generations, baseline, dependencies, failure signatures, and accepted state. Before
every spawn, confirm that the same revision has one owner at most and is not already
accepted. A contract-preserving follow-up continues the recorded canonical task path;
a new run consumes an attempt and must first fence and retire the old owner. Only the
primary Sol session updates this ledger.

## Select acceptance mode structurally

Record revision 1 in the acceptance chain before dispatch. `primary` is eligible only
for one routine contract with deterministic contract-defined verification, no public
interface/schema, security, authorization, concurrency, build, release, migration, or
destructive-data impact as enumerated contract risk flags, no Sol-owned change set, no concurrent Multi, and no explicit
request for independent review. Primary Sol still inspects the actual delta, reruns
acceptance-critical verification, builds complete passing evidence, and accepts only
the unchanged exact state.

Use `independent` before dispatch for every multi-node or complex graph, concurrent
Multi, Sol-owned node, public or safety-sensitive boundary, non-deterministic/manual
acceptance, or explicit review request. Monotonically upgrade `primary` to
`independent` if a retry, live follow-up, deviation, scope surprise, routing mismatch,
verification failure, partial result, blocked result, or material judgment appears. Append a
revision-2 decision whose previous hash names revision 1; bind the resulting chain in
all later packets and evidence. Never erase history or downgrade after the first worker
spawn. This is a structural gate, not a cost,
token, latency, or predicted-quality score.

## Route by contract closure

- Use the routine role when the contract fully determines the result.
- Use the complex role when architecture and interfaces are fixed but bounded
  algorithms, debugging, compatibility, security, or broad implementation judgment
  remains.
- Keep work in Sol while objective, architecture, public interfaces, ownership, or
  acceptance remains unresolved.

Routine and complex describe contract closure and judgment, not fixed models. Apply the
per-node model and effort policy only after the lane is structurally selected.

## Gate concurrent Multi structurally

Do not split tightly coupled work merely to create more cheap turns. Spawn workers
concurrently only when at least two nodes are dependency-ready, every contract and
input closure exists, leases are pairwise disjoint, acceptance ownership is complete,
the acceptance chain already ends in independent mode, and native capacity
for at least two worker threads is observable and available. Otherwise use a
single worker, serialize still-disjoint nodes, merge overlapping or artificial nodes
under one owner, or keep unresolved work in Sol. File count, diff size, price, tokens, latency, request count, and predicted quality
are advisory signals, never hard Multi gates.

## Compile bounded context

Use `fork_turns: none` when the `CCO_WORK` packet and repository anchors are sufficient.
Otherwise choose the smallest positive integer string that includes the earliest still
binding parent turn. The initial cco.v4 work packet and latest valid hash-chained
follow-up supersede conversational history. A follow-up is a same-thread delta and is
never sent as a standalone packet to a new or cold worker.

Never use `fork_turns: all` with these custom roles. CCO uses only `none` or the
smallest positive integer needed for correctness; other full-history and override
combinations vary by Codex surface and are outside this contract. A positive partial
fork rebuilds child context, so never claim that it produced a cache hit.

Keep stable policy in role TOMLs and variable facts in packets. Do not restate the
environment, permissions, plugin list, tool schema, role description, or complete diff.

## Dispatch through native agent tools

Use deterministic lowercase `task_name` values:

```text
work_<node>_<lane>_rNN
review_eNN_rNN
```

Initial routine example:

```text
task_name: work_n01_auth_routine_r01
agent_type: cost_orchestrator_routine_worker
fork_turns: none
model: <selected decision model>
reasoning_effort: <selected decision effort>
message: <CCO_WORK cco.v4 packet>
```

This example uses an adaptive route default. Replace either value with the user's exact
selection or omit only a fully native pair. If the first route-default proposal is
rejected before a thread exists, advance only through its bound fallback order; do not
count that proposal as a worker run. Use `cost_orchestrator_complex_worker` for the complex
lane. Record the returned canonical task path and address that exact path thereafter.
Native V2 wait is targetless: after a mailbox update, identify the source against the
single-flight ledger before accepting a result or issuing any target-bound operation.

## Continue live; respawn after completion

For a contract-preserving correction, verification request, or completion request:

- use `send_message` only while the worker is observably still running;
- keep the contract, run, routing, lease, and both generations unchanged;
- increment the consecutive `FOLLOWUP` within its finite limit; and
- create a new `INPUT_CLOSURE_SHA256` chained to
  `PREVIOUS_INPUT_CLOSURE_SHA256` for the `CCO_WORK_FOLLOWUP cco.v4` delta; and
- include compact canonical `BINDING_JSON` for the still-binding worker object so the
  continuation hook can recompute the new closure; and
- include the current canonical acceptance chain; a follow-up from primary must append
  the hash-linked `worker_followup` upgrade before dispatch; and
- bind the complete acceptance-ID set and exact canonical native `TARGET`, then address
  that same target with `send_message`.

Treat this as live same-session steering, not durable thread storage. Current native
V2 may transparently reload a known completed agent, and that reload does not replay
the original per-spawn model and effort overrides. Because worker profiles are
model-neutral, never use `followup_task` for a completed or idle model-neutral worker.
The continuation guardrail validates live `send_message` packets, and worker
`followup_task` is structurally blocked.
After a worker result arrives, or when residency is not observable, inspect its delta,
fence and retire that owner, then start a new `RUN` with a complete `CCO_WORK cco.v4`
packet and explicit routing. Fold the bounded correction into the new packet, consume
an attempt and a new lease generation, and never send a standalone worker delta.

The reviewer remains role-pinned to Sol High, so a contract-preserving delta review may
use the bounded `followup_task` flow described in the review-epoch section. Recheck its
effective route and read-only evidence after every turn; use a bounded fresh review
attempt if the reviewer cannot be recovered consistently.

Start a new worker run when role, model, effort, non-follow-up input, or a material
contract field changes. Before native interrupt, increment `STOP_GENERATION`; then
interrupt the old owner. `Interrupted` is not terminal: never steer that fenced path
again, and observe it idle or terminal before inspecting its partial delta, revoking
the lease, and issuing a new `LEASE_GENERATION`. Never overlap owners. Enforce finite
attempts across model and input changes. Recompute `FAILURE_SIGNATURE`; classify native
transport, context-capacity, auth/policy, sandbox, bad-request, active-turn, fence, and
verification failures before retrying. A recurrence for the same contract requires a
materially different intervention, not an unchanged failed prompt.

## Verify actual state

Treat worker reports as claims. Before integrating a node:

1. Match the canonical task path, active owner, node/revision/hash, latest input
   closure, run, counters, lease, and both generations to the single-flight ledger.
2. Match observed role/model/effort to the recorded routing policy.
3. Compare current state with the recorded baseline.
4. Confirm changed paths are a subset of the lease and pre-existing work survived.
5. Inspect the actual diff and material judgment calls.
6. Run or directly observe acceptance-critical verification in the primary session;
   record its operation, exit status, observed outcome, and exact artifact hashes.
7. Revalidate the immutable graph manifest and complete append-only acceptance chain.
8. Record exactly one primary evidence record for every globally unique required
   verification, matching its contract-defined operation, acceptance IDs, and owner at
   one `CURRENT_STATE`; then compute `GRAPH_MANIFEST_SHA256`,
   `ACCEPTANCE_CHAIN_SHA256`, and `EVIDENCE_SHA256`.

Do not accept a node from report text alone. Hooks and behavioral leases are
detect-only controls; stop generations reject stale reports but cannot prevent late
writes. Recheck the workspace even for a rejected result.

## Run an independent review epoch when required

When the acceptance chain ends in `primary`, skip the reviewer spawn and accept only if
all primary-mode eligibility remains true, every evidence record passes, and the
workspace still equals the evidenced `CURRENT_STATE`.

When the chain ends in `independent`, after the accumulated implementation passes
primary verification, start a fresh review epoch with a new reviewer,
`fork_turns: none`, and `MODE: fresh`. Supply the
fixed contract references, `GRAPH_MANIFEST_SHA256`, `ACCEPTANCE_CHAIN_SHA256`, complete acceptance-ID array,
baseline/current state, allowed paths, actual delta reference, `EVIDENCE_SHA256`, the
exact compact canonical `EVIDENCE_JSON` preimage containing the full graph and decision history,
and risks. Require the reviewer to recompute every contract hash, the manifest, every decision and chain hash, and
the evidence hash, and to match its contract references, acceptance IDs, and current
state before judgment. Hash this review input closure. Require `ship`, `fix-first`, or
`rethink` for the exact closure and reviewed state.

Count each fresh reviewer thread as a bounded `ATTEMPT` inside the fixed epoch and
each delta turn as a bounded `FOLLOWUP`. A cold reviewer uses another fresh attempt;
it never receives a standalone delta packet. The SubagentStop hook's one envelope-only
repair is not a review follow-up and cannot change evidence or state.

When `fix-first` names bounded contract-preserving fixes, delegate them to the owning
worker, verify them, then use `followup_task` on the same reviewer with a
`CCO_REVIEW_DELTA cco.v4` packet and `MODE: delta`. Refresh all primary evidence for
the new current state, supply its new canonical `EVIDENCE_JSON`, require the same
recomputation, and chain a new review input closure. Keep review attempts and
follow-ups finite.

Start a new fresh review epoch when goal, architecture, public interfaces or schemas,
safety constraints, write ownership, exclusions, or acceptance criteria change.
`rethink` always starts a new epoch. Any mutation after `ship` invalidates that verdict
and evidence closure until a delta or fresh review accepts the new exact state.

Call a review OS-enforced read-only only when observed runtime sandbox metadata says
`read-only`. Apply the broader-host procedure in the runtime-gates reference otherwise.

## Finish by route

- No-write: answer the request directly. Do not claim an implementation, worker, or
  review result.
- Direct: compare the final state with `DIRECT_BASELINE`, inspect every task-owned
  path, run focused verification, and report that no worker or review epoch was used.
- Orchestrated: report completion only when every Sol-owned and worker-owned change set
  is integrated, every contract-required verification has exactly one passing primary
  evidence record in the exact current contract/evidence closure, and that unchanged
  state either remains eligible for hashed `primary` acceptance or has a matching
  independent `ship` verdict. Summarize changed paths, decisive verification,
  acceptance mode, observed worker routing, reviewer isolation when used, and residual
  risk.

Never apply orchestrated-path acceptance claims to a no-write or direct result.

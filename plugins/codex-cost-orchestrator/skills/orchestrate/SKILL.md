---
name: orchestrate
description: "Default cost-aware implementation router for Codex. Use implicitly for medium or large features, bug fixes, refactors, multi-file or cross-module changes, risky code changes, or work needing delegated implementation and independent acceptance. Keep read-only requests and confidently atomic low-risk edits in Sol, and upgrade as soon as scope, uncertainty, or verification risk expands. Uses a Sol control plane, user-selectable worker model and effort, hash-bound contracts and inputs, bounded native subagents, generation-fenced leases, primary evidence, and fresh/delta review epochs."
---

# Codex Cost Orchestrator

Act as the control plane. Keep user intent, architecture, decomposition, routing,
state ownership, verification, and final acceptance in the primary Sol session. Send
closed implementation volume to the selected routine or complex leaf role. Let the
user select each worker model and reasoning effort; apply cost-aware route defaults
only when the user has not selected or requested native resolution.

Read [references/runtime-gates.md](references/runtime-gates.md) before the first spawn
in a task. Read [references/contracts-v4.md](references/contracts-v4.md) before creating
the work graph or a review epoch. Do not repeat those references in worker messages.

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
- no independent parallel node or specialist implementation judgment is useful;
- one focused verification can provide proportionate acceptance evidence; and
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
register it as a Sol-owned change set with exact paths and a state identifier, then
use the current state as each new worker lease baseline. The final accumulated review
must compare the finished state with `DIRECT_BASELINE` and include both the frozen Sol
delta and every later worker delta. Do not let already-written work disappear behind
a rebased lease or quietly continue under the old direct-route assumption.

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

This gate applies only to the orchestrated path. Require a primary Sol session, the
reviewer, and each worker lane actually used by the work graph:

- `cost_orchestrator_routine_worker`
- `cost_orchestrator_complex_worker`
- `cost_orchestrator_reviewer`

Run the companion-profile exactness check for the active workspace once per Codex task,
not before every node. It must reject visible differing same-name roles in user or
project config layers.
Cache that result for the task unless a role is missing, runtime evidence conflicts, or
the installed profiles change. Worker templates must be model-neutral; the reviewer
remains pinned to Sol High and requests read-only. Require native spawn to expose the
exact `agent_type` and every model/effort override field needed by the selected policy.

Record `MODEL_POLICY` and `EFFORT_POLICY` independently as `user`, `route_default`, or
`native`. User values win. The finite route-default order is Luna Max then Terra Max
for routine and Terra Max for complex; a native dimension is omitted from spawn. When
the current surface exposes a native model catalog, validate model, effort, and any
task-required capability before dispatch; native spawn validation is still final.
Build candidate tuples by overlaying dimensions: only a `route_default` dimension may
advance through its finite sequence, while `user` and `native` dimensions stay fixed.
Observe every usable worker's role/model/effort before accepting its work. Exact user
or selected route-default values must match; native values must be observable and
stable. Never silently substitute a role, model, or effort.

A native spawn rejection before it returns a usable canonical task path creates no
owner and consumes no worker attempt or lease generation. An explicit user selection
stops there. A route default may try only the next candidate in its already-recorded
finite order with a newly hashed input closure. Once a usable worker exists, any
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
- dependencies and one lane;
- a baseline-bound behavioral `LEASE`, `LEASE_GENERATION`, and `STOP_GENERATION`;
- exact write paths, interfaces, discretion, exclusions, and stable acceptance IDs;
- one implementation owner per acceptance ID and primary-Sol verification IDs;
- finite attempt and follow-up limits fixed before dispatch; and
- focused verification with concrete expected evidence.

Construct both canonical JSON preimages with the protocol helper before formatting the
readable packet. The trusted PreToolUse guardrail will rebuild the canonical contract
and initial input preimages from the readable packet and recompute both hashes before
native spawn. Include the exact `fork_turns` value in the input preimage; a different
context fork is a different authority closure.

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
an independent review epoch is planned for the accumulated state, and native capacity
for at least two worker threads is observable and available. Otherwise use a
single worker, serialize nodes, merge an artificial split, or keep unresolved work in
Sol. File count, diff size, price, tokens, latency, request count, and predicted quality
are advisory signals, never hard Multi gates.

## Compile bounded context

Use `fork_turns: none` when the `CCO_WORK` packet and repository anchors are sufficient.
Otherwise choose the smallest positive integer string that includes the earliest still
binding parent turn. The initial cco.v4 work packet and latest valid hash-chained
follow-up supersede conversational history. A follow-up is a same-thread delta and is
never sent as a standalone packet to a new or cold worker.

Never use `fork_turns: all` with these custom roles. The pinned Codex source rejects
the `agent_type` override on a full-history fork; model and effort overrides alone are
valid there, but CCO always keeps its custom leaf role. A positive partial fork
rebuilds child context, so use it for correctness rather than claiming a cache hit.

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
model: gpt-5.6-luna
reasoning_effort: max
message: <CCO_WORK cco.v4 packet>
```

This example uses route defaults. Replace either value with the user's exact selection
or omit only the native-policy dimension. If the first route-default proposal is
rejected before a thread exists, use only the next predeclared candidate; do not count
that proposal as a worker run. Use `cost_orchestrator_complex_worker` for the complex
lane. Record the returned canonical task path and address that exact path thereafter.

## Continue live; respawn after completion

For a contract-preserving correction, verification request, or completion request:

- use `send_message` only while the worker is observably still running;
- keep the contract, run, routing, lease, and both generations unchanged;
- increment the consecutive `FOLLOWUP` within its finite limit; and
- create a new `INPUT_CLOSURE_SHA256` chained to
  `PREVIOUS_INPUT_CLOSURE_SHA256` for the `CCO_WORK_FOLLOWUP cco.v4` delta; and
- include compact canonical `BINDING_JSON` for the still-binding worker object so the
  continuation hook can recompute the new closure; and
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
7. Map every stable acceptance ID to that primary evidence at one `CURRENT_STATE`, then
   compute `EVIDENCE_SHA256`.

Do not accept a node from report text alone. Hooks and behavioral leases are
detect-only controls; stop generations reject stale reports but cannot prevent late
writes. Recheck the workspace even for a rejected result.

## Run a review epoch

After the accumulated implementation passes primary verification, start a fresh
review epoch with a new reviewer, `fork_turns: none`, and `MODE: fresh`. Supply the
fixed contract hashes, complete acceptance-ID array, baseline/current state, allowed
paths, actual delta reference, `EVIDENCE_SHA256`, the exact compact canonical
`EVIDENCE_JSON` preimage, and risks. Require the reviewer to recompute the evidence
hash and match its acceptance IDs and current state before judgment. Hash this review
input closure. Require `ship`, `fix-first`, or `rethink` for the exact closure and
reviewed state.

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
  is integrated, every acceptance ID has passing primary evidence in the exact current
  evidence closure, and that unchanged state has a matching valid `ship` verdict.
  Summarize changed paths, decisive verification, review mode, observed worker routing,
  reviewer isolation, and residual risk.

Never apply orchestrated-path acceptance claims to a no-write or direct result.

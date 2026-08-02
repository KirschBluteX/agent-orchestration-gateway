---
name: orchestrate
description: "Default cost-aware implementation router for Codex. Use implicitly for medium or large features, bug fixes, refactors, multi-file or cross-module changes, risky code changes, or work needing delegated implementation and independent acceptance. Keep read-only requests and confidently atomic low-risk edits in Sol, and upgrade as soon as scope, uncertainty, or verification risk expands. Uses a Sol control plane, role-pinned Luna and Terra workers, versioned contracts, bounded context, write leases, same-thread corrections, baseline verification, and fresh/delta Sol review epochs."
---

# Codex Cost Orchestrator

Act as the control plane. Keep user intent, architecture, decomposition, routing,
state ownership, verification, and final acceptance in the primary Sol session. Send
implementation volume to the least costly adequate role without lowering its pinned
reasoning effort.

Read [references/runtime-gates.md](references/runtime-gates.md) before the first spawn
in a task. Read [references/contracts-v3.md](references/contracts-v3.md) before creating
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

Run the companion-profile exactness check once per Codex task, not before every node.
Cache that result for the task unless a role is missing, runtime evidence conflicts, or
the installed profiles change. Observe each spawned role/model/effort before accepting
its work. Never silently substitute a role or add per-spawn model/effort overrides.

If a required role is missing or mismatched, fail closed before delegated writes. Name
the failed role and the install/check action, but do not edit `CODEX_HOME` or downgrade
to an ordinary subagent automatically. Resume in a new task after installation, or use
Sol alone only after an explicit user route override.

## Build the work graph

Resolve material ambiguity before delegation. Create the coarsest independently
verifiable nodes, each with:

- a stable `NODE` and `CONTRACT_REV`;
- dependencies and one lane;
- a baseline-bound, behaviorally exclusive write `LEASE`;
- exact write paths, interfaces, discretion, exclusions, and acceptance criteria;
- focused verification with concrete expected evidence.

Treat a lease as a control-plane promise, not an OS filesystem lock. Do not issue
overlapping active leases. Preserve pre-existing dirty and untracked work. If an owned
path changes unexpectedly, stop the node and re-establish its baseline rather than
merging concurrent edits by guesswork.

Maintain one single-flight ledger row per `NODE@CONTRACT_REV`: status, lane, run,
canonical task path, lease, baseline, dependencies, and accepted state. Before every
spawn, confirm that the same revision is neither active nor already accepted. A
contract-preserving retry continues the recorded canonical task path; a new run must
first retire the old owner. Only the primary Sol session updates this ledger.

## Route by contract closure

- Use the routine role when the contract fully determines the result.
- Use the complex role when architecture and interfaces are fixed but bounded
  algorithms, debugging, compatibility, security, or broad implementation judgment
  remains.
- Keep work in Sol while objective, architecture, public interfaces, ownership, or
  acceptance remains unresolved.

Do not split tightly coupled work merely to create more cheap turns. Parallelize only
dependency-ready nodes with disjoint write leases.

## Compile bounded context

Use `fork_turns: none` when the `CCO_WORK` packet and repository anchors are sufficient.
Otherwise choose the smallest positive integer string that includes the earliest still
binding parent turn. The current packet and `CONTRACT_REV` supersede inherited history.

Never use `fork_turns: all` with these custom roles: full-history forks reject an
`agent_type`. A positive partial fork is allowed but rebuilds child context, so use it
for correctness rather than assuming a cache hit.

Keep stable policy in role TOMLs and variable facts in packets. Do not restate the
environment, permissions, plugin list, tool schema, role description, or complete diff.

## Dispatch valid V2 tasks

Use deterministic lowercase `task_name` values:

```text
work_<node>_<lane>_rNN
review_eNN
```

Initial routine example:

```text
task_name: work_n01_auth_routine_r01
agent_type: cost_orchestrator_routine_worker
fork_turns: none
message: <CCO_WORK cco.v3 packet>
```

Use `cost_orchestrator_complex_worker` for the complex lane. Record the returned
canonical task path and address that exact path thereafter.

## Continue instead of respawning

For a contract-preserving correction, verification request, or completion request:

- use `send_message` only to steer an already running worker without starting a turn;
- use `followup_task` to start another turn on the same idle worker;
- keep `CONTRACT_REV` and the lease unchanged;
- send only a `CCO_WORK_FOLLOWUP` delta.

Treat this as live same-session reuse, not durable thread storage. Hard leaf profiles
remain follow-up capable while the completed agent is still loaded, but a cold or
unloaded agent can return `ThreadNotFound`. Never weaken the leaf profile to preserve
reuse. For a worker, retire the missing owner and start a new `RUN` with the unchanged
contract plus the compact follow-up. For a reviewer, start a new fresh review epoch
against the complete current state.

Start a new worker run when the role changes or a material contract field changes.
Before transferring a lease, stop or wait for the old owner, inspect its partial delta,
revoke the old lease, then issue the new one. Never overlap owners. Do not resend an
unchanged failed prompt.

## Verify actual state

Treat worker reports as claims. Before integrating a node:

1. Compare current state with the recorded baseline.
2. Confirm changed paths are a subset of the lease and pre-existing work survived.
3. Inspect the actual diff and material judgment calls.
4. Run only acceptance-critical verification in the primary session.
5. Bind accepted evidence to an exact `STATE` identifier.

Do not accept a node from report text alone. Hooks and behavioral leases are
detect-only controls; failures must not bypass primary verification.

## Run a review epoch

After the accumulated implementation passes primary verification, start a fresh
review epoch with a new reviewer, `fork_turns: none`, and `MODE: fresh`. Supply the
fixed contracts, baseline/current state, allowed paths, actual delta reference, and
primary evidence. Require `ship`, `fix-first`, or `rethink` for the exact reviewed
state.

When `fix-first` names bounded contract-preserving fixes, delegate them to the owning
worker, verify them, then use `followup_task` on the same reviewer with a
`CCO_REVIEW_DELTA` packet and `MODE: delta`.

Start a new fresh review epoch when goal, architecture, public interfaces or schemas,
safety constraints, write ownership, exclusions, or acceptance criteria change.
`rethink` always starts a new epoch. Any mutation after `ship` invalidates that verdict
until a delta or fresh review accepts the new exact state.

Call a review OS-enforced read-only only when observed runtime sandbox metadata says
`read-only`. Apply the broader-host procedure in the runtime-gates reference otherwise.

## Finish by route

- No-write: answer the request directly. Do not claim an implementation, worker, or
  review result.
- Direct: compare the final state with `DIRECT_BASELINE`, inspect every task-owned
  path, run focused verification, and report that no worker or review epoch was used.
- Orchestrated: report completion only when every Sol-owned and worker-owned change set
  is integrated, acceptance-critical checks pass, and the current exact state has a
  valid `ship` verdict. Summarize changed paths, decisive verification, review mode,
  observed reviewer isolation, and residual risk.

Never apply orchestrated-path acceptance claims to a no-write or direct result.

# Repository agent policy

## Default implementation routing

Use the `codex-cost-orchestrator:orchestrate` skill as this repository's default
router for implementation work. Classify requests by uncertainty, coupling, impact,
and verification needs. File count and diff size are useful signals, not hard gates.

### Implicit invocation

- A user does not need to type `$codex-cost-orchestrator:orchestrate`.
- Read-only analysis, explanation, planning, status checks, and diagnosis without a
  requested fix stay in the primary Sol task and do not open an orchestration graph.
- Activating the skill selects the routing policy; it does not automatically require
  a worker spawn.

### Direct fast path

The primary Sol task may implement directly only when all of these are true:

- the result is unambiguous and mechanically determined;
- the edit is atomic, small, and confined to one bounded area;
- no public interface, schema, migration, dependency boundary, authentication,
  authorization, security, concurrency, build, release, or destructive data behavior
  changes;
- no independent node or specialist judgment would materially help;
- focused verification is sufficient; and
- current worktree ownership is clear.

Before the first write, record an exact `DIRECT_BASELINE` and pre-existing changed
paths. Record a brief direct-route reason, inspect the actual delta, and run
proportionate verification. Do not spawn agents or create a review epoch solely for
ceremony.

### Mandatory orchestration

Use the complete CCO work-graph, worker, verification, and review-epoch flow whenever
any direct-path condition is false or uncertain. This includes medium or large work,
multiple independently verifiable nodes, cross-module changes, public contracts,
security-sensitive or concurrent behavior, uncertain bug causes, broad regression
risk, useful parallelism, or a need for independent acceptance.

### Worker selection

Routine and complex describe contract closure, not fixed model families. The worker
model and reasoning effort are independently user-selectable per node. A user choice
wins; either dimension may instead use native Codex resolution. The finite default
order is Luna Max then Terra Max for routine and Terra Max for complex. Check a public
native capability catalog when exposed, then let spawn validation remain authoritative.
Candidate tuples overlay the independent dimensions: only a `route_default` dimension
may advance, while `user` and `native` dimensions remain fixed.
Worker templates stay model-neutral and the reviewer stays pinned to Sol High. A
rejected pre-thread route proposal consumes no worker attempt; an unavailable user
choice fails closed, while a route default may use only its next predeclared candidate.
Fence and reject any mismatch after a usable worker starts.

### Structural Multi gate

Full orchestration does not imply concurrent fan-out. Run workers concurrently only
with at least two dependency-ready nodes, pairwise-disjoint leases, closed contract and
input-closure hashes, complete acceptance ownership, and a planned independent review
epoch, plus observable native capacity for at least two worker threads. Otherwise
serialize, merge an artificial split, or retain unresolved work in Sol. Cost, token,
latency, request-count, and predicted-quality estimates are advisory, not hard gates.

### CCO v4 execution guards

Every delegated node carries canonical contract and input-closure hashes, a single
active owner, monotonically increasing lease and stop generations, finite attempt and
follow-up counters, and stable acceptance IDs. Chain every bounded follow-up to the
previous input closure; the initial packet plus latest same-thread delta is the full
authority. Validate exact preimage schemas before hashing. Increment the stop-generation
fence before interrupting or retiring an owner. `Interrupted` is not terminal: never
reuse the fenced path or transfer its lease before idle/terminal observation. A fence
rejects stale results but cannot prevent late writes, so inspect the baseline-relative
delta. Repeated failures require a structured Sol-recomputed signature and a materially
different intervention. The read-only PreToolUse and SubagentStop hooks are fail-open
syntax guardrails, not a ledger, lock, or substitute for Sol verification.

Every initial worker and fresh-review input closure binds the exact `fork_turns` value.
Before dispatch, construct canonical preimages with the helper; the trusted spawn hook
rebuilds them from the readable packet and recomputes contract/input/evidence hashes.
Live steers include canonical `BINDING_JSON`, the immutable acceptance IDs, and the
complete canonical native `TARGET`; reviewer deltas bind their full target too. The
continuation hook validates each self-contained steer or reviewer delta, but has no
persistent ledger and cannot prove prior issuance, liveness, lease disjointness, or
counter history.

A worker hash-chained follow-up is a live `send_message` steer only while that exact
owner is observably running. Native V2 may transparently reload a completed task
without replaying its original per-spawn model/effort overrides; therefore never use
worker `followup_task` after a result, idle state, or uncertain residency. Inspect and
retire the old owner, then create a new bounded run with a complete packet, explicit
routing, and a new lease generation. The role-pinned reviewer may use bounded delta
`followup_task`, with route, isolation, and workspace state rechecked afterward.

### Upgrade before continuing

Before expanding a direct task, upgrade it to full orchestration if it reaches another
bounded area, introduces a material interface or ownership decision, fails initial
verification for a non-trivial reason, requires systemic diagnosis, develops a wider
regression surface, or would benefit from independent review.

Retain `DIRECT_BASELINE`; freeze and inspect the current Sol delta; register it as a
Sol-owned change set with exact paths and state identity; then use current state as
each worker lease baseline. The final review must compare the finished state to
`DIRECT_BASELINE` and cover both the Sol-owned delta and all worker deltas.

### User override

- Merely naming or invoking the Skill selects its router and does not force delegation.
- A request for the full CCO flow, worker lanes, or review epoch forces orchestration.
- A no-delegation, single-agent, or direct-execution constraint overrides mere Skill
  selection and keeps work in Sol. If one instruction both forces full CCO and forbids
  delegation, stop before writing and request resolution.
- Higher-priority instructions and safety constraints still apply.

### Runtime availability

For full orchestration, require the reviewer and each worker role actually used by the
graph, plus native `task_name`, `message`, `agent_type`, and `fork_turns` spawn fields
and each selected model/effort override field. Run the profile `--check` against the
active workspace so visible differing same-name roles fail closed. If one is missing or
mismatched, fail closed before delegated writes, report
the exact role and recovery command, and do not edit `CODEX_HOME` or substitute a
generic agent. Resume after installation in a new task, or use Sol alone only when the
user explicitly chooses that route.

### Acceptance evidence

Worker reports are claims, not proof. For orchestrated work, the primary Sol task must
inspect the actual baseline-relative delta, enforce write ownership, rerun
acceptance-critical checks, map every acceptance ID to primary Sol evidence at one
current state, record operation/outcome/exit status/owner/artifact hashes, and hash that
evidence closure. Fresh and delta review packets must carry the exact compact canonical
`EVIDENCE_JSON`; the reviewer recomputes its hash and checks its acceptance IDs and
current state before judgment. Every record must be `passed` before either review mode
is eligible. A `ship` verdict must echo the complete
ID set, evidence hash, review input closure, and exact reviewed state.
For direct work, inspect the final delta and run focused checks. Never claim a review
epoch, read-only isolation, passing test, or accepted state without observed evidence.

Finish according to the selected route: no-write answers make no implementation
claim; direct changes require final-baseline comparison and focused checks but no
`ship` verdict; orchestrated changes require all owned change sets, critical checks,
and a current-state `ship` verdict.

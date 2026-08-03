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
- deterministic verification is required;
- no enumerated `RISK_FLAGS` apply: `authentication_authorization`, `build_release`,
  `concurrency`, `dependency_boundary`, `destructive_data`, `external_side_effect`,
  `migration`, `nondeterministic_verification`, `public_interface`, `schema`, or
  `security`;
- no independent node or specialist judgment would materially help;
- one deterministic focused verification is sufficient; and
- current worktree ownership is clear.

Before the first write, record an exact `DIRECT_BASELINE` and pre-existing changed
paths. Record a brief direct-route reason, inspect the actual delta, and run
proportionate verification. Do not spawn agents or create a review epoch solely for
ceremony.

### Mandatory orchestration

Use the complete CCO work-graph, worker, verification, and acceptance-mode flow whenever
any direct-path condition is false or uncertain. This includes medium or large work,
multiple independently verifiable nodes, cross-module changes, public contracts,
security-sensitive or concurrent behavior, uncertain bug causes, broad regression
risk, useful parallelism, or a need for independent acceptance.

### Worker selection

Routine and complex describe contract closure, not fixed model families. The worker
model and reasoning effort are independently user-selectable per node. A user choice
wins; either dimension may instead use native Codex resolution. At each new work graph,
resolve every remaining `route_default` dimension with
`scripts/routing_catalog.py`: intersect Codex's bundled capability catalog with the
validated Radar snapshot, require observed IQ strictly greater than 90 and sufficient
samples/cohort coverage, then select from a Wilson-aware strict Pareto frontier with
fixed-anchor quality/cost/time utility. The default refresh TTL is one hour; pin the
result for the whole graph and never switch mid-run. Do not surface the score breakdown
unless the user asks or routing diagnosis requires it.

User-fixed dimensions constrain the adaptive candidate set. A `route_default`
dimension cannot be mixed with `native`; use both native or close the other dimension.
Bind canonical `ROUTING_DECISION_JSON` through the `routing_decision` `INPUTS` anchor.
Worker templates stay model-neutral and the reviewer stays pinned to Sol High. A
rejected pre-thread route proposal consumes no worker attempt; an unavailable user
choice fails closed, while a route default may use only the next candidate in its
hash-bound fallback order. Radar/LKG exhaustion fails closed rather than reviving a
static model list.
Fence and reject any mismatch after a usable worker starts.

### Structural Multi gate

Full orchestration does not imply concurrent fan-out. Run workers concurrently only
with at least two dependency-ready nodes, pairwise-disjoint leases, closed contract and
input-closure hashes, complete acceptance ownership, an acceptance chain ending in
`independent`, and observable native capacity for at least two worker
threads. Otherwise
serialize still-disjoint nodes, merge overlapping or artificial nodes under one
owner, or retain unresolved work in Sol. Cost, token,
latency, request-count, and predicted-quality estimates are advisory, not hard gates.

### CCO v4 execution guards

Every delegated node carries canonical contract and input-closure hashes, a single
active owner, monotonically increasing lease and stop generations, finite attempt and
follow-up counters, explicit hash-bound exact/prefix scopes, and stable acceptance IDs.
Before the first worker spawn, build the immutable graph manifest and append-only acceptance chain,
require every acceptance owner to equal its declaring node, reject global ID
duplicates and scope overlap/case aliases, and cap the graph-wide scope union at 128.
Worker contract revisions are capped at three runs and each run at two live follow-ups;
review epochs are capped at two fresh threads and each reviewer thread at two delta
turns. Chain every bounded follow-up to the
previous input closure; the initial packet plus latest same-thread delta is the full
authority. Validate exact preimage schemas before hashing. Increment the stop-generation
fence before interrupting or retiring an owner. `Interrupted` is not terminal: never
reuse the fenced path or transfer its lease before idle/terminal observation. A fence
rejects stale results but cannot prevent late writes, so inspect the baseline-relative
delta. Repeated failures require a structured Sol-recomputed signature and a materially
different intervention. The read-only PreToolUse and SubagentStop hooks are fail-open
syntax guardrails, not a ledger, lock, or substitute for Sol verification.

Every initial worker and fresh-review input closure binds the exact `fork_turns` value.
Before dispatch, Sol constructs canonical preimages; `protocol_hash.py` validates and
hashes them but does not construct them. The trusted spawn hook rebuilds them from the
readable packet and recomputes contract/manifest/decision/chain/input/evidence hashes.
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

Native V2 wait is targetless. After a mailbox update, identify the source against the
single-flight ledger and canonical task path before accepting a result; only
`send_message`, `followup_task`, and `interrupt_agent` take an exact target.

Capture every lease baseline with `cco.workspace-state.v2`; verify emits
`cco.workspace-verification.v2`. It binds all tracked worktree
files, recursive initialized submodules plus protected nested control state, symbolic and commit HEAD, Git config/refs,
selected administrative state, and physical worktree/control-directory identities.
Pass every scope unchanged as `exact:<path>` or `prefix:<directory>`; never derive
prefix authority after hashing. Compare changed paths with exact Git spelling even on
Windows; reject case/8.3 aliases before dispatch, existing reparse or Git-control
aliases, prefix reparse descendants, Git lock files, and child/root/ancestor-prefix
leases that cross an indexed submodule. A submodule root exact lease covers content,
not nested Git control changes. Serialized nodes should use `--next-baseline`. This remains detect-only: ignored
files, alternate data streams, hardlink aliases, hook fail-open, and capture/verify
races require an observed read-only sandbox when hard isolation is needed. An empty
lease rejects all observed state changes, not every possible filesystem mutation.

### Upgrade before continuing

Before expanding a direct task, upgrade it to full orchestration if it reaches another
bounded area, introduces a material interface or ownership decision, fails initial
verification for a non-trivial reason, requires systemic diagnosis, develops a wider
regression surface, or would benefit from independent review.

Retain `DIRECT_BASELINE`; freeze and inspect the current Sol delta; register it as a
explicit `sol` contract node with hashed scopes and state identity; then use current state as
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

For full orchestration, require each worker role actually used by the graph and require
the reviewer only when the acceptance chain ends in independent mode, plus native `task_name`,
`message`, `agent_type`, and `fork_turns` spawn fields
and each selected model/effort override field. Run the profile `--check` against the
active workspace so visible differing same-name roles fail closed. If one is missing or
mismatched, fail closed before delegated writes, report
the exact role and recovery command, and do not edit `CODEX_HOME` or substitute a
generic agent. Resume after installation in a new task, or use Sol alone only when the
user explicitly chooses that route.

Keep a cached checked set of successful profiles. If a primary-to-independent upgrade
makes the reviewer necessary and it is not in that set, run
`python scripts/install_agents.py --workspace <repo> --check --profile reviewer` and
record success before any fix or review. A failed reviewer profile check blocks both;
do not defer it to the reviewer spawn.

### Acceptance evidence

Worker reports are claims, not proof. For orchestrated work, the primary Sol task must
build and validate the graph manifest and acceptance chain before the first worker spawn, inspect the
actual baseline-relative delta, enforce write ownership, rerun acceptance-critical
checks, and record exactly one primary Sol evidence record
for every required verification at one current state. Evidence must exactly match the
contract-defined operation, acceptance IDs, implementation owner, outcome, exit status,
and artifact hashes. Fresh and delta review packets carry
`GRAPH_MANIFEST_SHA256`, `ACCEPTANCE_CHAIN_SHA256`, and exact compact canonical
`EVIDENCE_JSON`; the reviewer recomputes every contract, decision, chain, and evidence hash before judgment.
Every record must be `passed` before either acceptance mode is eligible. Primary mode
is allowed only for one clean routine contract with deterministic evidence and no
public/safety/concurrency/build/release/migration/destructive, Sol-owned, Multi, retry,
deviation, or explicit-review trigger. Otherwise use independent mode; any anomaly
appends a hash-linked independent decision without erasing primary history. A `ship` verdict must echo the complete
ID set, evidence hash, review input closure, and exact reviewed state when independent
review is required.
For direct work, inspect the final delta and run focused checks. Never claim a review
epoch, read-only isolation, passing test, or accepted state without observed evidence.

Finish according to the selected route: no-write answers make no implementation
claim; direct changes require final-baseline comparison and focused checks but no
`ship` verdict; orchestrated changes require all owned change sets and critical checks
at the exact current state, plus either still-eligible primary acceptance or a matching
independent `ship` verdict.

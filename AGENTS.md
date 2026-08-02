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
graph. If one is missing or mismatched, fail closed before delegated writes, report
the exact role and recovery command, and do not edit `CODEX_HOME` or substitute a
generic agent. Resume after installation in a new task, or use Sol alone only when the
user explicitly chooses that route.

### Acceptance evidence

Worker reports are claims, not proof. For orchestrated work, the primary Sol task must
inspect the actual baseline-relative delta, enforce write ownership, rerun
acceptance-critical checks, and bind the final verdict to the exact reviewed state.
For direct work, inspect the final delta and run focused checks. Never claim a review
epoch, read-only isolation, passing test, or accepted state without observed evidence.

Finish according to the selected route: no-write answers make no implementation
claim; direct changes require final-baseline comparison and focused checks but no
`ship` verdict; orchestrated changes require all owned change sets, critical checks,
and a current-state `ship` verdict.

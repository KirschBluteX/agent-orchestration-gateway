# Codex Cost Orchestrator

[English](README.md) | [简体中文](README.zh-CN.md)

Codex Cost Orchestrator (CCO) is an implicit, cost-aware router for native Codex
Agents. Primary keeps the user goal, architecture, decomposition, integration,
verification, and final acceptance. Closed execution volume can move to a cheaper
eligible model without requiring the user to invoke CCO explicitly.

CCO does not add another Agent runtime. Codex native spawn, follow-up, wait, and
interrupt remain authoritative. CCO supplies deterministic routing, a compact
dispatch capsule, bounded ownership, and evidence-driven acceptance.

## How it decides

CCO separates decisions that are often incorrectly collapsed into “simple versus
complex coding”:

| Decision | Values | Meaning |
| --- | --- | --- |
| Purpose | `analysis_inspect`, `analysis_probe`, `implementation`, `acceptance` | Why another Agent is useful |
| Judgment | routine, complex | Whether bounded choices can affect the result |
| Placement | Primary, child | Whether another native turn adds structural value |
| Route | model + effort | Which supported pair executes the closed task |
| Assurance | deterministic, guarded | Whether existing acceptance facts permit the economical worker pool |
| Acceptance | primary, independent | Which evidence is required to finish |

An atomic deterministic edit stays in Primary. A child is created only for closed
execution, disjoint parallel work, context rescue, a source partition, runtime
isolation, independent evidence, or explicit delegation. CCO never creates a child
merely because a model is cheaper.

## Fast dispatch

Normal dispatch is one local graph compile followed by native spawns:

```text
authoring facts → route plan → cco.v6 capsule → native Agent
```

- The single prepared-graph entry derives policy and assurance labels from facts, captures one real
  workspace artifact, validates the whole route plan, selects ready nodes at observed
  native capacity, and builds every active and legal fallback capsule in memory.
- Project files are never used as dispatch scratch space.
- Light and strict dispatch share one implementation; strict mode adds evidence,
  not a second protocol.
- Logical task types remain distinct, while physical profiles are reduced to one
  writable leaf and one read-only leaf.
- Primary waits event-first after useful non-overlapping work is exhausted. It does
  not repeatedly poll or spend model turns narrating unchanged status.
- Callers cannot pair an arbitrary model with a detached plan hash; capsules retain
  only the validated plan identity, active rank, and selected pair.

## Adaptive routing

User-selected model and reasoning effort always win. Otherwise, one local batch
resolves every purpose/judgment/assurance route required by the graph. Routing never invokes
an Agent or asks Primary to compare candidates in natural language.

Automatic candidates must be supported by the current Codex Multi-Agent backend,
have observed CodexRadar IQ strictly above 90, and pass sample/cohort/coverage
checks. An automatic Luna candidate for complex work must also have a Wilson 95%
lower bound strictly above 90; routine work keeps the point-estimate gate and
uncertainty penalty. When the native catalog exposes backend metadata, CCO accepts explicitly known
multi-agent backend versions (`v1` or `v2`) and rejects unmarked entries; it never
guesses support from a model name. A Wilson-aware Pareto utility balances quality,
resource use, time, and uncertainty.

Luna and Terra are preferred for workers and reviewers. Sol may automatically win
only when no eligible Luna/Terra exists or Sol’s Wilson 95% lower bound is above the
best eligible Luna/Terra upper bound. A user-fixed Sol route is always honored.

Assurance is derived from existing acceptance facts. A deterministic route has no
declared risk, complete deterministic coverage, and no non-deterministic evidence;
guarded routes remove Luna from automatic selection. An explicit user-fixed pair
remains exact.

An evidence-backed Luna execution failure, deviation, or scope surprise retires that
owner and creates a newer guarded generation; another Luna effort is not retried for
the same failure. This escalation does not waive the strict Sol advantage gate.

The Radar TTL is one hour. If a bounded last-known-good snapshot is older than the
TTL, CCO dispatches immediately from it and refreshes for a later dispatch. Route
fallback advances through a pre-ranked plan; it does not rescore Radar or rebuild the
whole contract. A confirmed pre-thread rejection takes the next precompiled native
request without recapturing the baseline. One short-lived request suppresses duplicate
refresh processes and is removed after success. Scores are hidden unless `--explain`
is requested.

## Concurrency and acceptance

CCO imposes no concurrency cap below the native Codex limit. It fills available
slots when nodes are dependency-ready, responsibilities differ, and write scopes do
not overlap. Concurrency alone does not force a reviewer.

Primary acceptance is allowed when risks are explicitly absent and deterministic
evidence covers every acceptance criterion, including multi-node or complex graphs.
Independent review is reserved for real risk, semantic/manual evidence, integration
judgment, failure or deviation, a Primary-owned implementation change, or an
explicit request.

An independent review is a fresh read-only native Agent with `fork_turns: none` and
one exact evidence bundle. `fix-first` keeps that reviewer continuable for one
evidence-driven delta. Repeated attempts require new actionable evidence; fixed
retry counters are not carried through every packet.

## Safety and state

The capsule binds purpose, judgment, derived assurance, route, context fork, scopes, contract,
acceptance, evidence, baseline, graph identity, and one execution generation.
PreToolUse requires the matching prepared artifact; SubagentStop verifies the current
workspace against the whole graph's scope union before recording a result.

A small task-local ledger outside the repository keeps only the current owner,
generation, input cursor, and lifecycle phase. It rejects duplicate ownership,
concurrent continuations, and late results. It is not a coordinator, database,
durable audit log, filesystem lock, or acceptance record.

Light tasks avoid enumerating ignored files. Strict tasks fingerprint ignored files
and fail closed above the default 10,000-file or 256-MiB scan limits. Both retain the
tracked/untracked and Git control-state checks implemented by the workspace schema,
including path aliases, reparses, and submodules. Prepared artifacts live outside the
repository and are removed at SessionEnd. Hooks are process guardrails; OS-enforced
read-only isolation is claimed only when runtime metadata proves it.

CCO keeps no billing, token, fee, route-history, encryption, provider-session,
daemon, or database layer.

## Install

Python 3.11 or newer is required for installation, hooks, and validation.

```powershell
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add .agents/plugins
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --upgrade --workspace .
```

Verify the two physical profiles:

```powershell
python plugins/codex-cost-orchestrator/scripts/install_agents.py --check --workspace .
```

Start a new Codex task after installing or updating so profiles, hooks, and the skill
are reloaded. Then describe an ordinary implementation request; explicit
`$codex-cost-orchestrator:orchestrate` remains optional.

## Development

```powershell
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
```

See the [orchestration skill](plugins/codex-cost-orchestrator/skills/orchestrate/SKILL.md)
and [cco.v6 capsule reference](plugins/codex-cost-orchestrator/skills/orchestrate/references/contracts-v6.md).

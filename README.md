# Codex Cost Orchestrator

[简体中文](README.zh-CN.md)

Codex Cost Orchestrator (CCO) is a local control plane for Codex native Agents.
Primary keeps intent, integration, and final acceptance; CCO dispatches closed,
scoped work and returns exact acceptance evidence. Release
`5.0.0+codex.20260810093311` is a breaking release.

## Delegation contract

Normal work is delegated by default through one canonical `prepare` command. The
input is a schema-validated `cco.delegation.v1` envelope: closed work, explicit
acceptance IDs, and repository-relative scopes. Every scope is exactly one of
`{"kind":"exact","path":"…"}` or `{"kind":"prefix","path":"…"}`.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

Invoke each returned action with only its supplied tool input. A complex unresolved
task may first use one ordinary read-only Terra/max planning task; its proposal is
only a stateless `cco.planner-proposal.v1` DAG input. CCO does not create a planner
route or a second planner lifecycle.

Primary stays in control only for explicit authority, clarification, an explicit
direct request, or one declared tool bounded below 30 seconds. After dispatch,
repeat long `wait_agent` windows until completion or required attention. A
`timed_out` result is only an expired wait window: do not treat the child as failed,
narrate unchanged progress, or duplicate its work.

## Deterministic routing and review

| Assurance | Automatic route |
| --- | --- |
| Mechanical explorer or worker | Luna when the active V2 backend exposes it; otherwise Terra |
| Bounded explorer or worker | Terra |
| Guarded work and final reviewer | Terra |

The compiler filters routes through the active V2 native capability catalogue, so a
model advertised only for another Agent backend is never attempted. CCO does not start
a second V1 Agent runtime to reach Luna; when a future host exposes Luna through V2, the
existing static route becomes eligible without a protocol migration. It marks work
guarded for semantic or manual verification, public
interfaces, security/authentication, concurrency, persistence, migration or
recovery, installer work, filesystem transactions, irreversible actions, test
failure, retry, deviation, scope expansion, or a new dependency. A guarded plan
gets one final independent reviewer after every non-reviewer source node. The only way to omit
that reviewer is the current plan's explicit `accept_risk: true`. Primary final
authority and deterministic verification remain required.

An idle owner may be reused only for one direct clean predecessor with the exact
role, assurance, selected route, and scopes; it must have zero inherited context,
no retry, deviation, interruption, blocker, or unresolved receipt/lease. Each reuse
still has a fresh dispatch and baseline.

## State and upgrade

Current runtime records use `cco.wave.v3`, `cco.lifecycle.v2`, and
`cco.receipt.v2`. Earlier active state, wave, lifecycle, receipt, and aggregation
artifacts are not upgraded in place: clean them up before starting a 5.0.0 task.
There is no migration command and no active-state compatibility layer.

Readers scan only their declared scopes. CCO admits one normal writer at a time for
a canonical workspace and fails closed on conflicting live work. `status`,
`continue`, `native-failure`, `retry`, `restart`, and `cleanup` operate on the
current task; see [operations](docs/OPERATIONS.md).

## Experimental cooperative writers

`writer_isolation=cooperative` is opt-in and supports only two independent,
non-overlapping fresh writer nodes. A clean Git workspace uses managed worktrees;
dirty Git and directory workspaces use bounded copies. CCO stages exact backups and
a bounded apply journal before integration. Guarded writers may be followed by the
single compiler-injected final reviewer; no other cooperative DAG shape is admitted.
Successful cleanup removes completed isolate and journal material; an incomplete
journal and its backups remain until
rollback or explicit intervention establishes a safe outcome.

This is not an OS sandbox. Children, Primary, and the local host remain trusted;
inspect the final delta and never rely on cooperative isolation to contain a
malicious or compromised process.

## Install

Requirements are Python 3.11+ and `zstandard` on Python versions below 3.14, plus a
current Codex installation with plugins, Hooks, and native Agents.

```text
python -m pip install -r requirements.txt
codex plugin marketplace add .
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --bootstrap
```

Review and trust the five CCO Hooks in `/hooks`, start a new Codex task, then run:

```text
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --doctor
```

## Offline host-edge repair

Codex Desktop owns persisted task-card edges. CCO never changes that database from a
Hook. The optional repair utility is an offline fallback: leave the active task, keep
`CODEX_THREAD_ID` unset, use `--offline-confirm`, and name the exact parent and child
IDs. It creates an owner-only rollback journal before a repair. See
[operations](docs/OPERATIONS.md#offline-host-edge-repair).

## Development

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests benchmarks .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
git diff --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[ROADMAP.md](ROADMAP.md). Licensed under the [MIT License](LICENSE).

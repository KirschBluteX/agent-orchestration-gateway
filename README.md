# Codex Cost Orchestrator

[简体中文](README.zh-CN.md)

Codex Cost Orchestrator (CCO) is a local control plane for Codex native Agents.
Primary keeps intent, integration, and final acceptance; CCO dispatches closed,
scoped work and returns exact acceptance evidence. CCO remains pre-1.0.

## Delegation contract

Normal work is delegated by default through one canonical `prepare` command. The
input is a schema-validated `cco.delegation.v1` envelope: closed work, explicit
acceptance IDs, and repository-relative scopes. Every scope is exactly one of
`{"kind":"exact","path":"…"}` or `{"kind":"prefix","path":"…"}`.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

Invoke each returned action with only its supplied tool input. Primary must clarify or close
unresolved work before `prepare`; every native child must be prepared by CCO. A pre-existing
`cco.planner-proposal.v1` value is accepted only as stateless, schema-validated DAG input. It is
not a planner route, lifecycle, or direct-spawn permission.

Current Codex Desktop builds may replace the prepared Agent message with opaque
ciphertext at the Hook boundary. The default `trusted_host` policy admits it only
when every visible field matches exactly one prepared dispatch, then binds the exact
ciphertext digest and `tool_use_id` in the existing durable receipt and requires the
same pair at postflight. This restores native V2 spawn, reuse, and continuation
without adding another runtime or ledger. It trusts the host; it does not prove that
the hidden plaintext equals the prepared message. Set
`CCO_OPAQUE_MESSAGE_POLICY=strict` before starting Codex to reject all opaque Agent
inputs until the host exposes an authenticated plaintext digest.

Primary stays in control only for explicit authority, clarification, an explicit
direct request, or exactly one declared tool with a total upper bound under 30 seconds.
After dispatch,
repeat long `wait_agent` windows until completion or required attention. A
`timed_out` result is only an expired wait window: wait again on the same live dispatch;
do not treat the child as failed, narrate unchanged progress, or duplicate its work.

## Deterministic routing and review

| Assurance | Automatic route |
| --- | --- |
| Mechanical explorer or worker | Luna when the active native capability catalogue exposes it; otherwise Terra |
| Bounded explorer or worker | Terra |
| Guarded work and final reviewer | Terra |

The compiler filters routes through the active native capability catalogue, so a
model not offered by the host is never attempted. CCO does not start another Agent
runtime to reach Luna; a valid Luna entry that is not explicitly disabled makes the
existing static route eligible without a protocol migration. It marks work
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

## State

Current runtime records use `cco.wave.v3`, `cco.lifecycle.v2`, and
`cco.receipt.v2`.

Readers scan only their declared scopes. CCO admits one normal writer at a time for
a canonical workspace and fails closed on conflicting live work. `status`,
`continue`, `native-failure`, `retry`, `restart`, and `cleanup` operate on the
current task; see [operations](plugins/codex-cost-orchestrator/skills/manage-cco/references/operations.md).

## Experimental cooperative writers

`writer_isolation=cooperative` is opt-in and admits the largest pairwise-disjoint set
of fresh writer nodes that fits the requested native capacity, with a four-writer
safety ceiling. A clean Git workspace uses managed worktrees; dirty Git and directory
workspaces use bounded copies. File, byte, and journal limits apply to the whole wave,
not once per writer. CCO stages exact backups and one bounded apply journal before
integration. Guarded writers may be followed by the single compiler-injected final
reviewer; no other cooperative DAG shape is admitted.
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

## Development

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests benchmarks .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
git diff --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed under
the [MIT License](LICENSE).

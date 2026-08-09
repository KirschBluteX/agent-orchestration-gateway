---
name: orchestrate
description: >-
  Local control plane for any Codex native subagent requested by the user, AGENTS.md, or another
  skill. Route closed work through static Luna/Terra profiles while Primary keeps intent and final
  authority; never call spawn_agent directly around this skill.
---

# Orchestrate with CCO

Keep unresolved product, architecture, integration, and final acceptance choices in Primary.
Route every requested native child through CCO; never add a direct-spawn bypass.

For the first wave, pipe compact JSON directly to one command; do not inspect source, call
`--help`, or create a contract file:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

A single child requires `role` (`explorer|worker|reviewer`), `objective`, acceptance strings, and
`scopes` as `{"kind":"file|tree","path":"repo/relative"}`. Optional fields are `goal`,
`decision`, `verification`, `risks`, `pin`, and `context_turns`. A DAG adds `nodes`, per-node `id`,
and optional `depends_on` or `review_of`; use top-level acceptance IDs only for shared evidence.
Mechanical means every allowed choice is acceptance-equivalent.

Delegate only closed, scoped, independently verifiable work with no user interaction or
irreversible external action. Structural value exists when it is likely to wake Primary more than
once, compress a long or compacted thread into a short capsule, reuse a compatible idle owner, run
conflict-free beside another node, or isolate a probe. Predictive signals include at least two tool
round trips, more than two files not already in context, over 60 seconds, or over 64 KiB of output;
a single long deterministic test, build, lint, benchmark, or probe qualifies. Use explorer for
read-only work and worker for declared writes. Keep one-wakeup microtasks and unresolved choices in
Primary. Mechanical work prefers the cheapest supported route, normally Luna/max; bounded,
guarded, and reviewer work prefer Terra. Effort order is `max`, `xhigh`, then `high`; only a current
explicit user pin may select Sol. Primary must not rerun successful delegated evidence.

Do not pass a session ID. Invoke each returned `tool_name` with only its exact `tool_input`. CCO may
reuse one idle explorer or worker only when its clean direct dependency has the same role, route,
assurance, and covering scope. Aggregates, ambiguity, retry, scope expansion, inherited context,
and reviewers spawn fresh; reuse still gets a new contract, baseline, and acceptance check.

After dispatch, enter one long `wait_agent`. Do not poll, duplicate work, overlap Primary work, or
forward protected payloads. `{"timed_out":true}` is only a wait-window expiry; wait again and never
report it as `native-failure`.

For typed native failure, run `native-failure` and follow its envelope: unsupported model=
`route_rejected`, missing reused owner=`owner_unavailable`, 429=`rate_limit`, transport=`network`,
deadline=`timeout`, temporary 5xx=`service`, otherwise `other`. Never infer failure from prose.
Unavailable reuse falls back once; transient failures retry the unchanged call at most three times.

Hooks bind result claims to dispatch, cursor, baseline, scope, acceptance IDs, and owner. Call
`next` only after settlement. Primary inspects the delta and owns final acceptance. Use reviewer
only for semantic/manual evidence, risk, deviation, Primary changes, or explicit request; only
`accept` satisfies its gate.

Use `$codex-cost-orchestrator:manage-cco` only for installation, doctor, policy, status,
continuation, retry, restart recovery, or cleanup.

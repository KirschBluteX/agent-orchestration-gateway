---
name: orchestrate
description: >-
  Local control plane for any Codex native subagent requested by the user, AGENTS.md, or another
  skill. Route closed work through static Luna/Terra profiles while Primary keeps intent and final
  authority; never call spawn_agent directly around this skill.
---

# Orchestrate with CCO

Keep unresolved product, architecture, integration, and final acceptance choices in Primary. Route
every requested native child through CCO; never add a direct-spawn bypass.

For the first wave, pipe compact JSON directly to one command. Do not inspect CCO source, call
`--help`, or create a temporary contract file:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

A single-child contract requires `role` (`explorer|worker|reviewer`), `objective`, acceptance
strings, and `scopes` shaped as `{"kind":"file|tree","path":"repo/relative"}`. Optional fields
include `goal`, `decision`, `verification`, `risks`, `pin`, and `context_turns`. A compact DAG adds
`goal`, `nodes`, per-node `id`, and optional `depends_on` or `review_of`; use a full DAG with
top-level acceptance IDs only for shared evidence. Mechanical means every allowed choice is
acceptance-equivalent; omitted `decision` is bounded.

Do not pass a session ID. Invoke only each returned `tool_name` with its exact `tool_input`.
Mechanical explorer/worker prefers Luna; bounded, guarded, and reviewer work prefer Terra. Effort
order is `max`, `xhigh`, then `high`. Only a current explicit user pin may select Sol.

CCO may reuse an idle owner with `followup_task` only for one explorer or worker whose direct
dependency used the same role, model, effort, and assurance, has a clean result, and covers every
new scope. Explicit inherited context, aggregates, ambiguity, retries, scope expansion, and all
reviewers spawn fresh. Reuse still receives a fresh task, dispatch, baseline, and acceptance check.

After dispatch, enter one long `wait_agent`. Do not poll, duplicate a child, do overlapping
Primary work, or forward protected payloads. `{"timed_out":true}` means only that the wait window
ended; start another long wait. It is not a native timeout and must not call `native-failure`.

For a typed native failure, run `native-failure` and follow its envelope. Map only typed host
status: unsupported model=`route_rejected`, unavailable reused owner=`owner_unavailable`,
429=`rate_limit`, transport=`network`, deadline=`timeout`, temporary 5xx=`service`, and
non-retryable=`other`. Never infer failure from prose. Unavailable reuse falls back once to a fresh
spawn; transient failures retry the unchanged call at most three times.

Treat child results as claims. Hooks bind dispatch, cursor, baseline, scopes, acceptance IDs, and
owner. Call `next` only after the wave settles. Primary inspects the delta and owns final
acceptance. Use reviewer only for semantic/manual evidence, risk, deviation, Primary changes, or
explicit request; only reviewer `accept` satisfies its gate.

Use `$codex-cost-orchestrator:manage-cco` only for installation, doctor, policy, status,
continuation, retry, restart recovery, or cleanup.

---
name: orchestrate
description: >-
  Local control plane for any Codex native subagent requested by the user, AGENTS.md, or another
  skill. Route closed work through static Luna/Terra profiles while Primary keeps intent and final
  authority; never call spawn_agent directly around this skill.
---

# Orchestrate with CCO

Keep unresolved product, architecture, integration, and final acceptance choices in Primary.
Route every requested native child through CCO. Never add a direct-spawn bypass.

For the first wave, run one command and pipe compact JSON directly to stdin; do not inspect CCO
source, call `--help`, or create a temporary contract file:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

A single-child contract requires `role` (`explorer|worker|reviewer`), `objective`, string-list
`acceptance`, and `scopes` entries shaped as `{"kind":"file|tree","path":"repo/relative"}`.
Optional fields are `goal`, `decision`, `verification`, `risks`, `pin`, and `context_turns`;
`decision` defaults to bounded and is mechanical only when every allowed choice is equivalent.
A compact DAG has `goal` and `nodes`; every node has `id` plus the same required fields and may
also declare `depends_on` or `review_of`. A full DAG may instead add top-level `acceptance` and use
its IDs in nodes when evidence is shared. CCO compiles either form, captures one baseline, derives
the first ready wave, and returns complete `spawn_agent` inputs.

Do not pass a session ID. Dispatch every returned input unchanged and use its exact profile,
model, effort, name, and message. Mechanical explorer/worker prefers Luna; bounded work prefers
Terra; guarded work and reviewer use Terra. Effort order is `max`, `xhigh`, then `high`. Only a
current explicit user pin may select Sol. Never inherit Primary's route silently.

After dispatch, enter one long `wait_agent`. Do not poll, duplicate a child, do overlapping
Primary work, or forward protected payloads. `{"timed_out":true}` means only that the wait window
ended; start another long wait. It is not a native timeout and must not call `native-failure`.

For a typed native failure, run `native-failure`. If its `tool_name` is non-null, invoke only that
tool with the exact `tool_input`. Map typed host status only: unsupported model to
`route_rejected`, 429 to `rate_limit`, transport to `network`, deadline to `timeout`, temporary
5xx to `service`, and non-retryable failure to `other`. Never infer failure from assistant prose.
Transient kinds retry the same owner at most three times.

Treat child results as claims. Hooks bind dispatch, cursor, baseline, scopes, acceptance IDs, and
owner. Call `next` only after the current wave settles. Primary inspects the actual delta and owns
final acceptance. Use a reviewer only for semantic/manual evidence, risk, deviation,
Primary-owned changes, or explicit user request; only reviewer `accept` satisfies its downstream
gate.

Use `$codex-cost-orchestrator:manage-cco` only for installation, doctor, policy, status,
continuation, retry, restart recovery, or cleanup.

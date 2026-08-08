---
name: orchestrate
description: >-
  Local router for substantial Codex work. Use implicitly to send closed work to native Agents
  through static Luna/Terra routes while Primary keeps intent and final authority.
---

# Orchestrate with CCO

Keep unresolved product, architecture, integration, and acceptance choices in Primary.
Put only closed child work in one plan. Each node declares an objective, role
(`explorer`, `worker`, or `reviewer`), typed scopes, acceptance IDs, and dependencies.
Omit `decision` for bounded judgment; set it to `mechanical`
only when every permitted choice is acceptance-equivalent. Declare only actual risks
or semantic/manual verification. A complete single task may be delegated when isolating
it from long conversation history is useful.

Compile once from JSON on stdin:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py plan --repo <WORKSPACE>
python -B <PLUGIN_ROOT>/scripts/control_plane.py next --capacity <N>
```

Do not pass or invent a session ID; the CLI uses the current `CODEX_THREAD_ID`.
`next` derives ready non-conflicting work, safe aggregation, workspace state, and routes. It returns
complete
`spawn_agent` inputs, not references that a Hook must expand. Dispatch every returned
input without recapturing state or rewriting its message. Use the named read profile for
explorer/reviewer and write profile for worker exactly as returned.

Automatic routes are local and network-free: mechanical explorer/worker prefers Luna,
bounded work prefers Terra, and guarded work/reviewer uses Terra. Effort order is
`max`, `xhigh`, then `high`. Sol is selected only by a current explicit user pin.
Unsupported routes remain fenced; never silently inherit Primary's model.

After dispatch, call `wait_agent` once for one long event wait. Do not poll, duplicate a
child, perform overlapping Primary work, or forward protected collaboration payloads.
Wake for a native terminal event, blocking input, or user input. A paused worker keeps
the sole cross-task workspace lease until explicit continuation or abandonment.
For a typed native failure, run `native-failure` for that dispatch. If it returns a
non-null `tool_name`, invoke that tool with only the returned `tool_input`; never pass
the outer action metadata to the native tool. Transient kinds retry the same owner at
most three times.
Map only typed host status: unsupported/unknown model to
`route_rejected`, 429 to `rate_limit`, transport to `network`, deadline to `timeout`,
temporary 5xx to `service`, and non-retryable failure to `other`. Never infer a failure
or retry from arbitrary assistant prose.

Treat every child result as a claim. Hooks bind its dispatch, cursor, wave baseline,
scopes, acceptance IDs, and owner. Call `next` after a wave
settles; Primary never supplies completed nodes. Primary inspects the actual delta and
owns final acceptance. Use an independent reviewer only for semantic/manual evidence,
risk, deviation, Primary-owned implementation, or an explicit user request.
Only reviewer `accept` evidence satisfies a downstream review dependency.

Use `$codex-cost-orchestrator:manage-cco` only for installation, doctor, policy,
status, continuation, retry, restart recovery, or cleanup.

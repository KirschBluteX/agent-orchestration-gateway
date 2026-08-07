---
name: orchestrate
description: >-
  Default local router for medium or large Codex analysis, implementation, repair,
  refactoring, and acceptance. Use implicitly to send closed work to native Agents
  through static Luna/Terra routes while Primary keeps intent and final authority.
---

# Orchestrate with CCO

Keep unresolved product, architecture, integration, and acceptance choices in Primary.
Put only closed child work in one plan. A node needs an objective, role (`explorer`,
`worker`, or `reviewer`), typed scopes (`file` or `tree`), owned acceptance IDs, and
optional dependencies. Omit `decision` for bounded judgment; set it to `mechanical`
only when every permitted choice is acceptance-equivalent. Declare only actual risks
or semantic/manual verification. A complete single task may be delegated when isolating
it from long conversation history is useful.

Compile once from JSON on stdin:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py plan --repo <WORKSPACE>
python -B <PLUGIN_ROOT>/scripts/control_plane.py next --capacity <N>
```

Do not pass or invent a session ID; the CLI uses the current `CODEX_THREAD_ID`.
`next` derives dependency readiness, exact maximum non-conflicting work, safe mechanical
aggregation, fresh workspace state, and current native routes. It returns complete
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
the sole write lease until explicit continuation or abandonment.

Treat every child result as a claim. Hooks bind it to the dispatch, cursor, exact wave
baseline, scopes, logical acceptance IDs, and native owner. Call `next` after a wave
settles; Primary never supplies completed nodes. Primary inspects the actual delta and
owns final acceptance. Use an independent reviewer only for semantic/manual evidence,
risk, deviation, Primary-owned implementation, or an explicit user request.

Use `$codex-cost-orchestrator:manage-cco` only for installation, doctor, policy,
status, continuation, retry, restart recovery, or cleanup.

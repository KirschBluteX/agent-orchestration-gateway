---
name: orchestrate
description: >-
  Route closed Codex work through one local CCO prepare command while Primary keeps intent and
  final authority. Do not call native spawn tools directly around this skill.
---

# Orchestrate with CCO

Delegate closed, typed-scope work by default. Keep only Primary authority or clarification,
an explicit direct request, and one declared tool bounded below 30 seconds in Primary.

Send one schema-validated `cco.delegation.v1` request to this command. Its scopes are
repository-relative `exact` or `prefix` objects; `work` is an atomic task, a closed DAG,
or a stateless `cco.planner-proposal.v1` DAG input.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

For complex unresolved work, one ordinary Terra/max read-only planner task may produce the
proposal before this command. Do not create a planner route, planner lifecycle, contract file,
or direct-spawn bypass.

Invoke every returned `tool_name` with only its exact `tool_input`. Do not expose model or
cost reasoning in normal output. CCO applies static routing, baselines, owner reuse, and the
assurance ladder. Guarded work receives one final reviewer unless the current plan explicitly
sets `accept_risk: true`.

After dispatch, repeat long `wait_agent` windows without progress narration until completion or
required attention. A `timed_out` result is only an expired wait window: do not retry the child or
duplicate its work. Use typed `native-failure` only for a host-observed failure.

Use `$codex-cost-orchestrator:manage-cco` only for installation, trust, lifecycle commands,
cleanup, or offline host-edge maintenance.

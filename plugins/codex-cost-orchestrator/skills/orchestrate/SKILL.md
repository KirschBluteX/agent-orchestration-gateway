---
name: orchestrate
description: >-
  Compile and route closed, typed-scope Codex work through one CCO prepare command while Primary
  keeps intent and final acceptance. Use for delegable repository work; invoke native Agent
  actions only when returned by CCO.
---

# Orchestrate with CCO

Delegate closed, independently verifiable work by default. Keep work in Primary only when it
requires Primary authority or clarification, the user explicitly requests direct execution, or
it consists of exactly one declared tool with a total upper bound under 30 seconds. Do not split
work to manufacture this exception.

Send one schema-validated `cco.delegation.v1` request to this command. Give every node explicit
acceptance IDs, a role, and repository-relative `exact` or `prefix` scopes. Concurrent writer
scopes must be disjoint. `work` is an atomic task, a closed DAG, or a stateless
`cco.planner-proposal.v1` DAG input.

When Primary has a diagnosis, put the confirmed failure sequence, relevant symbols, host response,
and minimal change boundary in the objective. The child starts there and widens discovery only if
the evidence does not reproduce or conflicts with current code.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

If work is not closed, Primary clarifies or closes it before `prepare`; do not spawn an unprepared
planner or child. A pre-existing `cco.planner-proposal.v1` value is only schema-validated DAG input,
not a route, lifecycle, contract file, or permission to bypass CCO.

Invoke every returned `tool_name` with only its exact `tool_input`. Do not expose model or
cost reasoning in normal output, alter a returned input, or add an unreturned native action. CCO
applies static routing, baselines, owner reuse, and the assurance ladder. Guarded work receives
one final reviewer unless the current plan explicitly sets `accept_risk: true`.

After dispatch, without progress narration or polling, issue one event-driven `wait_agent` with
the longest practical timeout. It wakes for completion, required attention, user input, or the
fallback timeout. A `timed_out` result only expires that wait window: issue one new long wait on
the same live dispatch; do not retry the child or duplicate its work. Use typed `native-failure`
only for a host-observed failure.

While a dispatch is running, Primary must not independently inspect, test, or solve that dispatch's
scopes. It may work on disjoint scopes, perform CCO lifecycle or status checks, or wait. Re-enter
delegated scopes only for a reported blocker, contradictory external evidence, or, after the
terminal result, one acceptance and integration review.

Assign each validation check one owner per code revision. Workers run focused checks for their
scope. After all writers settle, Primary owns aggregate and full-suite checks. Rerun only after a
relevant code change or to diagnose failure; never run one check concurrently or in both Primary
and a worker for the same revision.

Use `$codex-cost-orchestrator:manage-cco` only for installation, trust, lifecycle commands,
cleanup, or offline host-edge maintenance.

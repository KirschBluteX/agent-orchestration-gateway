# Changelog

The project follows semantic versioning.

## 1.1.1 - 2026-08-05

- Fixed global lifecycle hooks for desktop tasks whose host working directory is a
  repository parent or is outside Git; an absent transaction now remains a no-op.
- Bound pending context, exact spawn-reference expansion, workspace verification,
  and event-first Stop protection to the transaction's prepare-time repository
  identity instead of re-deriving authority from the host working directory.
- Reduced the SessionEnd hook timeout to the desktop host's three-second limit and
  kept its cleanup session-local, so hook loading no longer warns and stale sweeps
  remain on SessionStart.

## 1.1.0 - 2026-08-05

- Added a fail-closed graph dispatch transaction: the normal compiler path persists
  full capsules outside model context and returns only short native spawn references.
- Added exact per-node pre-thread rejection recovery, so one unsupported candidate can
  advance to its precompiled fallback without cancelling successful siblings.
- Added explicit DAG dependencies, completed-node input, downstream-aware selection,
  and safe whole-graph aggregation of compatible Primary microtasks.
- Added pre-spawn workspace leases: the first child requires the exact prepared state;
  later children permit only changes owned by active non-conflicting siblings.
- Added route-aware task names that expose logical role, node, model, effort, and
  generation without sacrificing deterministic length bounds.
- Added a 30-minute event-first Primary wait guard, one bounded pending-dispatch
  recovery, and fail-closed fencing instead of short status polling.
- Added terminal-only SessionEnd cleanup, with the existing bounded SessionStart
  cleanup retained as a fallback for abandoned or older-host sessions.
- Documented and enforced the fast path: close once, compile once, dispatch the ready
  batch in one model turn, then enter one long event wait.
- Validated the model-neutral profile design against Codex CLI 0.146.0: current host
  support permits explicit Luna/Terra model and effort overrides without dedicated
  model-pinned profiles.

## 1.0.0 - 2026-08-04

- Published the clean-break `cco.v7` protocol with explorer/worker/reviewer logical
  roles and mechanical/bounded/guarded assurance.
- Made CCO implicit and added a strict raw-spawn gate with one exact user-authorized
  native bypass marker.
- Replaced runtime Radar routing with deterministic network-free local policy; removed
  runtime route caches, TTLs, pricing data, and billing/token accounting.
- Added global and trusted-project route configuration plus model-only, effort-only,
  and exact current-user pins. Sol is never automatic.
- Added one graph preparation entry and compact dispatch-batch output, per-node route
  recovery to Primary, native-capacity selection, and precompiled fallbacks.
- Connected acceptance IDs through decisions, capsules, ledgers, result evidence, and
  exact node workspace deltas.
- Added ignored-file protection inside typed scopes in light mode, multi-node terminal
  artifact cleanup, persistent tombstones, cursor/generation fencing, failure
  signatures, and guarded floors after any failed/deviating result.
- Added SessionStart enforcement context and authoritative local hook-trust checks via
  Codex `hooks/list`; bootstrap never grants trust.
- Removed the obsolete fixed ten-minute review timeout and unsupported SessionEnd
  assumption. Large artifacts delete immediately; small stale sessions expire after
  24 hours.
- Reworked profiles, installer, tests, security guidance, operations, and bilingual
  documentation for the v7 contract.

## 0.8.0

- Introduced model-neutral read/write leaves, cco.v6 prepared workspace graphs,
  native-capacity selection, exact-state verification, and task-local fencing.

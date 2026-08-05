# Changelog

The project follows semantic versioning.

## 1.2.0 - 2026-08-05

- Added a graph-level decision fast path: shared facts are closed once, self-contained
  one-action context partitions may be delegated without inherited history, and no
  per-node model-classifier request is needed.
- Changed normal static routing to resolve the complete ready graph once. Per-node
  probes now run only after a combined-route failure to isolate invalid nodes.
- Clarified that native `explorer`, `worker`, and `reviewer` are the only execution
  roles; CCO remains a policy and lifecycle layer over the Codex Agent runtime.
- Made dispatch-to-wait semantics explicit: issue every ready spawn in the same model
  turn, then wait for authoritative native events without progress polling.
- Added a PreToolUse guard that prevents opaque protected collaboration payloads from
  being copied into plain `send_message` or `followup_task` strings. Unreadable
  progress, elapsed time, and an unchanged workspace no longer justify interruption.
- Added a lightweight ordinary-tool fast exit when no transaction marker exists, so
  the global hook avoids loading graph, workspace, packet, and ledger runtimes.
- Added a reviewer `review_source` fast path that reuses a terminal worker's ledger
  baseline, acceptance IDs, scopes, exact changed paths, and validated evidence.
- Updated SubagentStop for current Desktop UUID identities. Valid `continue` results
  now end the native transaction while retaining only a continuable task owner;
  invalid results retire once without triggering a formatting-only second response.
- Added exact role-aware result templates to both model-neutral leaf profiles and
  shortened Stop-hook feedback so Primary can enter one `wait_agent` call quietly.
- Added exact published 1.1.3 read/write profile hashes so `--bootstrap` upgrades an
  unchanged previous installation while still preserving unknown user modifications.
- Invalidated reviewer seed evidence whenever a continuation advances dispatch
  identity or a valid result is too large to retain, preventing stale evidence from
  being rebound to a newer capsule.
- Kept deterministic routine acceptance in Primary and reserved independent review
  for real risk, semantic/manual evidence, deviation, or an explicit user request.
- Reworked the English and Simplified Chinese READMEs around installation, default
  routes, everyday use, workspace boundaries, and concise visitor-facing behavior.

## 1.1.3 - 2026-08-05

- Added bounded non-Git directory workspaces without automatic `git init`: workers
  capture the full root, while explorers and reviewers capture declared scopes.
- Added a 20,000-file / 1 GiB preflight budget that fails before file content is
  read, plus reparse, alias, root-identity, and scope fencing.
- Added compatibility for current desktop `collaboration*` Agent tool names and
  exact pending-transaction aborts carried by shell or native-spawn inputs.
- Kept completed sibling scopes leased until the batch settles and prevented early
  prepared-artifact deletion while any transaction node remains live.
- Distinguished an exhausted candidate chain from a fallback-pending rejection so a
  newer generation can restart at rank one.
- Added stale full-bundle cleanup and capacity-safe pruning of validated terminal
  transaction records without evicting an unsettled late-PostToolUse tombstone;
  cleanup and atomic writes never enter reparse-backed state roots.
- Added reviewer delta binding: a reviewer may inherit a previously verified worker
  baseline while its ledger and read-only verification remain bound to the freshly
  captured current workspace state.
- Corrected lifecycle and hook documentation to match the seven current definitions.

## 1.1.2 - 2026-08-05

- Removed the optional SessionEnd hook because the current desktop hook browser can
  discover it without rendering the untrusted definition, leaving users no visible
  trust action.
- Consolidated cleanup under SessionStart: each new task immediately removes validated
  terminal task ledgers and their workspace artifacts from prior sessions while
  preserving live, unknown, locked, or malformed state for bounded stale recovery.
- Reduced the public installation contract to six lifecycle events and seven visible,
  independently trusted hook definitions without changing the stable `cco.v7` wire
  protocol.

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

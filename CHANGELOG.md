# Changelog

The project follows semantic versioning.

## 4.0.6 - 2026-08-09

- Give validated recovery files stable session- and content-addressed names, so a task
  selects its own recovery without loading unrelated recovery payloads.
- Reconcile older random recovery names outside the shared state-root lock and fail
  closed if an older process publishes another one during the final authoritative scan.
- Cover interrupt settlement at the 32-file recovery limit without parsing any unrelated
  recovery state.

## 4.0.5 - 2026-08-09

- Limit the shared state-root lock to the final lifecycle path rescan and state load,
  so unrelated workspaces cannot hold it across plan/wave work, state calculation,
  persistence, or artifact cleanup.
- Reject a lock-time workspace change before recovery replay, preventing an A-to-B
  workspace lock inversion while preserving the recovery publication linearization point.

## 4.0.4 - 2026-08-09

- Serialize recovery publication with the complete authoritative lifecycle decision,
  preventing a repaired foreign lifecycle from appearing after the locked rescan but
  before a Hook uses the selected state.
- Merge coordination and authoritative state loading into one context, removing repeated
  lock-and-read boilerplate across lifecycle operations.
- Remove the duplicate scope-overlap implementation so one helper owns that rule.

## 4.0.3 - 2026-08-09

- Fail closed when one Codex session has indexed and recovery state from different
  workspaces or plan lineages, and revalidate that identity after coordination locks.
- Defer repair-during-quarantine replay until the recovered state matches the workspace
  whose lock is already held, avoiding cross-workspace nested lock acquisition.
- Replay valid recovery state independently of the quarantine ownership sentinel, so a
  full unmarked root keeps the recovery in its existing capacity slot.
- Bind the release identity test to the current changelog version.

## 4.0.2 - 2026-08-09

- Recover context inheritance for active v1 continuations from their immutable wave,
  while conservatively excluding an unresolvable legacy dispatch from owner reuse.
- Replay lifecycle recovery only while holding its canonical workspace lock and only
  across an exact state or a hash-proven direct parent transition.
- Let recovery quarantine reuse its own slot at the 32-file boundary instead of
  requiring a nonexistent extra slot.
- Settle managed interrupts in one control-plane transaction and bound all
  PostToolUse work to 3.5 seconds inside the host's five-second timeout.

## 4.0.1 - 2026-08-09

- Keep active recovery state visible to its owning Codex task and bind recovery
  finalization to the same workspace, session, and plan lineage. An unrelated
  higher-revision plan can no longer discard an active writer recovery lease.
- Settle interrupts against the unique active dispatch for a reused native owner,
  without treating its retired dispatch history as an ambiguous target.
- Reserve recovery staging capacity before moving legacy state, and accept active
  `cco.wave.v1` artifacts for settlement while writing all new waves as v2.
- Apply one shared 100,000-entry budget across Git administrative state, hooks, info,
  and every resolved reparse target.

## 4.0.0 - 2026-08-09

- Return every prepared native operation as one exact `action` / `tool_name` /
  `tool_input` envelope instead of exposing bare spawn arguments.
- Reuse an idle explorer or worker owner only across a direct dependency inside the
  same plan when role, route, assurance, and narrowed scope all match. Reviewers,
  aggregates, ambiguous candidates, explicit inherited context, and expanded scopes
  always receive a fresh owner.
- Bind reused work to a fresh dispatch, task contract, acceptance set, workspace
  baseline, and result verification. A typed unavailable owner falls back once to a
  fresh spawn; transient failures retry the unchanged task on that same owner at most
  three times.

- Reserve lifecycle capacity under one root-wide lock before writing a plan, while
  keeping current-task `status` and `cleanup` usable when an old root is already over
  the 4,096-state limit.
- Keep quarantine recovery objects visible at the state-root level and replay them with
  atomic no-replace links, so a crash or filesystem failure cannot hide an active lease
  in a maintenance subdirectory or expose a partial restored JSON file.
- Share one 100,000-entry budget across Git administrative inspection and every
  resolved reparse target, instead of granting each nested digest a fresh budget.

## 3.0.1 - 2026-08-09

- Bounded lifecycle-root discovery at 4,096 JSON files using incremental directory
  enumeration, so oversized roots block admission before a native claim is recorded.
- Spool Git stdout outside process memory, reject output above 64 MiB, limit parsed Git
  output to 200,000 records, and stop Git control-directory enumeration at 100,000
  entries instead of fully materializing untrusted output first.
- Made legacy quarantine atomically move the pathname to unique staging before validating
  the moved object. Concurrent replacements remain untouched, and an object repaired
  before the move is restored or retained rather than deleted.

## 3.0.0 - 2026-08-09

- Declared the unified `prepare` interface and removal of the native-spawn bypass as a
  major-version boundary; no deprecated bypass or duplicate planning path was restored.
- Made legacy-to-indexed lifecycle migration use one directory snapshot and recover an
  interrupted, provably identical migration without losing a workspace lease.
- Prevented an elapsed deadline from overturning a committed native admission, while
  retaining explicit checkpoints before commit and bounded chunked state/directory reads.
- Made identical `prepare` retries resume a plan that has not produced a wave, and moved
  capacity plus native-catalog validation ahead of plan persistence.
- Distinguished workspace violations from temporary Git/filesystem unavailability.
  SubagentStop now has an internal deadline and asks the same child to repeat the exact
  result instead of fencing it when verification infrastructure is unavailable.
- Recaptured stale fallback waves without retrying an already rejected route, and limited
  malformed legacy quarantine to marked CCO-owned roots with content-addressed no-replace
  files.
- Kept valid legacy reader/writer leases visible in unmarked mixed state directories;
  ownership sentinels now authorize quarantine only and never suppress lease discovery.
- Classified temporary Codex transcript and compressed-rollout I/O failures as replayable
  SubagentStop infrastructure failures instead of invalid child identity.
- Made Git HEAD capture accept only an explicitly confirmed unborn symbolic branch and
  bounded non-Git child enumeration before a directory can be fully materialized.
- Revalidated legacy state identity, metadata, bytes, and lifecycle validity immediately
  before quarantine deletion so a concurrently repaired replacement is preserved.

## 2.0.4 - 2026-08-09

- Removed the public native-spawn bypass and made every requested native subagent pass
  through the same CCO plan, while retaining Codex as the only Agent runtime.
- Restored legacy `interrupting` work to its prior running or paused state so an
  uncertain old interrupt keeps its reader/writer lease until explicit settlement.
- Partitioned lifecycle filenames by canonical workspace, quarantined unindexable
  legacy files, and made malformed same-workspace state fail closed without letting an
  unrelated state file block all workspaces.
- Added a shared internal PreToolUse deadline, bounded Git and directory inspection,
  short lock waits, and rollback reserve below the host Hook timeout.
- Discarded and recaptured an unexecuted stale spawn wave instead of replaying its old
  baseline forever.
- Unified single-child, compact DAG, and full DAG creation under one `prepare` entry,
  and removed unused failure-side PostTool compatibility parsing and rollout helpers.
- Clarified that `wait_agent` window expiry is not child failure and documented the
  Codex host's fail-open Hook process boundary.

## 2.0.3 - 2026-08-09

- Conservatively migrated persisted 2.0.1 `interrupting` dispatches and logical members
  to fenced work, while filtering global lifecycle scans by canonical workspace before
  validating a same-workspace state file.
- Closed spawn and continuation verification races by persisting the existing native
  admission claim before lock-free workspace checks, revalidating revision and cross-task
  compatibility afterward, and rolling back failed verification.
- Made explicit interrupt retries settle owners already reported as interrupted, while
  preserving idempotent terminal-result precedence.
- Preserved top-level typed native failure fields, made compatibility PostToolUse failures
  use the same bounded settlement policy, and prevented repeated rearming of one route.
- Standardized continuation and native-failure actions as `tool_name` plus exact
  `tool_input` envelopes, and made benchmark summaries exit non-zero for missing runs.

## 2.0.2 - 2026-08-08

- Removed the lifecycle dependency on failure-side PostToolUse and SubagentStop events.
  Unclaimed reservations now expire safely, claimed calls remain fail-closed, terminal
  results can settle a missing postflight directly, and Primary-observed typed failures
  use one explicit, duplicate-safe settlement path with at most three exact same-owner
  transient retries.
- Made interrupt preflight validation-only. A terminal result wins the race, postflight
  is idempotent for terminal work, and fencing requires a successful native response
  whose typed previous status was active.
- Extended canonical-workspace coordination to reject every cross-task overlapping
  reader/writer pair while retaining non-overlapping read concurrency and the global
  single-writer rule.
- Moved baseline capture and result workspace verification outside lifecycle locks,
  added Hook-context lock budgets, and made lock/I/O contention retryable instead of
  fencing a valid child result.
- Tightened native result classification, corrected ready-plus-waiting DAG status,
  declared pre-3.14 Zstandard support, and made Doctor use the canonical workspace root.
- Fixed benchmark pairing for repeated modes, duplicate rollout/root-thread reuse, and
  made post-commit rollback-journal cleanup warnings accurately report an already
  committed host-edge repair.

## 2.0.1 - 2026-08-08

- Enforced one active writer across different Codex tasks for the same canonical
  workspace, including ordinary and Windows extended path aliases, with a shared OS
  coordination lock and validated lifecycle-state scan; no second lease registry was
  added.
- Made reviewer acceptance an actual dependency gate, so a terminal rejection cannot
  release downstream implementation or deployment work.
- Added two-phase interrupt settlement. Writers remain leased while the native call is
  pending, native failure restores the prior state, and only confirmed success fences.
- Added bounded same-owner recovery for strong 429, network, timeout, and temporary
  service failures. SubagentStop uses the current native blocking-continuation
  contract, retries at most three times, and fences exhaustion.
- Allowed UUID/rollout evidence to bind a native owner at SubagentStop when the spawn
  response did not expose a canonical task path, avoiding false
  `native_owner_unresolved` failures.
- Removed cross-task `--session` control, added dispatch-derived task-name suffixes,
  preserved per-member aggregate scopes/dependencies/review sources, and required
  explicit cleanup before replacing lifecycle proof.
- Made Doctor validate the workspace-effective route policy, rejected normalized
  acceptance/evidence ID collisions, fixed exact-boundary rollout tails, and reused
  the hardened JSONL/JSONL.ZST reader in benchmark usage collection.
- Removed the unreferenced runtime-inspector surface and made compact status output
  identify paused, fenced, and owner-pending work directly.

## 2.0.0 - 2026-08-07

- Replaced caller-authored graph/lifecycle details with a compact plan brief and a
  single `plan → next → native Agent → result` control-plane interface.
- Split immutable logical Plan artifacts from fresh execution Wave artifacts; one
  atomic lifecycle state now advances dependencies, logical aggregate members,
  continuations, route fallback, restart fencing, and late-result tombstones.
- Removed short dispatch references and Hook message expansion. Wave output contains
  complete compact native inputs, while exact Hooks only validate prepared work.
- Removed the global all-tool PreToolUse matcher and reduced the runtime to five exact
  Hook definitions with no UserPromptSubmit path.
- Preserved maximum non-conflicting ready selection, one writer, compatible read
  concurrency, mechanical aggregation, static Luna/Terra routing, user pins, fresh
  Git/non-Git workspace verification, and Primary final authority.
- Replaced self-hashed result envelopes with a locally parsed result bound to dispatch
  identity and continuation cursor.
- Split the 382-word implicit `orchestrate` hot skill from the on-demand `manage-cco`
  installation, doctor, configuration, and recovery skill.
- Removed compatibility adapters, duplicated transaction/ledger state, historical
  profile migration tables, and the old protocol implementation and private tests.
- Simplified clean profile installation with atomic replacement, rollback, exact
  uninstall ownership, current Hook trust inspection, and static-route doctor checks.
- Aligned Stop and SubagentStop handling with the current Codex Hook contract: first
  Stop events guard active work, compaction never fences children, and terminal child
  results prevent accidental continuation by another Hook.
- Replaced exponential ready-set search with an exact polynomial selector specialized
  to CCO's one-writer/compatible-reader conflict model.
- Moved expensive workspace verification outside the lifecycle lock, bound prepared
  continuation messages exactly, revalidated snapshot content, and shared Git metadata
  reads during plan and Wave compilation.
- Added current-task-only explicit cleanup that refuses active or paused child work and
  leaves no background retention process.

## 1.3.1 - 2026-08-07

- Extended the workspace write lease through `continuable` worker ownership and
  rejected every worker result containing graph delta outside that worker's exact
  node scopes, closing continuation and sibling-attribution gaps without adding a
  second snapshot store.
- Made prepared `workspace_root` authoritative across spawn activation,
  continuation pre/postflight, and result lifecycle lookup even when the Desktop
  event `cwd` is a repository parent.
- Protected old graph artifacts whenever a live dispatch or continuable ledger row
  can still reach them; terminal and orphan artifacts retain bounded cleanup.
- Replaced check-then-delete stale cleanup with shared OS-lock revalidation,
  strengthened complete TaskLedger row validation, and preserved malformed state
  for explicit recovery instead of treating a `state` label as terminal proof.
- Removed the fixed JSON-wrapper depth bypass for protected collaboration payloads
  and added bounded decoded-byte, node-count, and recursion limits.
- Added binary bounded rollout reads, a 256 MiB total decompression ceiling,
  explicit UTF-8 errors, and bounded session-metadata/terminal-tail inspection for
  plain and compressed Codex rollouts.
- Reclaimed expired `.cco-transaction-*` crash residue while retaining fresh or
  live transaction files and bundles.

## 1.3.0 - 2026-08-07

- Published the clean-break `cco.v8` capsule/result contract and `cco.graph.v5`
  workspace-root identity. The prepare-time root now remains immutable through
  artifacts, transactions, continuations, ledgers, and SubagentStop verification.
- Added shared OS-backed state locking and restart reconciliation so TaskLedger and
  dispatch transactions cannot race or lose active-child fencing.
- Enforced one write-capable worker per workspace while retaining non-conflicting
  read-only concurrency; scope order is normalized at graph input and overlap is
  rejected before dispatch.
- Closed Git change-detection gaps for ignored files and scoped
  `skip-worktree`/`assume-unchanged` paths, and replaced the non-Git file-only budget
  with a bounded entry budget.
- Hardened protected collaboration payload detection for nested and re-encoded host
  values, including `reasoning` objects carrying `encrypted_content`.
- Hardened host-edge repair with proof-backed terminal evidence, Windows extended
  paths, `.jsonl.zst` rollout support, and minimal permission-restricted rollback
  journals with bounded retention.
- Updated native capability parsing for current Desktop `visibility: "list"` model
  entries while keeping unknown, hidden, and disabled values fail-closed.

## 1.2.2 - 2026-08-06

- Distinguish CCO lifecycle recovery from Codex Desktop's persisted V2 task-card
  state. A completed native rollout can still retain an `open` host spawn edge and
  appear as processing even though no Agent is running.
- Add an opt-in host-edge maintenance CLI outside the Hook/runtime path. Its default
  mode is read-only; repair requires one exact parent and explicit child IDs.
- Require matching CCO role/path metadata and an authoritative final
  `event_msg/task_complete` record before an edge is eligible for repair.
- Create a consistent pre-write state backup, revalidate under an immediate
  transaction, and fail the whole repair when any requested child is absent,
  changed, or unproven.
- Document that host task-card repair is never automatic and may require a Desktop
  restart before the corrected state is visible.

## 1.2.1 - 2026-08-06

- Treat a Codex Desktop restart as an interruption boundary: active and
  dispatching children are retired and fenced at the next `SessionStart` instead
  of remaining in a stale wait state.
- Record `host_restart` on interrupted TaskLedger rows, force a guarded floor for
  the next generation, and keep late-result tombstones intact.
- Add regression coverage for same-session restart recovery and update the
  lifecycle and recovery documentation.
- Reject current Codex `reasoning` payloads that carry top-level
  `encrypted_content` before they can be copied into plain collaboration messages.
- Preserve fenced transactions that still own an active sibling during capacity
  pruning, so Stop protection and late-result fencing cannot disappear early.
- Bind SubagentStop workspace verification to the prepare-time repository stored in
  the workspace claim instead of the event's potentially parent-directory `cwd`.
- Keep prior-session graph artifacts while any dispatch sibling remains pending,
  dispatching, or active.
- Raise the workspace-scanning PreToolUse hook bound from 5 to 30 seconds so the
  documented 20,000-file non-Git budget is usable on supported Windows systems;
  ordinary no-transaction tools still take the lightweight fast exit.

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

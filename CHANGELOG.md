# Changelog

The project follows semantic versioning.

## 5.0.0 - 2026-08-10

Build: `5.0.0+codex.20260810093311`.

### Breaking migration

- Use the canonical `cco.delegation.v1` input and clean up predecessor active state before
  upgrade. There is no old active-state compatibility or migration command.
- Aggregation is removed. Routes are static: mechanical work prefers Luna only when the active
  V2 native backend exposes it, otherwise it selects Terra directly; bounded and guarded work use
  Terra.
- Reader verification now scans declared scopes. Cooperative writers remain experimental.
- Host-edge repair remains an offline, explicitly confirmed fallback.
- Cleanup is required before starting a 5.0.0 task when predecessor state, wave, lifecycle, or
  receipt artifacts are present.

### Changed

- Planner proposals are stateless, schema-validated canonical DAG input only; the control plane
  has no planner route or second planner lifecycle.
- One deterministic assurance ladder governs compiler and routing behavior. Guarded plans receive
  one final independent reviewer after every source node unless the current plan explicitly sets
  `accept_risk: true`; planner proposals cannot accept that risk, pin routes, inherit Primary
  context, or enable cooperative writers inside the proposal. Primary may explicitly apply the
  top-level cooperative override after reviewing the proposal.
- Owner reuse now requires one direct clean predecessor with exact role, assurance, selected route,
  scopes, zero inherited context, and no retry, deviation, blocker, interruption, receipt, or lease.
- Restart, native-result, and interrupt observations use replayable receipts across state commit and
  receipt finalization; current-turn transcript recovery cannot reuse a historical owner result.
- Cooperative cleanup publishes terminal ownership detachment before deleting files and uses one
  short filesystem-namespace lock to prevent preparation/orphan-cleanup races without another
  ledger or a long global lifecycle lock. Journal creation remains locked through its first
  authoritative publication; orphan liveness scans are aggregate-bounded and best-effort.
- The experimental two-writer cooperative shape may retain the compiler's single downstream final
  reviewer instead of silently falling back to serial execution.
- Exact gitlink scopes capture bounded ignored content throughout the initialized submodule, and
  offline host-edge proof scans the complete bounded lifecycle so an early interruption cannot be
  hidden by a large terminal tail.
- Clean Git writer isolation avoids recursive untracked-tree enumeration. Luna remains dormant
  when the active host advertises it only for V1; CCO never starts a second Agent runtime.
- Current durable protocols are `cco.wave.v3`, `cco.lifecycle.v2`, and `cco.receipt.v2`.

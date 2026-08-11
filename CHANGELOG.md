# Changelog

CCO follows semantic versioning while it remains pre-1.0. A pre-1.0 minor release may include
breaking changes. Public, installer, and manifest identities use a plain release version without
build metadata.

## 0.9.1 - 2026-08-10

### Changed

- `0.9.1` is the current public, installer, and manifest identity. It remains deliberately
  pre-1.0 with no build metadata.
- Offline host-edge repair now durably publishes the exact owner-only rollback journal, pins that
  current journal through retention even when host clocks are out of order, and rechecks its
  rollout proof immediately before the edge-close commit.
- Benchmark results now require every model-route counter to add exactly to its `sol`, `terra`, or
  `luna` family counter. Doctor rejects missing, duplicate, and unknown CCO Hook definitions from
  the authoritative host inventory.
- Unbound opaque Agent messages now fail closed; exact plaintext or trusted prepared-input digest
  metadata is required before spawn, reuse, continuation, or postflight settlement. Git subprocesses
  also clear repository-routing environment overrides before inspecting or mutating a workspace.
- Cooperative writers use bounded copies when declared scopes contain ignored content, apply
  deadlines retain a replayable journal, and prefix-scope reparse inspection has one shared entry
  budget. Partial rollout tails are retryable rather than permanently fencing a valid result.
- The dependency marker, Hook-trust instructions, operational security boundary, and release
  documents are aligned with the current maintenance release.

## 0.9.0 - 2026-08-10

### Release policy

- `0.9.0` introduced the pre-1.0 public release identity without build metadata.
- Historical labels from 2.x through 5.x are compressed into the pre-0.9 development history
  below. Git history remains unchanged; this release does not rewrite commits, tags, or branches.

### Changed

- CCO accepts the canonical `cco.delegation.v1` input and keeps routing static. Mechanical work
  prefers Luna only when the active V2 native backend exposes it; bounded and guarded work use
  Terra.
- The current durable protocols are `cco.wave.v3`, `cco.lifecycle.v2`, and `cco.receipt.v2`.
  Predecessor active state must be inactive and cleaned up before a new task; there is no active
  state compatibility or migration command.
- Planner proposals remain stateless, schema-validated DAG input. Guarded work receives one final
  independent reviewer unless the current plan explicitly sets `accept_risk: true`.
- Owner reuse, restart observations, cooperative cleanup, Git isolation, and offline host-edge
  repair retain the exact-state and replay protections introduced during development.

## Pre-0.9 development history (2.x through 5.x)

The 2.x through 5.x labels described internal development iterations of routing, lifecycle,
recovery, cooperative writers, and host-edge maintenance. They are summarized here as pre-0.9
development history rather than release upgrade targets; the existing Git history is retained.

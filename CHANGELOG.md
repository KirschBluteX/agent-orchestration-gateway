# Changelog

CCO follows semantic versioning while it remains pre-1.0. A pre-1.0 minor release may include
breaking changes. Public, installer, and manifest identities use a plain release version without
build metadata.

## 0.9.0 - 2026-08-10

### Release policy

- `0.9.0` is the current public release identity. It is deliberately pre-1.0 and has no build
  metadata.
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

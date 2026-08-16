# Changelog

AOG follows semantic versioning while it remains pre-1.0. A pre-1.0 minor release may include
breaking changes. Public, installer, and manifest identities use a plain release version without
build metadata.

## 0.10.0 - 2026-08-16

### Changed

- Renamed the project to Agent Orchestration Gateway and defined the AOG namespace as the sole
  public, plugin, Agent, Hook, environment, state, and protocol identity.
- Native routing now treats every bundled capability entry except an explicitly disabled one as
  available, allowing current Codex builds to route mechanical work to Luna without a backend-
  version gate.
- Automatic routing has one source of truth: static Luna/Terra defaults intersected with the host
  catalogue. The undocumented global/project route override and trusted-root configuration path
  were removed; task-level explicit model and effort pins remain supported.
- Removed the standalone offline task-card repair utility and consolidated duplicated repository
  policy, operations documentation, and overlapping contract tests.

### Runtime

- AOG remains a local admission and lifecycle gateway for Codex native Agents. It does not add a
  network proxy, provider layer, background service, dynamic route scorer, or second scheduler.
- Current durable protocols are `aog.wave.v1`, `aog.lifecycle.v1`, and `aog.receipt.v1`.

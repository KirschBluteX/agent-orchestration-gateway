# Roadmap

## 0.9.1 release readiness

- CCO remains pre-1.0: its plain `0.9.1` identity has no build metadata, and a minor release may
  make breaking changes. The historical 2.x through 5.x labels are pre-0.9 development history;
  Git history remains unchanged.
- Validate clean 0.9.1 installation, Hook trust, and the breaking-state cleanup path on current
  Windows and Linux Codex builds.
- Exercise exact/prefix reader scans, serial writers, experimental cooperative copies/worktrees,
  final review, restart fencing, commit-bound offline host-edge repair, and clock-safe journal
  retention.
- Publish reproducible functional benchmarks without turning runtime routing into an online or
  background service.

## Quality gates

- Keep the hot skill concise, executable, and centered on one canonical `prepare` command.
- Keep native Codex Agents as the only Agent runtime and preserve Primary final authority.
- Keep `cco.wave.v3`, `cco.lifecycle.v2`, and `cco.receipt.v2` strict; prefer cleanup over
  compatibility adapters.
- Add tests at the canonical envelope, compiler, routing, workspace, and lifecycle seams.

## Deliberate non-goals

CCO will not add a second coordinator, dynamic model scoring, a background process, an MCP
dependency, automatic Sol escalation, or automatic host-edge repair.

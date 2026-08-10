# Roadmap

## 5.x adoption

- Validate clean 5.0.0 installation, Hook trust, and the breaking-state cleanup path on current
  Windows and Linux Codex builds.
- Exercise exact/prefix reader scans, serial writers, experimental cooperative copies/worktrees,
  final review, restart fencing, and offline host-edge repair.
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

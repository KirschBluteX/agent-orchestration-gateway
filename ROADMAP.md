# Roadmap

## 1.1 adoption

- Validate clean install, update, trust, first dispatch, and uninstall with Windows and
  Linux users.
- Publish a tagged release and short reproducible demonstration after repository CI is
  green on the exact release commit.
- Collect voluntary issue reports for false blocks, native route rejection, model
  capability differences, and scope attribution; add no plugin telemetry.
- Measure user-visible prepare-to-spawn latency and transaction recovery behavior with
  reproducible local traces before adding another orchestration layer.
- Publish workload-matched benchmark inputs and raw observations before making any
  cost or quality claim.

## Compatibility

- Track Codex hook and native Agent contract changes against pinned release versions.
- Add macOS only after a real installation, filesystem, profile, and hook-trust test;
  do not infer support from Linux.
- Keep unsupported models and exact user pins in Primary rather than guessing.

## Possible later work

- A small local configuration assistant that validates `cco.toml` without changing
  trust or route policy.
- Better native capability injection when Codex exposes a stable direct host API.
- Opt-in benchmark export containing only user-approved aggregate observations.

CCO will not add a second Agent runtime, runtime Radar dependency, billing ledger,
automatic Sol escalation, or background polling unless the project's core scope is
explicitly reconsidered.

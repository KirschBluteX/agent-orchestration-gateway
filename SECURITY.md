# Security policy

## Report a vulnerability

Use GitHub's private vulnerability reporting for this repository. Include the CCO version,
Codex version, operating system, a minimal reproduction, workspace type, and whether a Hook or
native Agent was involved. Do not attach credentials, private source, full task transcripts, or
local state files to a public issue.

## Trust boundary

CCO is a local workflow guardrail, not an authentication system or OS security boundary. Primary,
the local user, and the host process are trusted. Native read leaves request a read-only sandbox;
writer leases, exact scopes, baselines, receipts, and state records prevent accidental drift, but
do not contain a malicious process.

Experimental cooperative writers are especially not a sandbox. Clean Git workspaces use managed
worktrees; dirty Git and directory workspaces use bounded copies. Before integration CCO records
exact backups in a bounded apply journal. Completed journal material is cleaned up; incomplete
journals and their backups remain until a safe rollback or explicit intervention.

## State and Hooks

Current state uses `cco.wave.v3`, `cco.lifecycle.v2`, and `cco.receipt.v2`. Predecessor state,
wave, lifecycle, and receipt artifacts are rejected before use and require cleanup before a new
task. State can reveal repository paths, objectives, routes, changed paths, and acceptance
evidence; protect the local state directory accordingly.

CCO ships five exact Hook definitions. It has no global all-tool matcher. Hooks bind native calls
and results to an exact dispatch, scope, baseline, owner, and receipt. A host Hook crash or timeout
can remain fail-open at the host boundary; retain Hook trust and inspect failures.

Scoped readers inspect only declared exact/prefix scopes. Normal writers are admitted one at a time
for a canonical workspace and conflicting live work fails closed. An exact initialized-submodule
scope treats that submodule as one bounded unit and includes its ignored content.

## Offline host-edge repair

Codex Desktop owns its task-card database. CCO does not mutate it from a Hook. The repair utility
is offline-only: leave the active task, keep `CODEX_THREAD_ID` unset, pass
`--offline-confirm`, and specify exact parent and child IDs. It writes an owner-only rollback
journal before a verified repair and retains its bounded journal history.
The proof reader scans the complete bounded rollout lifecycle rather than trusting a terminal
window, so an earlier interruption remains fail-closed.

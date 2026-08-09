# Security policy

## Report a vulnerability

Use GitHub's private vulnerability reporting for this repository. Include the CCO
version, Codex version, operating system, reproduction, affected workspace type, and
whether a Hook or native Agent was involved. Do not attach credentials, private source,
full task transcripts, or Codex state databases to a public issue.

## Trust model

CCO is a local workflow guardrail, not an authentication system or OS security boundary.
The user and Primary are trusted. Codex remains the only Agent runtime. Child task
messages, artifact IDs, lifecycle state, workspace snapshots, and tombstones protect
against accidental scope drift, duplicate ownership, stale continuation, and late
results; they do not defend against a malicious Primary or process with the same local
account permissions.

Read leaves request a read-only sandbox. Workers receive one CCO write lease, but the
host sandbox remains the enforcement authority. Primary must inspect the final delta.

## Local state

The default runtime root is the operating-system temporary directory under
`codex-cost-orchestrator/v9`. It contains one immutable plan, at most one active wave
baseline, one mutable lifecycle state per Codex task, and bounded owner tombstones.
Wave artifacts are removed immediately after their physical dispatches settle.

State may reveal repository paths, task objectives, model routes, changed paths, and
acceptance evidence. It does not intentionally store source file contents, API keys,
token usage, billing history, or network credentials. Apply normal local-account and
temporary-directory protections.

## Hooks

CCO ships five synchronous definitions with exact matchers. There is no global
`PreToolUse: .*` Hook. PreToolUse validates only native spawn, continuation, message,
and interrupt tools; success-only PostToolUse settles spawn, continuation, and interrupt
outcomes.
SessionStart fences work at host `resume` or `clear` recovery boundaries, never during
context compaction. Stop is an exceptional fallback for a Primary attempting to end
while a native child turn is active. SubagentStop binds the native owner, cco.v9 result,
cursor, wave, scopes, and workspace state. It never interprets arbitrary child prose as
a retryable failure. Primary-observed typed native failures use explicit lifecycle
settlement, with at most three exact same-owner retries for transient kinds. Prepared
reservations expire defensively, but a PreToolUse-claimed call retains its lease until
typed settlement, a terminal result, or restart recovery because Codex has no native
tool-failure Hook.

Current Codex treats a PreToolUse command crash or host timeout as fail-open. CCO keeps
its own admission budget below the manifest timeout, bounds Git and directory inspection,
and reserves time to roll back an unfinished claim, so ordinary deadline exhaustion
returns an explicit block. A process killed before it can return that block cannot be
made fail-closed by a plugin and remains part of the trusted-host boundary.
Lifecycle roots are limited to 4,096 JSON files; Git output is limited to 64 MiB and
200,000 records; each Git control-directory digest is limited to 100,000 entries. Limit
violations block admission and never authorize truncated evidence.
SubagentStop has a separate internal budget below its manifest timeout. Git, filesystem,
deadline, and state-lock unavailability request an exact result replay; they do not fence
the owner as though it violated scope.

Review and trust Hook hashes in `/hooks` after every update. Doctor never changes trust.

Opaque host collaboration payloads, including encrypted reasoning objects, must remain
in their typed host field. CCO rejects attempts to copy them through plain message or
follow-up text.

## Workspace protection

Git workspaces protect the canonical root, Git directory identities, HEAD, refs, config,
hooks, info, index, typed scopes, ignored in-scope files, path spellings, hidden status
entries, reparses, and submodule control state. Non-Git workspaces bind the exact root,
reject reparses and special files, enforce entry/byte budgets before hashing, and never
run `git init`.

Only one physical worker is admitted across all Codex tasks sharing a canonical
workspace. Active prepared claims, running workers, and paused workers keep the lease.
Cross-task readers are also excluded from overlapping active writers. Compatible read
leaves may run beside a non-overlapping writer; their results
permit only the known sibling writer scope and must show no read-scope delta.
Legacy state migration is scanned from one directory snapshot. A duplicate left between
the canonical write and legacy unlink is removed only when its normalized state and
revision prove it is the same migration. The state-root ownership marker authorizes
quarantine only; valid legacy lifecycle files retain lease authority without it.
Quarantine atomically stages the exact pathname object before validation and finalization,
so it never performs a validation-then-unlink against a replaceable original pathname.

## Host maintenance

Hooks never edit Codex's host database. The explicit host-edge repair tool is outside
the Hook path, requires a proof-backed retired lifecycle result, writes a minimal
permission-restricted rollback journal under the database transaction, and operates
only on explicitly selected spawn edges. Journals contain task identifiers; protect and
remove them according to local policy.

## Unsupported claims

CCO does not guarantee a specific cost reduction, measure the real bill for a task,
encrypt local state, or make a weak model suitable for an open contract. Benchmark
results must state their workload, versions, route policy, and token fields.

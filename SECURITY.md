# Security policy

Codex Cost Orchestrator is a community project, not an official OpenAI product.

## Reporting

Report suspected vulnerabilities through this repository's private GitHub security
advisory flow. Do not put credentials, private task text, customer code, or unredacted
Codex logs in a public issue.

## Boundary

CCO treats Primary as the trusted control plane. Capsules, hashes, hooks, prepared
workspace state, and the task ledger are integrity/lifecycle guardrails. They are not
encryption, authentication against a malicious Primary or local process, a durable
scheduler, or an operating-system sandbox.

Codex native Agents are the only execution runtime. Both CCO profiles disable nested
multi-Agent features. The read profile requests read-only execution, but CCO calls it
OS-isolated only when runtime metadata confirms the effective sandbox.

## Data and network behavior

CCO makes no runtime network request. It may read:

- the native capability catalogue exposed by Codex or its local CLI fallback;
- hook payloads and authoritative local hook-trust status;
- declared Git or non-Git workspace scopes and, for Git worktrees, Git control state;
- global `~/.codex/cco.toml` and a trusted project's `.codex/cco.toml`;
- its two installed Agent profiles.

It does not transmit CCO telemetry or collect credentials, billing, token usage,
Radar data, or long-term route history. A temporary prepared artifact necessarily
contains closed contracts, scopes, route bindings, and workspace fingerprints. In a
non-Git directory, hashes are captured only after a path/type/size budget preflight;
source contents are never retained. It does not copy workspace file contents or the
full conversation.

The default ledger is below the operating-system temporary directory at
`codex-cost-orchestrator/ledger`; prepared artifacts and dispatch bundles use sibling
directories. `CCO_LEDGER_DIR` may select another external location. Workspace paths
and reparse-ancestor paths are rejected, including the sibling prepared-artifact and
dispatch-bundle state roots.

Large graph artifacts are deleted once the graph transaction has no pending,
dispatching, or active nodes. Settled full dispatch bundles delete immediately;
abandoned bundles share the seven-day stale bound. Small owner tombstones remain
across turns to fence late results and raw continuations. On a Codex Desktop
restart, the next SessionStart retires and fences active or dispatching children as
`host_restart` interruptions before removing validated terminal residue. Unknown,
locked, or malformed abandoned state remains only for bounded recovery of up to
seven days.

## Hook behavior

The plugin uses SessionStart, PreToolUse, PostToolUse, Stop, UserPromptSubmit, and
SubagentStop. Ordinary raw spawn and managed raw continuation fail closed. The only
unmanaged path is the exact `CCO_NATIVE_BYPASS v1` marker after current user
authorization.

Protected collaboration values remain typed host data. PreToolUse rejects attempts
to copy opaque values—including current `reasoning` objects carrying
`encrypted_content`—into plain `send_message` or `followup_task` text.

Hooks run only when Codex reports their current hashes as enabled and trusted. Review
them through `/hooks`. Bootstrap and doctor do not grant trust; doctor only reads the
authoritative `hooks/list` result. A host-level failure to launch a hook may follow
host policy, so Primary exact-state verification remains mandatory.

Worker result paths must equal the real post-baseline delta inside that node's typed
scopes. Default light Git graphs fingerprint ignored files inside those scopes.
Non-Git workers capture the complete root with a default 20,000-file / 1 GiB
preflight; read-only roles capture declared scopes and must finish unchanged. These
checks reduce accidental scope drift; they cannot prevent a process from writing after
the check completes.

Result-time workspace verification uses the canonical repository captured during
graph preparation. A SubagentStop event's `cwd` is not authoritative because Codex
may start the parent task above the repository. Cross-session cleanup preserves graph
artifacts while a sibling remains pending, dispatching, or active, and capacity
pruning never evicts a fenced transaction that still contains an active owner.

For a reviewer of a known worker delta, the capsule may retain the worker's old
baseline identity while `current_state`, the prepared artifact, and the ledger bind
the freshly captured review state. The old SHA-256 value is not a retained source
snapshot or independent proof of provenance; Primary remains responsible for the
review contract and delta evidence.

## Dependency and update practice

- Review plugin and profile diffs before update.
- Trust modified hooks again only after review.
- Use bootstrap's byte-identity upgrade rules; it preserves unknown or modified files.
- Start a new task after install, update, disable, or uninstall.

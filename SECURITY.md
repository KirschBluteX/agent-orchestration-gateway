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
- declared repository scopes and Git control state;
- global `~/.codex/cco.toml` and a trusted project's `.codex/cco.toml`;
- its two installed Agent profiles.

It does not transmit CCO telemetry or collect credentials, billing, token usage,
Radar data, or long-term route history. A temporary prepared artifact necessarily
contains closed contracts, scopes, route bindings, and workspace fingerprints. It
does not copy repository file contents or the full conversation.

The default ledger is below the operating-system temporary directory at
`codex-cost-orchestrator/ledger`; prepared artifacts use the sibling `workspace`
directory. `CCO_LEDGER_DIR` may select another external location. Repository paths and
reparse-ancestor paths are rejected.

Large graph artifacts are deleted once all of that graph's owners are terminal. Small
owner tombstones remain across turns to fence late results and raw continuations.
SessionEnd removes residue only when the session ledger is already terminal. A later
SessionStart remains the fallback: it removes terminal residue after 24 hours and
live/unknown abandoned state only after seven days.

## Hook behavior

The plugin uses SessionStart, PreToolUse, PostToolUse, and SubagentStop. Ordinary raw
spawn and managed raw continuation fail closed. The only unmanaged path is the exact
`CCO_NATIVE_BYPASS v1` marker after current user authorization.

Hooks run only when Codex reports their current hashes as enabled and trusted. Review
them through `/hooks`. Bootstrap and doctor do not grant trust; doctor only reads the
authoritative `hooks/list` result. A host-level failure to launch a hook may follow
host policy, so Primary exact-state verification remains mandatory.

Worker result paths must equal the real post-baseline delta inside that node's typed
scopes. Default light graphs fingerprint ignored files inside those scopes. These
checks reduce accidental scope drift; they cannot prevent a process from writing after
the check completes.

## Dependency and update practice

- Review plugin and profile diffs before update.
- Trust modified hooks again only after review.
- Use bootstrap's byte-identity upgrade rules; it preserves unknown or modified files.
- Start a new task after install, update, disable, or uninstall.

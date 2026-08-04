# Operations and compatibility

## Supported surface

| Dimension | Current support |
| --- | --- |
| Host | Codex CLI/Desktop with plugins, hooks, and native Agents |
| Tested Codex contract | 0.144.6 |
| Operating system | Windows and Linux |
| macOS | Not currently tested or claimed |
| Python | 3.11+; CI exercises 3.11 and 3.14 |
| IDE or other surfaces | Not claimed unless they load the same plugin/hook contract |
| Authentication | Inherited from Codex; never read or stored by CCO |
| Network | No CCO runtime network request |

## Install lifecycle

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
```

Open `/hooks`, inspect all six current CCO hooks, and trust their hashes. Then run:

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

Doctor is read-only. It verifies Python, shipped files, exact installed profile bytes,
active project shadows, authoritative Codex hook discovery/trust, the native model
catalogue, and one representative static route. A ready installation prints `HOOKS
READY` and `STATIC ROUTE READY`.

Bootstrap performs an atomic two-profile transaction. It replaces only known
published bytes, removes only known legacy bytes, rolls back on failure, and preserves
unknown/user-modified files. Check and uninstall use the same ownership rules:

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --check
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --uninstall
```

Start a new task after any plugin, profile, or hook change.

## Static route behavior

| Role / assurance | Automatic order |
| --- | --- |
| explorer/worker mechanical | Luna, Terra |
| explorer/worker bounded | Terra, Luna |
| explorer/worker guarded | Terra |
| reviewer | Terra |

Effort adapts through `max`, `xhigh`, `high`. Sol and `ultra` are never automatic.
Current user pins override project/global policy. Unsupported exact pins and invalid
higher-priority policy remain in Primary without substitution.

The graph compiler accepts host capability metadata to avoid a CLI round trip. If it
is absent, CCO reads the PATH Codex bundled catalogue once for that preparation. It
does not contact a model or network service.

## Hooks and local state

| Event | Purpose |
| --- | --- |
| SessionStart | Inject one compact mandatory CCO reminder; prune stale sessions |
| PreToolUse spawn | Validate cco.v7 request, prepared artifact, route, and reserve owner |
| PreToolUse continuation | Require exact next cursor for managed owners |
| PreToolUse interrupt | Retire/fence before native interruption |
| PostToolUse | Activate one owner, settle continuation, or release rejected spawn |
| SubagentStop | Validate result, acceptance evidence, role, scope, and exact delta |

There is deliberately no Stop hook or unsupported SessionEnd entry. Large terminal
artifacts delete immediately. A later SessionStart removes terminal ledgers after 24
hours and live/unknown abandoned artifacts and ledgers after seven days. This keeps
fencing across multiple turns without adding per-turn hook latency.

The state root defaults to the OS temporary directory under
`codex-cost-orchestrator`. `CCO_LEDGER_DIR` may choose another external absolute
location. Do not delete state belonging to an active task.

## Failure behavior

- Profile missing/shadowed/modified or hook untrusted: no delegation; run doctor.
- Node route unavailable: only that node returns to Primary.
- Confirmed pre-thread rejection: use the next precompiled fallback only.
- Managed malformed capsule, wrong owner/cursor, stale result, or raw follow-up: block.
- Result path/evidence/workspace mismatch: keep owner fenced and return exact error.
- Incomplete, blocked, or deviation: record one failure signature and force a guarded
  newer generation.
- Luna quality failure: Terra guarded. Terra quality failure: Primary replans.
- Sol: current explicit user pin only; never automatic escalation.

## Troubleshooting

Run `--doctor` first. If hooks are not ready, use `/hooks`; do not use the dangerous
trust-bypass flag for normal operation. For a shadow, inspect `.codex/agents` and
`config.toml` declarations in the current repository and configured Codex home. For a
modified profile, compare and decide manually. For route failure, inspect the native
catalogue or supply a supported current-user pin.

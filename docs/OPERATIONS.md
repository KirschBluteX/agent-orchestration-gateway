# Operations and compatibility

## Supported surface

| Dimension | Current support |
| --- | --- |
| Host | Codex CLI/Desktop with plugins, hooks, and native Agents |
| Tested Codex contract | CLI 0.146.0; Desktop build 26.730.8199.0 |
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

Open `/hooks`, inspect all seven current CCO hooks, and trust their hashes. Then run:

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

Both CCO leaf profiles are model-neutral. The compiler sends the selected model and
effort explicitly on native spawn, so Luna does not require a dedicated profile. The
graph compiler accepts host capability metadata to avoid a CLI round trip. If it
is absent, CCO reads the PATH Codex bundled catalogue once for that preparation. It
does not contact a model or network service. The actual native spawn response remains
the final capability evidence.

## Fast dispatch sequence

1. Close the complete graph from facts already present in the user request and known
   repository policy. If one material fact is missing, use one narrow explorer.
2. Put shared facts in graph `defaults` and invoke `graph_compiler.py` once.
   A terminal worker can be handed to a reviewer with `review_source`; the compiler
   reuses its ledger baseline, acceptance IDs, scopes, changed paths, and evidence.
3. Issue every ready short spawn reference in the same Primary model turn. No other
   tool is allowed while the transaction still has pending references.
4. Call `wait_agent` once and enter one long event wait. Do not poll or send progress-only requests. Child
   completion, blocking input, or a user message wakes Primary.

Only a native terminal event proves normal completion, failure, or interruption. A
Codex Desktop restart is the explicit host interruption boundary: the next
`SessionStart` retires and fences the previous session's active or dispatching
children with `host_restart`. An opaque or unreadable progress payload and an
unchanged workspace prove nothing about Agent state. Do not copy protected payloads
including `reasoning` objects with `encrypted_content` into `send_message` or
`followup_task`; keep the owner active and wait. The 30-minute
protection timeout is a recovery wakeup, not a forced Agent interruption or implicit
completion.

A confirmed pre-thread rejection advances only that node to its precompiled fallback.
Active siblings continue. Graph identity, workspace, or transaction corruption is a
graph-level failure and fences the undispatched remainder.

## Hooks and local state

| Event | Purpose |
| --- | --- |
| SessionStart | Treat Desktop restart as `host_restart` interruption for active/dispatching children; inject the reminder, remove terminal prior sessions, and prune stale state |
| PreToolUse all tools | Exit cheaply when no transaction marker exists; otherwise use a 30-second bound to gate pending work, verify a bounded workspace, and expand one exact short spawn reference |
| PreToolUse continuation | Require exact next cursor and reject opaque protected payload forwarding |
| PreToolUse interrupt | Retire/fence before native interruption |
| PostToolUse | Activate one owner, settle continuation, or release rejected spawn |
| Stop | Concisely require `wait_agent` or perform one bounded pending-batch recovery without exposing transaction internals |
| UserPromptSubmit | Restore compact active/pending transaction context after user input |
| SubagentStop | Map native UUID to owner; validate result, evidence, role, scope, and exact delta; retire once without a formatting-only second response |

Large terminal artifacts and settled dispatch bundles delete immediately. On a Codex
Desktop restart, `SessionStart` retires active/dispatching children as
`host_restart`, records a guarded floor, and keeps owner tombstones so late results
remain fenced. It then removes validated terminal ledgers and their workspace
artifacts from prior sessions. Unknown, locked, or malformed state remains subject
to bounded stale cleanup of up to seven days. This keeps fencing across multiple
turns without adding an optional SessionEnd hook that the current desktop browser
cannot expose for trust review.

Cross-session cleanup checks the matching dispatch-transaction file before deleting
a terminal TaskLedger or its graph artifact. A pending, dispatching, or active sibling
keeps that artifact available. Capacity pruning likewise excludes any fenced
transaction that still contains an active node.

The state root defaults to the OS temporary directory under
`codex-cost-orchestrator`. `CCO_LEDGER_DIR` may choose another external absolute
location. Do not manually delete state belonging to an active task; a Desktop
restart performs the safe `host_restart` retirement at `SessionStart`.

## Non-Git directory workspaces

CCO does not run `git init`. If the exact target root is not a Git worktree, the
prepared-workspace adapter uses directory mode:

- explorer and reviewer snapshots cover only their declared scopes and accept no
  change;
- worker snapshots cover the complete root, while only its declared scope may change;
- path/type/size preflight is limited to 20,000 files and 1 GiB by default and runs
  before any file content is read; the workspace-scanning PreToolUse hook allows 30
  seconds while its ordinary no-transaction path remains a lightweight fast exit;
- symlinks, junctions/reparse points, special files, case-insensitive aliases, root
  replacement, and capture-time changes fail closed;
- snapshots contain paths, types, sizes, bounded metadata, and SHA-256 values, never
  source copies; large artifacts are deleted with the graph lifecycle.

An over-budget workspace returns the prepared batch to Primary. It is never silently
filtered, including for `node_modules` or other large dependency trees. If a ready
batch contains any worker, the shared directory baseline covers the complete root;
CCO does not keep read-only siblings by weakening that batch to a partial baseline.

## Reviewer delta baseline

A fresh reviewer normally uses the state captured at review preparation as both its
comparison and workspace-verification baseline. To review a known worker delta,
Primary may pass that worker's previously verified baseline as `review_baseline`.
The compiler then places the old identity in the reviewer capsule and places the
freshly captured review state in `current_state`; the artifact, ledger, and read-only
workspace checks remain bound to the fresh state.

`review_baseline` is a SHA-256 state identity, not an archived source snapshot.
Primary must still provide the closed contract, anchors, and evidence needed to
inspect the delta. It is reviewer-only and cannot weaken current-state verification.

For the normal terminal-worker path, prefer node `review_source` with the worker's
`node` and `contract_rev`. The compiler resolves one exact current-task ledger row,
validates its bounded result seed, and derives `review_baseline`, exact scopes,
acceptance facts, source evidence, reviewer role, and `fork_turns: none`. Primary still
supplies the new reviewer node, epoch, and closed semantic contract. Manually supplied
derived fields must match the ledger or preparation fails closed.

## Failure behavior

- Profile missing/shadowed/modified or hook untrusted: no delegation; run doctor.
- Codex Desktop restart: retire and fence active/dispatching children as
  `host_restart`; inspect the actual workspace before starting a newer generation.
- Node route unavailable: only that node returns to Primary.
- Confirmed pre-thread rejection: use the next precompiled fallback only.
- Interrupted pending dispatch: one exact recovery; a second abandonment fences only
  nodes that never became active.
- Managed malformed capsule, wrong owner/cursor, or raw follow-up: block before delivery.
- Invalid or stale SubagentStop result: retire and fence once, show Primary a concise
  status, and do not trigger another child response merely to repair formatting.
- SubagentStop workspace verification uses the prepare-time repository bound in the
  task claim; the event's `cwd` may be a repository parent and is not authoritative.
- Result path/evidence/workspace mismatch: retire and fence; Primary inspects the
  actual state before replanning.
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

# CCO v7 runtime gates

## Install, trust, and profile health

Run bootstrap explicitly; plugin installation never silently writes global profiles:

```text
python scripts/install_agents.py --workspace <repo> --bootstrap
python scripts/install_agents.py --workspace <repo> --doctor
python scripts/install_agents.py --workspace <repo> --check --profile read --profile write
python scripts/install_agents.py --workspace <repo> --uninstall
```

Bootstrap atomically installs or upgrades the two CCO-owned physical profiles and
removes only published legacy bytes. Unknown and user-modified files are preserved.
Uninstall has the same ownership rule. Python 3.11 or newer is required.

Open `/hooks`, review all CCO definitions, and trust the current hashes. Doctor calls
Codex's local `hooks/list` interface and reports `NOT READY` when any required hook is
missing, disabled, untrusted, modified, or loaded from another plugin version. Doctor
never writes trust state and never uses `--dangerously-bypass-hook-trust`.

Start a new Codex task after plugin/profile changes. SessionStart injects one compact
mandatory-dispatch reminder. PreToolUse remains the mechanical enforcement point.
The 1.2 release contract was exercised with Codex CLI 0.146.0; an updated desktop or
CLI host is required when an older host rejects a model that its catalogue lists.

## Native capability and route health

Use capability metadata exposed by the active host when available. The PATH Codex CLI
bundled catalogue is only a fallback. An entry is eligible only when it explicitly
advertises native multi-Agent v1 or v2 plus the selected reasoning effort. The actual
spawn response remains final evidence.

The two physical leaf profiles intentionally do not pin a model or effort. CCO passes
the selected model and effort explicitly on each native spawn, which preserves user
pins and lets one read/write profile pair serve Luna, Terra, and supported explicit
models. Do not add model-specific profile copies merely to route Luna.

Routing is static and local. There is no Radar request, network dependency, route TTL,
cache, pricing table, token meter, or billing history. A full current-user pin has one
candidate. A partial pin adapts only its unpinned dimension. Built-in effort fallback
is `max`, `xhigh`, then `high`. A pre-thread rejection may use only the next prepared
fallback. Unsupported or invalid high-priority policy returns that node to Primary.

Global policy: `~/.codex/cco.toml`. Project policy: `.codex/cco.toml`, read only when
the canonical repository root is listed by the global `trusted_project_roots` array.

## Native capacity and quiescence

The host's live Agent capacity is the only concurrency ceiling. CCO adds none. Admit
dependency-ready nodes with distinct responsibilities and non-conflicting typed
scopes. The deterministic selector prioritizes downstream graph progress, then uses
available capacity and stable node ordering.

Closure and placement are decided once for the ready graph. There is no per-node
model-classifier request; node validation is a deterministic local step inside that
single compilation. A self-contained one-action capsule must include the evidence tokens
`capsule:self-contained` and `context:history-not-required`; without both tokens it
stays in Primary. A low-risk open-ended document task is eligible only after Primary
has supplied a minimal outline and deterministic acceptance facts.

Prepare the graph once. The normal compiler call commits one transaction and returns
short spawn references. Issue all ready references in the same model turn without
intervening tools, route prose, status checks, or baseline recapture. Shared node
facts belong in graph `defaults`, and compatible Primary microtasks are aggregated
before routing.

The compiler resolves the complete ready graph once on the normal path. Per-node route
probes are a failure-isolation fallback only, used when the combined resolution fails;
they are not a second normal routing pass.

After spawn, Primary performs only proven non-overlapping, dependency-independent
work. Otherwise it calls `wait_agent` once and enters one long native event wait rather than polling or issuing
progress-only model requests. The protection timeout is 30 minutes and does not
interrupt children. A user message or child completion wakes Primary normally.

The global PreToolUse hook first checks for the task's tiny transaction-state marker.
An ordinary tool in a task with no marker exits before importing the ledger, workspace,
packet, or transaction runtimes. Managed spawn/continuation tools and tasks with a
state marker still take the complete fail-closed path.

The native event stream owns normal completion state. Protected or unreadable
collaboration content is progress, not a failure and not a continuation contract.
This includes current `reasoning` objects carrying `encrypted_content`. Keep its owner
active, never paste the opaque value into a message tool, and wait for
an authoritative terminal, blocking-input, user, or protection-timeout event. A
timeout alone does not retire or fence the owner. A Codex Desktop restart is the
explicit host interruption boundary; its next `SessionStart` retires and fences
active or dispatching children as `host_restart`.

## Workspace and lifecycle strength

For Git worktrees, the prepared graph fingerprints tracked content and ignored files only inside declared
typed scopes. Git status and Git control state remain global so a newly created
out-of-scope delta is still detectable; the standalone unscoped workspace CLI retains
strict whole-repository content inspection. Path aliases, reparses, junctions, and
submodules remain protected. Ignored scans fail closed above the configured file/byte
bounds.

For an exact non-Git root, CCO never initializes Git. Explorer and reviewer capture
only declared scopes and must leave them unchanged. Worker capture covers the complete
root so scope-external writes remain visible. A path/type/size preflight defaults to
20,000 files and 1 GiB and happens before content hashing. The workspace-scanning
PreToolUse hook has a 30-second bound; its ordinary no-transaction path still exits
before loading the workspace runtime. Over-budget roots return to
Primary; no directory such as `node_modules` is silently ignored. Reparse points,
special files, path aliases, root replacement, and capture-time changes fail closed.
If a ready batch contains a worker, its shared directory snapshot covers the full
root; a budget failure returns that batch to Primary instead of retaining read-only
siblings on a weaker partial baseline.

A reviewer of a known worker delta may inherit the previously verified worker state
as capsule `baseline`. The compiler separately binds the freshly captured review
state as `current_state`, and all workspace/ledger checks use that fresh state. The
old baseline is only a canonical identity; it does not retain source content.

The CLI `review_source` fast path resolves one terminal worker row in the current
task ledger and binds its old baseline, acceptance IDs, exact scopes, changed paths,
and validated result evidence. Primary supplies only the new reviewer node, epoch,
and closed review contract. This is still the same graph compiler and native runtime.

The task-local ledger lives outside the repository. It provides one active owner per
node revision, cursor single-flight, generation fencing, guarded floors, and late-
result tombstones. It is not encryption or authentication against a malicious
Primary. It is also not a second Agent runtime, durable scheduler, or protection
against a late filesystem write. Primary still inspects actual state.

Transactions retain the canonical repository captured at prepare time. Lifecycle
discovery, exact spawn-reference expansion, and Stop protection use that identity;
they do not assume the desktop task's host working directory is itself a Git
repository. SubagentStop workspace verification uses the same bound repository rather
than the event `cwd`. With no session transaction, a global hook outside Git is a no-op.

The workspace-scanning PreToolUse hook has a 30-second bound; other lightweight
lifecycle hooks keep five seconds, and SubagentStop gets 120 seconds for legitimate
result-time verification. There is no ten-minute reviewer hard timeout. Large graph
artifacts are deleted only when the graph transaction has no pending, dispatching, or
active nodes. Cross-session cleanup checks that transaction before removing a terminal
TaskLedger, and capacity pruning never removes a fenced transaction with an active
sibling. The next SessionStart retires and fences active or
dispatching children from a restarted Desktop session as `host_restart`, then
removes validated terminal state from prior sessions. Unknown, locked, or malformed
abandoned state remains subject to bounded stale cleanup of up to seven days. CCO does not register the optional SessionEnd event
because the current desktop hook browser cannot render its untrusted definition for
review.

Codex Desktop owns its native V2 task-card state separately. SessionStart retirement
cannot relabel those host-owned cards. If a proof-backed completed child remains shown
as processing, use the explicit procedure in `docs/OPERATIONS.md`; never add host-card
mutation to a Hook.

Current Desktop SubagentStop events carry a native thread UUID rather than the
canonical `/root/...` path. CCO reads only the bounded first session-metadata record
under the configured Codex sessions root, verifies child UUID, parent task, and
canonical Agent path, then matches the exact dispatch identity. A valid `continue`
result ends the native dispatch transaction while keeping the TaskLedger owner
continuable. An invalid result is retired and fenced in that same callback; the hook
does not trigger a second child model response for formatting repair.

The read profile requests OS read-only, but describe it as isolated only when observed
runtime metadata confirms the effective sandbox. If hard isolation is required and
cannot be proven, stop rather than relying only on before/after comparison.

## Recovery table

- Missing/mismatched profile or untrusted hook: stop delegation and run doctor.
- Codex Desktop restart: retire/fence active or dispatching children as
  `host_restart`, retain tombstones, and inspect the workspace before a newer generation.
- Pending batch after an interrupted dispatch turn: use its exact references once;
  a second abandoned recovery fences only the remaining undispatched nodes.
- Confirmed pre-thread native rejection: take the next precompiled fallback.
- Same owner, same contract, new evidence: one cursor continuation.
- Completed, fenced, materially changed, or cold work: newer full generation.
- Incomplete, blocked, deviation, scope/routing surprise: retire, record a canonical
  failure signature, reclose facts, and use a newer guarded generation.
- Luna quality failure: use Terra guarded; never try another Luna effort automatically.
- Terra quality failure: return to Primary for replanning.
- Repeated failure signature without new evidence: do not retry.
- Auth, policy, sandbox, malformed packet, or unsupported exact pin: do not auto-retry.
- Sol: never an automatic fallback; only a current explicit user pin may select it.

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

## Native capability and route health

Use capability metadata exposed by the active host when available. The PATH Codex CLI
bundled catalogue is only a fallback. An entry is eligible only when it explicitly
advertises native multi-Agent v1 or v2 plus the selected reasoning effort. The actual
spawn response remains final evidence.

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

After spawn, Primary performs only proven non-overlapping, dependency-independent
work. Otherwise it waits for native events rather than polling or issuing progress-
only model requests.

## Workspace and lifecycle strength

The prepared graph fingerprints tracked content and ignored files only inside declared
typed scopes. Git status and Git control state remain global so a newly created
out-of-scope delta is still detectable; the standalone unscoped workspace CLI retains
strict whole-repository content inspection. Path aliases, reparses, junctions, and
submodules remain protected. Ignored scans fail closed above the configured file/byte
bounds.

The task-local ledger lives outside the repository. It provides one active owner per
node revision, cursor single-flight, generation fencing, guarded floors, and late-
result tombstones. It is not encryption or authentication against a malicious
Primary. It is also not a second Agent runtime, durable scheduler, or protection
against a late filesystem write. Primary still inspects actual state.

Spawn/continuation hooks keep a five-second bound. SubagentStop gets 120 seconds for a
legitimate workspace scan; there is no ten-minute reviewer hard timeout. Large graph
artifacts are deleted as soon as all graph owners are terminal. Current Codex does not
expose SessionEnd, so tiny tombstones remain across turns. A later SessionStart
removes terminal residue after 24 hours and live/unknown abandoned state after seven
days.

The read profile requests OS read-only, but describe it as isolated only when observed
runtime metadata confirms the effective sandbox. If hard isolation is required and
cannot be proven, stop rather than relying only on before/after comparison.

## Recovery table

- Missing/mismatched profile or untrusted hook: stop delegation and run doctor.
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

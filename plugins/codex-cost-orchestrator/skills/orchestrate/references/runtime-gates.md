# CCO v6 runtime gates

## Install and profile health

Install or upgrade explicitly:

```text
python scripts/install_agents.py --workspace <repo> --upgrade
python scripts/install_agents.py --workspace <repo> --check --profile read --profile write
```

Upgrade creates the two current profiles and removes only obsolete CCO profile files
whose bytes match a known published hash. Unknown or user-modified files are retained.
Start a new Codex task after installation because roles, hooks, and skills load at
task creation.

Both leaves are model-neutral and non-delegating; the read leaf requests read-only.
An exact user route that native spawn rejects stops. An adaptive pre-thread rejection
may advance only to the next request precompiled in the prepared graph. Never rerun
Radar or rebuild a capsule after that rejection, and never silently substitute after
a usable owner exists.

The native loader mirrors Codex Multi-Agent V2: when bundled entries expose backend
metadata, only `multi_agent_version=v2` is eligible. Do not infer support from a model
name, Radar presence, or a v1/unmarked bundled entry. This filter is dynamic, so a
model automatically returns when the installed Codex catalog marks it V2.

Use the native spawn response as route evidence when it exposes role/model/effort.
Only when a required dimension is absent—or before claiming OS read-only—run
`scripts/inspect_agent_runtime.py` once for that exact owner. It resolves a canonical
task path in one directory pass and streams only allowlisted rollout metadata; never
scan session history on every dispatch by default.

## Route health

The graph resolver performs network/catalog work outside its non-blocking state lock.
A fully fixed pair skips Radar. A fresh cache avoids the network. An expired but
source-valid LKG returns immediately with `needs_refresh`; refresh is off the current
dispatch path. Only one LKG, one small route-state file, and a native-catalog cache are
retained. One short-lived refresh request suppresses duplicate processes for the same
stale snapshot; it is removed after success and expires after a failed attempt. Atomic
staging files are removed on success/failure and stale abandoned ones are pruned
conservatively.

## Native capacity and waiting

The host `[agents].max_concurrent_threads_per_session` or its native default owns
capacity. CCO sets no lower cap. Dispatch only dependency-ready, responsibility- and
scope-disjoint nodes. After independent Primary work ends, make one event-driven wait
with a long useful timeout. Do not poll unchanged state.

## Lifecycle strength

The ledger is outside the repository and contains one active owner per node revision.
It detects duplicate owners, concurrent cursor advancement, retired owners, and late
results. PreToolUse requires the prepared workspace artifact, and SubagentStop checks
the exact baseline against the whole graph's typed scopes before recording a result.
The ledger is not durable coordination and cannot stop a late filesystem write.
Primary must still inspect attribution and check the workspace after every returned
or rejected write result.

Light mode intentionally does not enumerate ignored paths. Strict mode fingerprints
ignored paths and fails closed above 10,000 files or 256 MiB of ignored content unless
the graph compiler is given explicit tighter or broader limits. Both modes protect Git
control state, tracked/untracked status, path aliases, reparses, and submodules as
implemented by the workspace-state schema; strict is required when ignored files are
inside the risk boundary. Spawn and continuation preflight retain a five-second fast
bound; only SubagentStop workspace verification has a 120-second protection bound so
a legitimate strict scan is not killed by the old envelope-only timeout.

Hook failures may be fail-open at the host layer. Primary evidence remains mandatory.
Call a review OS read-only only when observed runtime metadata says `read-only`; with
broader permissions use before/after state comparison or stop if hard isolation is
required.

## Recovery

- Missing/mismatched profile: stop delegation; do not use a generic fallback.
- Pre-thread adaptive rejection: advance one bound candidate and rejection ticket.
- Resident owner missing information: one evidence-bearing continuation cursor.
- Completed, cold, fenced, or materially changed work: newer full generation.
- Repeated failure signature: require a materially different intervention.
- Auth, policy, sandbox, or malformed-request failure: do not auto-retry.
- Scope surprise: preserve state, retire owner, and reclose the contract/baseline.

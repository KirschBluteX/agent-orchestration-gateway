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
may advance the bound fallback rank. Never silently substitute after a usable owner
exists.

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
retained. Atomic staging files are removed on success/failure and stale abandoned
ones are pruned conservatively.

## Native capacity and waiting

The host `[agents].max_concurrent_threads_per_session` or its native default owns
capacity. CCO sets no lower cap. Dispatch only dependency-ready, responsibility- and
scope-disjoint nodes. After independent Primary work ends, make one event-driven wait
with a long useful timeout. Do not poll unchanged state.

## Lifecycle strength

The ledger is outside the repository and contains one active owner per node revision.
It detects duplicate owners, concurrent cursor advancement, retired owners, and late
results. It is not durable coordination and cannot stop a late filesystem write.
Check the workspace after every returned or rejected write result.

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

# Runtime gates

## Contents

- [Install profiles](#install-profiles)
- [Task preflight](#task-preflight)
- [Per-spawn evidence](#per-spawn-evidence)
- [Workspace state and lease verification](#workspace-state-and-lease-verification)
- [Reviewer isolation](#reviewer-isolation)
- [Control strength](#control-strength)
- [Failure rules](#failure-rules)

## Install profiles

Resolve scripts relative to `SKILL.md`, never from the caller's current directory:

```text
<skill-dir>/../../scripts/install_agents.py
<skill-dir>/../../scripts/inspect_agent_runtime.py
<skill-dir>/../../scripts/workspace_state.py
```

Install missing profiles without overwriting user-owned files:

```text
python <install_agents.py> [--target-dir <agents-dir>] [--workspace <active-workspace>]
```

Verify exact copies without mutation:

```text
python <install_agents.py> [--target-dir <agents-dir>] \
  [--workspace <active-workspace>] --check
```

Limit installation or checking to graph-required profiles by repeating
`--profile routine`, `--profile complex`, or `--profile reviewer`. Omitting the option
selects all profiles.

The default target is `$CODEX_HOME/agents` when `CODEX_HOME` is set, otherwise
`~/.codex/agents`; the workspace defaults to the current directory. The installer also
fails when a selected role is visibly shadowed by a differing same-name profile in the
target config home or active project `.codex` layers. Start a new Codex task after
installation because agent types are discovered at task creation.

## Task preflight

Once per orchestrated task, after routing the work graph:

1. Select the reviewer plus every worker profile used by a node.
2. Run the installer's non-mutating `--check` with those `--profile` values and the
   active workspace so visible same-name role shadows fail closed.
3. Confirm profile shape: Worker templates must omit `model` and
   `model_reasoning_effort`. The reviewer template must retain `gpt-5.6-sol` and
   `high` plus its read-only request.
4. Require `spawn_agent` to expose the corresponding exact custom roles and every
   override field required by this graph.
5. When exposed, inspect the native model capability catalog for the requested model,
   supported effort, and any task-required input or tool capability. Otherwise record
   that native spawn validation is the capability probe.
6. Record the successful profile/template identity, finite route-default preference
   order, and per-node routing policy.

Repeat only after an availability failure, runtime inconsistency, or profile change.
Never use a generic agent as a routing fallback.

If a required role is absent or differs from its template, fail closed before any
delegated write. A missing override field in the live spawn schema fails closed when
that dimension is user-selected or route-defaulted. Report the exact missing or
mismatched capability and the install/check command. Do not install into `CODEX_HOME`,
edit user configuration, or downgrade to a generic agent without an explicit user
decision. Installation requires a new Codex task before the role can be considered
available.

An unavailable explicit user model or effort always fails closed. A route-default
proposal may advance only through its predeclared finite preference order. Rejection
before native spawn returns a usable canonical task path creates no owner and consumes
no attempt or lease generation; any mismatch observed after a usable worker starts is
fenced and consumes that run. Native policy remains omission, never a silent fallback.

## Per-spawn evidence

V2 spawn returns a canonical task path but no public effective role, model, or effort
details. Native argument validation proves that the proposed combination was accepted,
not that a custom role did not override it. When the local rollout is accessible, pass
either the child UUID or the exact canonical task path returned by spawn. Path lookup
uses `CODEX_THREAD_ID` as the parent by default or an explicit parent UUID:

```text
python <inspect_agent_runtime.py> [--sessions-dir <dir>] \
  --expect-role <role> --expect-model <model> --expect-effort <effort> \
  [--parent-thread-id <parent-uuid>] <child-uuid-or-canonical-path>
```

The helper emits only:

- `thread_id`
- `agent_role`
- `model`
- `effort`
- `sandbox_policy_type`
- `permission_profile_type`

It must reject invalid IDs or paths, a missing parent for path lookup, zero or multiple
rollout matches, missing required role/model/effort, and inconsistent turn metadata.
Never expose prompts, messages, paths, parent IDs, provider configuration, environment
variables, or arbitrary rollout fields.

The inspector proves effective values, not whether they came from user, route default,
native agent defaults, or parent inheritance. Keep selection source in the Sol ledger.
For an exact user or route-default dimension, pass its expectation flag. For a native
dimension, omit its expectation flag but still require the emitted value to exist and
remain consistent. The role expectation is always exact. Reviewer expectations remain
Sol High.

Stop the affected lane when public and local evidence conflict or required routing is
unobservable. On mismatch, increment the stop-generation fence, interrupt the worker,
inspect its lease delta, and reject its result. `Interrupted` is not terminal; never
follow up the fenced path, and do not transfer its lease until it is observed idle or
terminal. Do not silently retry a usable worker with a different selection.

## Workspace state and lease verification

Before issuing a write lease, capture the repository state and keep the JSON outside
the repository:

```text
python <workspace_state.py> capture --repo <repo> --output <external-baseline.json>
```

The executable form is `workspace_state.py capture`; `--output` always writes UTF-8
and refuses a destination inside the repository. After the worker stops, verify the
exact baseline-relative delta:

```text
python <workspace_state.py> verify --repo <repo> --baseline <external-baseline.json> [--allow <path> ...]
```

The executable form is `workspace_state.py verify`. An allowed value names one exact
repository-relative path; a trailing slash names that directory prefix. The helper
fails when HEAD or the Git index changed, reports paths changed since the baseline,
and returns a violation for every path outside the declared lease. It never stages,
cleans, resets, or rewrites repository files.
Omit `--allow` to enforce an empty lease for a behaviorally read-only review.

Protocol `WRITE` and `ALLOWED_PATHS` values use exact NFC Git spelling with forward
slashes and no trailing slash. When invoking the workspace helper for a deliberately
owned directory, Sol may derive its documented trailing-slash prefix form. Reject
absolute, drive, UNC, backslash, empty-segment, and dot-segment aliases. On a
case-insensitive host, compare active leases with `casefold()` before dispatch so case
aliases cannot appear disjoint.

This is a detect-only Git workspace check, not a filesystem sandbox. Git ignored paths
are outside its observation surface, and a concurrent writer can still race between
capture and verification. Use disjoint ownership, serialize shared paths, compare
critical artifacts directly, and require an actual read-only sandbox when hard
isolation is necessary.

## Reviewer isolation

The reviewer profile requests `sandbox_mode = "read-only"`, but live runtime
permission state can broaden it. Apply observed state:

- With observed `read-only`, report OS-enforced read-only review.
- With a broader sandbox, proceed only when hard isolation is not required, the
  reviewer remains behaviorally read-only, and the primary captures and compares
  exact before/after repository and artifact state. Report the broader profile as
  residual risk.
- When hard isolation is required, observation is unavailable, or any mutation occurs,
  stop the review lane.

## Control strength

Runtime-hard controls:

- Native agent dispatch requires a syntactically valid `task_name`.
- Canonical active task paths are unique.
- Role TOMLs disable collaboration tools for all leaf roles; only the reviewer pins
  model and effort.
- Native spawn rejects unsupported roles, models, efforts, or parameter combinations
  before a usable worker thread is returned.
- When trusted and executed, the plugin's PreToolUse guardrail rejects a structurally
  malformed CCO spawn before native dispatch, rebuilds readable worker/review
  preimages, and recomputes contract, input-closure, and evidence hashes. It also
  rejects unsupported spawn override fields rather than letting them bypass the role
  profile.
- The continuation hook validates `send_message` worker live steers and
  `followup_task` reviewer deltas. It recomputes each self-contained closure and
  requires the hash-bound full canonical target and immutable worker acceptance set;
  it structurally rejects worker `followup_task`.
- An observed read-only sandbox prevents reviewer writes.

Detect-only gates:

- installed-template byte comparison and visible same-name role shadow scan;
- runtime role/model/effort inspection;
- contract/input/evidence hash comparison;
- generation and bounded-counter comparison against the Sol ledger;
- changed-path subset versus lease;
- parent verification rerun;
- before/after state comparison under broadened reviewer permissions.
- It has no persistent ledger and cannot prove that a previous hash was actually
  issued, that a target is still running, that leases are disjoint, or that hook
  coverage is complete. File scanning also cannot prove provenance through an
  unexposed managed or runtime config layer; Sol must perform every observable runtime
  and stateful check.

Prompt/policy conventions:

- behavioral write leases;
- version/result echo;
- no architecture changes by workers;
- same-thread correction and review-epoch classification;
- report and verdict schemas.

Do not describe detect-only or prompt controls as filesystem locks or sandbox
guarantees. Hook failures are fail-open in current Codex and never replace primary
acceptance.

Both hook adapters decode outer JSON explicitly as UTF-8, split protocol structure only
on CR/LF, reject a recognized CCO envelope larger than 1 MiB before parsing, and
length-check bounded integers before conversion. They reject missing/unknown reserved
roles, noncanonical repository paths, unmatched run/lease suffixes, and non-passing
review evidence. Malformed outer hook input still follows Codex's fail-open behavior.

Plugin command hooks are discovered enabled but untrusted. Inspect and trust the
current definition through `/hooks` before expecting execution; a changed hash needs
another trust decision. Hook processes run with ambient OS permissions, not the
reviewer sandbox, so the shipped hooks must remain read-only. Hook failure is fail-open;
the pre-dispatch hook is a structural guardrail, not a second ledger or coordinator.

## Failure rules

- Required role unavailable: stop before delegated writes, report the exact role and
  recovery command, and wait for installation plus a new task or an explicit Sol-only
  route override.
- Missing context while a worker is observably running: send one bounded,
  hash-chained live steer. If its final result already arrived, start a new run.
- Treat a completed or idle model-neutral worker as cold even when its canonical task
  path is still known. Current V2 can transparently reload that path, but it does not
  replay the original per-spawn model/effort overrides. Never call worker
  `followup_task`; fence and retire the old owner, inspect its lease delta, and start a
  new `RUN` with a complete work packet and explicit routing within the attempt limit.
  An unknown path may still return `ThreadNotFound`.
- A missing reviewer uses a bounded fresh attempt. A role-pinned, contract-preserving
  reviewer delta may use `followup_task`, but its effective route, sandbox evidence,
  and before/after workspace state must be rechecked after the turn.
- Contract defect: revise `CONTRACT_REV`; do not hide it as a correction.
- Lane or routing mismatch: fence and stop the old owner, judge its partial delta, then
  transfer the lease generation to a new exact-role run.
- Scope conflict: stop and preserve all current state; never reset or revert unrelated
  work.
- Verification failure: recompute the failure signature from actual evidence; never
  repeat an unchanged prompt for a recurring signature.
- Normalize native failures before retry: one bounded retry for transient transport;
  reclose smaller inputs for context capacity; wait for an active turn; start a new run
  after fencing; and do not auto-retry auth/policy, sandbox, or bad-request failures.
- Reviewer `fix-first`: keep the epoch only when every material contract field remains
  fixed.
- Reviewer `rethink`: start a fresh epoch.

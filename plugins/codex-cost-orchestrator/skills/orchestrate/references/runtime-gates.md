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

Upgrade only byte-exact profiles from known published CCO templates:

```text
python <install_agents.py> [--target-dir <agents-dir>] \
  [--workspace <active-workspace>] --upgrade
```

`--upgrade` is explicit. It prepares every replacement and backup before mutation,
replaces only recognized legacy files, and rolls the complete selected batch back if
any replacement or post-install exactness check fails. Same-directory hardlink
backups restore the tested original inode, bytes, mode, and mtime; rollback refuses to
overwrite a destination whose identity or bytes changed concurrently. It does not
promise to preserve POSIX ctime or eliminate the final check/replace race. If the target filesystem cannot
create same-directory hardlinks, preparation fails before mutation. It fails before writing when any
selected destination is unknown or user-modified.

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

1. Select every worker profile used by a node, plus the reviewer only when the
   pre-dispatch acceptance chain ends in `independent`.
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

Maintain a cached checked set of successful profile checks. A clean primary graph may
omit the reviewer initially. On a primary-to-independent upgrade, if the reviewer is
not in that cached checked set, run the non-mutating
`python <install_agents.py> --workspace <active-workspace> --check --profile reviewer`
check and record success before any fix or review. A failed check blocks corrective
worker work and review; it cannot be deferred to the fresh reviewer spawn.

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

The short successful inspector command and its pass criteria are in `worker-core.md`.
Load this section only for a mismatch, unavailable or ambiguous runtime metadata, a
permission/isolation concern, or recovery. The expanded command below is for resolving
those cases; it is not a replacement for the normal success path.

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
and refuses local repository/control-directory identities plus Win32 device or UNC
spellings. After the worker stops, verify the exact baseline-relative delta:

```text
python <workspace_state.py> verify --repo <repo> --baseline <external-baseline.json> [--allow exact:<path> ...] [--allow prefix:<directory> ...]
```

The executable form is `workspace_state.py verify`. Capture emits
`cco.workspace-state.v2`; verify emits `cco.workspace-verification.v2` with
`allowed_scopes`. The snapshot
binds all tracked worktree files independently of Git status shortcuts such as
`assume-unchanged` and `skip-worktree`, recursively fingerprints initialized
submodules, their marker and protected nested control state, and also binds commit and symbolic HEAD, the index, refs, effective Git
config, hooks, `info`, selected Git administrative state, physical worktree and Git control-directory identities.
Administrative coverage includes shallow state,
object alternates and other `objects/info` data, linked-worktree registry data,
reflogs, and merge/rebase/cherry-pick/revert/bisect/sequencer pseudo-state. Each
allowed value explicitly names either one exact repository-relative path or one
directory prefix. Untyped and trailing-slash-derived values are rejected. The helper
reports observed paths changed since the baseline and returns a
violation for every observed path outside the declared lease. It never stages, cleans,
resets, or rewrites repository files. Omit `--allow` to reject all observed state
changes during a behaviorally read-only review.

Protocol `WRITE` and `ALLOWED_PATHS` entries use `exact:<path>` or `prefix:<path>`;
their canonical JSON records carry both `kind` and exact NFC Git `path` spelling with
forward slashes and no trailing slash. The kind is contract-hashed and must be passed
unchanged to the workspace helper. Reject
absolute, drive, UNC, backslash, empty-segment, dot-segment, Git-control, Win32 device,
forbidden-character, and trailing-dot/space aliases. Compare changed paths to leases
using exact Git spelling on every host; an existing case or 8.3 alias is rejected
before dispatch. Treat each indexed submodule as one atomic lease for worktree-content
changes: allow its exact root, reject child-path leases, a prefix scope at the
submodule root, and any ancestor prefix containing a gitlink. Nested index/HEAD/refs/
config/admin changes remain violations even under the exact root. Before
dispatch and verification, reject
an existing path prefix that is a symlink/reparse traversal or resolves by filesystem
identity into the Git control directories.

This is a detect-only Git workspace check, not a filesystem sandbox. Hashing all
tracked files, recursive initialized submodules, documented administrative paths and
lock files, and recursive prefix reparse scans may be material on large repositories.
A passing serialized verify may use `--next-baseline` to atomically reuse the already
computed current snapshot and avoid another full capture. Git ignored paths, NTFS alternate data streams, hardlink content aliases,
and a reparse path created after validation remain outside the complete observation
guarantee; a concurrent writer can still race between capture and verification. Use
disjoint ownership, serialize shared paths, compare critical artifacts directly, and
require an actual read-only sandbox when hard isolation is necessary.

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
- When independent acceptance is required, a missing reviewer uses a bounded fresh
  attempt. A role-pinned, contract-preserving
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

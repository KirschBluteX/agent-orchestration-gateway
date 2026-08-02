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
python <install_agents.py> [--target-dir <agents-dir>]
```

Verify exact copies without mutation:

```text
python <install_agents.py> [--target-dir <agents-dir>] --check
```

Limit installation or checking to graph-required profiles by repeating
`--profile routine`, `--profile complex`, or `--profile reviewer`. Omitting the option
selects all profiles.

The default target is `$CODEX_HOME/agents` when `CODEX_HOME` is set, otherwise
`~/.codex/agents`. Start a new Codex task after installation because agent types are
discovered at task creation.

## Task preflight

Once per orchestrated task, after routing the work graph:

1. Select the reviewer plus every worker profile used by a node.
2. Run the installer's non-mutating `--check` with those `--profile` values.
3. Require `spawn_agent` to expose the corresponding exact custom roles.
4. Record the successful profile/template identity for this task.

Repeat only after an availability failure, runtime inconsistency, or profile change.
Do not silently choose a built-in role or another model.

If a required role is absent or differs from its template, fail closed before any
delegated write. Report the exact missing or mismatched role and the install/check
command. Do not install into `CODEX_HOME`, edit user configuration, or downgrade to a
generic agent without an explicit user decision. Installation requires a new Codex
task before the role can be considered available.

## Per-spawn evidence

Use public native spawn/details metadata first. Confirm the selected role and, when
present, its model and reasoning effort. When public details omit required values and
the local rollout is accessible, run:

```text
python <inspect_agent_runtime.py> [--sessions-dir <dir>] <thread-id>
```

The helper emits only:

- `thread_id`
- `agent_role`
- `model`
- `effort`
- `sandbox_policy_type`
- `permission_profile_type`

It must reject invalid IDs, zero or multiple rollout matches, missing required
role/model/effort, and inconsistent turn metadata. Never expose prompts, messages,
paths, provider configuration, environment variables, or arbitrary rollout fields.

Expected pins:

| Role | Model | Effort |
| --- | --- | --- |
| `cost_orchestrator_routine_worker` | `gpt-5.6-luna` | `max` |
| `cost_orchestrator_complex_worker` | `gpt-5.6-terra` | `max` |
| `cost_orchestrator_reviewer` | `gpt-5.6-sol` | `high` |

Stop the affected lane when public and local evidence conflict or required routing is
unobservable. Do not add a per-spawn model/effort override.

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

- V2 requires a syntactically valid `task_name`.
- Canonical active task paths are unique.
- Role TOMLs pin model/effort and disable collaboration tools for leaf roles.
- An observed read-only sandbox prevents reviewer writes.

Detect-only gates:

- installed-template byte comparison;
- runtime role/model/effort inspection;
- changed-path subset versus lease;
- parent verification rerun;
- before/after state comparison under broadened reviewer permissions.

Prompt/policy conventions:

- behavioral write leases;
- version/result echo;
- no architecture changes by workers;
- same-thread correction and review-epoch classification;
- report and verdict schemas.

Do not describe detect-only or prompt controls as filesystem locks or sandbox
guarantees. Hook failures are fail-open in current Codex and never replace primary
acceptance.

Plugin command hooks are discovered enabled but untrusted. Inspect and trust the
current definition through `/hooks` before expecting execution; a changed hash needs
another trust decision. Hook processes run with ambient OS permissions, not the
reviewer sandbox, so the shipped hook must remain read-only.

## Failure rules

- Required role unavailable: stop before delegated writes, report the exact role and
  recovery command, and wait for installation plus a new task or an explicit Sol-only
  route override.
- Missing context: issue one bounded same-thread follow-up.
- Cold or unloaded follow-up: hard leaf roles may return `ThreadNotFound`; never
  re-enable collaboration to preserve lineage. Retire a missing worker and start a
  new `RUN` with the unchanged contract, or start a fresh review epoch when the
  missing target was the reviewer.
- Contract defect: revise `CONTRACT_REV`; do not hide it as a correction.
- Lane mismatch: stop the old owner, judge its partial delta, then transfer the lease
  to a new role-pinned run.
- Scope conflict: stop and preserve all current state; never reset or revert unrelated
  work.
- Verification failure: return actual evidence; never repeat an unchanged prompt.
- Reviewer `fix-first`: keep the epoch only when every material contract field remains
  fixed.
- Reviewer `rethink`: start a fresh epoch.

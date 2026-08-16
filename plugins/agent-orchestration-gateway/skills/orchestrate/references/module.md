# Module

Own the closed module in the task prompt. Follow repository instructions and preserve work from
other modules. Write only within the declared `exact` file or `prefix` directory scopes; reading
outside them is allowed only to understand callers and contracts. Ask the supervisor before any
scope, dependency, public behavior, or acceptance change.

## Establish the module

1. Before Git inspection or commit, require repository-override variables (`GIT_DIR`,
   `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
   `GIT_ALTERNATE_OBJECT_DIRECTORIES`, and `GIT_NAMESPACE`) to be unset and verify the top level is
   this task's worktree.
2. When the prompt supplies an expected starting commit, verify the current `HEAD` equals it before
   writing. If it differs, stop and report the mismatch. When it supplies `base_sha: null`, confirm
   the project is not a Git work tree and do not initialize Git.
3. Inspect enough current code to confirm the prompt's assumptions. If evidence contradicts the
   objective or ownership boundary, report it instead of stacking a speculative fix.
4. Assign one owner to each implementation scope and each validation check for the current revision.
   Do not have the module root and a leaf independently inspect or test the same work while it runs.

## Use leaf agents selectively

The module root remains responsible for decisions, integration, validation, and its final commit.
Use native subagents only for closed leaves that can run independently; zero children is valid.
Never exceed the prompt's child cap or eight children, and respect the currently available native
capacity.

- Use Luna/max for mechanical, deterministic leaves with an exact expected artifact or check.
- Use Terra/max for implementation, diagnosis, exploration, tests, and review that require judgment.
- Select only native `worker` for writable leaves and native `explorer` for read-only leaves or the
  reviewer. Never select a custom Agent type.
- Use `fork_turns: none` with an explicit self-contained prompt when selecting a model, so the leaf
  receives only relevant context.
- Give every writable leaf a disjoint subset of the module's scopes. Read-only leaves declare no
  writes. Tell every leaf that it is not alone, must preserve other edits, must not delegate, must
  not commit, and must return evidence and changed paths.

Do not split tightly coupled work merely to fill capacity. Use `followup_task` to reuse the same leaf
for same-scope corrections. Create another leaf only for an independent review or a genuine model
escalation.

After dispatch, use one long event-driven `wait_agent` for all live leaves. Do not poll, duplicate
their inspection, or run their checks while waiting. A timeout expires only that wait window; keep
the same agents and use another long wait only when continued waiting is required.

## Review only high-impact changes

Use at most one independent Terra/max reviewer when the implemented change affects security or
authentication, concurrency, persistence or migration, public APIs or data formats, installation or
build behavior, destructive actions, or broad cross-cutting semantics. Do not add a reviewer for a
small local or mechanically proven change.

The reviewer is read-only and inspects the final diff plus existing acceptance evidence. It does not
rerun checks without a concrete gap or contradiction. Address same-scope findings through the
original implementer when one exists, then reuse the same reviewer for the revised state.

## Deliver the module

Integrate leaf edits in the shared module worktree, inspect the complete diff, and run the module's
acceptance checks once on the final revision. Remove debug artifacts and verify every changed path is
inside the declared scopes. A writable Git module creates exactly one completion commit. A read-only
module and the approved non-Git writer exception create none. Never push or merge.

Return the commit SHA when present, changed paths, each acceptance ID with evidence, reviewer outcome,
and any residual risk. If a decision belongs to the user, return a concise blocker and stop without
guessing; the supervisor's native wait will surface it.

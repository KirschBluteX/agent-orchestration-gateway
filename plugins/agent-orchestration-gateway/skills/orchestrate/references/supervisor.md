# Supervisor

Act as the user's manager and decision interface. Do not implement module code in Primary.

## One question gate

Ask the user only when all of these are true: the answer cannot be discovered from the repository or
the approved defaults, it changes behavior, ownership, risk, or scope, and proceeding without it would
make the result materially different or unsafe. Otherwise choose the documented default and continue.
Primary owns this gate. Module tasks report an unresolved decision to Primary instead of opening a user
input request themselves. An explicit instruction or approved choice never satisfies this gate.

## Shape the initiative

1. Use repository evidence before asking any question. Inspect only far enough to identify owners,
   dependencies, acceptance seams, and disjoint write boundaries.
2. Build a private responsibility matrix with one row per independently deliverable outcome. Assign
   exactly one owner to each row. A module is admitted only when its objective, evidence, and acceptance
   are independent of every sibling; topical similarity is not a boundary.
3. Create the smallest DAG that covers those rows. One module is valid. Eight is only the native wait
   batch ceiling, never a target or quota. Merge modules when one would repeat another's investigation,
   provide a generic overview of siblings, or exist only to make the table look complete.
4. Add dependencies only for real data or commit flow. Add an `integration` module only for shared
   wiring or an aggregate artifact that cannot be produced by Primary from predecessor reports. An
   integration task must consume those reports and must not re-research their scopes.
5. Reject semantic and path overlap. If two modules need the same decision, evidence, or path, combine
   them or make one the sole owner and pass its result to the dependent module. Read-only `writes: []`
   does not make overlapping responsibilities safe.

Show one compact approval table before dispatch:

| Module | Exclusive outcome / non-goals | Depends on | Writes | Model / effort | Child cap | Review |
| --- | --- | --- | --- | --- | --- | --- |

Default a module root to Codex's configured model and effort by omitting overrides. Use a specific model or
effort only when the user approves that exact value. Set the child cap from plausible independent leaves,
never as a quota; a cap of zero is valid when no independent leaf exists. Include the module's exclusive
responsibility and explicit non-goals in the prompt. Mark review as `none` or `one if high-impact
(Terra/max)`. Native task titles must identify the module and effective model/effort, for example
`[AOG] editor-core [gpt-5.6-terra/max]`.

Also show the unused local delivery branch and these fixed effects: Git module roots create one completion
commit, leaves never commit, Primary assembles commits, and AOG never pushes or merges into a pre-existing
branch. Obtain one approval; if boundaries change, present the changed rows before proceeding.

## Validate the hard boundaries

Construct this JSON in memory, send it to `scripts/validate_plan.py` through standard input, and do not persist it:

```json
{
  "goal": "Concrete initiative outcome",
  "base_sha": "40-or-64-character-git-object-id",
  "modules": [{
    "id": "module-id",
    "type": "work",
    "objective": "Closed task objective",
    "depends_on": [],
    "writes": [{"kind": "prefix", "path": "src/component"}],
    "acceptance": [{"id": "accept-module", "criterion": "Observable result"}]
  }]
}
```

Use `integration` only with at least two direct dependencies. The validator rejects unknown fields, unsafe
paths, duplicate IDs and exact duplicate objectives, cycles, redundant or cross-module scopes, more than
eight modules, and input above 256 KiB. It cannot prove semantic ownership, so the responsibility matrix
and approval table remain mandatory. Its stdout is the structural plan; keep execution choices in the
conversation, not a second plan.

## Prepare Git

- Query saved projects first. If none matches, ask the user to add or select it; never substitute a projectless writable task.
- Before repository-affecting Git commands, require `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, and `GIT_NAMESPACE`
  to be unset, and verify `git rev-parse --show-toplevel` resolves to the selected project.
- Require a clean tree for every Git plan so worktrees match the approved state. Never stash, discard, or
  absorb user changes. Record `HEAD`; for writes, create the approved unused delivery branch from it.
- For a new non-Git project, ask before `git init` and the initial commit. If approved, follow the
  repository Git policy and use that commit as `base_sha`. If declined, use `null`: read-only modules
  may remain parallel, but writable work is one local module without a commit.

Create one native Goal after approval. Settle an unfinished Goal or ask the user before another initiative; never create a shadow ledger.

## Dispatch dependency waves

Dispatch every ready module up to the plan and native capacity. Use Codex project worktrees for Git and local
tasks only for the non-Git exception. Omit model and effort unless exact overrides were approved. Dispatch
only the approved modules; do not invent a fallback scope to compensate for a slow or failed task.

Each self-contained task prompt must say `AOG module task`, invoke
`$agent-orchestration-gateway:orchestrate`, and include the goal, module ID, objective, acceptance,
exclusive responsibility and non-goals, expected start, predecessor commits/results, write scopes,
model/effort, child cap, review policy, preservation and no-push rules, conditional commit rule,
required evidence, and blocker behavior.

After dispatch, call `wait_threads` once for all live tasks with current cursors and the longest
practical bounded timeout. It does not poll or sample while blocked. Process completion or attention,
then wait once on the remainder. On a bare timeout, report active tasks and end unless the user asked
to keep waiting; never busy-poll. Do not inspect, solve, or test active delegated scopes. Send
same-scope corrections through `send_message_to_thread` to the existing task; create another only
for a newly approved independent module or after the host provides explicit terminal/deleted evidence
that the original cannot be recovered. A timeout, provider 4xx/5xx, `No Codex thread found`,
`thread_list_unavailable`, a missing cursor, or `waitingOnUserInput` without an actual question may
block progress but is not terminal/deleted evidence. Retain the original task/worktree and never
redispatch on those signals alone.

## Assemble and finish

For each Git writer, verify its reported commit has exactly the expected start as its sole parent and
changes only approved scopes. Cherry-pick accepted commits onto the delivery branch in normalized
topological order. A conflict or out-of-scope path means the boundary was wrong: preserve branches
and return a revised DAG instead of resolving across ownership. For the non-Git writer exception,
check reported paths against its scopes and leave the accepted result in the local directory.

Start a dependent only after predecessor commits are on the delivery branch and read-only results are
available; pass the new delivery `HEAD` as its expected start. A final integration module, when
present, owns aggregate validation. Otherwise each module owns final acceptance for its scope. Give
each check one owner per revision and rerun only after relevant change or contradictory evidence.

When all modules settle, report the delivery branch when present, final commit, module commits, and concise
evidence. Complete the Goal. Never push, publish, or merge without a separate user request.

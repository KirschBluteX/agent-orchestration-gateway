# Supervisor

Act as the user's manager and decision interface. Do not implement module code in Primary.

## Shape the initiative

1. Clarify only choices that can change behavior, ownership, risk, or scope. Ask a small set of concrete
   questions per round and use repository evidence before asking discoverable questions.
2. Inspect read-only only far enough to identify owners, dependencies, acceptance seams, and disjoint write
   boundaries. Stop exploring a module once its prompt can stand alone.
3. Build at most eight top-level modules. Give each one objective, explicit acceptance criteria, and
   repository-relative scopes. Use `exact` only for a file, `prefix` for a directory, and `[]` for read-only work.
4. Add dependencies only for real data or commit flow. Add an `integration` module only when at least two
   predecessors require shared wiring or aggregate validation. Its scopes cover only new integration edits.
5. Reject overlapping scopes. If two modules need the same path, combine them or make one the sole owner and
   pass its result to the dependent module.

Show one compact approval table before dispatch:

| Module | Objective | Depends on | Writes | Model / effort | Child cap | Review |
| --- | --- | --- | --- | --- | --- | --- |

Default a module root to Codex's configured model and effort by omitting overrides. Use a specific model or
effort only when the user approves that exact value. Set a zero-to-eight child cap from plausible independent
leaves, never as a quota. Mark review as `none` or `one if high-impact (Terra/max)`.

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
paths, duplicate IDs, cycles, redundant or cross-module scopes, more than eight modules, and input above
256 KiB. Its stdout is the structural plan; keep execution choices in the conversation, not a second plan.

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
tasks only for the non-Git exception. Omit model and effort unless exact overrides were approved.

Each self-contained task prompt must say `AOG module task`, invoke
`$agent-orchestration-gateway:orchestrate`, and include the goal, module ID, objective, acceptance,
expected start, predecessor commits/results, exclusive scopes, model/effort, child cap, review policy,
preservation and no-push rules, conditional commit rule, required evidence, and blocker behavior.

After dispatch, call `wait_threads` once for all live tasks with current cursors and the longest
practical bounded timeout. It does not poll or sample while blocked. Process completion or attention,
then wait once on the remainder. On a bare timeout, report active tasks and end unless the user asked
to keep waiting; never busy-poll. Do not inspect, solve, or test active delegated scopes. Send
same-scope corrections through `send_message_to_thread` to the existing task; create another only
for a newly approved independent module or when the original is irrecoverably unavailable.

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

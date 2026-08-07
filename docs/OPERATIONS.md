# CCO operations

Normal tasks use the thin `orchestrate` skill. This document covers installation,
configuration, lifecycle recovery, and explicit host maintenance.

## Install or update

From the repository root:

```text
codex plugin marketplace add .
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --bootstrap
```

Add `--replace` only when intentionally replacing the two exact CCO profile files.
The installer stages both profiles before replacement and rolls back a partial write.
It does not overwrite another filename or remove modified content.

Open `/hooks`, review and trust these five definitions:

1. SessionStart
2. exact PreToolUse for native spawn, continuation, message, and interrupt tools
3. exact PostToolUse for spawn and continuation
4. Stop fallback
5. SubagentStop for the two CCO leaf profiles

Session recovery runs only for `resume` or `clear`; context compaction does not fence
live child work. A terminal CCO result returns `continue:false` so another matching Hook
cannot accidentally run the completed child again.

Start a new Codex task, then run:

```text
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --doctor
```

Doctor is read-only. It verifies Python, exact profiles, manifest identity, Hook
discovery/trust, and at least one native static route.

## Normal control-plane flow

`control_plane.py` reads the current task from `CODEX_THREAD_ID`. Do not pass another
task's ID.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py plan --repo <PROJECT>
python -B <PLUGIN_ROOT>/scripts/control_plane.py next --capacity <N>
```

`plan` reads one brief from stdin. External scopes use `file` or `tree`. The only
required node fields are `id`, `role`, `objective`, `acceptance`, and `scopes`.

```json
{
  "goal": "Update and verify the parser",
  "acceptance": {
    "A01": "The parser accepts the new form",
    "A02": "Existing forms remain valid"
  },
  "nodes": [
    {
      "id": "parser_change",
      "role": "worker",
      "objective": "Implement the closed parser change",
      "acceptance": ["A01", "A02"],
      "scopes": [{"kind": "tree", "path": "src/parser"}]
    }
  ]
}
```

Omitted `decision` means bounded judgment. Set `decision` to `mechanical` only when
all allowed choices are acceptance-equivalent. `verification=semantic|manual` or a
non-empty `risks` list raises the route to guarded.

`next` returns complete native inputs. Dispatch them unchanged, then enter one long
`wait_agent`. Call `next` again only after the current wave settles. It advances
logical dependencies from lifecycle evidence; Primary never supplies completed nodes.

## Lifecycle operations

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py status
python -B <PLUGIN_ROOT>/scripts/control_plane.py continue --dispatch <sha256:id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py abandon --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py retry --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py restart
python -B <PLUGIN_ROOT>/scripts/control_plane.py cleanup
```

- `continue` reads a non-empty JSON evidence delta from stdin and returns one exact
  `followup_task` input. Dispatch it unchanged.
- `abandon` fences paused work and releases its write lease.
- `retry` creates a guarded newer generation for a fenced logical node.
- `restart` fences starting, running, and paused native turns. Inspect the workspace
  before retrying.
- `cleanup` deletes only the current task's inactive state and artifacts. It refuses
  active or paused child work; retain state until host-card maintenance is unnecessary.

State transitions are `waiting → ready → starting → running → retired`, with
`running → paused → starting` for continuation and `starting/running/paused → fenced`
for interruption. A paused writer keeps the sole workspace write lease.

Wave artifacts contain the fresh baseline and are deleted as soon as every physical
dispatch in that wave becomes terminal. The compact plan, lifecycle result evidence,
and bounded tombstones remain available for recovery and host proof.

## Static route policy

Global configuration: `~/.codex/cco.toml`.

Project configuration: `<PROJECT>/.codex/cco.toml`, loaded only when the canonical
project root is present in global `trusted_project_roots`.

```toml
trusted_project_roots = ["C:/work/project"]

[routes.explorer.mechanical]
candidates = [
  { model = "gpt-5.6-luna", effort = "max" },
  { model = "gpt-5.6-terra", effort = "max" },
]
```

Automatic policy may use `max`, `xhigh`, or `high`, cannot select Sol, and cannot use
Luna for guarded or reviewer work. A current explicit user pin can select any native
supported pair and has no silent fallback when fully fixed.

## Workspace rules

Git plans bind the canonical worktree root and protect Git control state, index, refs,
typed scopes, path aliases, ignored in-scope files, submodules, and hidden status
entries. A worker result must report the exact verified delta in its logical scopes.

Non-Git plans bind the exact directory root and never initialize Git. A write wave
captures the complete root; read-only waves capture declared scopes. Defaults are
20,000 total entries and 1 GiB.

## Host task-card maintenance

Codex Desktop owns task cards separately from CCO. CCO Hooks never modify the host
database. If a proof-backed terminal child remains displayed as processing, audit it:

```text
python -B plugins/codex-cost-orchestrator/maintenance/repair_host_edges.py --state-root <CCO_STATE_DIR> --check
```

Repair only explicitly named edges after inspecting the audit and backup:

```text
python -B plugins/codex-cost-orchestrator/maintenance/repair_host_edges.py --state-root <CCO_STATE_DIR> --parent-thread-id <PARENT_UUID> --child-thread-id <CHILD_UUID> --repair
```

The tool requires matching native session metadata, `task_complete`, a valid cco.v9
result, and a matching retired lifecycle dispatch. Paused, blocked, stale, malformed,
or hash-mismatched work is not repairable.

## Remove profiles

```text
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --uninstall
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
```

Uninstall removes only files that exactly match the current templates.

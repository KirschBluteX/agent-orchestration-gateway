# CCO operations

Normal tasks use the thin `orchestrate` skill. This document covers installation,
configuration, lifecycle recovery, and explicit host maintenance.

## Install or update

From the repository root:

```text
codex plugin marketplace add .
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python -m pip install -r requirements.txt
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --bootstrap
```

Add `--replace` only when intentionally replacing the two exact CCO profile files.
The installer stages both profiles before replacement and rolls back a partial write.
It does not overwrite another filename or remove modified content.

Open `/hooks`, review and trust these five definitions:

1. SessionStart
2. exact PreToolUse for native spawn, continuation, message, and interrupt tools
3. exact success-only PostToolUse for spawn, continuation, and interrupt settlement
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
discovery/trust, and at least one route under the workspace-effective static policy.

## Normal control-plane flow

`control_plane.py` reads the current task from `CODEX_THREAD_ID`. Do not pass another
task's ID.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <PROJECT> --capacity <N>
```

`prepare` reads a compact brief from stdin, creates the plan, captures one baseline, and
returns complete native inputs for the first ready wave. A single child needs `role`,
`objective`, acceptance criterion strings, and scopes. A compact DAG uses `goal` and
`nodes`; every node adds `id` and may add `depends_on` or `review_of`. External scopes use
`file` or `tree`.

```json
{
  "goal": "Update and verify the parser",
  "nodes": [
    {
      "id": "parser_change",
      "role": "worker",
      "objective": "Implement the closed parser change",
      "acceptance": [
        "The parser accepts the new form",
        "Existing forms remain valid"
      ],
      "scopes": [{"kind": "tree", "path": "src/parser"}]
    }
  ]
}
```

When nodes must share named acceptance IDs, submit a full brief with top-level
`acceptance` to the same `prepare` command. An existing lifecycle state must be explicitly
cleaned before another plan can be created in the same Codex task. No form needs a
temporary contract file.

Omitted `decision` means bounded judgment. Set `decision` to `mechanical` only when
all allowed choices are acceptance-equivalent. `verification=semantic|manual` or a
non-empty `risks` list raises the route to guarded.

`prepare` and `next` return complete native inputs. Dispatch them unchanged, then enter
one long `wait_agent`. Call `next` only after the current wave settles. It advances
logical dependencies from lifecycle evidence; Primary never supplies completed nodes.
`wait_agent` returning `timed_out:true` means only that the wait window ended. The child
is still active: enter another long wait and do not call `native-failure`.

## Lifecycle operations

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py status
python -B <PLUGIN_ROOT>/scripts/control_plane.py continue --dispatch <sha256:id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py native-failure --dispatch <sha256:id> --kind <kind>
python -B <PLUGIN_ROOT>/scripts/control_plane.py abandon --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py retry --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py restart
python -B <PLUGIN_ROOT>/scripts/control_plane.py cleanup
```

- `continue` reads a non-empty JSON evidence delta from stdin and returns
  `action`, `tool_name`, and `tool_input`. Invoke `tool_name` with only `tool_input`;
  do not pass the outer metadata to the native tool.
- `native-failure` settles one Primary-observed typed host failure. Supported kinds are
  `rate_limit`, `network`, `timeout`, `service`, `route_rejected`, and `other`. When its
  result has a non-null `tool_name`, invoke it with only the returned `tool_input`.
  Transient failures permit at most three exact retries of the same native owner.
- `abandon` fences paused work and releases its write lease.
- `retry` creates a guarded newer generation for a fenced logical node.
- `restart` fences active prepared claims, running turns, and paused turns. Inspect the
  workspace before retrying.
- `cleanup` deletes only the current task's inactive state and artifacts. It refuses
  active or paused child work; retain state until host-card maintenance is unnecessary.
- `status` includes compact counts and direct identities for paused, fenced, and
  owner-pending dispatches.

State transitions are `waiting → ready → starting → running → retired`, with
`running → paused → starting` for continuation. Interrupt PreToolUse is validation-only;
the dispatch is fenced only when the successful native result says its previous status
was active or already interrupted. A terminal result may therefore win the interrupt race.
The existing `starting` claim is persisted before lock-free workspace verification and
revalidated afterward; verification failure rolls it back. Prepared claims,
running writers, and paused writers keep the sole canonical-workspace lease across every
Codex task. Overlapping readers and writers are mutually excluded across tasks. A reviewer
dependency is satisfied only by `outcome=accept`.

Lifecycle v1 files left by 2.0.1 with `interrupting` state recover the recorded prior
`running` or `paused` state. If that field is absent, CCO conservatively restores
`running`; the uncertain native owner keeps its lease until restart, a terminal result,
or explicit interrupt settlement. New lifecycle filenames carry canonical workspace and
task digests, so an invalid indexed state blocks only its workspace. Unindexable malformed
legacy files move to the local `quarantine` directory for manual inspection.

Current Codex emits PostToolUse only after a successful native tool call and does not emit
SubagentStop for sampling failures. An unclaimed dispatch reservation expires after two
minutes and is rearmed only after admission is checked again. Once PreToolUse records a
native call identity, its lease stays fail-closed; overdue `status` output reports
`native_settlement_required`. Use `native-failure` as soon as Primary observes a typed host
error; never infer a retry from arbitrary assistant prose. A successful spawn response
without a canonical owner stays running as owner-pending; trusted UUID/rollout evidence may
bind it when the result stops.

CCO PreToolUse work uses a shared internal deadline, bounded Git subprocesses, and short
lock waits with rollback reserve below the 30-second manifest timeout. Current Codex logs
a Hook command crash or timeout but still allows the native tool; this host-level
fail-open behavior is outside a plugin's enforcement authority. Treat CCO as a workflow
guardrail, keep Hooks trusted, and inspect host Hook failures.

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

# CCO operations

Normal tasks use the thin `orchestrate` skill. This guide covers installation, lifecycle
commands, cooperative isolation, and explicit host maintenance.

## Install or update

```text
python -m pip install -r requirements.txt
codex plugin marketplace add .
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --bootstrap
```

Python 3.11+ is required; `zstandard` is required on Python versions below 3.14. Review and trust
the five exact CCO entries in `/hooks`, then start a new Codex task and run doctor:

```text
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --doctor
```

Doctor rejects missing, duplicate, or unknown CCO Hook definitions from the authoritative host
inventory.

CCO is pre-1.0. Version `0.9.2` is the plain public, installer, and manifest identity: it has no
build metadata, and a pre-1.0 minor release may make breaking changes. Historical labels from 2.x
through 5.x are compressed into pre-0.9 development history; Git history remains unchanged.
Current records are `cco.wave.v3`, `cco.lifecycle.v2`, and `cco.receipt.v2`. If predecessor state,
wave, lifecycle, or receipt artifacts exist, first ensure their work is inactive, clean them up,
and then start a new task. There is no compatibility mode or migration command.

## Normal flow

Use the single canonical preparation command:

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <PROJECT> --capacity <N>
```

Send a closed `cco.delegation.v1` envelope on stdin. Each repository scope is an exact or prefix
object. Invoke returned `tool_name` values only with their supplied `tool_input`. `next` advances a
settled plan. After dispatch, repeat long `wait_agent` windows until completion or required
attention. A `timed_out` result means another long wait, not a retry, progress report, or duplicate
execution.

The default `trusted_host` policy supports the opaque whole-message input emitted by current Codex
Desktop builds. Preflight requires one uniquely matching prepared visible envelope and durably
binds the actual ciphertext digest plus `tool_use_id`; postflight must present the same pair. This
is a host-trust compatibility mode, not proof of the hidden plaintext. To fail closed on every
opaque Agent input, set `CCO_OPAQUE_MESSAGE_POLICY=strict` in the environment inherited by Codex
and start a new Codex task. Already admitted calls remain settlement-compatible so a policy change
cannot strand their leases.

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py status
python -B <PLUGIN_ROOT>/scripts/control_plane.py continue --dispatch <sha256:id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py native-failure --dispatch <sha256:id> --kind <kind>
python -B <PLUGIN_ROOT>/scripts/control_plane.py abandon --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py retry --node <node_id>
python -B <PLUGIN_ROOT>/scripts/control_plane.py restart
python -B <PLUGIN_ROOT>/scripts/control_plane.py cleanup
```

`retry` creates a guarded newer generation. `cleanup` is current-task-only and refuses active or
paused work. Do not edit lifecycle JSON by hand.

## Experimental cooperative writers

Cooperative isolation is opt-in and accepts only two independent, non-overlapping fresh writers,
optionally followed by the compiler's single final reviewer.
Clean Git workspaces receive managed worktrees; dirty Git and directory workspaces receive bounded
copies. CCO stages exact backups and a bounded apply journal before integration. Successful cleanup
removes completed isolate and journal material; incomplete journals and backups are retained until
rollback or explicit intervention. This is a coordination boundary, not a sandbox.

## Offline host-edge repair

Codex Desktop owns persisted task-card edges. The repair tool is an offline fallback and is never
called by a Hook. Leave the active task, keep `CODEX_THREAD_ID` unset, use `--offline-confirm`, and
name the exact parent and every child that may be closed. It creates an owner-only rollback journal
before repair, retains that current journal despite clock anomalies, and rechecks the rollout proof
immediately before the database commit.

```text
python -B plugins/codex-cost-orchestrator/maintenance/repair_host_edges.py --check
python -B plugins/codex-cost-orchestrator/maintenance/repair_host_edges.py --repair --offline-confirm --parent-thread-id <PARENT_UUID> --child-thread-id <CHILD_UUID>
```

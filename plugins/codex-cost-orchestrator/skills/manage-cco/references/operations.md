# Operations

## Install and doctor

```text
python -B scripts/install_agents.py --workspace <repo> --bootstrap
python -B scripts/install_agents.py --workspace <repo> --uninstall
```

Use `--replace` only when explicitly replacing the two CCO-owned profile files. Review and trust
the five exact CCO Hook definitions in `/hooks`, then begin a new Codex task before running doctor:

```text
python -B scripts/install_agents.py --workspace <repo> --doctor
```

Doctor rejects missing, duplicate, and unknown CCO Hook definitions reported by the host.

Current Desktop builds may expose an opaque whole-message value at the Agent Hook boundary. The
default `trusted_host` policy binds its exact digest and native call ID to one uniquely matching
prepared visible envelope. Set `CCO_OPAQUE_MESSAGE_POLICY=strict` in the environment inherited by
Codex, then start a new task, to reject every opaque Agent admission. Strict mode is optional and
does not prevent settlement of an attempt that already has a durable receipt.

## Lifecycle

```text
python -B scripts/control_plane.py status
python -B scripts/control_plane.py continue --dispatch <sha256:id>
python -B scripts/control_plane.py native-failure --dispatch <sha256:id> --kind <kind>
python -B scripts/control_plane.py abandon --node <node_id>
python -B scripts/control_plane.py retry --node <node_id>
python -B scripts/control_plane.py restart
python -B scripts/control_plane.py cleanup
```

`continue` reads one non-empty evidence delta from stdin. A `timed_out` wait is another wait,
not a native failure. `retry` makes a guarded newer generation. `cleanup` is current-task-only
and refuses active or paused work.

CCO is pre-1.0. Its current public, installer, and manifest identity is `0.9.3` with no build
metadata, and a pre-1.0 minor release may make breaking changes. Historical labels from 2.x
through 5.x are pre-0.9 development history; Git history remains unchanged. Predecessor active
state, wave, lifecycle, and receipt artifacts are rejected before use. Remove those old artifacts
only after their work is known inactive, then start a new task. No compatibility or migration
command exists.

## Offline host task-card maintenance

Run the repair utility only outside an active Codex task with `CODEX_THREAD_ID` unset. `--check`
is read-only. `--repair` requires `--offline-confirm`, one exact parent UUID, and every exact
child UUID it may close. It durably creates an owner-only rollback journal before changing an edge,
retains that current journal despite clock anomalies, and rechecks the bound rollout proof just
before the database commit.

```text
python -B <PLUGIN_ROOT>/maintenance/repair_host_edges.py --check
python -B <PLUGIN_ROOT>/maintenance/repair_host_edges.py --repair --offline-confirm --parent-thread-id <PARENT_UUID> --child-thread-id <CHILD_UUID>
```

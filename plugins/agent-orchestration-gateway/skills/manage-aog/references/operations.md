# Operations

## Install and doctor

```text
python -B scripts/install_agents.py --workspace <repo> --bootstrap
python -B scripts/install_agents.py --workspace <repo> --uninstall
```

Use `--replace` only when explicitly replacing the two AOG-owned profile files. Review and trust
the five exact AOG Hook definitions in `/hooks`, then begin a new Codex task before running doctor:

```text
python -B scripts/install_agents.py --workspace <repo> --doctor
```

Doctor rejects missing, duplicate, and unknown AOG Hook definitions reported by the host.

Current Desktop builds may expose an opaque whole-message value at the Agent Hook boundary. The
default `trusted_host` policy binds its exact digest and native call ID to one uniquely matching
prepared visible envelope. Set `AOG_OPAQUE_MESSAGE_POLICY=strict` in the environment inherited by
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

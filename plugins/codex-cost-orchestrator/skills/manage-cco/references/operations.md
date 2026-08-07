# Operations

## Install and doctor

```text
python -B scripts/install_agents.py --workspace <repo> --bootstrap
python -B scripts/install_agents.py --workspace <repo> --bootstrap --replace
python -B scripts/install_agents.py --workspace <repo> --doctor
python -B scripts/install_agents.py --workspace <repo> --uninstall
```

Use `--replace` only when the user explicitly intends to replace the two exact CCO
profile filenames. It never touches another agent profile. Review and trust all CCO
entries in `/hooks`, then start a new Codex task.

## Lifecycle

```text
python -B scripts/control_plane.py status
python -B scripts/control_plane.py continue --dispatch <sha256:id>
python -B scripts/control_plane.py abandon --node <node_id>
python -B scripts/control_plane.py retry --node <node_id>
python -B scripts/control_plane.py restart
python -B scripts/control_plane.py cleanup
```

`continue` reads one non-empty JSON evidence delta from stdin. Dispatch its output with
`followup_task` unchanged. A Desktop restart fences every starting, running, or paused
native turn; inspect the workspace before `retry`.

`cleanup` is current-task-only and refuses active or paused child work. Run it only
after host-card maintenance is no longer needed.

## Static routes

Global route policy lives at `~/.codex/cco.toml`. Project policy at `.codex/cco.toml`
is read only when the canonical project root appears in global
`trusted_project_roots`. Automatic policy cannot select Sol; a current user pin can.

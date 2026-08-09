---
name: manage-cco
description: >-
  Install, configure, diagnose, inspect, recover, retry, abandon, or remove Codex Cost
  Orchestrator. Use for hook trust, agent profiles, route pins, paused/fenced work,
  Desktop restart recovery, and explicit lifecycle maintenance; not for normal routing.
---

# Manage CCO

Use this cold-path skill only when normal orchestration is unavailable or the user asks
to manage CCO itself. Read [references/operations.md](references/operations.md) for exact
commands and failure rules.

Run installer actions from the plugin root. `--bootstrap` performs a clean install and
never overwrites an unrelated profile. `--replace` is an explicit replacement of the two
CCO-owned profile paths. `--doctor` is read-only and checks profiles, plugin identity,
Hook visibility/trust, Python, and native route availability. Start a new Codex task after
installing or changing trusted Hooks.

Use the control-plane CLI for lifecycle work. Except for the root-wide
`migrate-recoveries` command, it reads the current task identity from `CODEX_THREAD_ID`;
never substitute a parent, child, or handoff task ID.

- `status`: show compact logical state counts.
- `continue --dispatch <id>`: read a non-empty evidence delta from stdin and return an
  `action` / `tool_name` / `tool_input` envelope for the same native owner. Invoke the
  named tool with only `tool_input`.
- `native-failure --dispatch <id> --kind <kind>`: settle a typed native call or sampling
  failure and return the same envelope shape. A null `tool_name` means no native call.
- `abandon --node <id>`: fence paused work and release its lease.
- `retry --node <id>`: create a guarded newer generation for fenced work.
- `restart`: explicitly apply the same interruption fencing as SessionStart.
- `cleanup`: remove only the current task's inactive lifecycle state and artifacts;
  active or paused child work is refused.
- `migrate-recoveries`: migrate pre-4.0.7 random recovery names one durable file at a
  time. It needs no current task identity. Run it when a Hook requests it; invalid files
  in an unmarked shared state root remain untouched and fail closed.

Do not edit lifecycle JSON by hand, mutate Codex's host database from a Hook, or treat a
Desktop task card as CCO authority. Use explicit maintenance only after evidence proves a
host-owned card is stale. Preserve backups and report any manual host repair.

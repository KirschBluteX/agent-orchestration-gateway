---
name: manage-cco
description: >-
  Install, diagnose, inspect, continue, retry, restart, clean up, or perform offline host-edge
  maintenance for Codex Cost Orchestrator. Do not use for normal task routing.
---

# Manage CCO

Use this cold-path skill only when normal orchestration is unavailable or the user asks to manage
CCO. Read [references/operations.md](references/operations.md) for exact commands and safety
rules.

Except for offline host-edge repair, lifecycle commands use the current `CODEX_THREAD_ID`; never
substitute another task ID. Current commands are `status`, `continue`, `native-failure`, `abandon`,
`retry`, `restart`, and `cleanup`. Invoke returned native actions only with their exact input.

Version 5.0.0 is breaking: CCO rejects predecessor active state, wave, lifecycle, and receipt
artifacts. Clean them up before starting a new task; there is no migration command.

Do not edit lifecycle JSON by hand or modify Codex Desktop's database from a Hook. Offline repair
requires leaving the active task, unsetting `CODEX_THREAD_ID`, `--offline-confirm`, and exact
parent/child identifiers.

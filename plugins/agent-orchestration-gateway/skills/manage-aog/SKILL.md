---
name: manage-aog
description: >-
  Install, diagnose, inspect, continue, retry, restart, or clean up Agent Orchestration Gateway.
  Do not use for normal task routing.
---

# Manage AOG

Use this cold-path skill only when normal orchestration is unavailable or the user asks to manage
AOG. Read [references/operations.md](references/operations.md) for exact commands and safety
rules.

Lifecycle commands use the current `CODEX_THREAD_ID`; never substitute another task ID or edit
lifecycle JSON by hand. Invoke returned native actions only with their exact input.

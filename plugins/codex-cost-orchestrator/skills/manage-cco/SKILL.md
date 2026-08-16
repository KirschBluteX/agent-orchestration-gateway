---
name: manage-cco
description: >-
  Install, diagnose, inspect, continue, retry, restart, or clean up Codex Cost Orchestrator.
  Do not use for normal task routing.
---

# Manage CCO

Use this cold-path skill only when normal orchestration is unavailable or the user asks to manage
CCO. Read [references/operations.md](references/operations.md) for exact commands and safety
rules.

Lifecycle commands use the current `CODEX_THREAD_ID`; never substitute another task ID or edit
lifecycle JSON by hand. Invoke returned native actions only with their exact input.

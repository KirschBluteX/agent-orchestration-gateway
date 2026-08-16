---
name: orchestrate
description: >-
  Plan and supervise user-approved software initiatives through Codex native tasks and bounded
  native subagents. Use when the user explicitly asks Agent Orchestration Gateway to clarify work,
  split it into non-overlapping modules, dispatch module tasks, or run an AOG module task.
---

# Orchestrate

Load exactly one role reference:

- For the user-facing Primary that defines and supervises an initiative, read
  [references/supervisor.md](references/supervisor.md).
- For a task whose prompt identifies it as an AOG module task, read
  [references/module.md](references/module.md).

Do not load both references or invent a third role. Validate an approved structural plan by sending
its JSON to standard input of:

```text
python -B <SKILL_ROOT>/scripts/validate_plan.py
```

The validator is stateless. AOG stores no lifecycle, lease, receipt, recovery, routing, or workspace
files; Codex owns task, worktree, Goal, and wait state.

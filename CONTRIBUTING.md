# Contributing

Issues and focused pull requests are welcome. Please do not include secrets, private
task content, credentials, or unredacted Codex logs.

## Development

Requirements:

- Python 3.11 or newer;
- Git;
- Codex CLI only for live native-catalog and end-to-end checks.

Run before opening a pull request:

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
python <skill-validator>/quick_validate.py plugins/codex-cost-orchestrator/skills/orchestrate
git diff --check
```

## Design boundaries

- Codex native Agent tools remain the only Agent runtime.
- Primary owns unresolved choices, integration conflicts, and final acceptance.
- Only closed, typed-scope work is eligible for a leaf.
- Runtime model routing remains static, local, network-free, and user-overridable.
- Do not add billing/token history, telemetry, provider sessions, daemons, or a
  database without an explicit project decision.
- Preserve user-owned files and unrelated worktree changes.

Behavior changes should include focused public tests and matching English and Chinese
documentation where they affect installation or normal use.

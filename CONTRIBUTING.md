# Contributing

Focused issues and pull requests are welcome. Do not include secrets, credentials,
private task content, or unredacted Codex logs.

## Development

Use Python 3.11+ and install `requirements.txt`; `zstandard` is required on Python versions
below 3.14. Codex CLI is needed only for live native-catalog and end-to-end checks.

Run before opening a pull request:

```text
python -m pip install -r requirements.txt
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests benchmarks .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
python <skill-validator>/quick_validate.py plugins/codex-cost-orchestrator/skills/orchestrate
python <skill-validator>/quick_validate.py plugins/codex-cost-orchestrator/skills/manage-cco
git diff --check
```

## Project boundaries

- Keep the canonical `cco.delegation.v1` compiler input closed and schema validated.
- Keep one deterministic assurance ladder and static Luna/Terra automatic routes.
- Preserve exact/prefix scope handling, scoped reader scans, Primary final authority, and the
  current wave/lifecycle/receipt protocols.
- Treat cooperative writers as experimental copies/worktrees with bounded backup journals, never
  as a security sandbox.
- Do not add compatibility adapters, a second planner lifecycle, host background work, or external
  accounting services without an explicit project decision.
- Preserve user-owned files and unrelated worktree changes.

Behavior changes need focused tests and matching English and Chinese documentation when they affect
installation or normal use.

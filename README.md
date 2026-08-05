# Codex Cost Orchestrator

[简体中文](README.zh-CN.md)

[![CI](https://github.com/KirschBluteX/codex-cost-orchestrator/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/KirschBluteX/codex-cost-orchestrator/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> Let Codex keep the plan. Let the right native Agent handle each closed piece.

Codex Cost Orchestrator (CCO) is a local plugin for Codex native Agents. It decides
when a task is worth splitting, prepares clear work packages, selects a supported
model and effort level, and checks the resulting workspace before the main Codex
Agent (the **Primary**) integrates the work.

You use Codex normally. CCO stays out of the way for small, single-step requests and
helps with larger tasks that have independent pieces.

## What you get

- **Practical delegation.** Routine, repeatable work prefers Luna; work that needs
  more judgment or an independent review prefers Terra.
- **One native runtime.** Agent creation, execution, follow-up, interruption, and
  sandboxing remain Codex responsibilities. CCO adds the policy and lifecycle checks.
- **Clear ownership.** Each delegated piece has a responsibility, scope, dependency,
  and acceptance condition, which limits duplicate or unrelated work.
- **Fast review handoff.** An independent reviewer can receive the completed worker's
  baseline, exact scope, changed paths, and evidence directly from task-local state.
- **One local graph pass.** Shared facts are closed once and the complete ready graph
  is routed locally, without a separate model request for every child.
- **Quiet execution.** After dispatch, the Primary waits for a meaningful event instead
  of repeatedly asking for progress. Only native terminal events establish completion.
- **Local by default.** Routing uses a static local policy. CCO does not require an
  online routing service or collect billing and telemetry history.

## How it works

```mermaid
flowchart LR
    U["Your request"] --> P["Primary closes the plan"]
    P --> R["CCO compiles the ready graph once"]
    R --> A["Codex native Agents execute"]
    A --> E["Native terminal events return evidence"]
    E --> V["Primary integrates and accepts"]
```

CCO uses three roles:

| Role | Purpose | Access |
| --- | --- | --- |
| `explorer` | Inspect a focused area and return evidence | Read-only |
| `worker` | Implement a closed change | Writes only in its declared scope |
| `reviewer` | Independently check a completed state | Read-only |

The Primary remains responsible for the overall goal, integration, and final answer.
If a task cannot be split safely or delegation would add overhead, it stays in the
Primary.

## Default model routing

CCO uses the first supported route in each row. The current user's explicit model or
effort request takes priority over these defaults.

| Task | Default | Fallback |
| --- | --- | --- |
| Routine, deterministic exploration or implementation | Luna / `max` | Terra / `max` |
| Bounded work that still needs judgment | Terra / `max` | Luna / `max` |
| Risk-sensitive work or independent review | Terra / `max` | None |

Sol and `ultra` are not selected automatically. Routes can be customized in a trusted
local configuration; unsupported choices stay with the Primary instead of being
silently replaced.

For each selected model, CCO starts with `max` and uses `xhigh` or `high` only when the
current Codex host does not expose the stronger effort level.

## Requirements

- Codex CLI or Codex desktop with plugin hooks and native Agents
- Python 3.11 or newer
- Windows or Linux (macOS is not currently tested)
- Git is optional; it improves workspace change detection when the folder is a Git
  worktree

The published contract has been validated with Codex CLI `0.146.0` and desktop build
`26.730.8199.0`. Newer hosts may work, but should be checked with `--doctor`.

## Install

```text
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add KirschBluteX/codex-cost-orchestrator --ref main
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
```

Then trust the hooks and verify the installation:

1. Open `/hooks` in Codex.
2. Review and trust every CCO hook shown there.
3. Run:

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

The check should report `HOOKS READY` and `STATIC ROUTE READY`. Start a new Codex
task after installing or updating so the current profiles, skill, and hooks are loaded.

## Use it

No special command or skill name is required. Describe the outcome you want:

```text
Refactor the authentication module, preserve its public behavior, and verify the result.
```

```text
Inspect this service for concurrency bugs, implement safe fixes, and run the relevant checks.
```

CCO will delegate only the parts that are closed and useful to run separately. You
continue to receive one integrated result from the Primary.

## Customize routes (optional)

Configuration precedence is:

```text
current user request → trusted project policy → global policy → built-in defaults
```

Global configuration lives at `~/.codex/cco.toml`:

```toml
trusted_project_roots = ["C:/work/my-project"]

[routes.worker.mechanical]
candidates = [
  { model = "gpt-5.6-luna", effort = "max" },
  { model = "gpt-5.6-terra", effort = "max" },
]

[routes.reviewer.guarded]
candidates = [
  { model = "gpt-5.6-terra", effort = "max" },
]
```

A trusted project may add `.codex/cco.toml` with the same route tables. See
[Operations](docs/OPERATIONS.md) for the complete configuration and recovery rules.

## Workspace and data boundaries

- Git and non-Git folders are supported; CCO never runs `git init` automatically.
- Delegated writes are checked against the declared scope. Read-only roles must leave
  their scope unchanged.
- Symlinks, junctions/reparse points, ambiguous aliases, special files, and root
  replacement fail closed.
- Temporary state contains contracts, routes, metadata, and hashes—not source copies,
  full conversations, credentials, billing history, or telemetry.
- CCO is a workflow guard, not an operating-system sandbox or a defense against a
  malicious local process. See [Security](SECURITY.md) for the trust model.

## Update and uninstall

<details>
<summary>Show commands</summary>

```text
codex plugin marketplace upgrade codex-cost-orchestrator
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --uninstall
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin marketplace remove codex-cost-orchestrator
```

The installer preserves modified or unknown user files for manual review.

</details>

## Learn more

- [Operations and compatibility](docs/OPERATIONS.md)
- [Security model](SECURITY.md)
- [Benchmark methodology](docs/BENCHMARK.md)
- [Changelog](CHANGELOG.md)
- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)

# Codex Cost Orchestrator

[简体中文](README.zh-CN.md)

Codex Cost Orchestrator (CCO) is a local orchestration layer for Codex native Agents.
It keeps planning, integration, and final acceptance in the Primary Agent, while
routing well-defined child work to Luna or Terra with a static, customizable policy.

The goal is simple: use capable lower-cost workers where the task is safe to delegate,
without giving up clear ownership, workspace protection, or final verification.

## Why use CCO?

- **Lower-cost delegation by default.** Mechanical work prefers Luna; work requiring
  more judgment and independent review prefers Terra. Sol is never selected
  automatically for a child.
- **No extra Agent runtime.** Codex still owns Agent creation, execution, follow-up,
  interruption, and sandboxing. CCO prepares and guards those native calls.
- **Less duplicate work.** CCO delegates only closed tasks with explicit scopes,
  dependencies, and acceptance criteria.
- **Quiet waiting after dispatch.** Once children are running, the Primary waits for
  meaningful events instead of repeatedly polling for progress.
- **Runs locally.** Routing uses a static local policy and requires no additional
  online routing service.
- **Works with Git and non-Git folders.** CCO protects both without automatically
  running `git init`.

## How it works

```mermaid
flowchart LR
    U["Your request"] --> P["Primary plans and closes tasks"]
    P --> C["CCO selects role, route, scope, and acceptance"]
    C --> A["Codex native Agents execute in parallel"]
    A --> E["Results return with workspace evidence"]
    E --> V["Primary integrates and accepts"]
```

CCO uses three logical roles:

| Role | Used for | Workspace access |
| --- | --- | --- |
| `explorer` | Focused inspection, investigation, and evidence gathering | Read-only |
| `worker` | Closed implementation or modification tasks | Writable within declared scope |
| `reviewer` | Fresh, independent acceptance of a completed state | Read-only |

Before delegation, CCO checks that a child has a distinct responsibility, enough
context to finish, a non-conflicting scope, and a verifiable result. Work that is not
safe or useful to split remains with the Primary.

## Default model policy

| Work type | Preferred model | Fallback |
| --- | --- | --- |
| Mechanical explorer or worker | Luna | Terra |
| Bounded explorer or worker | Terra | Luna |
| Guarded explorer or worker | Terra | None |
| Reviewer | Terra | None |

CCO tries supported effort levels in the order `max → xhigh → high`. It never
automatically selects Sol or `ultra`. A model or effort explicitly requested by the
current user takes priority, and trusted local configuration can replace the default
routes.

In plain language:

- **Mechanical** means the steps and expected output are deterministic.
- **Bounded** means some judgment remains, but the scope and acceptance conditions are
  complete.
- **Guarded** means semantic judgment, integration risk, or failure history requires
  the stronger default route.

CCO is designed to reduce unnecessary expensive child execution. Actual savings
depend on the workload and the models available to the current Codex host.

## Requirements

- Codex CLI or Codex desktop with plugin hooks and native Agents. Release-tested
  contracts: CLI `0.146.0` and Desktop `26.730.8199.0`.
- Python 3.11 or newer.
- Windows or Linux. macOS is not currently tested.
- Git is optional. It is used for Git-worktree protection when available.

## Install

```text
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add KirschBluteX/codex-cost-orchestrator --ref main
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
```

Then complete the trust check:

1. Open `/hooks` in Codex.
2. Review and trust every CCO hook shown there.
3. Run the read-only readiness check:

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

A ready installation reports both `HOOKS READY` and `STATIC ROUTE READY`. Start a new
Codex task after installation or update so the current skill, profiles, and hooks are
loaded.

## Use

CCO runs implicitly. Ask Codex normally; no skill name or special command is needed.

```text
Refactor this module, preserve its public behavior, and verify the result.
```

```text
Inspect this service for concurrency bugs, then implement and verify the safe fixes.
```

When useful, CCO will prepare the work graph, dispatch closed child tasks, and return
control to the Primary for integration and acceptance. Small tasks that do not benefit
from another Agent remain in the Primary.

## Customize routes

Configuration precedence is:

```text
current user request → trusted project policy → global policy → built-in defaults
```

Global policy lives at `~/.codex/cco.toml`:

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

A trusted project can use the same route tables in `.codex/cco.toml`. Its canonical
root must first appear in the global `trusted_project_roots` list.

Automatic configuration cannot contain Sol. Guarded and reviewer routes cannot use
Luna automatically. Unsupported or invalid routes leave the affected task with the
Primary instead of silently choosing an unknown model.

## Workspace and data safety

- Git worktrees use Git status, control state, and scoped content fingerprints.
- In a non-Git folder, explorers and reviewers capture declared scopes; workers
  capture the complete root so out-of-scope writes remain visible.
- Non-Git capture defaults to 20,000 files and 1 GiB. Over-budget work stays with the
  Primary before file content is read.
- Symlinks, junctions/reparse points, special files, ambiguous path aliases, and root
  replacement fail closed.
- Temporary state stores contracts, routes, paths, metadata, and hashes—not source
  file copies, full conversations, or credentials.
- Child results are evidence for Primary acceptance, not a replacement for it.

CCO is a workflow guard, not a hard security boundary against a malicious Primary,
Agent, process, or operating system. See [Security](SECURITY.md) and
[Operations](docs/OPERATIONS.md) for the full trust model and recovery guidance.

## Update

```text
codex plugin marketplace upgrade codex-cost-orchestrator
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

Review changed hooks again, then start a new Codex task.

## Uninstall

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --uninstall
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin marketplace remove codex-cost-orchestrator
```

The installer removes only CCO-owned profiles that still match published content.
Modified or unknown files are preserved for manual review.

## Project resources

- [Operations and compatibility](docs/OPERATIONS.md)
- [Security model](SECURITY.md)
- [Benchmark methodology](docs/BENCHMARK.md)
- [Changelog](CHANGELOG.md)
- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)

MIT License. Copyright (c) 2026 KirschQAQ.

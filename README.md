# Agent Orchestration Gateway

## English

Agent Orchestration Gateway (AOG) is a thin, explicit Codex Skill for supervising software work.
Primary clarifies the initiative with the user, proposes a non-overlapping module DAG, and dispatches
approved modules as native Codex tasks. Each module may use bounded native subagents for independent
leaves. Codex itself owns tasks, worktrees, Goals, and waits; AOG adds no runtime, Hooks, database, or
lifecycle state.

## 简体中文

Agent Orchestration Gateway（AOG）是一个轻量、显式调用的 Codex 软件工作编排 Skill。
Primary 先与用户澄清目标，提出写入范围互不重叠的模块 DAG，并在用户一次确认后创建原生
Codex 任务。每个模块可把独立叶子工作交给有界的原生子代理。任务、worktree、Goal 和等待
状态均由 Codex 管理；AOG 不增加运行时、Hook、数据库或生命周期状态。

## Workflow / 工作流

```text
User approval
      |
      v
Primary supervisor -- native wait_threads --> module tasks in managed worktrees
                                                |
                                                +-- native leaf subagents
                                                +-- one module commit
      |
      +-- topological commit assembly --> local delivery branch
```

1. Invoke `$agent-orchestration-gateway:orchestrate` explicitly.
2. Primary asks only material questions and inspects enough repository context to define ownership.
3. Primary presents one table with modules, dependencies, write scopes, model/effort, child caps, and
   review policy. Nothing is dispatched before approval.
4. A stateless validator checks the structural plan, including cycles and every cross-module scope
   overlap.
5. Ready modules run in parallel native tasks. Dependency modules start only after predecessor
   results and commits are available.
6. Primary assembles accepted module commits on a dedicated local branch. AOG never pushes or merges
   into a pre-existing branch without a separate request.

`wait_threads` and `wait_agent` are event waits. They do not poll or consume model sampling while
blocked. A timeout ends only that wait window; it does not restart or duplicate work.

## Routing / 模型路由

| Role / 角色 | Default / 默认 |
| --- | --- |
| Module root / 模块根任务 | Codex configured model and effort / Codex 配置默认值 |
| Mechanical deterministic leaf / 机械且可确定验证的叶子任务 | Luna/max |
| Other leaf or high-impact reviewer / 其他叶子任务或高影响审查 | Terra/max |

The approval table can override a module root. A module uses zero to eight leaves based on real
independent work; the cap is never a quota. The same leaf is reused for corrections in the same
scope. Only security, concurrency, persistence, public contracts, installation, destructive, or
broad semantic changes receive one independent reviewer.

## Plan validation / 计划校验

Send UTF-8 JSON to the validator through standard input:

```text
python -B plugins/agent-orchestration-gateway/skills/orchestrate/scripts/validate_plan.py
```

```json
{
  "goal": "Add the approved capability",
  "base_sha": "0123456789abcdef0123456789abcdef01234567",
  "modules": [
    {
      "id": "core",
      "type": "work",
      "objective": "Implement the core behavior",
      "depends_on": [],
      "writes": [{"kind": "prefix", "path": "src/core"}],
      "acceptance": [{"id": "core-tests", "criterion": "Focused tests pass"}]
    }
  ]
}
```

The standard-library validator reads at most 256 KiB, accepts at most eight modules, rejects
duplicate JSON keys and unknown fields, validates safe repository-relative `exact` file and `prefix`
directory scopes, rejects redundant or cross-module overlap, validates dependency references, and
detects cycles. It emits deterministic normalized JSON and never reads or writes repository state.

Parallel writable work requires a clean Git baseline. For a non-Git project, AOG asks before
initializing Git and creating the initial commit. If the user declines, read-only modules may remain
parallel, but writable work is limited to one local, uncommitted module. All Git plans require a
clean baseline so managed worktrees inspect the same state Primary approved.

Write scopes are an orchestration contract, not an OS sandbox. Module roots still inspect diffs and
verify changed paths before committing.

## Install / 安装

Requirements: a current Codex build with native tasks and subagents, plus Python 3.11 or newer for
plan validation.

```text
codex plugin marketplace add .
codex plugin add agent-orchestration-gateway@agent-orchestration-gateway
```

Start a new Codex task after installation, then invoke:

```text
$agent-orchestration-gateway:orchestrate
```

The plugin uses the models and authentication already configured in Codex. It has no provider or
API-key configuration.

## Development / 开发

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests
python <skill-creator>/scripts/quick_validate.py plugins/agent-orchestration-gateway/skills/orchestrate
python <plugin-creator>/scripts/validate_plugin.py plugins/agent-orchestration-gateway
```

Released under the [MIT License](LICENSE).

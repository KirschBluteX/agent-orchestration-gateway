# Agent Orchestration Gateway

[English](README.md)

Agent Orchestration Gateway（AOG）是一个轻量、显式调用的 Codex 软件工作编排 Skill。
Primary 先与用户澄清目标，提出写入范围互不重叠的模块 DAG，并在用户确认后创建原生
Codex 任务。每个模块可把独立的叶子工作交给数量受限的原生子代理。任务、worktree、Goal
和等待状态均由 Codex 管理；AOG 不增加运行时、Hook、数据库或生命周期状态。

## 工作流

```text
用户确认
   |
   v
Primary 主管 -- 原生 wait_threads --> 托管 worktree 中的模块任务
                                      |
                                      +-- 原生叶子子代理
                                      +-- 一个模块提交
   |
   +-- 按拓扑顺序组装提交 --> 本地交付分支
```

1. 显式调用 `$agent-orchestration-gateway:orchestrate`。
2. 只有无法从仓库或默认值获知、且会实质改变行为、责任、风险或范围的问题才由 Primary
   询问用户；模块任务把这类阻塞回报给 Primary，不直接打扰用户。
3. Primary 为每个结果、证据流、检查和写入范围指定唯一所有者，再形成最小的不重叠 DAG。
   一个模块完全有效；八个只是原生等待批次的技术上限，不是目标。
4. Primary 用一张表展示每个模块的独占责任、非目标、依赖、写入范围、模型与思考强度、
   子代理上限及审查策略。无状态校验器随后检查计划结构。
5. 已就绪模块通过原生任务并行运行；依赖模块只在前序结果与提交可用后启动。
6. Primary 在专用本地分支上组装已验收的模块提交。除非用户另行要求，AOG 不会推送，也
   不会合并到已有分支。

`wait_threads` 与 `wait_agent` 是事件等待；阻塞期间不会轮询，也不会消耗模型采样。超时只会
结束当前等待窗口，不会重启或重复派遣工作。

## 模型路由

| 角色 | 默认值 |
| --- | --- |
| 模块根任务 | Codex 已配置的模型与思考强度 |
| 机械且可确定验证的叶子任务 | Luna/max |
| 其他叶子任务或高影响审查 | Terra/max |

确认表可以覆盖模块根任务的模型。模块会先派遣所有可独立执行的叶任务，数量不超过已确认
上限，然后等待结果而不重复叶任务的工作；上限不是配额。同一范围的修正会复用原代理。
只有涉及安全、并发、持久化、公开契约、安装、破坏性操作或广泛语义变化时，才增加一名
独立审查代理。

## 计划校验

通过标准输入向校验器发送 UTF-8 JSON：

```text
python -B plugins/agent-orchestration-gateway/skills/orchestrate/scripts/validate_plan.py
```

```json
{
  "goal": "交付已确认的能力",
  "base_sha": "0123456789abcdef0123456789abcdef01234567",
  "modules": [
    {
      "id": "core",
      "type": "work",
      "objective": "实现核心行为",
      "depends_on": [],
      "writes": [{"kind": "prefix", "path": "src/core"}],
      "acceptance": [{"id": "core-tests", "criterion": "聚焦测试通过"}]
    }
  ]
}
```

这个纯标准库校验器最多读取 256 KiB，最多接受八个模块；它拒绝重复 JSON 键、未知字段、
完全重复的模块目标、不安全的仓库相对路径、冗余或跨模块重叠写入范围、未知依赖和依赖环。
语义责任仍在确认表中人工核对，因为无状态结构校验器不能推断两个不同表述的调查是否重叠。
`exact` 表示文件，`prefix` 表示目录。校验器输出确定性的规范化 JSON，不读取或写入仓库状态。

可并行写入的工作要求干净的 Git 基线。对于非 Git 项目，AOG 会先询问是否初始化 Git 并
创建初始提交。如果用户拒绝，只读模块仍可并行，但写入工作仅允许一个本地、未提交模块。
所有 Git 计划都要求干净基线，以确保托管 worktree 检查的是 Primary 已确认的同一状态。

写入范围是编排契约，不是操作系统沙箱。模块根任务仍需检查差异，并在提交前验证所有改动
路径。

## 安装

要求：支持原生任务与子代理的当前 Codex 版本，以及用于计划校验的 Python 3.11 或更高版本。

```text
codex plugin marketplace add .
codex plugin add agent-orchestration-gateway@agent-orchestration-gateway
```

安装后新建一个 Codex 任务，再调用：

```text
$agent-orchestration-gateway:orchestrate
```

插件直接使用 Codex 已配置的模型和认证方式，不包含 provider 或 API Key 配置。

## 开发

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests
python <skill-creator>/scripts/quick_validate.py plugins/agent-orchestration-gateway/skills/orchestrate
python <plugin-creator>/scripts/validate_plugin.py plugins/agent-orchestration-gateway
```

项目采用 [MIT License](LICENSE)。

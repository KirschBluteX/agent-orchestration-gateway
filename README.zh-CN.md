# Codex Cost Orchestrator

[English](README.md)

[![CI](https://github.com/KirschBluteX/codex-cost-orchestrator/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/KirschBluteX/codex-cost-orchestrator/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> 让 Codex 保留整体规划，把边界清晰的工作交给合适的原生 Agent。

Codex Cost Orchestrator（CCO）是一个面向 Codex 原生 Agent 的本地插件。它会判断任务
是否适合拆分，准备清晰的工作包，选择本机支持的模型与思考强度，并在子任务完成后检查
工作区，再由负责总体任务的主 Agent（下文称 **Primary**）集成结果。

用户仍然像平时一样使用 Codex。对于简单的单步请求，CCO 不会增加额外流程；对于包含
多个独立部分的较大任务，它可以帮助合理分工。

## 你能得到什么

- **实用的任务分派。** 重复、确定性的工作优先使用 Luna；需要更多判断或独立审查的工作
  优先使用 Terra。
- **使用同一套原生运行时。** Agent 的创建、执行、继续、终止和沙箱仍由 Codex 负责，
  CCO 只增加策略与生命周期检查。
- **职责清晰。** 每个分派任务都有明确的责任、范围、依赖和验收条件，减少重复或无关工作。
- **审查衔接更快。** 独立 reviewer 可直接从任务本地状态获得已完成 worker 的基线、精确范围、变更路径与证据。
- **整图只做一次本地决策。** 共享事实只需闭合一次，再由本地编译器统一路由当前就绪任务，
  不会为每个子 Agent 单独发起模型分类请求。
- **派遣后安静等待。** 子任务运行时，Primary 等待有意义的事件，不反复查询进度；只有原生
  终态事件能够证明任务已经结束。
- **重启后安全恢复。** Codex 桌面端重启会把活跃子 Agent 视为中断，fence 迟到结果，
  不会让旧任务继续阻塞下一次任务。
- **默认本地运行。** 路由使用静态本地策略，不依赖在线路由服务，也不收集计费或遥测历史。

## 工作方式

```mermaid
flowchart LR
    U["你的需求"] --> P["Primary 闭合整体计划"]
    P --> R["CCO 一次编译就绪工作图"]
    R --> A["Codex 原生 Agent 执行"]
    A --> E["原生终态事件返回证据"]
    E --> V["Primary 集成并验收"]
```

CCO 使用三个角色：

| 角色 | 用途 | 权限 |
| --- | --- | --- |
| `explorer` | 检查指定区域并返回证据 | 只读 |
| `worker` | 实现边界闭合的修改 | 只能写入声明的范围 |
| `reviewer` | 独立检查已经完成的状态 | 只读 |

Primary 始终负责总体目标、结果集成和最终答复。如果任务不适合安全拆分，或分派的收益
不足以抵消额外开销，就由 Primary 直接完成。

## 默认模型路由

CCO 会在每一行中选择本机支持的第一个路由。当前用户明确指定的模型或思考强度会覆盖
这些默认值。

| 任务类型 | 默认路由 | 后备路由 |
| --- | --- | --- |
| 常规、确定性的检查或实现 | Luna / `max` | Terra / `max` |
| 仍需一定判断的有界任务 | Terra / `max` | Luna / `max` |
| 风险敏感任务或独立审查 | Terra / `max` | 无 |

CCO 不会自动选择 Sol 或 `ultra`。你可以在受信的本地配置中自定义路由；如果选择不受
本机支持，任务会留在 Primary，不会被静默替换成未知模型。

对于选中的模型，CCO 会优先使用 `max`；只有当前 Codex 宿主不提供更高强度时，才会依次
尝试 `xhigh` 或 `high`。

## 环境要求

- 支持插件 hook 和原生 Agent 的 Codex CLI 或 Codex 桌面端
- Python 3.11 或更高版本
- Windows 或 Linux（当前未测试 macOS）
- Git 可选；在 Git worktree 中可提供更精确的工作区变更检测

当前发布契约已在 Codex CLI `0.146.0` 与桌面端 `26.730.8199.0` 上验证。使用更新版本的
宿主时，建议先运行 `--doctor` 检查兼容性。

## 安装

```text
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add KirschBluteX/codex-cost-orchestrator --ref main
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
```

然后信任 hook 并验证安装：

1. 在 Codex 中打开 `/hooks`。
2. 检查并信任其中显示的全部 CCO hook。
3. 运行：

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

检查结果应同时包含 `HOOKS READY` 和 `STATIC ROUTE READY`。安装或更新后请新建 Codex
任务，使最新的 profile、skill 与 hook 生效。

## 使用方式

不需要特殊命令，也不需要手动写出 skill 名称。像平时一样描述你希望得到的结果：

```text
重构认证模块，保持公开行为不变，并验证结果。
```

```text
检查这个服务是否存在并发问题，实现安全修复，并运行相关检查。
```

CCO 只会分派那些边界清晰、适合独立执行的部分。最终你仍会从 Primary 收到一个整合后
的结果。

## 自定义路由（可选）

配置优先级如下：

```text
当前用户请求 → 受信项目配置 → 全局配置 → 内置默认值
```

全局配置位于 `~/.codex/cco.toml`：

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

受信项目可以在 `.codex/cco.toml` 中添加相同的 route 表。完整配置和故障恢复规则见
[运维说明](docs/OPERATIONS.md)。

## 工作区与数据边界

- 同时支持 Git 与非 Git 目录；CCO 不会自动执行 `git init`。
- 分派写入会根据声明的范围进行检查；只读角色必须保持其范围不变。
- symlink、junction/reparse point、路径别名歧义、特殊文件和根目录替换会被安全拒绝。
- 临时状态只保存合同、路由、元数据和 hash，不保存源码副本、完整对话、凭据、计费历史
  或遥测数据。
- Codex 桌面端重启时，活跃或正在派遣的子 Agent 会记录为 `host_restart` 中断，
  不会伪装成成功完成；其 tombstone 会保留，用于拒绝迟到结果。
- CCO 是工作流守卫，不是操作系统沙箱，也不能抵御恶意的本地进程。完整信任模型见
  [安全说明](SECURITY.md)。

## 更新与卸载

<details>
<summary>显示命令</summary>

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

安装器会保留已修改或来源未知的用户文件，供手动检查。

</details>

## 了解更多

- [运维与兼容性](docs/OPERATIONS.md)
- [安全模型](SECURITY.md)
- [基准测试方法](docs/BENCHMARK.md)
- [版本记录](CHANGELOG.md)
- [开发路线](ROADMAP.md)
- [参与贡献](CONTRIBUTING.md)

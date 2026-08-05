# Codex Cost Orchestrator

[English](README.md)

Codex Cost Orchestrator（CCO）是 Codex 原生 Agent 的本地编排层。它让 Primary
保留规划、集成与最终验收，同时通过静态、可自定义的策略，将边界明确的子任务交给
Luna 或 Terra。

目标很直接：在适合分派时优先使用成本更低且能力足够的 worker，同时保留清晰的职责、
工作区保护和最终质量控制。

## 为什么使用 CCO？

- **默认控制子任务成本。** 机械性工作优先 Luna，需要更多判断或独立审查时优先 Terra；
  CCO 不会自动为 child 选择 Sol。
- **不创建第二套 Agent 运行时。** Agent 的创建、执行、follow-up、interrupt 与 sandbox
  仍由 Codex 原生能力负责，CCO 只负责准备和守卫这些调用。
- **减少重复与越界。** 只有合同、scope、依赖和验收条件已经闭合的任务才会分派。
- **派遣后静默等待。** child 运行期间，Primary 等待有意义的事件，不为查看进度反复轮询。
- **本地运行。** 路由使用静态本地策略，不需要额外的在线路由服务。
- **同时支持 Git 与非 Git 目录。** CCO 不会为了分派任务自动执行 `git init`。

## 工作方式

```mermaid
flowchart LR
    U["你的需求"] --> P["Primary 规划并闭合任务"]
    P --> C["CCO 选择角色、模型、scope 与验收方式"]
    C --> A["Codex 原生 Agent 并行执行"]
    A --> E["结果与工作区证据返回"]
    E --> V["Primary 集成并最终验收"]
```

CCO 使用三个逻辑角色：

| 角色 | 适用工作 | 工作区权限 |
| --- | --- | --- |
| `explorer` | 定向检查、问题探索与证据收集 | 只读 |
| `worker` | 边界闭合的实现或修改任务 | 仅在声明 scope 内写入 |
| `reviewer` | 对已完成状态进行独立验收 | 只读 |

分派前，CCO 会确认 child 具有独立职责、完成任务所需的上下文、不会冲突的 scope，以及
可以验证的结果。无法安全拆分或新开 Agent 没有实际收益的工作会留给 Primary。

## 默认模型策略

| 工作类型 | 首选模型 | 后备模型 |
| --- | --- | --- |
| 机械性 explorer 或 worker | Luna | Terra |
| 有界 explorer 或 worker | Terra | Luna |
| 受保护 explorer 或 worker | Terra | 无 |
| reviewer | Terra | 无 |

CCO 按 `max → xhigh → high` 选择本机支持的思考强度，不会自动选择 Sol 或 `ultra`。
当前用户明确指定的模型/强度具有最高优先级，也可以通过受信的本地配置替换默认路由。

三种工作等级的含义：

- **机械性（mechanical）**：步骤和预期结果均可确定。
- **有界（bounded）**：仍需一定判断，但 scope 与验收条件已经完整。
- **受保护（guarded）**：涉及语义判断、集成风险或既往失败，需要更稳健的默认路由。

CCO 旨在减少不必要的高成本 child 执行；实际节省取决于任务类型与当前 Codex 宿主
可用的模型。

## 环境要求

- 支持插件 hooks 和原生 Agent 的 Codex CLI 或 Codex 桌面端。本版本按 CLI `0.146.0`
  与 Desktop `26.730.8199.0` 契约验证。
- Python 3.11 或更高版本。
- Windows 或 Linux；当前不测试 macOS。
- Git 可选；存在 Git worktree 时用于更精确的工作区保护。

## 安装

```text
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add KirschBluteX/codex-cost-orchestrator --ref main
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
```

随后完成信任检查：

1. 在 Codex 中打开 `/hooks`。
2. 阅读并信任其中显示的全部 CCO hook。
3. 运行只读 readiness 检查：

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

安装成功时会同时显示 `HOOKS READY` 与 `STATIC ROUTE READY`。安装或更新后请新建一个
Codex 任务，使当前 skill、profile 与 hook 生效。

## 使用

CCO 会隐式运行，无需写 skill 名称或特殊命令，像平时一样向 Codex 提出任务即可。

```text
重构这个模块，保持公开行为不变，并验证最终结果。
```

```text
检查这个服务是否存在并发问题，然后实现并验证安全修复。
```

适合分派时，CCO 会准备工作图、派遣闭合的 child 任务，再由 Primary 完成集成与验收。
对于无法从新 Agent 获益的微小任务，Primary 会直接处理。

## 自定义路由

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

受信项目可以在 `.codex/cco.toml` 中使用相同的 route 表；项目规范化根路径必须先加入
全局 `trusted_project_roots`。

自动配置不能包含 Sol，guarded 与 reviewer 不能自动使用 Luna。模型不受本机支持或
配置无效，该任务会留在 Primary，不会静默改用未知路线。

## 工作区与数据安全

- Git worktree 使用 Git status、control state 与 scope 内容指纹进行保护。
- 非 Git 目录中，explorer/reviewer 捕获声明 scope；worker 捕获完整根目录，以发现
  scope 外写入。
- 非 Git 捕获默认上限为 20,000 个文件与 1 GiB。超限任务会在读取文件正文前留给
  Primary。
- symlink、junction/reparse、特殊文件、路径别名歧义与根目录替换均采用 fail-closed。
- 临时状态保存合同、路由、路径、元数据与 hash，不保存源码副本、完整对话或凭据。
- child 结果是 Primary 验收的证据，不会取代 Primary 的最终判断。

CCO 是工作流守卫，不是用来抵抗恶意 Primary、Agent、进程或操作系统的硬安全边界。
完整信任模型与恢复方式见 [安全说明](SECURITY.md) 和
[运维说明](docs/OPERATIONS.md)。

## 更新

```text
codex plugin marketplace upgrade codex-cost-orchestrator
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --bootstrap
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --doctor
```

重新检查发生变化的 hook，然后新建一个 Codex 任务。

## 卸载

```text
python plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace . --uninstall
codex plugin remove codex-cost-orchestrator@codex-cost-orchestrator
codex plugin marketplace remove codex-cost-orchestrator
```

安装器只删除内容仍与发布版本一致的 CCO profile。被修改或来源未知的文件会保留，供
用户自行检查。

## 项目资料

- [运维与兼容性](docs/OPERATIONS.md)
- [安全模型](SECURITY.md)
- [基准测试方法](docs/BENCHMARK.md)
- [版本记录](CHANGELOG.md)
- [开发路线](ROADMAP.md)
- [参与贡献](CONTRIBUTING.md)

MIT License。Copyright (c) 2026 KirschQAQ。

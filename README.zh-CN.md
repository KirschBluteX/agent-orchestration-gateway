# Codex Cost Orchestrator

[English](README.md) | [简体中文](README.zh-CN.md)

Codex Cost Orchestrator（CCO）是 Codex 原生 Agent 的隐式成本感知路由器。Primary
保留用户目标、架构、拆分、集成、验证和最终验收；已经闭合的执行工作可以交给更便宜且
合格的模型，用户无需每次显式调用 CCO。

CCO 不增加第二套 Agent runtime。Codex 原生 spawn、follow-up、wait 和 interrupt
仍是唯一执行机制；CCO 只提供确定性路由、紧凑派遣胶囊、有限所有权与证据驱动验收。

## 决策方式

CCO 将经常被错误合并为“简单/复杂编程”的判断拆开：

| 判断 | 可选值 | 含义 |
| --- | --- | --- |
| Purpose | `analysis_inspect`、`analysis_probe`、`implementation`、`acceptance` | 为什么需要另一个 Agent |
| Judgment | routine、complex | 闭合后是否仍有影响结果的有限选择 |
| Placement | Primary、child | 新开原生轮次是否有结构收益 |
| Route | 模型 + 思考强度 | 由哪个受支持组合执行 |
| Acceptance | primary、independent | 完成需要哪类证据 |

原子、确定、低风险的小修改留在 Primary。只有闭合执行、互不冲突的并行节点、上下文
救援、独立源码分区、运行隔离、独立证据或用户明确委派时才创建 child；仅仅价格更低
不是派遣理由。

## 快速派遣

普通派遣只需要一次本地编译和一次原生 spawn：

```text
任务事实 → 路由计划 → cco.v6 胶囊 → 原生 Agent
```

- 编译器先校验完整 route plan，再按宿主可用容量选择 ready 节点，并在内存中生成各自的
  canonical capsule。
- 不使用项目文件作为派遣临时文件。
- 轻量与严格路径共用同一实现；严格路径只是增加证据，不维护第二套协议。
- 逻辑任务类型仍完整区分，物理 profile 仅保留可写 leaf 与只读 leaf。
- 派遣后，Primary 完成真正互不重叠的工作便进入事件驱动等待，不短轮询、不反复消耗
  模型轮次播报无变化状态。
- 调用方不能把任意模型与单独的 plan hash 拼在一起；胶囊只保留已验证 plan 的身份、
  当前 rank 与 selected pair。

## 自适应模型路由

用户指定的模型与思考强度始终优先。未指定时，一个本地批处理会同时解析工作图所需的
所有 purpose × judgment 路由。路由阶段不创建 Agent，也不让 Primary 用自然语言逐个
比较候选。

自动候选必须受当前 Codex 原生目录支持、CodexRadar 观测 IQ 严格大于 90，并通过样本、
同群和覆盖率检查。算法使用 Wilson 区间、Pareto 前沿以及质量/资源/时间/不确定性效用。

worker 与 reviewer 默认优先 Luna/Terra。只有不存在合格的 Luna/Terra，或 Sol 的
Wilson 95% 下界高于最佳 Luna/Terra 的上界时，Sol 才能自动胜出。用户明确指定 Sol 时
始终照常执行。

Radar TTL 默认为一小时。若 LKG 超过 TTL 但仍在 72 小时有效期内，CCO 会立即用它完成
当前派遣，并刷新供后续派遣使用。fallback 只推进预排序计划的 rank，不重新评分、不重建
整份合同。默认不显示 IQ、价格、耗时或效用解释；仅在请求 `--explain` 时显示。

## 并发与验收

CCO 不设置低于 Codex 原生限制的人为并发上限。只要节点依赖就绪、职责不同且写入范围
不重叠，就会填满可用原生槽位。并发本身不再强制 reviewer。

当风险被明确排除，且确定性证据覆盖全部验收标准时，即使是 complex 或多节点图也可由
Primary 验收。只有真实风险、语义/人工证据、集成判断、失败或偏差、Primary 自己修改了
实现，或用户明确要求时，才启动独立 reviewer。

独立审查使用 fresh 只读原生 Agent、`fork_turns: none` 和一份精确证据包。`fix-first`
会保留同一 reviewer，允许一次证据驱动的 delta；再次尝试必须具有新的可行动证据，不再让
每份 packet 携带固定 3/2 次数仪式。

## 安全与状态

胶囊绑定 purpose、judgment、route、上下文 fork、scope、合同、验收、证据、baseline 和
一个执行 generation；hook 会独立复算胶囊并核对原生参数。

仓库外仅保留一个很小的任务级 ledger，记录当前 owner、generation、input cursor 和
lifecycle phase，用于拒绝重复 owner、并发 continuation 与迟到结果。它不是 coordinator、
数据库、永久审计日志、文件锁或验收记录。

轻量任务在工作图开始时共享一次 baseline，并校验 owned scopes 的 Git delta；严格路径
才追加 Git 控制状态、路径别名、reparse 和 submodule 检查。Hook 只是进程守卫；只有运行
元数据证明只读时，才称 reviewer 具有操作系统强制只读隔离。

CCO 不保存计费、token、费用或路由历史，也不增加加密层、provider session、daemon 或
数据库。

## 安装

```powershell
git clone https://github.com/KirschBluteX/codex-cost-orchestrator.git
cd codex-cost-orchestrator
codex plugin marketplace add .agents/plugins
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python plugins/codex-cost-orchestrator/scripts/install_agents.py --upgrade --workspace .
```

验证两个物理 profile：

```powershell
python plugins/codex-cost-orchestrator/scripts/install_agents.py --check --workspace .
```

安装或更新后新建一个 Codex 任务，使 profile、hook 和 skill 重新加载。之后直接描述普通
实现需求即可；显式 `$codex-cost-orchestrator:orchestrate` 仍可使用，但不是必需。

## 开发验证

```powershell
python -B -m unittest discover -s tests -v
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
```

详细规则见 [orchestration skill](plugins/codex-cost-orchestrator/skills/orchestrate/SKILL.md)
和 [cco.v6 胶囊参考](plugins/codex-cost-orchestrator/skills/orchestrate/references/contracts-v6.md)。

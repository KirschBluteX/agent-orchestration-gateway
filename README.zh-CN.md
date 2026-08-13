# Codex Cost Orchestrator

[English](README.md)

> **预 1.0（pre-1.0）发布政策：**公开、安装程序和清单的发布标识均为无构建元数据的 `0.9.3`。在 1.0 之前，次版本可能包含破坏性变更。历史的 2.x 至 5.x 标签已压缩为 0.9 之前的开发历史；Git 历史保持不变。

Codex Cost Orchestrator（CCO）是 Codex 原生 Agent 的本地控制面。Primary 保留意图、
集成和最终验收；CCO 负责已闭合、已定范围工作的派发与验收证据。当前预 1.0 版本为
`0.9.3`。

## 派发契约

普通工作默认通过唯一的 `prepare` 命令派发。输入是经过 schema 校验的
`cco.delegation.v1` 信封，包含闭合工作、明确的验收 ID 和仓库相对范围。范围只能是
`{"kind":"exact","path":"…"}` 或 `{"kind":"prefix","path":"…"}`。

```text
python -B <PLUGIN_ROOT>/scripts/control_plane.py prepare --repo <WORKSPACE> --capacity <N>
```

只以返回的原始工具输入调用每个 action。未闭合的工作必须由 Primary 在 `prepare` 前
澄清或闭合；每个原生子 Agent 都必须先经 CCO 准备。已有的 `cco.planner-proposal.v1`
值只能作为无状态、经过 schema 校验的 DAG 输入，不能形成规划路由、生命周期或直接
派发权限。

当前 Codex Desktop 可能在 Hook 边界把准备好的 Agent 消息替换为 opaque 密文。默认
`trusted_host` 策略只在全部可见字段唯一匹配一个已准备 dispatch 时准入，并把实际密文
摘要与 `tool_use_id` 写入现有持久收据；PostToolUse 必须再次匹配同一对值。这样无需第二
运行时或第二账本即可恢复原生 V2 spawn、复用与 continuation。该模式信任宿主，但不能
证明隐藏明文等于准备消息。若要在宿主提供认证明文摘要前拒绝所有 opaque Agent 输入，
请在启动 Codex 前设置 `CCO_OPAQUE_MESSAGE_POLICY=strict`。

只有显式权限、需要澄清、显式 direct 请求，或恰好一个声明总上限低于 30 秒的工具时，
工作才留在 Primary。派发后持续使用长 `wait_agent` 窗口，直到子任务完成或确实需要
处理；`timed_out` 只表示本次等待窗口结束，应继续等待同一个仍存活的 dispatch，不能
据此判定失败、重复子任务或输出无变化进度。

## 确定性路由与审查

| 保障等级 | 自动路由 |
| --- | --- |
| mechanical explorer 或 worker | 当前 V2 后端提供 Luna 时用 Luna，否则直接用 Terra |
| bounded explorer 或 worker | Terra |
| guarded 工作和最终 reviewer | Terra |

CCO 会先与当前 V2 原生能力目录取交集，不会尝试仅供其他 Agent 后端使用的模型。
CCO 不会为了调用 Luna 再启动一套 V1 Agent 运行时；后续宿主通过 V2 提供 Luna 时，
现有静态路由会直接生效，无需迁移 CCO 协议。
语义或人工验证、公开接口、安全/认证、并发、持久化、迁移或恢复、安装器、文件系统
事务、不可逆动作、测试失败、重试、偏差、范围扩大或新依赖都会让工作进入 guarded。
guarded plan 在所有非 reviewer 源节点之后有一个独立最终 reviewer。只有当前 plan 中明确的
`accept_risk: true` 可以省略它；Primary 的最终权威和确定性验证仍然必须保留。

只有一个直接且干净的前置派发满足完全相同的角色、保障等级、已选路由和范围，并且
没有继承上下文、重试、偏差、中断、阻塞或未结 receipt/lease 时，才可复用 owner。
每次复用仍生成新的 dispatch 和基线。

## 状态与升级

当前协议为 `cco.wave.v3`、`cco.lifecycle.v2` 与 `cco.receipt.v2`。早期活动状态、wave、
lifecycle、receipt 或 aggregation 工件不会原地升级；升级到 0.9.3 前必须清理，再开始
新任务。不存在迁移命令，也不存在活动状态兼容层。

只读任务只扫描声明的范围。对于同一规范工作区，CCO 只允许一个普通 writer，并会对
冲突的活动工作 fail closed。`status`、`continue`、`native-failure`、`retry`、`restart`
和 `cleanup` 仅操作当前任务；详见 [操作说明](docs/OPERATIONS.md)。

## 实验性 cooperative writers

`writer_isolation=cooperative` 需要显式开启。CCO 会在请求的原生容量内选择最大的、两两
范围不重叠的全新 writer 集合，并设置四个 writer 的安全上限。干净 Git 工作区使用受管
worktree；脏 Git 和目录工作区使用有界副本；文件数、字节数和 journal 限额按整个 wave
聚合计算，不会按 writer 重复放大。集成前 CCO 会准备精确备份和一个有界 apply journal。
guarded writer 后可跟随编译器自动加入的唯一 final reviewer；其他 cooperative DAG 形状仍不准入。成功清理会删除完成的 isolate 与 journal；
未完成 journal 及备份会保留，直到回滚或显式处置确认安全结果。

这不是 OS sandbox。子 Agent、Primary 和本机宿主仍在可信边界内；请审查最终 delta，
不要依赖 cooperative isolation 来限制恶意或已受损的进程。

## 安装

需要 Python 3.11+；Python 3.14 以下需要 `zstandard`；还需要支持 plugins、Hooks 和原生
Agents 的当前 Codex 安装。

```text
python -m pip install -r requirements.txt
codex plugin marketplace add .
codex plugin add codex-cost-orchestrator@codex-cost-orchestrator
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --bootstrap
```

在 `/hooks` 审查并信任五个 CCO Hook，启动新的 Codex 任务后执行：

```text
python -B plugins/codex-cost-orchestrator/scripts/install_agents.py --workspace <PROJECT> --doctor
```

Doctor 会拒绝缺失、重复或未知的 CCO Hook 定义。

## 离线 host-edge 修复

Codex Desktop 拥有持久化 task-card edge。CCO 不会在 Hook 中修改该数据库。可选修复工具是
离线兜底：离开活动任务，保持 `CODEX_THREAD_ID` 未设置，使用 `--offline-confirm`，并提供
精确的 parent 和 child ID。修复前会持久化创建仅 owner 可读写的回滚 journal，并在数据库提交前
重新校验精确 rollout 证明。详见
[操作说明](docs/OPERATIONS.md#offline-host-edge-repair)。

## 开发

```text
python -X utf8 -B -m unittest discover -s tests -v
python -m ruff check plugins tests benchmarks .github/scripts
python .github/scripts/validate_plugin.py plugins/codex-cost-orchestrator
git diff --check
```

参见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和
[ROADMAP.md](ROADMAP.md)。项目采用 [MIT License](LICENSE)。

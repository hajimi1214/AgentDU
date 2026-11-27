# G2 Desktop Runtime 集成方案

设计基线为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，日期为 2026-11-27。本文件记录接口与生命周期方案，不表示桌面多 Agent Runtime 已经实现。

## 源码审计结论

已验证的现状如下。

- Desktop Host 在 `src/agentdu/desktop_host.py` 的 `RuntimeHost._start_task` 为每个主任务建立 `AgentRuntime`，并以 Host 级单任务限制拒绝第二个活动任务。
- Desktop Host 通过 `RuntimeHost._runtime_event` 将主 Runtime 事件映射到版本化 JSONL 事件。父会话由 `SessionStore` 持久化。
- CLI 在 `src/agentdu/orchestration.py` 的 `AutonomousCoordinator.install` 中向主 Runtime 注册委派、咨询、审核、状态查询、恢复、接管和集成工具。
- `AutonomousCoordinator` 使用有界线程池，默认并发上限为 2，每会话任务上限为 8，总模型调用预算为 80，每任务调用预算为 20。
- 子任务状态由 `TaskStateStore` 持久化在 `.agentdu/agents/<parent-session>/tasks.json`。工作 Agent 使用独立会话，任务私有数据目录位于 `.agentdu/agents/<parent-session>/tasks/<task-id>`。
- Coding Worker 使用独立 Git worktree，Review Worker 使用只读 worktree，Consult Worker 采用只读策略且不访问项目工具。
- `HandoffLedger` 以版本化状态、owner 检查和证据记录管理任务交接。`GitWorkspaces` 验证基线、结果快照、改动文件和 Diff digest，并在集成前检查主 worktree 状态。

源码证据包括 `src/agentdu/desktop_host.py` 的 `_start_task`、`_runtime_event`、`_dispatch` 与 `_recover_workspace_tasks`，`src/agentdu/orchestration.py` 的 `AutonomousCoordinator.install`、`_submit`、`_launch_queued`、`_execute_task` 与 `TaskStateStore`，以及 `src/agentdu/collaboration.py` 的 `HandoffCoordinator` 和 `src/agentdu/git_workspaces.py` 的 `GitWorkspaces`。

## Desktop Host 组合方式

每个用户任务保留一个 Main Brain `AgentRuntime`。Host 在创建 Runtime 时读取已保存的 Provider profiles，并根据配置决定是否安装 `AutonomousCoordinator`。没有可用的不同协作 profile 时，Main Brain 继续以单模型方式工作。

Coordinator 由该用户任务的父 `AgentRuntime` 拥有。委派工具只在父 Runtime 的模型循环中调用。Worker 在已有有界调度器中异步执行，父 Runtime 可继续处理独立工作。Host 不等待所有 Worker 才继续接收父 Runtime 事件。

Host 侧需要一个 `DesktopTaskService` 负责把 CLI 协作接口连接到 Desktop 生命周期。它管理父任务的 Runtime 句柄、Coordinator 生命周期、活动 Worker 查询、取消、恢复和结果集成请求。该服务通过既有 `AgentRuntime`、`AutonomousCoordinator`、`HandoffCoordinator` 和 `GitWorkspaces` 工作，不通过解析 CLI 文本输出建立接口。

## 任务注册与持久化所有权

`TaskStateStore` 作为 Worker 执行状态、profile identity、依赖、预算、attempt、worktree、worker session、结果 SHA 和验收数据的权威来源。`HandoffLedger` 作为 handoff owner、version、acceptance 和 evidence 的权威来源。Git commit、worktree 状态及验收命令结果作为代码结果的事实来源。

父 `SessionStore` 持久化用户可见的 Main Brain 与 Worker 活动事件，供 UI 时间线恢复。Host 重新连接时先读取任务快照，再按事件序号补充增量。界面不得单凭 Worker 文本报告推断代码已验证或已集成。

桌面任务记录至少包含 `taskId`、`parentTaskId`、`agentId`、`role`、`profile`、`modelIdentity`、`parentSessionId`、`workerSessionId`、`dependencies`、`owner`、`version`、`executionStatus`、`handoffStatus`、`baselineCommit`、`resultCommit`、`changedFiles`、`diffDigest`、`acceptanceChecks`、`attempt`、`modelCallBudget`、`timestamps` 和 `lastError`。凭据、完整私有推理和不需要呈现的私有上下文不得进入桌面任务事件。

Main Brain 持有用户目标与计划的所有权。Worker 只能更新自己的任务记录和私有 worktree。Host 持有 Runtime 进程、取消 token、审批等待项和并发租约。Handoff 状态转换继续使用 `HandoffLedger.transition` 的 expected version、expected status 与 actor 检查。

## 状态模型

执行状态与交接状态分开保存，避免把 Worker 完成、结果验证和最终集成折叠为一个状态。

| 执行状态 | 含义 |
| --- | --- |
| `queued` | 等待并发槽位或依赖条件 |
| `running` | Agent 正在执行模型或工具工作 |
| `waiting_approval` | 工具权限需要用户决定 |
| `completed` | Agent 已返回结果，尚不代表结果已验证 |
| `failed` | 执行失败 |
| `blocked` | 前置任务或资源条件无法满足 |
| `cancelled` | 用户或 Main Brain 明确取消 |
| `interrupted` | 进程退出后发现任务未记录终态 |

| 交接状态 | 含义 |
| --- | --- |
| `none` | 当前结果未进入交接 |
| `offered` | 结果等待接收方确认 |
| `accepted` | 接收方已确认任务与基线 |
| `validating` | 代码快照或验收检查正在运行 |
| `verified` | 基线、Diff 与验收检查均通过 |
| `validation_failed` | 结果检查失败 |
| `stale` | 任务基线或结果版本已过期 |
| `rejected` | 接收方或用户拒绝结果 |
| `integrated` | 用户确认后集成完成且再次验证通过 |

Worker 的 `completed` 状态不可直接触发 `integrated`。实现任务需满足 baseline SHA、结果 SHA、改动文件、Diff digest、验收命令和用户集成确认。Review 与 Consult 结果采用相应的只读验证规则。

## Host 协议与事件

当前协议版本为 1，使用 `desktop/src/shared/protocol.ts` 定义的 request、response、event 消息。G2 优先以新增 method 和 event 扩展协议，并由 `host.initialize` 返回 capabilities。旧 Host 不宣称支持多 Agent 时，Renderer 隐藏协作控制项。破坏既有消息契约时再升级 `protocolVersion`。

建议新增的请求包括 `agentTasks.list`、`agentTasks.read`、`agentTasks.cancel`、`agentTasks.resume`、`agentTasks.takeover`、`agentTasks.validate` 和 `agentTasks.integrate`。所有请求校验 session、task ownership、权限与参数范围。集成需携带用户确认状态和验收命令。

事件统一由 Host 添加 `taskId`、`parentTaskId`、`agentId`、`role`、`profile`、`sessionId`、`statusVersion` 和 `occurredAt`，使用既有单调 `sequence` 与去重 `eventId`。

| 事件 | 触发条件 |
| --- | --- |
| `agent.task.queued` | Worker 记录持久化成功 |
| `agent.task.started` | Worker 获得并发租约并开始运行 |
| `agent.task.progress` | 模型或工具活动发生 |
| `agent.task.waiting_approval` | Worker 请求权限审批 |
| `agent.task.completed` | Worker 结果已持久化 |
| `agent.task.failed` | Worker 执行失败 |
| `agent.task.cancelled` | 取消已确认并记录 |
| `agent.task.interrupted` | 恢复扫描发现未完成任务 |
| `agent.handoff.changed` | HandoffLedger 状态或 owner 版本变化 |
| `agent.task.validation_finished` | 快照和验收检查结束 |
| `agent.task.integrated` | 用户确认且集成后检查通过 |
| `agent.task.recovery_required` | 基线、会话或 worktree 无法安全恢复 |

## 并发、权限与隔离

不能移除 `RuntimeHost._start_task` 的单任务限制来开启协作。实现前需把 Host 活动表升级为拥有主任务及 Worker 的 Task Registry，并为每个任务分配 owner、状态版本、取消句柄、执行预算和独立工作区。资源控制至少涵盖同时活动的 Worker 数、任务总数、每任务模型调用、总模型调用和超时。桌面运行默认值应经用户体验与资源测试确认，CLI 当前默认值只作为实现参考。

一个 Worker 独占一个可写 worktree。Review Worker 使用只读 worktree。Consult Worker 不获得项目文件或命令工具。Main Brain 继续使用原工作区。接受 Worker 结果前，Host 在 candidate merge 环境运行检查，确认主工作区仍位于基线之后且没有未经记录的改动。Worktree 不替代 AppContainer 或其他 OS 级工具限制。

任务取消使用任务级取消 token。取消 Main Brain 时，Host 按依赖图记录尚未结束的 Worker，并向用户显示仍在运行的数量和状态。取消或 Host 崩溃不会自动重放不确定工具操作。恢复后先重建任务快照、验证 worktree 和基线，再由用户决定恢复、接管、丢弃或集成。

## 实现顺序

1. 定义 DesktopTaskService 与现有 Coordinator 的适配接口，明确 parent task、worker task 与 handoff 的 ID 关联。
2. 为 Host 增加任务快照读取、状态事件发布、分页、任务级取消及重连恢复。
3. 在单个主 Runtime 上安装 Coordinator，验证没有不同协作 profile 时保留单模型路径。
4. 建立 worker task 到 Parent Session 时间线的事件关联，恢复 UI 快照与事件流的一致性。
5. 接入 isolated worktree、基线校验、candidate merge、验收与用户确认集成。
6. 再建设 Worker UI。运行中断恢复和重试通过后，才加入并行和动态重新规划体验。

## G2 测试矩阵

- 无协作 profile 时，桌面主 Runtime 正常完成单模型任务。
- 主 Agent 启动咨询任务后继续执行父任务，咨询上下文与工作区保持隔离。
- Coding Worker 与 Main Brain 并行运行，Host 返回正确 owner、task id 和状态版本。
- 依赖任务只在前置实现结果通过集成后启动。
- Worker 请求审批、取消、失败、超时和 Runtime 崩溃均保留可恢复任务状态。
- Host 重启后从持久化快照恢复主任务与 Worker 列表，不重放模型或工具副作用。
- Worker 结果的 baseline SHA 过期、worktree 缺失、改动冲突和验收失败会阻止集成。
- 多个 Worker 不共享可写工作区。Review worktree 无法产生未授权写入。
- 主任务和 Worker 的并发数、调用预算、费用信息和超时均有上限。
- Preload 暴露的任务 API 经过参数校验。Renderer 不直接访问 Worker 文件或凭据。
- 断线后使用 task snapshot 加事件序号恢复，没有重复或漏掉终态。
- 至少两个真实 Provider 完成真实任务交接，并记录模型、请求数、token、时间、费用可用性及最终 Git SHA。

G2 仅在 G1 的关键 D2 验收解除后进入实现。本文件不证明 Desktop 已支持 Worker 或多 Agent 并行。

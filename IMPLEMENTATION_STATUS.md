# AgentDU 实现状态

最后核对日期为 2026-11-27。仓库 `main` 与 `origin/main` 的验收起始 HEAD 均为 `c582fa831de4b92e456a4fefdd9f97139799b052`，工作区干净。最近的源码提交为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，`c582fa8` 包含验收文档更新。自动化在 `c582fa8` 工作树重跑。完整证据见 [G1 验收记录](docs/desktop/g1-closeout-2026-11-27.md) 与 [测试记录](TESTING.md)。

状态枚举使用 `PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED` 与 `BLOCKED`。

## 阶段状态

| 阶段 | 状态 | 当前证据与未完成项 |
| --- | --- | --- |
| G0 仓库审计与基线 | CONDITIONAL PASS | 当前代码、分支、远端和测试基线已核对。真实 Provider 工作流与隔离 profile 未修改验收仍未通过。 |
| G1 桌面基础能力稳定化 | BLOCKED | Python 96 项、Desktop 19 项、生产构建、AppContainer 探针与当前 NSIS 包检查通过。实际 Electron `safeStorage` 和本地合成 Provider 注入有部分证据。隔离验收误启动了使用默认 profile 的应用，原用户数据目录因此发生变化。活动任务期间的 Main 崩溃恢复未验证，真实 Provider 编程任务因 AgentDU 内没有已配置的真实凭据而阻塞。详见 G1 报告。 |
| G2 多 Agent Runtime 集成 | NOT VERIFIED | G2 设计已完成二次审计并补足取消、Host 资源、Worker 权限与状态存储所有权契约。桌面 Host 尚未接入 `AutonomousCoordinator`，G1 关键门槛解除前不开始实现。 |
| G3 多 Agent 并行执行 | NOT VERIFIED | CLI 协调器有 Worker、依赖和隔离 Worktree 逻辑。桌面并行执行未验收。 |
| G4 桌面多 Agent 工作台 | NOT VERIFIED | 桌面 Agent、Worker、任务依赖、审批与集成审查界面未完整实现。 |
| G5 自主协作与独立验证 | NOT VERIFIED | 桌面端和真实 Provider 组合未通过自主委派、审核、整合与重新规划验收。 |
| G6 容错与长期运行 | NOT VERIFIED | Host 层受控任务恢复已有证据。真实模型故障、长任务、进程树资源限制和产品级恢复仍未完成。 |
| G7 扩展生态 | NOT VERIFIED | CLI 层已有 MCP、Skills 和插件。桌面产品生命周期集成未验收。 |
| G8 产品化与正式发布 | NOT VERIFIED | 正式签名发布、升级迁移、完整安全审查和真实项目验收未完成。 |

## 已核实实现

- Python `AgentRuntime` 管理模型工具循环、事件和 JSONL 会话。Provider 适配覆盖 OpenAI-compatible Chat Completions 与 Responses。
- CLI 的 `AutonomousCoordinator` 支持有界 Worker 调度、依赖、独立会话、结果验收和 Git 集成。
- Handoff 使用版本、owner、快照和验收证据。
- Desktop 使用 Electron、React、TypeScript 和独立 Python Host，通过版本化 JSONL 通信。
- Desktop Host 持久化主任务的 accepted、completed、failed、cancelled 与 interrupted 生命周期事件。Host 恢复扫描会话并恢复未终结任务，不重放不确定的模型或工具操作。
- Windows AppContainer 探针确认项目读写限制、loopback 网络限制和子进程限制。
- 当前源码安装包已在含空格路径下启动，进程映像指向安装目录中的 Embedded Python。隔离 profile 中的合成 Provider 档案、Runtime 配置与最近项目在重启后可见。

## 当前未完成的桌面验收

- NSIS 隔离检查的“原用户 profile 未修改”条件失败。本轮一次默认实例启动写入了 `C:\Users\ASUS\AppData\Roaming\agentdu-desktop` 下的偏好、缓存、Runtime 日志和最近工作区数据。该目录中的 Provider 密文文件时间戳与用户项目会话文件时间戳未变化。完整记录见 G1 报告。
- Provider 的创建、Endpoint 与 Model ID 保存、默认档案首次选择、Electron `safeStorage` 加密、Runtime 环境注入和本地 `/models` 认证请求已有合成凭据证据。默认档案切换、删除后凭据不再注入、Runtime 重启失败回滚及 Renderer 侧隔离仍需端到端补测。
- 安装版 Main 空闲异常退出后，嵌入式 Host 在 8 秒内退出且没有 Host 孤儿进程。会话当时只有 `session_created` 事件，活动任务中断写入、重启后的任务恢复和防重复执行尚未验证。
- 真实 Provider 连通性、真实跨文件编程任务和真实模型任务中断恢复没有运行。Codex 或其他应用凭据没有读取或复用。

G1 当前为 `BLOCKED`。在隔离 profile 复验、活动任务 Main 崩溃恢复及真实 Provider 编程验收完成前，不进入 G2 实现。

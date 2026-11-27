# AgentDU 实现状态

最后核对：2026-11-27。代码与自动化验证基线为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，分支为 `main`。验收报告文档单独提交并明确保留测试目标 SHA。
状态枚举：`PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED`、`BLOCKED`。

## 阶段状态

| 阶段 | 状态 | 当前证据与未完成项 |
| --- | --- | --- |
| G0 仓库审计与基线 | CONDITIONAL PASS | 初始核对时 `main` 与 `origin/main` 位于 `c030c6c6184a8b2ad3f66e95fa9a244c33a15b0d` 且工作区干净。检查了关键 Runtime、Provider、CLI 协调、Host、IPC 与阶段文档。当前自动化在 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e` 上通过 Python 96 项、Desktop 19 项和生产构建。真实 Provider 与安装版全流程仍未验证。 |
| G1 桌面基础能力稳定化 | NOT VERIFIED | Host 层现有任务恢复机制和重新打开项目时的持久化扫描通过了新增集成测试。会话损坏、工作区不可用和重复恢复有错误与幂等证据。Provider 保存与加密、真实模型任务、Electron Main 异常退出恢复和最新 NSIS 包安装运行仍未完成。详见 [G1 验收记录](docs/desktop/g1-closeout-2026-11-27.md)。 |
| G2 多 Agent Runtime 集成 | NOT VERIFIED | 已形成 [G2 集成方案](docs/desktop/g2-runtime-integration.md)。Desktop Host 尚未接入 `AutonomousCoordinator`。G1 关键验收通过前不开始实现。 |
| G3 多 Agent 并行执行 | NOT VERIFIED | CLI 协调器有并行 Worker、依赖和隔离 Worktree 逻辑。桌面并行执行未验收。 |
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
- Desktop Host 持久化主任务的 accepted、completed、failed、cancelled 与 interrupted 生命周期事件。新 Host 打开原项目时扫描会话，恢复未终结任务。恢复不重放不确定的模型或工具操作。
- Windows AppContainer 探针确认项目读写限制、loopback 网络限制和子进程限制。细节见 D1/D2 与 G1 验收记录。

## 当前未完成的桌面验收

- AgentDU 设置中没有本轮验证过的 Provider 档案和可用凭据。Codex 或其他客户端的凭据没有读取或复用。
- Provider 档案创建、`safeStorage` 加密与解密、默认模型、Chat Completions/Responses 配置、连通性检查和应用重启后的配置恢复没有完成端到端测试。
- 尚无真实 Provider 连通性、真实跨文件编程任务和真实模型任务中断恢复记录。
- 受控 Runtime 任务取消和 Host 崩溃恢复已有自动化及 NSIS 协议证据，但 Electron Main 异常退出后的完整 Windows UI 恢复仍未验证。
- 基于验证 SHA 构建的最新 NSIS 安装器 SHA-256 为 `B5446953C8DB27D0B2EC704830C770F0B8644A6C720EEA60D2B60D8355945FC6`。该安装器尚未在隔离 profile 中安装运行。
- 安装版试运行发现 `APPDATA` 环境变量没有隔离 Electron `userData`。主进程现提供可选 `AGENTDU_DESKTOP_USER_DATA_DIR` 覆盖，并已构建含该更改的安装器。运行时隔离效果仍需验证。

G1 维持 `NOT VERIFIED`。在真实 Provider 任务、Electron 应用崩溃恢复及最新安装版验收解除前，不进入 G2 实现。

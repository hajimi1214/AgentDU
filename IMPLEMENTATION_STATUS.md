# AgentDU 实现状态

最后核对：2026-11-26
Git 基线：`main` / `8363f82146f95c67ed8f4edb0c9d60b46b2594c6`，本次进度在该提交上继续。
状态枚举：`PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED`。

## 阶段状态

| 阶段 | 状态 | 当前证据与未完成项 |
| --- | --- | --- |
| G0 仓库审计与基线 | CONDITIONAL PASS | 已检查分支、提交和工作区，审阅 Python Runtime、Provider、CLI 编排、Handoff、桌面 Host、IPC 与已有验收记录。当前复测 Python 90 项、Desktop 17 项测试通过，Desktop 生产构建通过。长期审计随新发现继续更新。 |
| G1 桌面基础能力稳定化 | NOT VERIFIED | D1/D2 已有实现、自动化和安装版 Host 证据。真实 Provider 任务、Renderer UI 取消端到端、最近项目恢复超时及完整会话恢复仍未解决或验收。 |
| G2 多 Agent Runtime 集成 | NOT VERIFIED | CLI 有 `AutonomousCoordinator`。桌面 Host 仍为单活动任务执行路径，尚未接入协调器。 |
| G3 多 Agent 并行执行 | NOT VERIFIED | CLI 协调器具备并行 Worker、依赖和隔离 Worktree 的实现与测试。桌面多 Agent 运行路径未验收。 |
| G4 桌面多 Agent 工作台 | NOT VERIFIED | 尚无桌面多 Agent 任务和 Worker 管理完整验收。 |
| G5 自主协作与独立验证 | NOT VERIFIED | CLI 有自主协作和验证逻辑。桌面端及真实提供商组合尚未完成验收。 |
| G6 容错与长期运行 | NOT VERIFIED | Runtime 有会话和任务恢复机制，产品级长任务、故障注入及恢复验收仍未完成。 |
| G7 扩展生态 | NOT VERIFIED | CLI 层有 MCP stdio、Skills 和显式启用的 Python 插件。桌面配置与产品生命周期集成仍未验收。 |
| G8 产品化与正式发布 | NOT VERIFIED | 有可构建的 Windows NSIS 包和安装版探针记录。正式发布、升级迁移、完整回归、安全审查和真实项目验收未完成。 |

## 已核实实现

- Python `AgentRuntime` 管理模型工具循环、事件和 JSONL 会话。Provider 适配目前覆盖 OpenAI-compatible Chat Completions 与 Responses。
- CLI 已集成有界自主协作、依赖调度、Worker 私有会话与工作区、结果验收和 Git 集成。
- Handoff 路径使用独立工作区及基线、结果、验收检查。
- Desktop 使用 Electron、React、TypeScript 和独立 Python Host，以版本化 JSONL 通信。
- Windows AppContainer 探针和 Python 集成测试覆盖所记录的文件、网络和子进程边界。

## 当前未完成的桌面验收

- AgentDU 本地 Provider 档案和凭据未配置。Codex 使用的 Provider 配置不会自动成为 AgentDU 配置。
- 尚无真实 Provider 连通性和真实跨文件编程任务记录。
- 已在开发版 Electron 窗口看到受控 Runtime 流式文本，样式 CSP 已针对 Vite 开发态修复，生产构建仍使用严格策略。Renderer 取消按钮端到端验收仍未完成。
- 最近项目恢复后仍出现 `desktop.inspectWorkspace` 超时。已将工作区检查移出 Host 事件循环并缩短 Git 状态检查超时，但重启开发版后症状仍复现，根因尚未解决。
- 尚未完成安装版中已有模型会话的完整恢复及真实任务恢复流程。
- 最新源码打包输出存在于忽略目录 `desktop/release/d1-current-2026-11-26`，该构建尚未安装运行验证。已安装的 `d2-final` 测试副本不应因复用固定输出目录而被覆盖。

下一阶段门槛和长期顺序见 [路线图](ROADMAP.md)。

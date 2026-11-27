# AgentDU 实现状态

最后核对：2026-11-27
本轮实施起点：`main` / `1f75df76562ff25a7680af6748fd1b345be083cf`。
状态枚举：`PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED`。

## 阶段状态

| 阶段 | 状态 | 当前证据与未完成项 |
| --- | --- | --- |
| G0 仓库审计与基线 | CONDITIONAL PASS | 已检查分支、提交和工作区，审阅 Python Runtime、Provider、CLI 编排、Handoff、桌面 Host、IPC 与已有验收记录。当前复测 Python 92 项、Desktop 19 项测试通过，Desktop 生产构建通过。长期审计随新发现继续更新。 |
| G1 桌面基础能力稳定化 | NOT VERIFIED | D1/D2 已有实现、自动化和安装版运行证据。最新 NSIS 包在含空格隔离目录、受限 `PATH` 下使用 embedded Python 启动 Runtime，并通过 Renderer 取消和空闲 Host 异常后的 UI 重启恢复测试，截图、会话 JSONL 和 Host 日志已保存。新源码包隔离 profile 恢复最近项目与空白会话。真实 Provider 任务、活动任务中断恢复和已有模型对话恢复仍未验收。 |
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
- Desktop 使用 Electron、React、TypeScript 和独立 Python Host，以版本化 JSONL 通信。Host 已把 task.accepted、task.cancel_requested、task.cancelled、task.failed、task.completed 写入会话 JSONL，取消生命周期持久化有回归测试。
- Windows AppContainer 探针和 Python 集成测试覆盖所记录的文件、网络和子进程边界。

## 当前未完成的桌面验收

- AgentDU 本地 Provider 档案和凭据未配置。未授权读取或复用 Codex 凭据。对用户给定 Endpoint 的未认证 `/models` 请求返回 HTTP 401。
- 尚无真实 Provider 连通性和真实跨文件编程任务记录。
- Electron 窗口已显示 Runtime 在线、最近项目及 Git 状态。2026-11-27 在隔离用户数据目录的含空格源码包副本中，从 Renderer 点击停止按钮取消受控 Runtime 自检，界面收到“任务已取消”，会话 JSONL 记录 task.cancelled 且无 task.completed，证据见 `docs/desktop/evidence/d2-runtime-lifecycle-persist-2026-11-27.jpg`。
- 已修复 Host 内 Git 状态子进程继承 JSONL 输入管道后挂起的问题。Host 集成测试覆盖非 Git 目录、嵌套 Git 仓库、工作区范围内的已跟踪与未跟踪改动，以及 Git 超时后的文件列表保留。2026-11-27 已在 NSIS 安装版复验最近项目恢复和 Git 状态，并在新源码包隔离 profile 中复验最近项目与空白会话恢复。已有模型对话的安装版恢复仍待验证。
- 尚未完成安装版中已有模型会话的完整恢复及真实任务恢复流程。
- 2026-11-27 早期安装验证包位于忽略目录 `desktop/release/d2-ui-cancel-manual-2026-11-26-60s`，其自检间隔为临时 60 秒版本。当前源码使用 6 秒间隔。当前 NSIS 包已于 2026-11-27 安装到隔离含空格目录，且已验证安装版 Renderer 取消和空闲 Host 异常后的 UI 重启恢复。活动模型任务恢复仍待验证。

下一阶段门槛和长期顺序见 [路线图](ROADMAP.md)。

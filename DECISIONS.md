# AgentDU 架构决策记录

最后更新：2026-11-26。下列记录描述当前实现基线和已识别的演进边界，持续随代码证据更新。

## ADR-001 桌面端复用 Python Runtime

**状态：已采用**

桌面端采用 Electron、React、TypeScript 和独立 Python Runtime Host。Electron 负责桌面窗口与受限 IPC，Python 保留 Agent 执行核心。Main 和 Host 使用版本化 JSONL stdio 协议。这样桌面化沿用 `AgentRuntime`、Provider、工具与会话代码。

证据见 `desktop/src/main/index.ts`、`desktop/src/preload/index.ts`、`desktop/src/shared/protocol.ts` 和 `src/agentdu/desktop_host.py`。

## ADR-002 Renderer 只获得显式授权 API

**状态：已采用**

Renderer 通过 Preload 的明确 API 请求运行时操作。Electron 不向 Renderer 开启 Node 集成或通用 IPC。

证据见 `desktop/src/preload/index.ts` 和 `desktop/src/main/index.ts`。

## ADR-003 模型 Provider 通过配置引用密钥

**状态：已采用**

模型档案引用环境变量中的密钥。命令行参数、仓库文件和共享提交不承载明文 API Key。Codex 本身的模型配置和 AgentDU 的 Provider 配置保持独立。

证据见 `src/agentdu/settings.py`、`src/agentdu/security.py` 和 `README.zh-CN.md`。

## ADR-004 分开记录 CLI 协作与桌面协作

**状态：桌面集成待定**

CLI 的 `AutonomousCoordinator` 与 Handoff 具有各自的任务、依赖和工作区实现。桌面 Host 当前没有接入自主协调器，因此后续需要先定义共享任务协议与生命周期，再将 CLI 能力纳入桌面 UI。

证据见 `src/agentdu/cli.py`、`src/agentdu/orchestration.py`、`src/agentdu/collaboration.py` 和 `src/agentdu/desktop_host.py`。

## ADR-005 不通过移除单任务检查启用桌面并发

**状态：保留为当前约束**

Desktop Host 当前拒绝并发的第二个活动 Agent 任务。并发扩展需要先解决任务所有权、取消、恢复、事件订阅、资源约束及代码工作区隔离。

证据见 `src/agentdu/desktop_host.py` 的任务启动与活动任务管理路径。

## ADR-006 AppContainer 与 Git Worktree 解决不同问题

**状态：已采用**

AppContainer 用于限制桌面终端命令的操作系统访问范围。Git Worktree 用于隔离协作 Agent 的代码状态。Worktree 本身不提供文件、网络或进程安全边界。

证据见 `src/agentdu/windows_sandbox.py`、`src/agentdu/windows_appcontainer_command.ps1`、`src/agentdu/git_workspaces.py` 和对应测试。

## ADR-007 保留 JSONL 会话持久化

**状态：已采用**

Runtime 会话以追加式 JSONL 事件保存并验证事件序号，测试覆盖不完整尾记录修复和跨进程锁。桌面可在此基础上继续完善会话列表和恢复交互。

证据见 `src/agentdu/sessions.py` 和 `tests/test_runtime.py`。

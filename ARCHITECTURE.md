# AgentDU 架构基线

最后更新：2026-11-26
基线：`85789531927c597c75d213b8a7f5fd68cc65d374`

## 运行时结构

```mermaid
flowchart TD
    UI[React Renderer] --> API[受限 Preload API]
    API --> Main[Electron Main]
    Main -->|版本化 JSONL over stdio| Host[Python Desktop Host]
    Host --> Runtime[AgentRuntime]
    Runtime --> Context[ContextManager]
    Runtime --> Provider[OpenAI-compatible Provider]
    Runtime --> Tools[ToolRegistry 与 PermissionPolicy]
    Runtime --> Events[EventBus]
    Runtime --> Sessions[JSONL SessionStore]
    CLI[Python CLI] --> Runtime
    CLI --> Coordinator[AutonomousCoordinator]
    Coordinator --> Workers[私有 Worker Runtime 与 Worktree]
```

Electron、Renderer 和 Host 的职责与数据流记录在 [D1 实施记录](docs/desktop/d1-implementation.md)。Runtime 模块契约见 [现有架构说明](docs/architecture.md)。

## 桌面调用链

- Renderer 使用 React 和 TypeScript，通过 `desktop/src/preload/index.ts` 暴露的有限 API 调用主进程。它不获得 Node 集成、文件系统或通用 Shell API。
- Electron Main 在 `desktop/src/main/index.ts` 管理窗口、IPC、Host 生命周期和请求校验。Host 客户端位于 `desktop/src/main/host-client.ts`。
- Main 以参数数组和 `shell: false` 启动 Python Runtime Host。Host 通过 stdin/stdout 交换版本化 JSONL 消息，stdout 保留给协议，诊断输出走 stderr。
- 共享协议定义位于 `desktop/src/shared/protocol.ts`，Python 端位于 `src/agentdu/desktop_host.py`。协议版本为 1，包含请求响应及有序事件。
- Host 复用 `AgentRuntime`、Provider、工具注册表、事件和会话持久化能力。核心运行循环入口为 `src/agentdu/agent.py`。

## Runtime 与协作边界

- 模型调用经统一 `ChatModel` 契约进入 `src/agentdu/provider.py`。当前实现覆盖 OpenAI-compatible Chat Completions 和 Responses 协议。
- CLI 已在 `src/agentdu/cli.py` 中创建自主协调器。`src/agentdu/orchestration.py` 管理有界 Worker 调度、依赖、独立会话和结果验证。Worker 工作区由 Git Worktree 相关模块隔离。
- 桌面 Host 当前通过默认 Runtime 启动桌面任务，限制单个活动任务。桌面 Host 尚未接入 `AutonomousCoordinator`，CLI DAG 调度与固定 Handoff 流程也尚未统一成桌面任务协议。
- 会话以 JSONL 持久化。恢复路径应根据记录状态继续处理，未确认的副作用操作不能因重启而被自动重放。

## 信任与权限边界

- Renderer 仅通过显式 Preload API 访问主进程能力。
- 文件写入、终端命令和扩展启动受 Runtime 权限策略管理。桌面 Host 的 Windows 终端执行使用 AppContainer 路径，测试覆盖项目目录访问、网络和子进程边界，记录见 D1/D2 验收文档。
- Git Worktree 用于代码状态隔离，不代表操作系统安全沙箱。
- API 凭据通过配置指定的环境变量解析，不能存入仓库或命令行参数。AgentDU Provider 配置与 Codex 应用配置分离。

## 当前主要缺口

1. D1/D2 桌面端到端验收未完成，细项见 [实现状态](IMPLEMENTATION_STATUS.md)。
2. 桌面没有接入 CLI 已有的自主多 Agent 调度。
3. 桌面任务生命周期、恢复、并发与协作事件缺少统一协议。
4. 真实 Provider 兼容性和真实编程任务尚无本地运行证据。
5. 当前构建脚本将 Windows 打包输出目录固定为 `desktop/release/d2-final`，复用该路径前需要考虑已有安装测试副本。

# Codex 与 DeepSeek Harness 桌面端研究

| 项目 | 值 |
| --- | --- |
| 研究日期 | 2026-11-23 |
| AgentDU 基线 | `main`，`2aa31341feea69ac1f3c3c062ddfba08082129e4` |
| DeepSeek Harness 上游 | `master`，`4b399c39a6d1479cd84ca591b4d7c88e24d38a96`，通过 `git ls-remote` 核验 |
| 范围 | 公开产品资料、公开协议与可读源码。未将未公开的 Codex Desktop 内部实现当作事实。 |

## 结论速览

| 主题 | Codex Desktop | DeepSeek Harness Desktop | 对 AgentDU 的结论 |
| --- | --- | --- | --- |
| 产品中心 | 按项目组织多个长期线程，用户监督并行 Agent | 桌面壳承载完整 Harness Web 工作区 | 主界面应围绕项目、任务线程、活动记录和代码审查设计 |
| 工作隔离 | 官方产品资料明确支持 Git worktree | Desktop 与 Web 共享 Harness Host 和产品会话 | 保留 AgentDU 已实现的独立会话、Git worktree 和验证式集成 |
| 桌面实现证据 | 官方公开 app-server 协议和 Codex CLI 源码，桌面 UI 本身没有在本次来源中公开 | Electron 主进程、隔离 Preload、共享 Web Client、Node 模式 Host 均有源码 | 学习公开协议和产品行为，不推测 Codex Desktop 私有实现 |
| 本地 Runtime | 产品资料关注线程、工具、授权、工作树和后台工作 | Electron 通过进程通道启动本地 Host，Web 资产与桌面适配共用 | AgentDU 的 Python Runtime 保持独立，通过受版本控制的本地 IPC 接入 UI |
| 语言 | 官方页面未在本次查阅范围内给出完整语言列表 | Desktop 文档和 `locale.ts` 包含中文语言路径 | AgentDU 首版界面和文档以简体中文为默认语言，保留英文扩展点 |
| 许可证 | `openai/codex` 快照根许可证为 Apache-2.0 | 上游根目录及 Desktop package 声明 MIT | 仅吸收设计思路。任何代码复用必须逐文件核对许可、NOTICE 和第三方依赖 |

证据标签在本文中采用“事实”“推断”“待验证”。事实指能在所列官方文档、公开协议或源码找到直接证据。推断指基于事实提出的产品方案。待验证指需要在 AgentDU 开发阶段通过运行或测试确认。

## Codex Desktop

### 可确认的产品行为

- **事实**：OpenAI 于 2026-02-02 发布 Codex 桌面应用介绍，页面注明 Windows 版本于 2026-03-04 开放。页面将它描述为 Agent 工作指挥界面，支持多个 Agent 并行执行、按项目组织独立线程、在会话中审查变更和评论 Diff、打开本地编辑器，并提供 worktree 隔离。该产品说明也介绍 Skills 管理和后台 Automations 结果审阅。来源为 [OpenAI Codex app 发布说明](https://openai.com/index/introducing-the-codex-app/)。
- **推断**：桌面界面的核心价值是让人可以持续监督长任务和切换任务，而不只是把对话窗口放进桌面壳。AgentDU 应把“当前工作、运行状态、文件变更和下一步可执行操作”放在主要视图。
- **待验证**：Codex Desktop 的完整内部 UI、进程边界、持久化格式、具体沙箱实现及私有 API 不作为本研究的事实依据。

### 可复用的公开协议研究

- **事实**：Codex App Server 官方文档称其是 Codex 为丰富客户端提供的接口，例子包括 Codex VS Code 扩展，公开代码位于 `openai/codex/codex-rs/app-server`。协议采用 JSON-RPC 风格双向消息，默认 `stdio` 使用逐行 JSON。连接先执行 `initialize`，之后客户端可创建或恢复 thread，启动 turn 并接收事件通知。公开 schema 包含 thread、turn、item、审批、Skills、命令执行和模型列表等能力。来源为 [Codex App Server 官方文档](https://developers.openai.com/codex/app-server/)。
- **推断**：对 AgentDU 而言，价值在于用版本化请求/响应和事件通知替代桌面 UI 对 Python 内部类的直接调用。AgentDU 应定义自身协议，不依赖 Codex 协议的字段和许可。
- **事实**：公开 App Server 的协议不等于 Codex Desktop 私有实现的完整规格，也不意味着 AgentDU 可以将 Codex CLI 当作产品内核。

### 值得吸收与边界

1. 以项目为导航边界，每个任务使用可恢复线程，支持从任务视图进入会话和代码审查。
2. 将并行任务展示成可监督的状态，而不是只显示一个生成中的对话气泡。
3. Diff 审查成为任务完成的一级流程，允许用户查看、评论、接受或继续修改。
4. 将后台任务与通知纳入产品模型，任务完成后进入可审查队列。
5. 把线程、turn、工具活动和 Agent 子任务分成不同层级的数据结构。

以上 1 至 5 项属于根据官方产品介绍提出的产品设计推断。Codex 页面没有公开所有功能的本地实现细节。

## DeepSeek Harness Desktop

### 版本与许可证范围

- **事实**：本地 `agent-learning-library/sources/deepseek-harness` 是已下载的源码快照，缺少 `.git` 历史。为桌面研究核对的官方 `master` 头部 SHA 为 `4b399c39a6d1479cd84ca591b4d7c88e24d38a96`。桌面源码链接应固定到该 SHA，而非依赖会变化的 `master`。
- **事实**：上游根 `LICENSE` 和 `apps/desktop/package.json` 声明 MIT。该声明不覆盖所有 vendored 文件和依赖包的各自许可，AgentDU 若考虑复用具体代码仍须检查对应目录、NOTICE 和依赖许可证。
- **推断**：Harness 是较适合研究桌面进程集成和产品生命周期的公开样本，但其复杂的 Web 插件生态、内置运行时和发布体系不应整体移植到 AgentDU。

### 应用进程与 Web Host

- **事实**：`apps/desktop/README.md` 将 Desktop 描述为完整 Harness Web 应用外的一层 Electron 壳。`apps/desktop/src/main.ts` 负责 Electron 应用生命周期、窗口、协议、IPC、目录选择、更新和恢复流程。
- **事实**：`apps/desktop/src/host-process.ts` 的 `DesktopHostProcess` 启动子进程并接收带类型的 Host 消息。`apps/desktop-host/src/index.ts` 的 `main()` 启动共享 profile runner，将 Host 绑定到 loopback 临时端口，完成认证后向父进程发布 ready URL 和启动注入。
- **事实**：主进程为每个本地 Host 生成认证凭据，经私有进程 IPC 交付给 Host。主进程持有附加到 Host HTTP 与 WebSocket 请求上的凭据，渲染进程只接收启动所需数据。Harness 桌面文档说明凭据不进入普通 Electron session Cookie。
- **事实**：Electron 使用自定义 `dsh-app://app/` 协议加载本地打包 Web 资源，Web Client 和 Desktop 通过共享 Host 与前端包提供产品行为。源码中的 `preload-app.ts` 通过 `contextBridge` 暴露有限桥接函数。
- **推断**：AgentDU 可以复用“桌面壳负责平台能力，Runtime 服务负责 Agent 工作”的边界，但不需要复制 Harness 的 Web Host、Node 插件系统或自定义协议实现。

### 启动、关闭与故障恢复

- **事实**：Harness Desktop 有单实例控制、显式 Host 启动和停止、Host 就绪事件、关闭确认、崩溃诊断、重启恢复、更新协调和后台运行提示。`apps/desktop/README.md` 记录了活动任务、任务计划、托盘与退出确认的不同语义。
- **事实**：`apps/desktop-host/src/index.ts` 收到 shutdown 后尝试停止 profile runner，等待后台文件操作并向父进程报告 shutdown-complete。主进程和 Host 分别维护启动错误与致命故障诊断。
- **推断**：AgentDU 的桌面首版应先定义启动握手、健康状态、关闭阶段、不可恢复错误和用户可执行恢复操作，再接入丰富 UI。
- **待验证**：AgentDU 当前 Runtime 没有桌面 Host 生命周期监督器。Python 子进程启动、stdout 协议损坏、退出码、崩溃后会话重新载入、正在运行工具的中断语义必须通过 D1/D2 集成测试验证。

### 语言支持

- **事实**：Harness 的 `apps/desktop/src/locale.ts` 包含 `zh-CN` 语言字典。桌面文档记录了桌面壳语言缓存、系统语言回退和已选语言向主 Client UI 传递的逻辑。源码测试覆盖操作系统 `zh-CN` 和显式设置后重载持久化。
- **推断**：AgentDU 的语言选项应由应用设置统一管理，并由启动恢复对话框、窗口标题、菜单、主界面和任务通知共同遵循。首启选择系统语言，默认支持 `zh-CN`，保留 `en`。
- **待验证**：当前 AgentDU 有中文 README 和中文 CLI 帮助格式，但没有桌面语言资源、语言切换 UI 或桌面壳，不能将 CLI 的中文支持误认为桌面中文版已经存在。

### 安全和工程取舍

- **事实**：Harness Desktop 对 Host 地址限制为 loopback，使用随机启动凭据和主进程授权校验。Preload 仅为受信任的本地页面暴露选定功能。Electron 官方安全指南建议启用 context isolation、进程沙箱、限制导航、定义 CSP，并避免为远程内容开启 Node integration。来源为 [Electron 安全指南](https://www.electronjs.org/docs/latest/tutorial/security) 和 [Context Isolation](https://www.electronjs.org/docs/latest/tutorial/context-isolation)。
- **事实**：Harness 采用绑定版本的 Electron、Web Client、Host、插件和打包资源，并拥有独立的桌面开发、打包、更新、签名及恢复脚本。源码和 README 表明桌面启动会准备内置运行时，首次准备可能涉及下载锁定依赖。
- **推断**：桌面产品安全不是把权限弹窗放进 UI 就能解决。AgentDU 还必须隔离 renderer、约束 Shell 执行、限制凭据和模型数据流，并让用户能查阅已批准的实际命令。
- **事实**：Harness 打包文档将 Linux 列为非正式 Desktop 发布目标。这只描述 Harness 当前打包策略，不能外推为 Electron 不支持 Linux。

## 与 AgentDU 现状的对应

### 可以直接复用的 Runtime 能力

| AgentDU 能力 | 代码依据 | 桌面化复用方式 |
| --- | --- | --- |
| 单模型 Agent Loop 与流式事件 | `src/agentdu/agent.py` `AgentRuntime.run`、`src/agentdu/events.py` `EventBus` | 通过 Runtime 应用服务启动 turn，并把已定义事件转换为 UI 活动 |
| Provider 契约和 OpenAI 兼容流式调用 | `src/agentdu/models.py` `ChatModel`、`src/agentdu/provider.py` `OpenAICompatibleModel` | 保留适配器边界，之后扩展真正的 Provider 实现和能力差异 |
| 会话与中断恢复数据 | `src/agentdu/sessions.py` `SessionStore` | 保留 JSONL 源数据，用 UI 查询接口投影会话、消息、工具调用和恢复状态 |
| 工具权限与文件/Shell/Git 工具 | `src/agentdu/tools.py` `ToolRegistry`、`src/agentdu/permissions.py` `PermissionPolicy` | 用桌面审批视图替换终端交互策略，工具执行契约继续由 Runtime 拥有 |
| 多 Agent 任务和 Git worktree | `src/agentdu/orchestration.py` `AutonomousCoordinator`、`src/agentdu/git_workspaces.py` `GitWorkspaces` | 展示现有任务状态、分支差异和整合结果，保留真实 worktree 验证 |
| 固定角色交接状态机 | `src/agentdu/handoff.py` `HandoffLedger`、`src/agentdu/collaboration.py` `HandoffCoordinator` | 用 UI 呈现 offer、accept、验证和集成证据 |
| MCP、Skills 和 Python 扩展 | `src/agentdu/mcp.py`、`src/agentdu/skills.py`、`src/agentdu/plugins.py` | 将现有配置和授权能力接入设置页，扩展隔离仍待设计 |

### 必须承认的差距

- **事实**：`docs/v0.6-status.zh-CN.md` 声明 AgentDU 当前没有 Codex 级桌面界面、实时协作时间线、模型配置 GUI 或会话查看器。
- **事实**：本地 `pyproject.toml` 只定义 Python 包和命令行入口，没有 Electron、Tauri 或 Qt 依赖。
- **事实**：AgentDU 当前 `provider.py` 实现 OpenAI 兼容 Chat Completions 形状，`settings.py` 为对应模型 Profile 提供 TOML 配置。真实多 Provider 能力、模型特有协议和跨提供商任务接力还没有真实凭据评测证据。
- **事实**：`tools.py` 执行的 Shell 命令以当前用户权限运行，Git worktree 不是操作系统安全边界。审批能限制 AgentDU 工具入口，但不能阻止已批准命令访问工作区外文件。
- **事实**：协作任务状态目前绑定单进程。进程重启时活动任务会标记为 interrupted，不会自动重放不确定的模型操作。
- **推断**：桌面 UI 可以较高比例复用 AgentDU Runtime，但桌面安全、稳定 IPC、任务恢复策略和审批界面都需要新增工程工作。

## 许可证与代码复用界限

1. AgentDU 当前自身源码按仓库根目录许可证管理。
2. DeepSeek Harness 根许可证和 Desktop package 声明 MIT。若复制源文件，需保留许可证与版权信息并记录改动，同时检查该文件所依赖代码和包的许可证。
3. 本地 Codex 源码快照根许可证为 Apache-2.0，复用时需保留许可和适用的 NOTICE，并标记修改。
4. Codex Desktop 的产品文章与 App Server 公开协议可以用于产品研究。研究到的行为、界面组织和协议思想不等同于可直接复制其他仓库代码。
5. 本阶段只记录设计决策，不复制上述参考实现代码。

## 资料索引

- [Codex App 产品介绍](https://openai.com/index/introducing-the-codex-app/)
- [Codex App Server 官方文档](https://developers.openai.com/codex/app-server/)
- [DeepSeek Harness Desktop 英文开发文档](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop/README.md)
- [DeepSeek Harness Desktop 中文开发文档](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop/README.zh.md)
- [DeepSeek Harness Electron 主进程](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop/src/main.ts)
- [DeepSeek Harness Host 子进程管理](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop/src/host-process.ts)
- [DeepSeek Harness Host 入口](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop-host/src/index.ts)
- [Electron 安全指南](https://www.electronjs.org/docs/latest/tutorial/security)

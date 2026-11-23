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

- **事实** OpenAI 于 2026-02-02 发布 Codex 桌面应用介绍，页面注明 Windows 版本于 2026-03-04 开放。页面将它描述为 Agent 工作指挥界面，支持多个 Agent 并行执行、按项目组织独立线程、在会话中审查变更和评论 Diff、打开本地编辑器，并提供 worktree 隔离。该产品说明也介绍 Skills 管理和后台 Automations 结果审阅。来源为 [OpenAI Codex app 发布说明](https://openai.com/index/introducing-the-codex-app/)。
- **事实，截至 D0 研究时** OpenAI Help Center 的桌面应用迁移说明已将独立 Codex app 更新到新的 ChatGPT Desktop，导航中包含 Chat、Work 和 Codex。原有 Codex 会话和项目保留，Codex 仍是独立视图，其工作流和历史与 ChatGPT 分开。[迁移说明](https://help.openai.com/en/articles/20001276-moving-to-the-new-chatgpt-desktop-app)；[Work 与 Codex 使用说明](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)。因此本文沿用“Codex Desktop”指代被研究的编码 Agent 产品形态，现行安装和导航名称以 OpenAI Help Center 为准。
- **事实** OpenAI Help Center 将本地 Codex 描述为使用本机文件夹、仓库、终端和开发工具的桌面工作流。Codex Cloud 在 OpenAI 管理的计算机上执行任务，可在本机休眠时继续。两种执行位置不能混为一谈。[Work 与 Codex 使用说明](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)；[Codex 使用说明](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)。
- **事实** OpenAI Business 发布说明将 Windows Codex app 描述为支持并行 Agent、隔离 worktree、可审查 Diff，并可与 CLI 和 IDE 协同的桌面工作面。[Windows 桌面发布说明](https://help.openai.com/en/articles/11391654-chatgpt-business-release-notes)。该说明给出产品能力，不公开 Codex Desktop 的内部 IPC 或 Runtime 结构。
- **推断** 桌面界面的核心价值是让人可以持续监督长任务和切换任务，而不只是把对话窗口放进桌面壳。AgentDU 应把“当前工作、运行状态、文件变更和下一步可执行操作”放在主要视图。
- **待验证** Codex Desktop 的完整内部 UI、进程边界、持久化格式、具体沙箱实现及私有 API 不作为本研究的事实依据。

### 可复用的公开协议研究

- **事实** Codex App Server 官方文档称其是 Codex 为丰富客户端提供的接口，例子包括 Codex VS Code 扩展，公开代码位于 `openai/codex/codex-rs/app-server`。协议采用 JSON-RPC 风格双向消息，默认 `stdio` 使用逐行 JSON。连接先执行 `initialize`，之后客户端可创建或恢复 thread，启动 turn 并接收事件通知。公开 schema 包含 thread、turn、item、审批、Skills、命令执行和模型列表等能力。来源为 [Codex App Server 官方文档](https://developers.openai.com/codex/app-server/)。
- **推断** 对 AgentDU 而言，价值在于用版本化请求/响应和事件通知替代桌面 UI 对 Python 内部类的直接调用。AgentDU 应定义自身协议，不依赖 Codex 协议的字段和许可。
- **事实** 公开 App Server 的协议不等于 Codex Desktop 私有实现的完整规格，也不意味着 AgentDU 可以将 Codex CLI 当作产品内核。

### Codex 仓库中可复核的 App Server 实现

- **事实** 本地学习库保存的 Codex 源码快照对应 `1476a08f1b410e359e64becf2494bc248743e396`。这是 Codex 开源仓库快照版本，不是 Codex Desktop 可执行程序版本。根许可证为 Apache-2.0。版本记录在 `agent-learning-library/README.md`。
- **事实** 本地 `lib.rs`、initialize processor、thread processor、turn processor、command process manager 和 transport 文件已逐字节与该 SHA 的 GitHub raw 文件比对，均匹配。
- **事实** `codex-rs/app-server/src/main.rs` 调用 `run_main_with_transport_options`；`src/lib.rs` 的 `run_main` 默认传入 `AppServerTransport::Stdio`，并启动 `start_stdio_connection`。官方 App Server 文档确认 stdio 上每行一条 JSON 消息。这个边界使 UI 客户端可以只读写进程管道，不需要暴露监听端口。
- **事实** `src/request_processors/initialize_processor.rs` 的 `InitializeRequestProcessor::initialize` 拒绝重复 initialize，读取客户端能力和通知订阅选项，并在完成握手后设置 outbound initialized 状态。能力协商因此发生在通知开始投递之前。
- **事实** `src/request_processors/thread_processor.rs` 的 `thread_start_inner` 从请求显式取得模型、工作目录、approval policy、sandbox 或 permissions 等配置；例如同时提供 sandbox 与 permissions 会被拒绝。`src/request_processors/turn_processor.rs` 的 `turn_start_inner` 加载 thread、验证输入和模型配置，再调用 `start_or_steer_turn`。UI 请求将对话生命周期、任务执行和单次轮次分别建模。
- **事实** `src/bespoke_event_handling.rs` 把文件变更和命令执行审批转换为发往客户端的 server request，并等待具有关联 request ID 的答复，再映射 accept、decline、cancel 或 session approval 结果。它提供 UI 审批交互的源码证据，审批结果最终再交回 Agent 执行流程。
- **事实** `src/command_exec.rs` 按连接和进程 ID 管理终端子进程，支持启动、输出事件、写入 stdin、调整 PTY 和终止。源码拒绝没有 client-supplied process ID 的流式/TTY 会话，并指出当前 Windows sandbox 不支持部分流式和进程控制操作。AgentDU 的终端体验必须将 process lifecycle 纳入 Runtime 协议。
- **事实** `src/transport.rs` 对连接分别管理 outbound writer，过滤不支持的实验通知和客户端退订项。慢连接的有界队列填满后会断开连接；可丢弃的部分搜索进度会避让必须发送的响应。对 AgentDU 的参考点是让 UI 断线可重连、通过事件序号补齐状态，而不是让无限队列消耗内存。
- **推断** Codex App Server 展示了成熟 UI 与 Agent Runtime 之间的可观测协议如何承载 thread、turn、文件/命令审批、进程控制和客户端能力协商。AgentDU 可以参考这些职责和边界，仍需定义自己的稳定版本、事件模型与 Python 运行时接口。
- **待验证** 这些源码为指定 Codex 仓库快照，不代表 Codex Desktop 当前所有内部通信或桌面界面实现。当前公开资料不足以证明桌面产品恰好以公开 app-server 代码实现每一项 UI 功能。

本地源码位置索引如下。源码来自不含 Git 历史的归档快照，引用版本号由学习库清单记录。

| 职责 | Codex 源码目录为 `agent-learning-library/sources/codex/codex-rs/app-server/` | 关键实现 |
| --- | --- | --- |
| App Server 入口与传输选择 | `src/main.rs`、`src/lib.rs` | `run_main`、`run_main_with_transport_options`、`start_stdio_connection` |
| 客户端握手和能力协商 | `src/request_processors/initialize_processor.rs` | `InitializeRequestProcessor::initialize` |
| Thread 配置与生命周期 | `src/request_processors/thread_processor.rs` | `thread_start_inner` |
| Turn 启动和转向 | `src/request_processors/turn_processor.rs` | `turn_start_inner`、`start_or_steer_turn` |
| 审批请求桥接 | `src/bespoke_event_handling.rs` | `FileChangeRequestApproval`、`CommandExecutionRequestApproval` |
| 终端和进程控制 | `src/command_exec.rs` | `CommandExecManager`、进程 ID、PTY 与 stdin 控制 |
| 事件路由和慢客户端 | `src/transport.rs` | outbound queue、通知过滤与慢连接断开 |
| Windows 沙箱边界说明 | `README.md` | “Windows sandbox implementation selection” 小节 |

固定版本的 [Codex initialize processor](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server/src/request_processors/initialize_processor.rs)、[thread processor](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server/src/request_processors/thread_processor.rs)、[turn processor](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server/src/request_processors/turn_processor.rs)、[terminal process manager](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server/src/command_exec.rs) 和 [transport](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server/src/transport.rs) 可按该 SHA 查看。

### AgentDU 基线源码证据

以下源码位置均以 D0 开始时确认的 AgentDU 基线 `2aa31341feea69ac1f3c3c062ddfba08082129e4` 为准。当前 D0 文档只新增在该基线之上的研究资料，没有修改运行时代码。

| 已有能力或边界 | 源码证据 |
| --- | --- |
| 单模型工具循环和持久会话执行 | [`AgentRuntime.run`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/agent.py#L125) |
| OpenAI 兼容 Provider Adapter | [`OpenAICompatibleModel`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/provider.py#L20) |
| JSONL Session 事件与不确定工具调用恢复 | [`SessionStore`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/sessions.py#L93)、[`recover_interrupted_tool_calls`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/sessions.py#L227) |
| 工具执行和 Shell 命令 | [`ToolRegistry`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/tools.py#L67)、[`_run_command`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/tools.py#L444)、[`PermissionPolicy`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/permissions.py#L16) |
| 多 Agent 任务调度与任务状态 | [`AutonomousCoordinator`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/orchestration.py#L139) |
| Handoff 账本和双 Agent 交接流程 | [`HandoffLedger`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/handoff.py#L37)、[`HandoffCoordinator`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/collaboration.py#L41) |
| Git worktree、快照校验和验证后合并 | [`verify_snapshot`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/git_workspaces.py#L190)、[`merge_verified_result`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/git_workspaces.py#L215) |
| MCP、Skills、插件、凭据过滤和桌面缺口 | [`MCPStdioClient`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/mcp.py#L32)、[`SkillsExtension`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/skills.py#L26)、[`load_plugins`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/plugins.py#L18)、[`safe_child_environment`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/security.py#L30)、[V0.6 产品边界](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/docs/v0.6-status.zh-CN.md#L34) |

### 值得吸收与边界

1. 以项目为导航边界，每个任务使用可恢复线程，支持从任务视图进入会话和代码审查。
2. 将并行任务展示成可监督的状态，而不是只显示一个生成中的对话气泡。
3. Diff 审查成为任务完成的一级流程，允许用户查看、评论、接受或继续修改。
4. 将后台任务与通知纳入产品模型，任务完成后进入可审查队列。
5. 把线程、turn、工具活动和 Agent 子任务分成不同层级的数据结构。

以上 1 至 5 项属于根据官方产品介绍提出的产品设计推断。Codex 页面没有公开所有功能的本地实现细节。

## DeepSeek Harness Desktop

### 版本与许可证范围

- **事实** 本地 `agent-learning-library/sources/deepseek-harness` 是已下载的源码快照，缺少 `.git` 历史。为桌面研究核对的官方 `master` 头部 SHA 为 `4b399c39a6d1479cd84ca591b4d7c88e24d38a96`。桌面源码链接应固定到该 SHA，而非依赖会变化的 `master`。
- **事实** 上游根 `LICENSE` 和 `apps/desktop/package.json` 声明 MIT。该声明不覆盖所有 vendored 文件和依赖包的各自许可，AgentDU 若考虑复用具体代码仍须检查对应目录、NOTICE 和依赖许可证。
- **推断** Harness 是较适合研究桌面进程集成和产品生命周期的公开样本，但其复杂的 Web 插件生态、内置运行时和发布体系不应整体移植到 AgentDU。

### 应用进程与 Web Host

- **事实** `apps/desktop/README.md` 将 Desktop 描述为完整 Harness Web 应用外的一层 Electron 壳。`apps/desktop/src/main.ts` 负责 Electron 应用生命周期、窗口、协议、IPC、目录选择、更新和恢复流程。
- **事实** `apps/desktop/src/host-process.ts` 的 `DesktopHostProcess` 启动子进程并接收带类型的 Host 消息。`apps/desktop-host/src/index.ts` 的 `main()` 启动共享 profile runner，将 Host 绑定到 loopback 临时端口，完成认证后向父进程发布 ready URL 和启动注入。
- **事实** 主进程为每个本地 Host 生成认证凭据，经私有进程 IPC 交付给 Host。主进程持有附加到 Host HTTP 与 WebSocket 请求上的凭据，渲染进程只接收启动所需数据。Harness 桌面文档说明凭据不进入普通 Electron session Cookie。
- **事实** Electron 使用自定义 `dsh-app://app/` 协议加载本地打包 Web 资源，Web Client 和 Desktop 通过共享 Host 与前端包提供产品行为。源码中的 `preload-app.ts` 通过 `contextBridge` 暴露有限桥接函数。
- **推断** AgentDU 可以复用“桌面壳负责平台能力，Runtime 服务负责 Agent 工作”的边界，但不需要复制 Harness 的 Web Host、Node 插件系统或自定义协议实现。

### 启动、关闭与故障恢复

- **事实** Harness Desktop 有单实例控制、显式 Host 启动和停止、Host 就绪事件、关闭确认、崩溃诊断、重启恢复、更新协调和后台运行提示。`apps/desktop/README.md` 记录了活动任务、任务计划、托盘与退出确认的不同语义。
- **事实** `apps/desktop-host/src/index.ts` 收到 shutdown 后尝试停止 profile runner，等待后台文件操作并向父进程报告 shutdown-complete。主进程和 Host 分别维护启动错误与致命故障诊断。
- **推断** AgentDU 的桌面首版应先定义启动握手、健康状态、关闭阶段、不可恢复错误和用户可执行恢复操作，再接入丰富 UI。
- **待验证** AgentDU 当前 Runtime 没有桌面 Host 生命周期监督器。Python 子进程启动、stdout 协议损坏、退出码、崩溃后会话重新载入、正在运行工具的中断语义必须通过 D1/D2 集成测试验证。

### 语言支持

- **事实** Harness 的 `apps/desktop/src/locale.ts` 包含 `zh-CN` 语言字典。桌面文档记录了桌面壳语言缓存、系统语言回退和已选语言向主 Client UI 传递的逻辑。源码测试覆盖操作系统 `zh-CN` 和显式设置后重载持久化。
- **推断** AgentDU 的语言选项应由应用设置统一管理，并由启动恢复对话框、窗口标题、菜单、主界面和任务通知共同遵循。首启选择系统语言，默认支持 `zh-CN`，保留 `en`。
- **待验证** 当前 AgentDU 有中文 README 和中文 CLI 帮助格式，但没有桌面语言资源、语言切换 UI 或桌面壳，不能将 CLI 的中文支持误认为桌面中文版已经存在。

### 安全和工程取舍

- **事实** Harness Desktop 对 Host 地址限制为 loopback，使用随机启动凭据和主进程授权校验。Preload 仅为受信任的本地页面暴露选定功能。Electron 官方安全指南建议启用 context isolation、进程沙箱、限制导航、定义 CSP，并避免为远程内容开启 Node integration。来源为 [Electron 安全指南](https://www.electronjs.org/docs/latest/tutorial/security) 和 [Context Isolation](https://www.electronjs.org/docs/latest/tutorial/context-isolation)。
- **事实** Harness 采用绑定版本的 Electron、Web Client、Host、插件和打包资源，并拥有独立的桌面开发、打包、更新、签名及恢复脚本。源码和 README 表明桌面启动会准备内置运行时，首次准备可能涉及下载锁定依赖。
- **推断** 桌面产品安全不是把权限弹窗放进 UI 就能解决。AgentDU 还必须隔离 renderer、约束 Shell 执行、限制凭据和模型数据流，并让用户能查阅已批准的实际命令。
- **事实** Harness 打包文档将 Linux 列为非正式 Desktop 发布目标。这只描述 Harness 当前打包策略，不能外推为 Electron 不支持 Linux。

### 与 Codex 桌面产品和公开协议对照

| 观察面 | Codex 的公开证据 | Harness 的源码证据 | AgentDU 的架构取舍 |
| --- | --- | --- | --- |
| 桌面产品边界 | OpenAI 产品介绍公开了项目、并行 Agent、worktree、Diff、Skills 和 Automations 等用户能力，没有公开 Codex Desktop 的进程图。 | Electron 壳加载本地 Web Client，主进程监督独立 Desktop Host。 | 用 Electron 做桌面入口，Python Runtime 保持 AgentDU 自有执行内核。 |
| UI 与 Runtime 通信 | Codex App Server 是公开的丰富客户端接口，采用 JSON-RPC 消息和 stdio JSONL。文档说明 App Server 供 Codex VS Code 扩展等客户端使用，没有证明 Codex Desktop 内部直接采用同一实现。 | Renderer 与本地 Host 使用 HTTP/WebSocket，Main 与 Host 通过带类型的私有进程 IPC 传递启动注入、认证和关闭消息。 | D0 选择本机 stdio JSONL，避免新增监听端口、HTTP Host 和本地认证票据。该协议需要由 AgentDU 自己定义并版本化。 |
| Agent 与任务轨迹 | 产品介绍描述多 Agent 并行和长任务监督。App Server 把 thread、turn、item、审批和事件建模为不同协议对象。 | Agent Loop 将 turn、step、模型消息、工具调用和结果写入 Session 事件。子 Agent 有独立 Session 与生命周期事件，Web Client 从投影读取任务目录和进度。 | 复用 AgentDU JSONL Session、任务状态和 Handoff 证据。UI 时间线只反映持久事件、工具结果和真实退出状态。 |
| 执行与权限 | App Server 支持客户端审批交互和终端进程控制。Codex Desktop 产品资料公开其沙箱与权限体验，具体内部执行边界仍未公开。 | 文件工具、Shell/PTY、插件、子 Agent 分属多个包。部分能力经过策略或审批服务，运行时仍要单独处理 OS 执行隔离。 | 保留 AgentDU 权限策略，同时新增桌面审批协议和经实测的 OS 级执行隔离。 |
| 任务恢复与后台 | 产品资料公开了 Automations 与长任务结果审阅，不能据此推断全部本地恢复实现。 | 有 Host 生命周期、会话事件、任务计划和更新协调。桌面默认未启用 scheduled tasks，提醒只对当前加载的 Session 运行。 | 把可恢复事件、未知副作用和后台运行状态明确呈现。不能把“任务已排程”展示成“模型已完成”。 |
| 扩展与本地化 | 产品资料公开 Skills 管理。其完整桌面扩展实现没有公开。 | Skill Provider、模型侧 `skill` 工具、插件包管理、Web UI 插槽和 `zh` 字典在源码中分别实现。 | 接入 AgentDU 已有 Skills、MCP 和插件入口，扩展进程另做权限与隔离设计。 |
| 发布复杂度 | Windows 版于 2026-03-04 开放。产品文章不足以核验其私有打包、更新和运行时装配细节。 | Electron/Web Client/Host 同版本协同，另有 Node、Python、pnpm runtime、Windows 签名和更新检查。 | 先以 Windows 为验收基线，D1 测启动、IPC 与安装包，D5 再做签名、更新、迁移和独立验收。 |

Codex 的产品优势可依据官方产品资料讨论，内部实现必须保留为未知。Codex App Server 的源码只能证明公开协议与该仓库实现的行为，不能证明 Codex Desktop 的全部产品能力由它提供。

### Harness Agent Runtime、工具与任务轨迹源码

研究对象仍是固定 SHA `4b399c39a6d1479cd84ca591b4d7c88e24d38a96`。以下源码路径均对应该上游版本。

| 子系统 | 可核验实现 | 技术结论和迁移边界 |
| --- | --- | --- |
| Agent Loop | [`ReactLoopAgent.turn` 与 `step`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/core/agent-loop/src/agent.ts#L323) | Loop 按 turn/step 驱动请求，流式读取模型结果，再把 tool-call 交给 ToolExecutor。开始、结束、Assistant 消息、工具结果、取消和结构化错误写入 Session。工具批次出错时会回收已完成的部分结果，避免它们从会话记录中消失。 |
| Session 与任务轨迹 | [`Session.append`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/core/session/src/index.ts#L557)、[Chat turn projection](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/client/ui-chat/src/client/conversation-nodes/assistant.ts#L308) | Session 事件以序号组织，UI 将 `turn/start`、`step/start` 等会话事实投影为可导航的聊天活动。产品遥测 `EventLogReporter` 明确与 Session-log 队列分离，模型正文和凭据不应作为普通分析摘要。 |
| 子 Agent 与上下文交接 | [`SubagentManager` 创建路径](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/subagent/subagent/src/manager.ts#L332)、[子 Agent 权限快照](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/subagent/subagent/src/child-agent.ts#L244)、[UI 子 Agent 投影](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/client/ui-subagent/src/client/index.ts#L56) | 子任务拥有独立 Session、描述信息、工作目录和生命周期。创建时可带受控的父 Session 事件种子，完成后通过结算消息回到父 Agent。父级路由设置会成为子任务初始值，任务创建时同步记录权限边界。该结构适合研究独立上下文、 lineage 和事件投影，具体 seed 规则不能简化成复制完整聊天记录。 |
| 文件读取与工作区范围 | [`WorkspaceFiles`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/api/workspace-files/src/index.ts#L1)、[路径包含检查](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/fs/fs-sandbox/src/containment.ts#L58) | `WorkspaceFiles` 提供有限大小的文本/字节预览、目录列表和搜索。源码注释明确，显式文件读取遵循底层 filesystem 的读取权限，绝对路径可在工作区外；目录列表、搜索和变更观察仍按工作区范围处理。AgentDU 应让 IPC API 的数据外发边界独立于“路径以项目目录开头”这一表面判断。 |
| Shell 与终端 | [`LocalPtySession`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/terminal/terminal-bash/src/session.ts#L237)、[`BrowserTerminal`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/api/terminal-controller/src/terminal.ts#L21) | 终端由持久 PTY Session、启动就绪检查、有界 scrollback、输入控制、尺寸同步和进程清理共同组成。浏览器侧 Terminal 订阅的是进程控制服务，单独显示一个终端组件不足以构成完整集成终端。 |
| Skills | [`tool-skill` 注册与执行](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/skill/tool-skill/src/index.ts#L84)、[`Skill` Provider registry](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/skill/skill/src/index.ts#L125) | Skill Provider 负责发现与加载。模型侧先看到名称和短描述，再通过 `skill` 工具按任务请求完整指令，加载按 Agent composition scope 解析。分离发现、目录和正文加载有助于控制上下文占用。 |
| 插件安装与管理 | [`PluginManager`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/boot/plugin-manager/src/index.ts#L325)、[管理器的失败与限制](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/boot/plugin-manager/README.md#L166) | Plugin Manager 管理 profile 包、版本兼容、安装/移除、操作日志、取消与进程树清理。安装或升级会运行包管理操作，并处理依赖脚本批准和部分回滚。README 仍列出安装中断后下载文件残留、移除不完整等限制。扩展生态显著增加供应链和维护面。 |
| 后台计划任务 | [`ScheduleRuntime`](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/schedule/schedule/src/runtime.ts#L67)、[Desktop 退出和提醒行为](https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/apps/desktop/README.md#L80) | Schedule Runtime 以 Host timer 投递持久化任务，任务送达后仍由目标 Session 和 Agent 执行。桌面文档说明计划功能默认关闭，卸载的 Session 不会在后台自动加载或恢复提醒，因此不应把它理解成任意关窗后仍持续执行的通用后台 Agent。 |

### Harness 的优势、复杂度与适用边界

- **源码事实** Desktop 复用完整 Web Client，并用独立 Host 暴露会话、文件、终端、插件和调度服务。启动凭据由主进程保管，Host 绑定 loopback，关闭与更新流程包含对活动任务的检查。
- **源码事实** Agent Loop 的持久 Session 事件同时支撑会话读取、子 Agent 目录和 UI 时间线。这个事实链能把“模型说完成”与工具结果、进程退出和任务结算区分开。
- **工程推断** 这套 Host/Web 结构有利于共享已有 Web Client 和插件服务，代价是 Desktop、Web Host、runtime payload、包管理和更新必须按版本协同。AgentDU 首版使用 Python Host 的 stdio IPC 可以减少本地 HTTP Host 与启动认证的组件数量，但要自行实现事件重放、Host 状态和 UI 查询服务。
- **已知边界** Desktop README 将 Linux 列为非正式目标。计划任务默认关闭且只对当前已加载 Session 生效。文件预览 API 的显式绝对路径读取可能超出工作区根目录，取决于 filesystem read 权限。插件安装失败可能保留下载文件，部分移除错误需要重试。
- **适用结论** 值得吸收的是明确的 Host 生命周期、窄化 Preload、Session 事件投影、子 Agent 独立会话、PTY 控制和 Skill 延迟加载。AgentDU 不应照搬 Harness 的完整 Web Host、插件包管理和 bundled Node/Python/PNPM 发布体系，因为现有 AgentDU Runtime 与目标首版边界不同。

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

- **事实** `docs/v0.6-status.zh-CN.md` 声明 AgentDU 当前没有 Codex 级桌面界面、实时协作时间线、模型配置 GUI 或会话查看器。
- **事实** 本地 `pyproject.toml` 只定义 Python 包和命令行入口，没有 Electron、Tauri 或 Qt 依赖。
- **事实** AgentDU 当前 [`provider.py` 的 `OpenAICompatibleModel`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/provider.py#L20) 实现 OpenAI 兼容 Chat Completions 形状。[`settings.py` 的 `ModelSettings.load`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/settings.py#L48) 读取并校验 TOML Profile，[`resolve_model_options`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/src/agentdu/settings.py#L166) 处理命令行、环境变量和 Profile 的模型选项优先级。真实多 Provider 能力、模型特有协议和跨提供商任务接力还没有真实凭据评测证据。
- **事实** [`docs/architecture.md`](https://github.com/hajimi1214/AgentDU/blob/2aa31341feea69ac1f3c3c062ddfba08082129e4/docs/architecture.md) 记录了当前 Runtime 的模块契约、会话与工具调用持久化顺序、协作边界和已知限制。该文档是 AgentDU V0.6 的架构基线，不代表桌面 Host、UI 或 OS 级执行隔离已经实现。
- **事实** `tools.py` 执行的 Shell 命令以当前用户权限运行，Git worktree 不是操作系统安全边界。审批能限制 AgentDU 工具入口，但不能阻止已批准命令访问工作区外文件。
- **事实** 协作任务状态目前绑定单进程。进程重启时活动任务会标记为 interrupted，不会自动重放不确定的模型操作。
- **推断** 桌面 UI 可以较高比例复用 AgentDU Runtime，但桌面安全、稳定 IPC、任务恢复策略和审批界面都需要新增工程工作。

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

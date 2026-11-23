# AgentDU Desktop 后续五阶段路线

路线基于 AgentDU D0 研究和 `main` 基线 `2aa31341feea69ac1f3c3c062ddfba08082129e4`。每一阶段都需要通过本阶段验收后再进入下一阶段。

## D1 桌面运行骨架与 Runtime 通信

### 目标

建立 Windows 本地 Electron 应用框架，保持 Python Runtime 主体不变，验证协议、进程启动和安全边界。

### 范围

- Electron + React/TypeScript 最小窗口和简体中文欢迎页。
- Runtime Host 子进程、JSONL 握手、健康检查、请求/响应、事件流和结构化错误。
- 无凭据启动；Provider 配置界面最小骨架。
- 本地安装目录、日志、单实例、关闭、崩溃错误和恢复提示。
- OS Shell 隔离技术 spike 与 Windows 打包验证。

### 验收标准

- Windows 用户打开可运行应用，不需要手动启动 CLI 或全局安装 Python。
- 界面能显示 Runtime 版本、Provider 配置缺失状态和 Host 进程状态。
- 本地测试可完成一次受控假 Provider 流式 turn 和一次 UI 取消，不把测试伪造能力作为真实模型性能证据。
- Runtime 子进程异常退出、stdout 协议损坏、安装路径含空格和权限不足均可见地失败。
- Renderer 无文件系统和任意命令权限；Shell sandbox spike 有文件和网络逃逸测试记录。
- 不实现正式协作策略或未验证的后台任务保证。

## D2 单模型桌面编程体验

### 目标

让用户能够从项目主页进入真实代码工作区，执行日常单模型开发任务并审查结果。

### 范围

- 项目导航、最近工作区、会话列表和会话恢复。
- 模型档案、凭据安全存储、Provider 状态和用户权限配置。
- 对话时间线、工具调用、终端、文件视图、Git 状态和 Diff 审查。
- 桌面审批卡片、取消和工具输出上限。
- 使用 AgentRuntime、ToolRegistry、SessionStore、EventBus、ContextManager。

### 验收标准

- 对真实项目完成跨文件修改、Shell 测试、错误修复和 Git Diff 审查。
- 所有 UI 状态可追溯到持久会话事件、工具结果或命令退出码。
- Provider 连续错误、超时、流式截断、上下文超限和用户取消均有集成测试。
- 写入、Shell 和 Git 变更未授权时被拒绝，授权后日志包含实际命令与工作目录。
- 运行至少一个真实模型 Provider 完成代表性任务。保存配置、请求行为和测试结果。

## D3 多 Agent 任务视图与可靠交接

### 目标

在已有 V0.4/V0.5 Runtime 能力上提供清晰可审查的桌面任务协作与整合流程。

### 范围

- 主 Agent 与 Worker 任务卡、模型档案、依赖、独立会话和 worktree。
- Handoff 事件、接受确认、owner/version 校验、基线与结果 SHA、Diff digest、验收记录。
- 任务取消、过期基线、冲突、阻塞和中断恢复 UI。
- 让主 Agent 在 Worker 运行时继续处理自身任务。

### 验收标准

- 两个不同真实 Provider 完成同一项目的模型接力任务，保留逐步审计证据。
- 并行 Worker 在独立 worktree 工作，失败或冲突不改变用户工作区。
- UI 能识别不完整 handoff、版本不匹配、过期基线、检查失败和结果变更。
- 单模型与多模型配对测试具有同一干净初始 tree，报告质量、时间、调用次数和可用费用数据。

## D4 安全、恢复、扩展和后台工作

### 目标

提升长任务、故障恢复、OS 安全边界和 MCP/Skills/插件的可操作性。

### 范围

- 跨进程任务状态协调、应用崩溃恢复、后台通知与退出策略。
- OS 沙箱、进程树/资源限制、网络控制、凭据注入和敏感数据流策略。
- MCP Server、Skills、插件的 UI 生命周期、权限清单与隔离执行。
- 更新流程、签名、版本迁移、日志轮换、诊断导出和可观测面板。
- 针对故障恢复和并发冲突的 fault injection。

### 验收标准

- 强制终止、系统注销、Host 崩溃、Provider 挂起、MCP 无响应和磁盘满等故障测试有确定结果。
- 不会自动重放不确定的 Shell 或文件副作用。
- 目标平台的文件越权、网络越权、凭据读取和子进程逃逸测试通过。
- 跨 Provider 数据包、Secrets redaction 和会话日志脱敏有自动化回归。
- 后台任务退出、通知、恢复和任务所有权能够复现并审计。

## D5 产品化、跨平台发布与独立验收

### 目标

发布可安装、可升级、可恢复并具备正式支持边界的 AgentDU Desktop。

### 范围

- Windows 正式安装包、签名、升级和卸载；根据 D0 验证结果扩展 macOS/Linux。
- 完整中文和英文界面、可访问性、性能预算和用户操作文档。
- Provider/模型矩阵、单模型和多模型质量/成本/延迟/恢复评测。
- 依赖许可证、第三方 notices、安全策略、数据收集和隐私说明。
- 独立产品、安全和可靠性验收。

### 验收标准

- 在干净 Windows 测试机完成安装、首次配置、代码开发、审查、更新和卸载。
- 无真实凭据时不产生外部请求，所有密钥和对话保持本地或按用户明确选择发送给 Provider。
- 两个真实不同 Provider 完成可重复任务接力，保留日志和匹配基线。
- 发布报告如实给出任务成功率、失败分类、恢复率、执行时间、模型调用和费用估算，并说明统计范围。
- 独立验收通过后才将 AgentDU Desktop 标记为可日常使用。未过项目不得通过演示截图替代。

## 阶段依赖

```mermaid
flowchart LR
    D0[D0 研究与定案] --> D1[D1 桌面骨架 / Runtime IPC]
    D1 --> D2[D2 单模型编程工作台]
    D2 --> D3[D3 多 Agent 与可靠交接]
    D3 --> D4[D4 安全、恢复与扩展]
    D4 --> D5[D5 产品发布与独立验收]
```

## D0 结束条件

D0 交付中文产品需求、桌面架构、页面结构、技术选型、研究证据和五阶段路线。D0 不实现桌面 UI，不修改 Agent Runtime，不直接进入 D1。进入 D1 需要下一阶段明确授权，并优先完成 Electron/Python IPC 与 Windows Shell 隔离技术 spike。

## D0 最终验收问答

| 验收问题 | D0 决策 | 详细依据 |
| --- | --- | --- |
| AgentDU 应该做成什么样的桌面产品？ | 本地优先、以项目和持久任务会话为核心的 AI 编程工作台，涵盖模型、Agent 活动、代码审查、审批和恢复 | [产品需求](product-requirements.md)、[页面结构](screen-map.md) |
| 如何最大程度复用当前 AgentDU 核心？ | 保留 `AgentRuntime`、Provider 契约、SessionStore、协作协调器、Handoff 账本、Git worktree 和现有 MCP/Skills/插件入口，在其上增加 Desktop Host 与 UI 应用服务 | [源码研究](reference-study.md)、[总体架构](architecture.md) |
| 应该采用什么桌面技术架构？ | Electron 主进程和隔离 Renderer + 独立 Python Runtime Host，通过 stdio 版本化 JSONL 双向通信 | [总体架构](architecture.md)、[技术选型](technology-decisions.md) |
| 桌面 UI 应当包含哪些页面？ | 项目主页、工作区、会话与任务时间线、Agent 协作、Diff 审查、终端、权限、Provider、MCP/Skills/插件、通知和设置 | [页面结构](screen-map.md) |
| 多模型协作如何在界面中呈现？ | 主/子 Agent 任务卡、私有会话、模型档案、依赖、基线与结果 SHA、验证证据、Diff 和显式集成状态 | [产品需求](product-requirements.md)、[页面结构](screen-map.md) |
| 桌面进程与 Python Runtime 如何可靠通信？ | 单实例桌面主进程监督 Python Host，使用握手、schema、请求 ID、持久事件序号、健康状态、去重请求和明确关闭协议 | [总体架构](architecture.md) |
| 如何保障文件、Shell、凭据和模型数据安全？ | Renderer 窄化能力、OS 级 Shell 隔离、系统密钥存储、最小环境注入、数据传输策略和本地日志脱敏；平台隔离能力必须在 D1 实测 | [总体架构](architecture.md)、[产品需求](product-requirements.md) |
| 与 Codex、DeepSeek Harness 相比，AgentDU 的独特优势是什么？ | 已拥有本地可读源码的多模型 handoff、任务 owner/version 校验、Git snapshot 验证和验收后集成路径，可围绕可证明的工程状态设计协作监督界面 | [源码研究](reference-study.md) |
| 后续五个阶段分别应达到什么产品状态？ | D1 桌面 IPC 骨架，D2 单模型工作台，D3 多 Agent 和交接，D4 安全/恢复/扩展，D5 产品发布与独立验收 | [D1–D5 路线](roadmap.md#d1-桌面运行骨架与-runtime-通信) |

### D0 结论

- **研究和架构设计已达到进入 D1 技术验证的条件**。D1 应先验证 Windows 上 Electron 与 Python Runtime 的打包、stdio IPC 和 Shell 隔离，期间保留现有 Runtime 核心。
- **桌面产品尚未实现或运行**。本地项目当前提供中文 CLI，不包含 AgentDU Desktop 窗口、Renderer、安装包或桌面 Host。开发并运行桌面窗口属于 D1 工作。
- **安全与平台结论仍有明确待验证项**，尤其 Windows OS 隔离、进程树资源限制、密钥存储、真实 Provider 接入和跨 Provider 任务接力。
- 本阶段只交付 D0 中文研究资料并停止。尚不能宣称 AgentDU 已经具备日常使用的桌面产品或完成 D1。

# 桌面技术选型与决策

## 结论

| 层 | D0 决策 |
| --- | --- |
| 桌面框架 | Electron |
| UI | React + TypeScript，Renderer 运行于沙箱化 Chromium 页面 |
| Agent Runtime | 继续使用现有 Python Runtime，作为独立子进程打包 |
| 桌面与 Runtime 通信 | 本地 stdio JSONL 双向协议，协议带版本与 schema |
| 原生操作 | 由 Electron 主进程和受限 preload 管理，Renderer 不直接访问系统 API |
| 安全执行 | 由受限执行服务管理 Shell、MCP 与插件，平台 OS sandbox 是 D1 验证项 |
| 凭据 | 使用操作系统密钥存储，Runtime 按需获取单次调用需要的凭据 |

Electron 方案为 D0 的明确技术建议，可在 D1 命令隔离与 Windows 打包 spike 中以验收标准推翻。进入 D1 时不应再同时维护三个 UI 原型。

## 选型标准

1. 对 React/TypeScript 多面板开发、代码 Diff、终端和长任务时间线的支持。
2. 与已有 Python Runtime 组合时能清晰监督子进程和交互式流。
3. Windows 安装、签名、更新、系统通知、文件选择器与终端集成能力。
4. Renderer 与本地操作之间能建立可审计的安全边界。
5. 安装体积、启动时间、自动更新和跨平台维护成本。
6. 许可证、依赖、未来发布和安全更新负担。

## 备选对比

| 方案 | 已确认能力 | AgentDU 适配度 | 主要代价和风险 |
| --- | --- | --- | --- |
| Electron | Chromium/Node 桌面壳、成熟 Web UI 生态、IPC 和原生桌面打包生态。Harness Desktop 是一个公开的 Electron + Web + 本地 Host 参考实现。官方安全指南明确要求隔离 renderer 并限制高权限 API。 | 高。适合实现工作台型多面板 UI，可独立拉起并监督 Python Runtime。 | 包体和内存占用较大，需持续跟进 Electron/Chromium 安全更新；Renderer 失陷可能影响本机，必须限制桥接能力。 |
| Tauri 2 | 官方支持将 Python CLI 或服务器打包为 sidecar，并可通过权限 capability 限定可执行文件和参数。来源为 [Tauri sidecar 文档](https://v2.tauri.app/develop/sidecar/)。 | 中。可作为轻量桌面壳和 Python sidecar 容器。 | Rust host、WebView 平台差异和 Tauri capability 配置增加新技术面；需要验证 Windows WebView2、终端和窗口多视图需求。 |
| Qt / PySide6 | 官方 Python 绑定，Qt Widgets/Qt Quick，可直接使用 Python API。PySide 通过多种工具部署 Windows、macOS 和 Linux。来源为 [Qt for Python](https://doc.qt.io/qtforpython-6/) 与 [部署文档](https://doc.qt.io/qtforpython-6/deployment/index.html)。 | 中。Python 运行时集成自然，可减少应用进程间 API 设计。 | 复杂 Web 风格界面和 JS/React 复用度较低；桌面 Web 技能生态较小；部署文档注明 PySide6 许可覆盖 LGPLv3/GPLv3 或商业许可，需审核发版义务。 |

### 为什么当前选 Electron

- **事实**：产品目标要求高密度的代码工作台、可调整面板、聊天活动时间线、Diff、集成终端和设置页面。
- **事实**：已有 Runtime 是 Python 包，AgentDU 源码没有 GUI 依赖和桌面工程。
- **事实**：Electron 官方文档说明隔离 renderer 和 preload bridge 的安全机制，Harness Desktop 有实际主进程/Host/Preload 分层源码。
- **推断**：Electron + React/TypeScript 能用单一 UI 技术栈构建多面板开发界面，并让 Python Runtime 保持独立。它需要更严格的本机权限约束与更大的安装包。
- **待验证**：在目标 Windows 设备上，Runtime 打包、Shell 沙箱、Windows 文件通知、长时间运行和升级后的数据迁移都必须通过实际测试。

## 进程通信选择

### 采用 stdio JSONL

- 不打开 TCP 端口，避免本地服务被其他进程通过端口扫描访问。
- Electron 主进程直接创建 Python 子进程，并独占 stdin/stdout/stderr。
- stdout 仅承载 JSON 行，stderr 仅承载诊断日志。
- 每个消息有请求 ID、协议版本、schema 和确定的关联任务 ID。
- Runtime 在事件写入持久会话日志之后发送对应 UI event，使 UI 可在断线后重放。

### 不把 WebSocket 作为首版默认

Codex App Server 的公开文档同时展示 stdio 和 WebSocket 等传输，也说明非 loopback WebSocket 要配置认证。AgentDU 的 UI 与 Python Runtime 位于同一设备，因此首版 stdio 简化监听器和认证边界。未来若需要远程 UI 或多个本机客户端，再评估经过认证的本地 socket/WebSocket。

### 不直接包装现有 CLI 命令

CLI 面向终端输入和输出，桌面需要稳定的 session read、turn control、审批请求、任务查询、事件订阅和恢复方法。将 CLI 输出解析成 UI API 会形成脆弱耦合。建议 Runtime Host 复用相同 Runtime service 对象，而不是把 `python -m agentdu` 的文本输出当协议。

## 凭据、更新和日志

- Profile 文件保存 endpoint、模型名、超时和密钥引用。凭据放到平台密钥存储。
- Python Host 通过受控 IPC 请求主进程提供指定 Profile 的密钥，或由主进程创建有 allowlist 的进程环境。具体实现需 D1 spike 验证。
- Provider 请求日志、MCP stderr 和崩溃报告必须先脱敏再持久化。
- Desktop、Runtime 和 IPC 协议版本联合发布。协议不兼容时应用不得静默降级执行。
- 自动更新必须先询问当前 Agent turn、外部命令、后台 Worker 和审批是否正在执行，再完成关闭或延期。

## 许可证和依赖审查

- Electron、Tauri、Qt 及相关打包依赖应在 D1 建立依赖清单和许可证扫描。
- OpenAI Codex CLI 仓库的 Apache-2.0 许可证不自动适用于 Codex Desktop 未公开代码，也不授予 AgentDU 复制 UI 的权利。
- DeepSeek Harness 项目的 MIT 许可证不替代其各个依赖和单独标注文件的许可审查。
- PySide6 文档声明 Qt for Python 可在 LGPLv3/GPLv3 或商业许可下使用。选择 Qt 时要对动态链接、发布材料和修改要求进行专门许可评审。
- 本阶段只引用公开架构与产品行为，不复制参考项目代码。

## D1 技术验证门槛

只有以下 spike 全部通过，Electron 选型才转为已验证决定：

1. Windows 开发环境启动 UI 与 Python Host，并通过 stdio JSONL 完成握手和一次模型流式任务。
2. 无凭据启动、Provider 配置失败、Host 崩溃和协议不兼容时应用仍可恢复。
3. 一次 Shell 权限请求可从 Runtime 发往 UI，用户拒绝后无命令执行副作用。
4. 打包后的 Python Runtime 无需系统 Python 可启动，安装包大小与冷启动时间可接受。
5. Renderer 无法读写工作区或启动任意命令，Shell sandbox 的平台实现通过文件、网络和进程逃逸测试。
6. 应用更新与旧会话、任务状态和 Provider Profile 兼容。

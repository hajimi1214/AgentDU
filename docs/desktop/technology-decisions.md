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
| Electron | Electron 使用 Chromium 多进程模型，主进程运行于 Node.js 环境。Renderer 可通过隔离的 Preload API 请求主进程能力。官方文档提供 Windows 和 macOS 自动更新机制。 | 高。React/TypeScript 多面板 UI、桌面生命周期和 Python 子进程可分别落在 Renderer、主进程与 Runtime Host。 | 应用分发需要包含 Electron、Chromium 和 Node.js 组件。需维护安全更新，并限制 Renderer 到主进程的桥接能力。 |
| Tauri 2 | 支持把外部可执行文件作为 sidecar，并用 capability 约束应用可调用的程序和参数。Windows 使用系统 WebView2，也可以由安装器配置 WebView2 的部署方式。 | 中。可采用 React/TypeScript 和独立 Python Host，但 Python 必须先制作为可分发的 sidecar，Rust 主进程负责监督。 | Rust、WebView2 和 capability 配置增加技术面。Tauri 的 sidecar 调用权限本身不限制子进程对文件、网络和其他进程的操作。 |
| Qt / PySide6 | PySide6 提供 Python API。`QProcess` 可启动外部程序、读写标准输入输出并读取退出状态。`pyside6-deploy` 支持 Windows、macOS 和 Linux 打包。 | 中。Python 集成路径直接，适合以 Qt Widgets 或 Qt Quick 建立独立桌面 UI。 | UI 需采用 Qt 技术栈。`QProcess` 和 Qt UI 本身不隔离 Agent Shell。发行时需审查 LGPLv3/GPLv3 或商业许可条件。 |

### 按研发约束逐项比较

| 维度 | Electron | Tauri 2 | Qt / PySide6 | D0 判断 |
| --- | --- | --- | --- | --- |
| Windows 兼容与分发 | 有独立 Chromium/Node 运行环境，支持 Windows 应用分发与更新。 | 使用 Windows WebView2。安装器可下载或内置 WebView2，离线包会显著增大。 | 官方部署工具覆盖 Windows。需验证 Qt/Python 依赖、签名和升级路径。 | 三种方案都能面向 Windows 发布，离线安装、清洁机器和更新必须在 D1 验证。 |
| 界面与研发效率 | React/TypeScript 与代码工作台、多面板和 Web 组件的匹配度高。此项属于工程判断。 | 可用 Web 前端，但还需维护 Rust 主进程和 WebView 平台差异。 | Python API 与现有 Runtime 同语言，界面组件体系需要采用 Qt。 | Electron 与拟定的 React/TypeScript 界面分工最一致。 |
| Python Runtime 集成 | 由主进程监督独立 Python Host，通过 stdio 协议通信。 | 将 Python Host 制作为 sidecar，由 Rust 进程启动和监督。 | 可在 Python 应用中组合 Runtime，也可通过 `QProcess` 分进程运行。 | 三者都能接现有 Runtime。独立 Host 和协议边界仍需 AgentDU 自己实现。 |
| 终端与进程管理 | 主进程能管理独立进程，完整终端仍需实现 PTY、输入输出、尺寸变化和取消。 | Shell 插件可按能力授权启动配置过的 sidecar，交互式 PTY 体验仍需实测。 | `QProcess` 提供进程与管道 I/O，完整终端仍需单独实现。 | 框架的子进程 API 不等于可用的集成终端。D1 应用真实交互任务验收。 |
| 权限与安全边界 | Renderer sandbox、context isolation、CSP 和窄化 Preload 可降低 UI 权限，不能替代 Shell 的 OS 级隔离。 | capability 能收窄前端可调用的 API，不能替代 sidecar 的 OS 级隔离。 | UI 与 `QProcess` 默认处于应用用户权限下，隔离需要单独的 OS 方案。 | 三种框架都不自动解决 Agent 命令的系统级限制。 |
| 安装体积与资源占用 | Electron 分发包含 Electron、Chromium 和 Node.js。 | 基础应用可复用系统 WebView，安装器策略和 Python sidecar 会改变实际体积。 | 需要分发 Python、Qt 与应用依赖。 | D0 没有 AgentDU 三套构建的实测体积、启动时延或内存数据。D1 必须记录这些指标。 |
| 自动更新 | Electron `autoUpdater` 支持 Windows 和 macOS，不同打包格式采用不同更新通路。 | 官方 updater 插件支持签名更新包和 Windows 安装器。 | `pyside6-deploy` 负责打包。D0 参考的部署文档未确定适合 AgentDU 的更新通路。 | 更新、签名、数据迁移和失败回滚都要通过 D1/D5 的实际发布流程验证。 |
| 许可证 | Electron、Chromium、Node.js 与 npm 依赖需要一并盘点。 | Tauri、Rust crates、前端依赖和 Python sidecar 需要一并盘点。 | Community Edition 按 LGPLv3/GPLv3 规则提供，也有商业版本。 | D1 建立锁定依赖清单与许可证审查结果，D0 不复制参考实现。 |

上述对比使用官方框架文档描述可用能力。性能、包体、开发效率和终端交互结论中没有 AgentDU 实测数据的部分均属于待验证工程判断，不作为性能结论。

### 官方实现依据

- Electron 主进程、Renderer、Preload 与 Utility Process 见 [Process Model](https://www.electronjs.org/docs/latest/tutorial/process-model)。Renderer 隔离建议见 [Security](https://www.electronjs.org/docs/latest/tutorial/security)。Windows/macOS 更新边界见 [autoUpdater](https://www.electronjs.org/docs/latest/api/auto-updater/) 和 [Updating Applications](https://www.electronjs.org/docs/latest/tutorial/updates)。
- Tauri 外部可执行文件与参数权限见 [Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)。Windows WebView2 部署选项见 [WebView Versions](https://v2.tauri.app/reference/webview-versions/) 和 [Windows Installer](https://v2.tauri.app/distribute/windows-installer/)。应用体积说明见 [App Size](https://v2.tauri.app/concept/size/)，更新包签名见 [Updater](https://v2.tauri.app/plugin/updater/)。
- PySide 子进程、管道和退出状态见 [QProcess](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)。跨平台打包见 [Deployment](https://doc.qt.io/qtforpython-6/deployment/index.html) 和 [pyside6-deploy](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)。许可方式见 [Commercial Use](https://doc.qt.io/qtforpython-6/commercial/index.html)。

### 为什么当前选 Electron

- **事实** 产品目标要求高密度的代码工作台、可调整面板、聊天活动时间线、Diff、集成终端和设置页面。
- **事实** 已有 Runtime 是 Python 包，AgentDU 源码没有 GUI 依赖和桌面工程。
- **事实** Electron 官方文档说明隔离 renderer 和 preload bridge 的安全机制，Harness Desktop 有实际主进程/Host/Preload 分层源码。
- **推断** Electron + React/TypeScript 能用单一 UI 技术栈构建多面板开发界面，并让 Python Runtime 保持独立。它需要更严格的本机权限约束与更大的安装包。
- **待验证** 在目标 Windows 设备上，Runtime 打包、Shell 沙箱、Windows 文件通知、长时间运行和升级后的数据迁移都必须通过实际测试。

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

Electron 选型只有在以下 spike 全部通过后，才视为已验证决定。

1. Windows 开发环境启动 UI 与 Python Host，并通过 stdio JSONL 完成握手和一次模型流式任务。
2. 无凭据启动、Provider 配置失败、Host 崩溃和协议不兼容时应用仍可恢复。
3. 一次 Shell 权限请求可从 Runtime 发往 UI，用户拒绝后无命令执行副作用。
4. 打包后的 Python Runtime 无需系统 Python 可启动，安装包大小与冷启动时间可接受。
5. Renderer 无法读写工作区或启动任意命令，Shell sandbox 的平台实现通过文件、网络和进程逃逸测试。
6. 应用更新与旧会话、任务状态和 Provider Profile 兼容。

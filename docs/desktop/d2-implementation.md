# AgentDU Desktop D2 实现与验收记录

日期：2026-11-27（Asia/Shanghai）

## 状态

**D2 尚未通过独立验收。** 本地工作区已实现单模型工作台主要路径，自动化与 Windows AppContainer 集成测试通过。修复了真实冷启动时的 Runtime 就绪竞态，并生成、安装和启动了 D2 Windows 包。针对当前配置增加了 OpenAI-compatible Responses API 支持，覆盖文本输入、流式事件、函数调用和函数结果续传，同时保留 Chat Completions 默认行为。Renderer UI 取消已在此前 NSIS 包端到端验证，生命周期持久化修复又在当前源码包的 unpacked 分发副本中复测。AgentDU 当前没有真实 Provider 档案或凭据。新源码包副本已验证最近项目与空白会话恢复。真实 Provider 任务、已有模型对话恢复仍未完成。未将受控测试任务或模拟模型调用作为真实 Provider 证据。

本轮修复从 `c61933a667b6e044cc9cf2e50a732288824eea72` 开始。D2 仍未通过独立验收。

## 功能实现

- 左侧项目列表支持系统目录选择、最近项目恢复和项目切换。会话按工作区持久化，可新建、列出、恢复，并呈现消息、流式增量和 Runtime 事件。
- 主区展示项目 Git 分支与状态、Agent 活动时间线、模型输出、取消操作及异常恢复提示。
- 右侧面板提供项目文件浏览、文本查看和受审批写入、Git Diff、隔离终端命令记录。
- Provider 档案支持 Endpoint、Model ID、API Key 和 Chat Completions / Responses 协议选择。Windows `safeStorage` 加密凭据，Runtime 启动时将解密值注入子进程环境，不通过命令行传密钥。连通性测试只验证 Provider 的 `/models` 响应。
- 审批事件呈现来源、命令、工作目录、目标文件、范围和网络需求。审批请求、审批结论及实际执行结果分别写入会话事件记录。
- Runtime Host 对桌面编辑写入、Agent turn 和终端命令实施工作区互斥。Windows 终端命令通过 AppContainer 运行，限制网络、进程、内存、输出及运行时长。
- Renderer 通过受限 Preload 调用显式 IPC API。任务接受、取消请求及终态持久化进会话 JSONL。窗口最小尺寸、响应式窄屏布局和侧栏横向调整由 Electron 设置和 CSS 实现。

## 自动化和隔离测试

- `cd desktop; pnpm run build` 和 `pnpm test`：通过，包含 Renderer/Electron TypeScript 检查、Vite 生产构建及最新 17 项桌面测试。新增启动竞态回归测试覆盖 `starting → ready`、初始已就绪、Runtime 错误和卸载后忽略迟到响应。
- `python -m unittest discover -s tests -v`：通过，最新完整复测为 92 项测试。新增 Responses API 非流式工具往返、流式文本增量/函数调用，以及 content filter 不完整响应不得报告成功的回归测试。
- `cd desktop; pnpm run verify:appcontainer`：通过。探针报告 AppContainer token、项目目录拒绝读写、loopback 网络阻断、子进程仍处于 AppContainer 且项目外写入被拒绝。
- Windows 终端 Host 集成测试真实启动经审批的 AppContainer 命令，并检查退出码、输出及会话记录。它验证隔离和协议路径，不作为真实模型任务证据。
- `cd desktop; pnpm run package:win`：通过。脚本使用本机已安装的 Electron 分发目录，避免依赖损坏的 Electron zip 下载，并将产物输出到 `desktop/release/d2-final`。
- 最新 NSIS 安装器为 `desktop/release/d2-final/AgentDU-Setup-0.1.0-x64.exe`，SHA-256 为 `5C4934D29E8687FB5CEB5BD39AAA449C1C2294545721A75D4E0F23D778C2CD8B`。最新安装器与 unpacked 包均未配置代码签名证书，安装器不含签名。以归档 API 解包确认 Renderer bundle 包含 Runtime 状态订阅、最近项目加载逻辑和启动错误反馈。
- 本轮打包后对 bundled Python 3.14.10 Host 进行 JSONL 探针，`host.initialize`、`host.health` 和 `host.shutdown` 均返回有效响应，Host 以退出码 0 结束。打包内 `provider.py` 与源码 SHA-256 一致。
- 已将 NSIS 包安装到 `desktop/release/d2-final/installed app with spaces`。从该路径的 embedded Python 启动 Host 时将 `PATH` 限制为 `%WINDIR%\System32`，Host 仍成功初始化和关闭，退出码为 0；Runtime 报告 Python 3.14.10、协议版本 1，并正确解析 `wire_api = "responses"`。Provider 凭据状态为缺失，验证过程未执行外部模型请求。
- 已从含空格的安装路径启动新版 `AgentDU.exe`，进程树确认 Electron Main 与 bundled Python Runtime 正在运行。通过同一安装目录下的 Python Host 子进程启动受控任务并发送取消请求，收到 `task.cancelled`，未收到 `task.completed`，`host.shutdown` 返回 `stopped`，退出码为 0。此验证覆盖打包 Runtime 的取消语义，不代表 UI 按钮操作或真实 Provider 任务验收。
- 第一次包运行发现 Renderer 在 Runtime 仍为 `starting` 时调用 `listWorkspaces`，产生“Python Runtime 当前不可用”提示。现已调整为先订阅状态，仅在 Runtime `ready` 后加载最近项目，并使用幂等标记避免重复加载。2026-11-25 从安装版完成异常退出恢复验证，窗口显示恢复提示和 Runtime 在线状态，工作区为空。

## 2026-11-26 本轮进度

- Vite 开发态 CSP 允许内联样式及本机动态 HMR 端口，解决开发版样式未加载。生产构建继续采用严格 CSP。
- 在 Electron 开发版中观察到本地受控 Runtime 自检的流式文本及事件。此路径不调用外部 Provider，不能作为真实模型任务证据。
- Renderer 任务状态 reducer 覆盖任务接受、启动响应迟到、无关终态事件和 Runtime 丢失。取消请求失败时界面保留运行状态，允许重试。
- Host 的 `workspace.inspect` 在线程池执行。对照复测确认嵌套仓库集成测试在 Git 子进程继承 Host 输入管道时超时，使用 `stdin=DEVNULL` 后通过。非 Git 目录跳过 Git 调用，Git 仓库状态限定到当前选中目录。集成测试确认选中目录中的已跟踪与未跟踪变更可返回，工作区外文件不进入状态结果，Git 超时时文件列表仍可返回。随后在最新安装版中已恢复 `C:\AgentDU` 并看到 Git 状态，安装版完整会话恢复仍待验证。
- 本轮验证结果为 Python 92 项测试通过、Desktop 17 项测试通过、TypeScript 检查和生产构建通过。Host 工作区检查专项集成测试通过。并行验证时一次出现本地 HTTP 流测试连接重置，单项及 Python 全量串行复跑通过。

## 2026-11-26 进度提交前复测

- 为增加从 Renderer 点击取消的操作窗口，将受控桌面自检任务的事件间隔从 1.5 秒调整为 3 秒。此设置仅用于 `controlled-test` 自检路径。
- `python -m unittest discover -s tests -p test_desktop_host.py -v`：PASS，19 项测试，包含 Host 层真实任务取消测试。
- 最新源码已构建并安装到 `desktop/release/d2-cancel-verify-2026-11-26/installed app with spaces`。从该安装版启动后，已恢复 `C:\AgentDU` 项目并显示 Git 分支状态 `main...origin/main [ahead 1]`。
- 新安装版的 Renderer 取消按钮尚未重新完成端到端验证。此前一次按钮操作发生在受控任务结束后，不能作为取消成功证据。D2 继续保持 **NOT VERIFIED**。
- 测试结束后已正常关闭安装版应用，并恢复原有最近项目记录。恢复文件与测试前备份的 SHA-256 一致。


## 2026-11-27 安装版与 Runtime 复测

- 在 `desktop/release/d2-ui-cancel-manual-2026-11-26-60s` 构建临时 Windows 包，并通过 NSIS 交互安装到 `installed app with spaces`。一次静默安装调用未等待安装器完成，过早启动时 Runtime 退出码为 1，stderr 显示 `SyntaxError: source code string cannot contain null bytes`。本轮没有将这次启动作为通过证据。随后从安装向导完成重装，安装版成功启动，Runtime 显示在线。
- 安装版从用户数据恢复了 `C:\AgentDU` 和 `C:\Users\ASUS\Downloads\02、模块烧录软件` 两个最近项目。界面显示 `C:\AgentDU`、Git `main...origin/main` 和本地修改状态。截图已保存为 [安装版项目恢复与 Runtime 在线](evidence/d2-installed-project-runtime-online-2026-11-27.png)。截图裁掉了屏幕底部的非应用字幕。应用启动更新了 `recent-workspaces.json` 中 AgentDU 的 `openedAt` 时间，两个最近项目条目均保留。
- 截图所用安装包的受控自检每段延迟为 60 秒，便于人工操作。当前源码已收敛为 6 秒，并将取消回归测试等待设为 8 秒。源码验证结果为 `python -m unittest discover -s tests`：PASS，92 项；Host 专项 19 项：PASS；Desktop 测试 17 项：PASS；`pnpm run build`：PASS。
- 后续独立 UI 取消复测前，安装版 Runtime 自检能启动并流式完成，但停止按钮尚未取得取消成功证据。当时 Host 层取消测试通过不能替代 UI 验收。该项已在下文完成复测。D2 继续保持 **NOT VERIFIED**。

## 2026-11-27 任务生命周期持久化修复与新源码包复测

- Runtime Host 现将 `task.accepted`、`task.cancel_requested`、`task.cancelled`、`task.failed`、`task.completed` 写入对应会话 JSONL。取消请求事件同时包含 taskId、sessionId 和 source。完成事件只持久化模型步数与调用数，完整响应继续通过实时 IPC 返回。
- `python -m unittest discover -s tests -p test_desktop_host.py -v`：PASS，19 项。受控取消回归测试确认会话事件包含 accepted、cancel_requested、cancelled，且没有 completed。另一受控任务验证 completed 只记录模型步数和调用数，不重复持久化完整响应。
- 全量 Python 测试：PASS，92 项。Desktop 测试：PASS，17 项。`pnpm run build`：PASS。Windows Runtime 准备和 NSIS 包构建通过。NSIS 包 SHA-256 为 `71337E8ED4C498C44ED549AAA72700D1D39734E914620E3BF6D1359AF91C334B`。
- 从新包的 `win-unpacked` 副本在 `desktop/release/d2-lifecycle-2026-11-27/installed app with spaces` 启动，Runtime 在线。Renderer UI 取消成功，临时 Runtime 会话的事件顺序为 `session_created`、`task.accepted`、消息、`run_started`、`model_request`、`task.cancel_requested`、`task.cancelled`，没有 `task.completed`。截图：[新源码包 UI 取消与事件](evidence/d2-runtime-lifecycle-persist-2026-11-27.jpg)。本轮新 NSIS 安装器没有完成安装向导，因此安装器安装验收仍待完成。

## 2026-11-27 Provider 配置表单复核

- 在新源码包中打开 Provider 设置，目视确认档案名、Endpoint、Model ID、协议、API Key、默认档案和连通性按钮均可见，协议选项包含 Chat Completions 与 Responses API。选中 Responses API 后未保存。截图：[Responses API Provider 表单](evidence/d2-provider-responses-profile-form-2026-11-27.jpg)。
- AgentDU 隔离测试 profile 与正常用户 profile 的 Provider 档案列表均为空。本轮没有输入、读取或复制任何 API Key。给定 Endpoint 的未认证 `/models` 请求返回 HTTP 401。

## 2026-11-27 新源码包项目与空白会话恢复

- 使用 `desktop/release/d2-lifecycle-2026-11-27/installed app with spaces/AgentDU.exe` 和隔离的 `%TEMP%\AgentDU-D2-Lifecycle-2026-11-27` 用户数据目录，首次启动后从界面打开 `%TEMP%\AgentDU-D2-Session-Recovery-2026-11-27\sample project`。Renderer 显示项目文件 `README.md`、`src/main.py`，Runtime 在线，并创建 `新会话`，活动流出现 `session_created`。
- 关闭实例后，Host 日志记录 `Python Runtime Host stopped`。使用相同可执行文件和 `--user-data-dir` 重启后，界面自动恢复 `sample project`、`新会话` 和持久事件 `session_created`。
- 磁盘证据包括隔离 profile 的 `runtime/recent-workspaces.json` 中的项目路径，以及测试项目 `.agentdu/sessions/<session-id>.jsonl` 中唯一的 `session_created` 事件。重启后 UI 展示同一项目、会话和事件。
- 该结果验证隔离 profile 下的最近项目与空白会话事件恢复。该 profile 没有 Provider 档案，发送按钮处于禁用状态，因此不能证明已有模型消息或真实模型任务恢复。

## 仍未验证的 D2 验收项

- AgentDU 当前 Windows 安全 Provider 档案为空。用户提供的 Codex 配置使用 `wire_api = "responses"`，现已实现对应适配。用户给定 Endpoint 的未认证 `/models` 请求返回 HTTP 401。未读取或复用 Codex 凭据。真实 Provider 连通性、跨文件模型修改、真实测试修复、真实 Git Diff 审查和已有模型对话恢复均未执行。
- 较早 NSIS 安装版的 Electron 主进程和 Runtime Host 已联合启动，Runtime 在线。安装版最近项目和 Git 状态已复验并保存截图。新源码包的 Provider 设置表单和 Responses 选项已目视复核。
- 已验证较早 NSIS 包安装、带空格路径、embedded Python 启动、Host 初始化和关闭，以及最近项目与 Git 状态恢复。新源码 NSIS 包已构建，但本轮没有完成向导安装。其 unpacked 副本在带空格路径成功启动。较早的人工取消包使用 60 秒间隔，当前源码使用 6 秒间隔。
- 已验证安装版正常关闭、Runtime 有序退出、异常退出恢复提示、Host 层任务取消，以及 Renderer UI 取消受控任务。尚未验证 Runtime 重启按钮、已有模型对话恢复和真实 Provider 任务。

## 当前结论

D2 的主要桌面工作流和 Runtime IPC 已经落地，自动化及 AppContainer 命令隔离测试通过，较早 NSIS 安装版在含空格路径下成功启动 embedded Runtime，并恢复最近项目和 Git 状态。新源码 NSIS 包成功构建，其 unpacked 副本在含空格路径下启动并完成 Runtime UI 取消和会话生命周期持久化验证。AgentDU 没有 Provider 凭据，因此真实 Provider 任务、已有模型对话恢复和新 NSIS 包向导安装仍未完成。隔离 profile 下的项目与空白会话恢复已验证，但不能替代真实会话恢复。D2 继续为 NOT VERIFIED，不进入 D3。完成真实任务验收需要在 AgentDU 桌面 Provider 设置中配置档案并安全保存 API Key。

# AgentDU Desktop D2 实现与验收记录

日期：2026-11-26（Asia/Shanghai）

## 状态

**D2 尚未通过独立验收。** 本地工作区已实现单模型工作台主要路径，自动化与 Windows AppContainer 集成测试通过。修复了真实冷启动时的 Runtime 就绪竞态，并生成、安装和启动了 D2 Windows 包。针对当前配置增加了 OpenAI-compatible Responses API 支持，覆盖文本输入、流式事件、函数调用和函数结果续传，同时保留 Chat Completions 默认行为。真实 Provider 任务仍未完成。新版界面已目视确认异常恢复提示和 Runtime 在线状态，但没有保存为可交付的截图文件，也未复核 Provider 设置表单。未将受控测试任务或模拟模型调用作为真实 Provider 证据。

本轮修复从 `c61933a667b6e044cc9cf2e50a732288824eea72` 开始。D2 仍未通过独立验收。

## 功能实现

- 左侧项目列表支持系统目录选择、最近项目恢复和项目切换。会话按工作区持久化，可新建、列出、恢复，并呈现消息、流式增量和 Runtime 事件。
- 主区展示项目 Git 分支与状态、Agent 活动时间线、模型输出、取消操作及异常恢复提示。
- 右侧面板提供项目文件浏览、文本查看和受审批写入、Git Diff、隔离终端命令记录。
- Provider 档案支持 Endpoint、Model ID、API Key 和 Chat Completions / Responses 协议选择。Windows `safeStorage` 加密凭据，Runtime 启动时将解密值注入子进程环境，不通过命令行传密钥。连通性测试只验证 Provider 的 `/models` 响应。
- 审批事件呈现来源、命令、工作目录、目标文件、范围和网络需求。审批请求、审批结论及实际执行结果分别写入会话事件记录。
- Runtime Host 对桌面编辑写入、Agent turn 和终端命令实施工作区互斥。Windows 终端命令通过 AppContainer 运行，限制网络、进程、内存、输出及运行时长。
- Renderer 通过受限 Preload 调用显式 IPC API。窗口最小尺寸、响应式窄屏布局和侧栏横向调整由 Electron 设置和 CSS 实现。

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


## 仍未验证的 D2 验收项

- AgentDU 当前 Windows 安全 Provider 档案为空。用户提供的 Codex 配置使用 `wire_api = "responses"`，现已实现对应适配；Codex 与 AgentDU 的凭据存储互不共享。真实 Provider 连通性、跨文件模型修改、真实测试修复、真实 Git Diff 审查和界面恢复模型会话均未执行，状态为 **BLOCKED**。
- 新构建 Electron 主进程和 Runtime Host 已联合启动。已目视确认异常恢复横幅、Runtime 在线和空工作区。未保存截图文件，Provider 设置表单的 Responses 选项仍未目视复核。
- 已验证新版 NSIS 安装、带空格安装路径、embedded Python、Host 初始化/关闭及受控 Runtime 取消。此前根目录 `release/AgentDU-Setup-0.1.0-x64.exe` 是 2026-11-23 的 D1 构建，本轮未覆盖。最近源码的 Electron 工作区恢复流程未用新构建重新验收。
- 已验证安装版正常关闭、Runtime 有序退出、异常退出恢复提示，以及 Host 层任务取消。尚未验证 Renderer UI 取消按钮端到端、Runtime 重启按钮、已有模型会话恢复和真实 Provider 任务。

## 当前结论

D2 的主要桌面工作流和 Runtime IPC 已经落地，自动化及 AppContainer 命令隔离测试通过，`package:win` 可复现生成 Windows 安装器，含空格安装路径下的 Electron 与 embedded Runtime 已启动，Host 探针、正常关闭、异常恢复及 Host 层受控取消通过，启动阶段的最近项目加载竞态已修复。Responses API 适配通过本地协议测试，尚未用目标 Provider 进行真实请求。真实模型任务、UI 取消端到端、会话恢复和保存截图文件仍缺失，因此当前不能报告 D2 完成，也不进入 D3。完成真实任务验收仍需在 AgentDU 桌面 Provider 设置中配置档案并安全保存凭据。

# AgentDU 测试与验证记录

最近全量自动化验证日期为 2026-11-27。被测代码 SHA 为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，分支为 `main`。G1 验收矩阵和未完成项见 [G1 验收报告](docs/desktop/g1-closeout-2026-11-27.md)。本轮文件更改是报告与架构文档，测试目标 SHA 保留为实际测试时 HEAD。

## 本轮验证

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m compileall -q src tests` | PASS |
| Python 自动化测试 | `python -m unittest discover -s tests -v` | PASS，96 项，约 94 秒 |
| Desktop Host 专项测试 | `python -m unittest discover -s tests -p test_desktop_host.py -v` | PASS。22 项专项结果来自 `f5fc15d7b05b7ab4bf76c636fe7385e66c3c6a5c`；`aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e` 上的 96 项全量测试也包含这些 Host 测试 |
| Desktop 自动化测试 | `cd desktop; pnpm test` | PASS，4 个测试文件、19 项 |
| Desktop TypeScript 检查与生产构建 | `cd desktop; pnpm run build` | PASS，类型检查、Vite Renderer 构建和 Electron 构建通过 |
| Windows AppContainer 探针 | `cd desktop; pnpm run verify:appcontainer` | PASS，项目读写限制、loopback 网络限制和子进程限制 |
| NSIS 构建 | `pnpm run prepare:runtime` 后运行 electron-builder | PASS，Electron 45.4.1 与嵌入式 Python 3.14.10 |

本轮 NSIS 安装器位于 `desktop/release/g1-closeout-aacf3c2-2026-11-27/AgentDU-Setup-0.1.0-x64.exe`，SHA-256 为 `B5446953C8DB27D0B2EC704830C770F0B8644A6C720EEA60D2B60D8355945FC6`。该产物已构建但没有在隔离 Windows profile 中安装和运行。较早的 `c030c6c` 安装版已验证含空格安装路径、受限 PATH 和嵌入式 Python 启动。尝试以 `APPDATA` 隔离 Electron `userData` 失败后，本轮在主进程增加 `AGENTDU_DESKTOP_USER_DATA_DIR` 覆盖并重建安装器。新覆盖已通过 TypeScript/Electron 构建，但尚未在安装版启动验证。详细结果见 G1 报告。

Python 自动化包含本地 HTTP Provider 夹具、Runtime、会话、工具、CLI Handoff、编排、MCP、Skills、插件及 Windows AppContainer 集成测试。模拟 Provider 结果不代表外部 Provider 的兼容性、质量、延迟或费用。

## 已有桌面运行证据

[D1 验收记录](docs/desktop/d1-implementation.md) 和 [D2 验收记录](docs/desktop/d2-implementation.md) 包含安装包、embedded Python Host、带空格安装路径、Host 生命周期、AppContainer 探针和 Runtime 取消的详细证据。2026-11-27 已额外完成 Renderer UI 取消和 Provider Responses API 表单选项验证，结果和截图记录于 D2 验收记录。

较早的临时 NSIS 安装版位于 `desktop/release/d2-ui-cancel-manual-2026-11-26-60s/installed app with spaces`，Runtime 在线并恢复了最近项目及 Git 状态。该包的 60 秒自检间隔只用于此前的人工取消验证。新源码 NSIS 包位于 `desktop/release/d2-lifecycle-2026-11-27`，SHA-256 为 `71337E8ED4C498C44ED549AAA72700D1D39734E914620E3BF6D1359AF91C334B`。2026-11-27 使用 `/S /D=<目录>` 安装到 `nsis-install-2026-11-27/AgentDU Installed App With Spaces`。从该目录启动时父进程 `PATH` 限制为 `%WINDIR%\System32`，UI 显示 Runtime 在线，Host 映像路径位于安装目录的 embedded Python 中。另将 `win-unpacked` 内容复制到 `installed app with spaces` 目录，完成 Renderer UI 取消和会话生命周期验证。2026-11-27 又在最新 NSIS 安装版中通过 Renderer 启动并取消受控 Runtime 自检。界面显示任务已取消，JSONL 会话记录含 task.accepted、task.cancel_requested 和 task.cancelled，且没有 task.completed。最新安装版证据包括 [UI 截图](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27.jpg)、[会话 JSONL](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27-session.jsonl) 和 [Host 日志](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27-host.log)。先前 unpacked 包验证截图为 `docs/desktop/evidence/d2-runtime-lifecycle-persist-2026-11-27.jpg`。当前 tracked 源码自检间隔为 6 秒。隔离 profile 关闭后 launch marker 已清除。

2026-11-27 又从当前源码构建 NSIS 包，安装器 SHA-256 为 `E25AEE5DC79E4B12CB5F6AC0866CFA27EC3E503D6471823E6426423356E103C8`，安装到 `desktop/release/d2-runtime-recovery-2026-11-27/nsis-install/AgentDU Installed App With Spaces`。父进程 `PATH` 限制为 `%WINDIR%\System32` 时，Host 从安装目录的 embedded Python 启动。两次终止空闲 Runtime Host 后，UI 均显示故障和重启入口，点击后 Runtime 恢复在线。异常与恢复证据见 [错误界面截图](docs/desktop/evidence/d2-nsis-runtime-restart-error-2026-11-27.jpg)、[恢复界面截图](docs/desktop/evidence/d2-nsis-runtime-restart-recovered-2026-11-27.jpg) 和 [Host 日志](docs/desktop/evidence/d2-nsis-runtime-restart-2026-11-27-host.log)。该测试没有活动模型任务。
活动任务崩溃注入已修复并复验。安装版 Host 在受控流任务 `model_request` 后被终止，重启后会话 JSONL 写入唯一的 `task.interrupted`，且不自动重放。重复恢复协议请求均返回 `already_terminal`。证据包括[修复前失败记录](docs/desktop/evidence/d2-nsis-active-task-crash-2026-11-27-session.jsonl)、[修复后会话记录](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-session.jsonl)、[打包 Host 协议响应](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-host-protocol.jsonl)和[Host 日志](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-host-stderr.log)。该测试使用受控任务，不证明真实 Provider 任务的中断恢复。

2026-11-27 在新源码包副本和隔离 `--user-data-dir` 下完成项目与空白会话恢复检查。重启后 UI 自动恢复临时项目、`新会话` 和 `session_created` 活动事件。磁盘上的 `runtime/recent-workspaces.json` 保留项目路径，项目 `.agentdu/sessions/<session-id>.jsonl` 保留创建事件。该测试 profile 没有 Provider，发送按钮禁用，未验证模型消息或模型任务恢复。

## 尚待验收

- 在 AgentDU 应用中安全配置真实 Provider，并完成连通性验证。当前档案列表为空，未认证 `/models` 请求返回 HTTP 401。Provider 表单与 Responses API 选项已目视复核，截图位于 `docs/desktop/evidence/d2-provider-responses-profile-form-2026-11-27.jpg`。
- 用真实 Provider 在真实项目中完成跨文件读取、修改、测试和修复。
- 在活动真实 Provider 模型任务期间验证 Runtime 异常退出、重新连接和会话状态恢复。受控测试任务的崩溃恢复已通过安装版 Host 协议验证。
- 配置真实 Provider 后，从实际 NSIS 安装版恢复已有模型会话，并核对会话记录、任务状态与工作区内容。

- 完成桌面 Runtime 接入多 Agent 后的并发、恢复、故障注入和真实多模型验证。

## 可复现命令

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
cd desktop
pnpm test
pnpm run build
pnpm run verify:appcontainer
pnpm run prepare:runtime
pnpm exec electron-builder --win nsis --x64 --config.electronDist=./node_modules/electron/dist --config.directories.output=release/d2-lifecycle-2026-11-27
```

打包脚本 `pnpm run package:win` 会准备 Python Runtime 并构建 NSIS 安装程序。它当前默认写入 `desktop/release/d2-final`，而该目录有已安装的测试副本。构建不同验证版本时应指定独立的输出目录。

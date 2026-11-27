# AgentDU 测试与验证记录

最近全量自动化验证日期为 2026-11-27。验收起始仓库 HEAD 为 `c582fa831de4b92e456a4fefdd9f97139799b052`，源码变更基线为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，分支为 `main`。Python、Desktop、生产构建与 AppContainer 检查在 `c582fa8` 工作树重跑。后续提交只更新报告、架构文档与证据，不改变被测代码。G1 矩阵与未通过项见 [G1 验收报告](docs/desktop/g1-closeout-2026-11-27.md)。

## 本轮验证

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m compileall -q src tests` | PASS |
| Python 自动化测试 | `python -m unittest discover -s tests -v` | PASS，96 项，98.581 秒 |
| Desktop Host 专项测试 | `python -m unittest discover -s tests -p test_desktop_host.py -v` | PASS。22 项专项结果来自 `f5fc15d7b05b7ab4bf76c636fe7385e66c3c6a5c`；`aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e` 上的 96 项全量测试也包含这些 Host 测试 |
| Desktop 自动化测试 | `cd desktop; pnpm test` | PASS，4 个测试文件、19 项 |
| Desktop TypeScript 检查与生产构建 | `cd desktop; pnpm run build` | PASS，类型检查、Vite Renderer 构建和 Electron 构建通过 |
| Windows AppContainer 探针 | `cd desktop; pnpm run verify:appcontainer` | PASS，项目读写限制、loopback 网络限制和子进程限制 |
| NSIS 构建 | `pnpm run prepare:runtime` 后运行 electron-builder | PASS，Electron 45.4.1 与嵌入式 Python 3.14.10 |

当前源码 NSIS 安装器位于 `desktop/release/g1-remaining-c582fa8-2026-11-27/AgentDU-Setup-0.1.0-x64.exe`，SHA-256 为 `C2F2ED20CB62022C71EDD84958B1F45DA8AE9CE3B6EC662C49A8E5DD32F54EA0`。它已安装到 `AgentDU Installed With Spaces` 并在 `AGENTDU_DESKTOP_USER_DATA_DIR` 指定的隔离目录启动。Runtime 使用安装目录的 Embedded Python 3.14.10，合成 Provider 和临时项目在应用重启后恢复。验收曾误启动一个默认 profile 实例，该隔离要求因此判 FAIL。详见 G1 报告。

Python 自动化包含本地 HTTP Provider 夹具、Runtime、会话、工具、CLI Handoff、编排、MCP、Skills、插件及 Windows AppContainer 集成测试。模拟 Provider 结果不代表外部 Provider 的兼容性、质量、延迟或费用。

## G1 安装版与 Provider 验收

- 当前 NSIS 包在含空格安装路径及受限 PATH 下运行，运行证据见 [Runtime 日志](docs/desktop/evidence/g1-installed-runtime-2026-11-27.log)。
- Electron `safeStorage` 保存的合成 Provider 档案在隔离数据目录中重读成功。Runtime 使用该凭据请求本机 `/v1/models`，服务记录为授权通过及 HTTP 200，见 [本地请求记录](docs/desktop/evidence/g1-provider-http-2026-11-27.jsonl)。该检查不是对真实 Provider 的请求。
- 安装版启动后显示 Runtime 在线、合成档案、临时 Git 项目和异常关闭提示，见 [隔离安装恢复截图](docs/desktop/evidence/g1-isolated-install-restore-2026-11-27.jpg)。空闲 Main 被单独终止后 Host 在 8 秒内退出，进程记录见 [空闲 Main 退出记录](docs/desktop/evidence/g1-main-idle-crash-2026-11-27.json)。该会话没有活动任务，所以 Main 崩溃期间的任务恢复保持未验证。
- 自动化原始记录见 [Python 测试](docs/desktop/evidence/g1-python-tests-2026-11-27.log)、[Desktop 测试](docs/desktop/evidence/g1-desktop-tests-2026-11-27.log)、[Desktop 构建](docs/desktop/evidence/g1-desktop-build-2026-11-27.log) 与 [AppContainer 探针](docs/desktop/evidence/g1-appcontainer-2026-11-27.log)。

## 已有桌面运行证据

[D1 验收记录](docs/desktop/d1-implementation.md) 和 [D2 验收记录](docs/desktop/d2-implementation.md) 包含安装包、embedded Python Host、带空格安装路径、Host 生命周期、AppContainer 探针和 Runtime 取消的详细证据。2026-11-27 已额外完成 Renderer UI 取消和 Provider Responses API 表单选项验证，结果和截图记录于 D2 验收记录。

较早的临时 NSIS 安装版位于 `desktop/release/d2-ui-cancel-manual-2026-11-26-60s/installed app with spaces`，Runtime 在线并恢复了最近项目及 Git 状态。该包的 60 秒自检间隔只用于此前的人工取消验证。新源码 NSIS 包位于 `desktop/release/d2-lifecycle-2026-11-27`，SHA-256 为 `71337E8ED4C498C44ED549AAA72700D1D39734E914620E3BF6D1359AF91C334B`。2026-11-27 使用 `/S /D=<目录>` 安装到 `nsis-install-2026-11-27/AgentDU Installed App With Spaces`。从该目录启动时父进程 `PATH` 限制为 `%WINDIR%\System32`，UI 显示 Runtime 在线，Host 映像路径位于安装目录的 embedded Python 中。另将 `win-unpacked` 内容复制到 `installed app with spaces` 目录，完成 Renderer UI 取消和会话生命周期验证。2026-11-27 又在最新 NSIS 安装版中通过 Renderer 启动并取消受控 Runtime 自检。界面显示任务已取消，JSONL 会话记录含 task.accepted、task.cancel_requested 和 task.cancelled，且没有 task.completed。最新安装版证据包括 [UI 截图](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27.jpg)、[会话 JSONL](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27-session.jsonl) 和 [Host 日志](docs/desktop/evidence/d2-nsis-ui-cancel-2026-11-27-host.log)。先前 unpacked 包验证截图为 `docs/desktop/evidence/d2-runtime-lifecycle-persist-2026-11-27.jpg`。当前 tracked 源码自检间隔为 6 秒。隔离 profile 关闭后 launch marker 已清除。

2026-11-27 又从当前源码构建 NSIS 包，安装器 SHA-256 为 `E25AEE5DC79E4B12CB5F6AC0866CFA27EC3E503D6471823E6426423356E103C8`，安装到 `desktop/release/d2-runtime-recovery-2026-11-27/nsis-install/AgentDU Installed App With Spaces`。父进程 `PATH` 限制为 `%WINDIR%\System32` 时，Host 从安装目录的 embedded Python 启动。两次终止空闲 Runtime Host 后，UI 均显示故障和重启入口，点击后 Runtime 恢复在线。异常与恢复证据见 [错误界面截图](docs/desktop/evidence/d2-nsis-runtime-restart-error-2026-11-27.jpg)、[恢复界面截图](docs/desktop/evidence/d2-nsis-runtime-restart-recovered-2026-11-27.jpg) 和 [Host 日志](docs/desktop/evidence/d2-nsis-runtime-restart-2026-11-27-host.log)。该测试没有活动模型任务。
活动任务崩溃注入已修复并复验。安装版 Host 在受控流任务 `model_request` 后被终止，重启后会话 JSONL 写入唯一的 `task.interrupted`，且不自动重放。重复恢复协议请求均返回 `already_terminal`。证据包括[修复前失败记录](docs/desktop/evidence/d2-nsis-active-task-crash-2026-11-27-session.jsonl)、[修复后会话记录](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-session.jsonl)、[打包 Host 协议响应](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-host-protocol.jsonl)和[Host 日志](docs/desktop/evidence/d2-nsis-active-task-recovery-2026-11-27-host-stderr.log)。该测试使用受控任务，不证明真实 Provider 任务的中断恢复。

2026-11-27 在新源码包副本和隔离 `--user-data-dir` 下完成项目与空白会话恢复检查。重启后 UI 自动恢复临时项目、`新会话` 和 `session_created` 活动事件。磁盘上的 `runtime/recent-workspaces.json` 保留项目路径，项目 `.agentdu/sessions/<session-id>.jsonl` 保留创建事件。该测试 profile 没有 Provider，发送按钮禁用，未验证模型消息或模型任务恢复。

## 尚待验收

- 在新建的隔离目录中重跑 NSIS 检查，并证明默认 profile 文件没有变化。
- 完成 Provider 默认档案切换、删除、删除后凭据不注入、Runtime 重启失败回滚及 Renderer 隔离测试。
- 在活动任务期间外部终止安装版 Electron Main，恢复同一项目与会话，检查 `task.interrupted` 只记录一次且任务不自动重放。
- 用户在 AgentDU 设置中配置真实 Provider 后，完成连通性、跨文件编程和真实模型任务中断恢复。凭据不得通过聊天或读取其他应用配置导入。
- G1 通过独立验收后再决定是否进入 G2。G2 当前只有经二次审计的设计文档，没有桌面多 Agent 实现。

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

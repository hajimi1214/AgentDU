# AgentDU 测试与验证记录

最后复测：2026-11-27，工作目录 `C:\AgentDU`，分支 `main`。本轮全量自动化复测在 `443cf432a0b090170d8fe4e55075004fcdb428b6` 基线上执行，并覆盖本次会话生命周期持久化修复。

## 本次复测

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m compileall -q src tests` | PASS |
| Python 自动化测试 | `python -m unittest discover -s tests` | PASS，92 项测试。并行运行时一次出现本地 HTTP 流测试连接重置，单项复跑及 Python 全量串行复跑均通过 |
| Desktop Host 专项测试 | `python -m unittest discover -s tests -p test_desktop_host.py -v` | PASS，19 项测试，当前受控自检间隔为 6 秒 |
| Desktop 自动化测试 | `cd desktop; pnpm test` | PASS，4 个测试文件、17 项测试 |
| Desktop TypeScript 检查与生产构建 | `cd desktop; pnpm run build` | PASS，主进程/Renderer 类型检查、Vite Renderer 构建及 Electron 构建通过 |
| Windows AppContainer 探针 | `cd desktop; pnpm run verify:appcontainer` | PASS，项目目录读写拒绝、loopback 网络阻断、子进程仍受 AppContainer 限制 |
| Windows Runtime 和 NSIS 包 | `cd desktop; pnpm run prepare:runtime` 后运行 electron-builder | PASS，embedded Python 与 NSIS 安装器成功构建。安装器尚未通过向导安装 |
| Git 差异空白检查 | `git diff --check` | PASS |

Python 测试包含本地 HTTP Provider 夹具、Runtime、会话、工具、CLI Handoff、编排、MCP、Skills、插件及 Windows AppContainer 集成测试。使用模拟 Provider 的测试不能代表外部 Provider 的真实兼容性、质量、延迟或费用。

## 已有桌面运行证据

[D1 验收记录](docs/desktop/d1-implementation.md) 和 [D2 验收记录](docs/desktop/d2-implementation.md) 包含安装包、embedded Python Host、带空格安装路径、Host 生命周期、AppContainer 探针和 Runtime 取消的详细证据。2026-11-27 已额外完成 Renderer UI 取消和 Provider Responses API 表单选项验证，结果和截图记录于 D2 验收记录。

较早的临时 NSIS 安装版位于 `desktop/release/d2-ui-cancel-manual-2026-11-26-60s/installed app with spaces`，Runtime 在线并恢复了最近项目及 Git 状态。该包的 60 秒自检间隔只用于此前的人工取消验证。新源码 NSIS 包已构建到 `desktop/release/d2-lifecycle-2026-11-27`，SHA-256 为 `71337E8ED4C498C44ED549AAA72700D1D39734E914620E3BF6D1359AF91C334B`，但本轮没有通过 NSIS 安装向导完成安装。已将其 `win-unpacked` 内容复制到 `installed app with spaces` 目录启动，Runtime 在线。通过 Renderer 取消后，界面显示任务已取消，JSONL 会话记录包含 task.accepted、task.cancel_requested 和 task.cancelled，且没有 task.completed。截图位于 `docs/desktop/evidence/d2-runtime-lifecycle-persist-2026-11-27.jpg`。当前 tracked 源码自检间隔为 6 秒。临时 profile 关闭后 launch marker 已清除。

## 尚待验收

- 在 AgentDU 应用中安全配置真实 Provider，并完成连通性验证。当前档案列表为空，未认证 `/models` 请求返回 HTTP 401。Provider 表单与 Responses API 选项已目视复核，截图位于 `docs/desktop/evidence/d2-provider-responses-profile-form-2026-11-27.jpg`。
- 用真实 Provider 在真实项目中完成跨文件读取、修改、测试和修复。
- 在最新 tracked 源码构建的安装包上复测 Renderer 取消，再核对 Host 退出和安装包恢复。
- 从安装版恢复一个已有模型会话，并核对会话记录和工作区状态。
- 从 Windows 安装版恢复已有会话，并验证任务状态与工作区内容。
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

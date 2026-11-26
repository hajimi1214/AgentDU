# AgentDU 测试与验证记录

最后复测：2026-11-26，工作目录 `C:\AgentDU`，分支 `main`。本轮测试覆盖基于 `c61933a667b6e044cc9cf2e50a732288824eea72` 的实现及本次进度改动。

## 本次复测

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| Python 编译 | `python -m compileall -q src tests` | PASS |
| Python 自动化测试 | `python -m unittest discover -s tests` | PASS，92 项测试。并行运行时一次出现本地 HTTP 流测试连接重置，单项复跑及 Python 全量串行复跑均通过 |
| Desktop 自动化测试 | `cd desktop; pnpm test` | PASS，4 个测试文件、17 项测试 |
| Desktop TypeScript 检查与生产构建 | `cd desktop; pnpm run build` | PASS，主进程/Renderer 类型检查、Vite Renderer 构建及 Electron 构建通过 |
| Git 差异空白检查 | `git diff --check` | PASS |

Python 测试包含本地 HTTP Provider 夹具、Runtime、会话、工具、CLI Handoff、编排、MCP、Skills、插件及 Windows AppContainer 集成测试。使用模拟 Provider 的测试不能代表外部 Provider 的真实兼容性、质量、延迟或费用。

## 已有桌面运行证据

[D1 验收记录](docs/desktop/d1-implementation.md) 和 [D2 验收记录](docs/desktop/d2-implementation.md) 包含安装包、embedded Python Host、带空格安装路径、Host 生命周期、AppContainer 探针和 Runtime 取消的详细证据。记录中注明的 Host 取消测试不等同于 Renderer UI 取消端到端验收。

最新源码的 Windows 包曾输出至 `desktop/release/d1-current-2026-11-26`。此包尚未安装或启动验收。此前安装版测试副本位于 `desktop/release/d2-final`。

## 尚待验收

- 在 AgentDU 应用中安全配置真实 Provider，并完成连通性验证。
- 用真实 Provider 在真实项目中完成跨文件读取、修改、测试和修复。
- 从 Renderer 发起受控任务并取消，核对 Host 事件、任务终态和退出状态。
- 在 Electron 中复验最近项目恢复是否已随 Host Git 子进程修复而恢复正常。
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
```

打包脚本 `pnpm run package:win` 会准备 Python Runtime 并构建 NSIS 安装程序。它当前默认写入 `desktop/release/d2-final`，而该目录有已安装的测试副本。构建不同验证版本时应指定独立的输出目录。

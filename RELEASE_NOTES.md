# AgentDU 进展记录

## 2026-11-26 项目状态基线

这是开发进度检查点，不是正式产品发布。

- Git 基线 `85789531927c597c75d213b8a7f5fd68cc65d374`，分支 `main`，检查时与 `origin/main` 同步。
- 创建长期路线图、架构基线、实现状态、决策、测试和发布记录文档。
- Python 编译通过，89 项 Python 测试通过。
- Desktop 13 项测试通过，TypeScript 检查及 Renderer/Electron 生产构建通过。
- 桌面 Runtime、JSONL IPC、受限 Preload、AppContainer 命令执行及安装版 Host 运行证据已在 D1/D2 记录中归档。
- D1/D2 尚未整体通过验收。真实 Provider 编程任务、Renderer 取消端到端和安装版完整会话恢复仍待完成。
- G1 尚未验证，后续阶段 G2–G8 保持 `NOT VERIFIED`。详见 [阶段状态](IMPLEMENTATION_STATUS.md) 和 [路线图](ROADMAP.md)。

本记录不创建安装发行版本，也不宣称性能或真实模型能力结论。
## 2026-11-27 D2 活动任务恢复进度

- 增加 Runtime Host 崩溃后的任务生命周期核对，受控活动任务恢复为唯一的 `task.interrupted` 终态，并且不会自动重放。
- NSIS embedded Python Host 协议复核连续两次返回 `already_terminal`。恢复会话和协议证据已存入 `docs/desktop/evidence`。
- Python 94 项、Desktop 19 项测试和生产构建通过。D2 仍为 `NOT VERIFIED`，真实 Provider 编程任务、真实模型故障恢复及已有模型会话恢复仍未完成。

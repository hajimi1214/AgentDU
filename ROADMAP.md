# AgentDU 长期研发路线

最后更新日期为 2026-11-27。最近源码提交为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`，验收起始仓库 HEAD 为 `c582fa831de4b92e456a4fefdd9f97139799b052`。自动化在该 HEAD 的干净源码树重跑。总目标是建设可日常使用、可靠、可验证的多模型协同 AI Agent 桌面工作站。构建成功或单次测试通过不代表阶段验收完成。

## 状态定义

阶段状态使用 `PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED` 与 `BLOCKED`。结论由实现、测试和运行证据支撑。详细结果见 [测试记录](TESTING.md)、[实现状态](IMPLEMENTATION_STATUS.md)、[G1 验收报告](docs/desktop/g1-closeout-2026-11-27.md)、[D1 验收记录](docs/desktop/d1-implementation.md) 与 [D2 验收记录](docs/desktop/d2-implementation.md)。

## 阶段路线

| 阶段 | 目标 | 当前状态 | 进入下一步前的主要条件 |
| --- | --- | --- | --- |
| G0 | 现有仓库审计与基线 | CONDITIONAL PASS | 当前代码和测试基线已核对。真实 Provider 与隔离 profile 未修改验收仍需完成。 |
| G1 | 桌面基础能力稳定化 | BLOCKED | 自动化测试、当前 NSIS 启动、合成 Provider 安全保存与注入通过部分检查。隔离检查记录了默认 profile 变更，活动任务 Main 崩溃恢复未验证，真实 Provider 编程任务阻塞。 |
| G2 | 多 Agent Runtime 集成 | NOT VERIFIED | [集成方案](docs/desktop/g2-runtime-integration.md) 已完成二次设计审计。G1 关键门槛通过前不接入 `AutonomousCoordinator`。 |
| G3 | 多 Agent 并行执行 | NOT VERIFIED | 验证独立工作区、依赖调度、结果检查、冲突处理和集成恢复。 |
| G4 | 桌面多 Agent 工作台 | NOT VERIFIED | 将 Agent、任务、依赖、事件、审批和结果审查接入桌面交互。 |
| G5 | 自主协作与独立验证 | NOT VERIFIED | 通过真实多模型任务验证自主委派、审查、整合和重新规划。 |
| G6 | 容错与长期运行 | NOT VERIFIED | 通过故障注入和长任务验证崩溃恢复、重试、接管、资源控制与状态一致性。 |
| G7 | 扩展生态 | NOT VERIFIED | 验证桌面产品中的 MCP、Skills、插件和自定义 Agent 生命周期。 |
| G8 | 产品化与正式发布 | NOT VERIFIED | 完成 Windows 安装升级、迁移、文档、安全审计、完整回归及真实项目验收。 |

## 当前推进门槛

首先完成 G1 剩余验收。隔离 NSIS 验收需要在新建的专用测试 profile 中复跑，并记录原用户 profile 的前后状态。Electron Main 异常退出必须发生在真实活动任务期间，随后检查唯一的中断事件、会话恢复和无自动重放。

真实 Provider 档案必须由用户直接在 AgentDU 的“模型与 Provider”设置中保存。可使用此前提供的 Endpoint `https://apikey.casa/v1`，Model ID 按服务实际返回的模型填写。保存后在 AgentDU 内运行连通性测试。API Key 直接输入 AgentDU，不发送到聊天窗口。其他客户端凭据不会被读取或复制。

Provider 配置完成后，在临时 Git 项目执行真实跨文件任务、测试与错误修复，再进行真实 Provider 活动任务恢复验收。CLI 中已有的协作能力和经过二次审计的 G2 文档不能作为桌面集成完成的证明。

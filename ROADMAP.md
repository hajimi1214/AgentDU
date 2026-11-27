# AgentDU 长期研发路线

最后更新：2026-11-27。代码与自动化验证基线为 `aacf3c2a7dc8ba5536079fbc6f848bfffa62b48e`。
总目标是建设可日常使用、可靠、可验证的多模型协同 AI Agent 桌面工作站。项目保持进行中，构建成功或单次测试通过不代表最终验收完成。

## 状态定义

阶段状态使用 `PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED` 与 `BLOCKED`。结论由实现、测试和运行证据支撑。详细结果见 [测试记录](TESTING.md)、[实现状态](IMPLEMENTATION_STATUS.md)、[G1 验收报告](docs/desktop/g1-closeout-2026-11-27.md) 和 [D1/D2 验收记录](docs/desktop/d1-implementation.md) 与 [D2 记录](docs/desktop/d2-implementation.md)。

## 阶段路线

| 阶段 | 目标 | 当前状态 | 进入下一步前的主要条件 |
| --- | --- | --- | --- |
| G0 | 现有仓库审计与基线 | CONDITIONAL PASS | 当前代码和测试基线已核对。真实 Provider 与完整安装版工作流仍需验收。 |
| G1 | 桌面基础能力稳定化 | NOT VERIFIED | Host 持久化任务恢复、损坏会话和不可用工作区已增加自动化证据。Provider 保存与加密、真实模型编程任务、Electron Main 异常退出恢复及最新安装器隔离 profile 验收未完成。 |
| G2 | 多 Agent Runtime 集成 | NOT VERIFIED | [技术方案](docs/desktop/g2-runtime-integration.md) 已定义接口和状态。必须先解除 G1 的关键验收门槛，再实现 Coordinator 与 Desktop Host 集成。 |
| G3 | 多 Agent 并行执行 | NOT VERIFIED | 验证独立工作区、依赖调度、结果检查、冲突处理和集成恢复。 |
| G4 | 桌面多 Agent 工作台 | NOT VERIFIED | 将 Agent、任务、依赖、事件、审批和结果审查接入桌面交互。 |
| G5 | 自主协作与独立验证 | NOT VERIFIED | 通过真实多模型任务验证自主委派、审查、整合和重新规划。 |
| G6 | 容错与长期运行 | NOT VERIFIED | 通过故障注入和长任务验证崩溃恢复、重试、接管、资源控制与状态一致性。 |
| G7 | 扩展生态 | NOT VERIFIED | 验证桌面产品中的 MCP、Skills、插件和自定义 Agent 生命周期。 |
| G8 | 产品化与正式发布 | NOT VERIFIED | 完成 Windows 安装升级、迁移、文档、安全审计、完整回归及真实项目验收。 |

## 当前推进门槛

首先完成 G1。AgentDU Provider 档案和凭据必须由用户在 AgentDU 设置中配置。其他应用凭据不会被复制到 AgentDU。

桌面多 Agent 实现进入 G2 前，单模型桌面工作流、任务持久化恢复、Provider 保存与加载、真实项目任务和当前源码安装包都需要有对应证据。CLI 中已有的协作能力和架构设计不能作为桌面集成完成的证明。

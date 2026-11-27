# AgentDU 长期研发路线

最后更新：2026-11-27
本轮实施起点：`main`，`c61933a667b6e044cc9cf2e50a732288824eea72`
总目标：建设可日常使用、可靠、可验证的多模型协同 AI Agent 桌面工作站。项目保持进行中，阶段演示、构建成功或单次测试通过均不代表最终验收完成。

## 状态定义

只使用以下阶段状态：`PASS`、`CONDITIONAL PASS`、`FAIL`、`NOT VERIFIED`。结论必须由实现、测试和运行证据支撑。详细运行记录见 [测试记录](TESTING.md)、[实现状态](IMPLEMENTATION_STATUS.md) 和 [桌面验收记录](docs/desktop/d1-implementation.md)、[D2 记录](docs/desktop/d2-implementation.md)。

## 阶段路线

| 阶段 | 目标 | 当前状态 | 进入下一步前的主要条件 |
| --- | --- | --- | --- |
| G0 | 现有仓库审计与基线 | CONDITIONAL PASS | 已核对 Git 基线、主要 Runtime 与桌面边界，并复跑本地测试。后续审计发现仍需持续记入状态和决策文档。 |
| G1 | 桌面基础能力稳定化 | NOT VERIFIED | Renderer UI 取消端到端及任务生命周期持久化已验证。新源码包副本在隔离 profile 下恢复最近项目与空白会话。仍需真实 Provider 编程任务、已有模型对话恢复、最新 NSIS 包安装和 Windows 安装版完整工作流。 |
| G2 | 多 Agent Runtime 集成 | NOT VERIFIED | 将现有 `AutonomousCoordinator` 接入桌面 Host，定义统一任务生命周期、事件和并发约束。 |
| G3 | 多 Agent 并行执行 | NOT VERIFIED | 验证独立工作区、依赖调度、结果验证、冲突处理和集成恢复。 |
| G4 | 桌面多 Agent 工作台 | NOT VERIFIED | 将 Agent、任务、依赖、事件、审批和结果审查接入桌面交互。 |
| G5 | 自主协作与独立验证 | NOT VERIFIED | 通过真实多模型任务验证自主委派、审查、整合和重新规划。 |
| G6 | 容错与长期运行 | NOT VERIFIED | 通过故障注入和长任务验证崩溃恢复、重试、接管、资源控制与状态一致性。 |
| G7 | 扩展生态 | NOT VERIFIED | 验证桌面产品中的 MCP、Skills、插件和自定义 Agent 生命周期。 |
| G8 | 产品化与正式发布 | NOT VERIFIED | 完成 Windows 安装升级、迁移、文档、安全审计、完整回归及真实项目验收。 |

## 当前推进门槛

首先完成 G1。AgentDU 桌面 Provider 档案和凭据需要在应用设置中由用户安全配置。当前审计没有发现可用于 AgentDU 真实请求的本地 Provider 配置。已提供给其他应用的凭据不会被复制到 AgentDU。

桌面多 Agent 能力进入 G2 前，应先确认单模型桌面工作流的端到端可靠性。CLI 中已有的协作能力不能作为桌面集成完成的证据。

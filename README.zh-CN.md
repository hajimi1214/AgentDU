# AgentDU

[简体中文](README.zh-CN.md) | [English](README.md)

AgentDU V0.6 在单模型编程运行时和自主多模型协作的基础上，增加了任务恢复、Token 与费用统计、MCP stdio 工具、渐进式 Agent Skills 加载，以及显式启用的 Python 扩展。V0.6 尚未通过产品验收。已验证能力和未完成要求见 [V0.6 中文状态报告](docs/v0.6-status.zh-CN.md)。

## 环境要求

- Python 3.12 或更高版本
- 支持工具调用的 OpenAI 兼容 Chat Completions 接口
- Git 在 `PATH` 中，或使用 AgentDU 附带的 Git

Python 运行时没有第三方依赖。打包或安装项目时才需要 `setuptools`。

## 配置模型

可以直接从源码运行。在 PowerShell 中设置凭据和模型：

```powershell
$env:PYTHONPATH = "src"
$env:AGENTDU_API_KEY = "你的 API 密钥"
$env:AGENTDU_MODEL = "模型名称"
$env:AGENTDU_BASE_URL = "https://api.example.com/v1"
python -m agentdu --workspace . "检查项目并总结代码结构"
```

也可以在 `%APPDATA%\AgentDU\config.toml` 中定义模型档案，或通过 `--config` 指定其他文件：

```toml
default_profile = "openai"

[profiles.openai]
provider = "openai-compatible"
base_url = "https://api.openai.com/v1"
model = "模型名称"
api_key_env = "OPENAI_API_KEY"
timeout = 120
temperature = 0
streaming = true
capabilities = ["代码实现", "代码审查"]
stream_usage = true
input_cost_per_million = 2.0
output_cost_per_million = 8.0
```

使用 `--profile openai` 选择档案。`--model` 和 `--base-url` 可以覆盖档案设置。密钥通过 `api_key_env` 指定的环境变量、`AGENTDU_API_KEY` 或 `OPENAI_API_KEY` 提供。CLI 不接受明文 API 密钥参数，避免密钥出现在进程命令行中。

当前支持 OpenAI 兼容的 Chat Completions 协议，包括流式工具调用。尚未实现提供商原生协议和各模型专属功能。只有提供商返回 Token 用量，且输入、输出价格均已配置时，才会估算费用。价格由用户维护，估算结果不等同于提供商账单。

## 启动与会话

```powershell
python -m agentdu --workspace C:\path\to\project --interactive
python -m agentdu --workspace C:\path\to\project --session "会话 ID" "继续上次任务"
python -m agentdu --workspace C:\path\to\project --list-sessions
```

交互会话支持多轮输入，输入 `/exit` 或 `/quit` 退出。运行结束时会显示会话 ID。会话 JSONL 日志保存在工作区的 `.agentdu/sessions/` 目录中，其中保留完整对话，包括为适配上下文预算而未发送给模型的旧轮次。

内置工具支持文件列表、搜索、带行号读取、唯一文本块编辑、原子文件写入、Shell 命令，以及 Git 状态、差异和日志查询。读取默认允许。写入和 Shell 命令需要在交互终端确认。`--yes` 会在本次运行中批准所有写入和 Shell 命令。Shell 命令以当前用户权限执行，工作区不是操作系统沙箱，命令可以访问工作区以外的文件和资源。

## MCP、Skills 与插件

MCP stdio 服务在模型 TOML 配置中声明。服务工作目录必须位于工作区内，子进程环境变量通过配置从指定环境变量复制：

```toml
[mcp.servers.repo]
command = "python"
args = ["-m", "my_mcp_server"]
cwd = "."
timeout = 60

[mcp.servers.repo.env]
ACCESS_TOKEN = "MCP_SERVER_TOKEN"
```

AgentDU 在启动每个服务和调用每个 MCP 工具前请求授权。`--yes` 也会批准这些操作。当前仅支持本地 stdio 工具，不支持远程 Streamable HTTP、资源、提示词、服务端采样，也不会转发图像和音频工具结果。

项目中的 `.agents/skills/<名称>/SKILL.md` 会自动发现。可以重复使用 `--skills-dir` 添加目录。Agent 首先读取技能名称和简介，需要时再加载技能说明和文本资源。Frontmatter 解析支持 `name`、`description` 常见纯文本、引号和块文本格式，不是完整 YAML 解析器。

已安装的 Python 扩展使用 `agentdu.extensions` entry point 分组，必须声明 `api_version = 1`、名称、`register_tools(registry)` 和 `on_event(event)`。entry point 可以指向扩展类、实例或零参数工厂。通过 `--plugin 名称` 显式启用。导入前会请求执行授权。插件在 AgentDU 进程内以当前用户权限运行任意 Python 代码。

## 智能自主协作

默认模式会在配置中存在不同模型与 endpoint 的档案时向主 Agent 提供协作工具。主 Agent 可以独立完成任务、咨询其他模型、发起只读审查或委派实现任务。档案中的 `capabilities` 只是供模型参考的标签，不代表经过测量的能力保证。使用 `--solo` 可关闭协作，运行单模型基线。

```powershell
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile main "修复失败的测试"
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile main --solo "修复失败的测试"
```

调度器默认最多同时运行 2 个 Agent，每个用户会话最多 8 个任务，协作模型总请求数为 80，每个任务最多请求 20 次。可以使用 `--collaboration-concurrency`、`--collaboration-max-tasks`、`--collaboration-max-model-calls` 和 `--collaboration-task-model-calls` 修改上限。这些上限统计提供商请求次数，包括重试，不是美元费用上限。费用只在运行后估算，主 Agent 另有 `--max-steps` 限制。

实现和审查任务要求 Git 工作区干净且已有初始提交。每个任务在独立 Git worktree 和私有模型会话中运行，主 Agent 可以同时继续工作。集成前主工作区必须干净。AgentDU 会在候选 worktree 中执行明确指定的验收命令，只有合并和检查均成功后才更新原分支。冲突或验收失败不会修改主工作区。依赖任务会等待前置任务完成，并等待实现结果集成。

咨询任务只能看到主 Agent 提供的问题和上下文，且没有工具。审查任务在只读 Git worktree 中运行。实现 Agent 使用已配置的工作区权限，不能继续创建 Agent。任务状态和有限长度的进度历史存储在 `.agentdu/agents/<session-id>/tasks.json`。Git worktree 会保留以供审计。Shell 命令仍以当前用户身份运行，worktree 不构成操作系统安全沙箱。

## 模型交接

在配置文件中定义两个模型档案。以下示例为不同提供商使用不同环境变量保存凭据：

```toml
[profiles.builder]
provider = "openai-compatible"
base_url = "https://api.example.com/v1"
model = "coding-model"
api_key_env = "BUILDER_API_KEY"
streaming = true

[profiles.reviewer]
provider = "openai-compatible"
base_url = "https://api.example.net/v1"
model = "review-model"
api_key_env = "REVIEWER_API_KEY"
streaming = true
```

在已有初始提交且工作区干净的 Git 项目中启动交接：

```powershell
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile builder --handoff-profile reviewer --check "python -m unittest discover -s tests -v" --yes "修复失败的测试并说明改动"
```

交接双方使用不同的 Git worktree、会话和模型上下文。AgentDU 会传递受限的交接包，其中包含任务、来源 Agent 报告、提交 ID、文件清单、差异哈希和已记录的命令证据。接收 Agent 先进行只读确认，必须明确返回 `Handoff: ACCEPT` 才会转移任务所有权。随后接收方检查并继续任务。AgentDU 会执行每一条 `--check` 命令，记录输出哈希和结果，验证接收方 worktree 干净后，将已验证提交快进合并到原分支。双方解析出的 endpoint 和模型组合必须不同。目前仅支持 OpenAI 兼容 Chat Completions。

交接记录位于 `.agentdu/handoffs/`。为审计保留的 worktree 和分支位于 Git common directory 下的 `agentdu-worktrees/`。`--list-handoffs` 显示已保存状态。`--abandon-handoff ID` 会在仓库锁保护下放弃未完成的交接。若进程中断发生在验证和集成之间，后续运行会核对并恢复已验证的交接。其他非终态交接会阻止新交接，直到检查或放弃。失败、拒绝和验证失败的结果仍会隔离保存，供审计。

Git 协作要求工作区干净且已有提交。交接期间若其他进程修改了原分支，结果会变为过期。Git worktree 可以隔离 AgentDU 文件工具的常规写入，但 Shell 命令仍以当前用户身份运行，能够访问或修改其他 worktree 以及项目外部路径，因此它不是安全边界。

## 本地验证

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m agentdu --help
python -m agentdu.evaluation --help
```

配对评测命令使用分别准备的单模型和协作模式工作区。每一对工作区必须是初始 Git tree 相同的干净仓库。验收命令使用 argv 数组执行，不经过 Shell。评测结果和配对指标差异以 JSONL 写入。

```json
[
  {
    "id": "fix-regression",
    "profile": "main",
    "prompt": "修复失败的回归测试并说明改动",
    "solo_workspace": "C:/eval/fix-regression-solo",
    "collaborative_workspace": "C:/eval/fix-regression-collaborative",
    "checks": [["python", "-m", "unittest", "discover", "-s", "tests"]]
  }
]
```

运行 `agentdu-evaluate --cases cases.json --config config.toml --output results.jsonl`。两种模式都会使用 `--yes`，允许 Agent 完成任务。输出包含耗时、模型请求数、Token 用量、可用时的费用估算、验收结果和成功状态。没有代表性测试集和真实提供商运行时，这些记录不能支持质量或性能结论。

测试使用本地 HTTP endpoint、真实文件系统、子进程和附带的 Git。编排集成测试会在临时仓库中实际执行两个并行实现 Agent、主 Agent 修改、验收检查、候选合并和 Git 集成。额外测试覆盖咨询、请求预算、失败验收回滚和合并冲突。这些测试验证本地协作协议，不代表外部模型提供商的实时兼容性、质量、延迟或成本。当前没有完成与 `--solo` 基线的真实多模型对照。

模块契约见 [架构说明](docs/architecture.md)，产品边界、验收缺口和后续路线见 [V0.6 中文状态报告](docs/v0.6-status.zh-CN.md)。

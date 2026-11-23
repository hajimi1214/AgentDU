# AgentDU

AgentDU V0.6 development builds on the single-model programming runtime and smart autonomous multi-model collaboration with task recovery, token and cost telemetry, MCP stdio tools, progressive Agent Skills, and explicitly enabled Python extensions. The V0.6 milestone has not passed product acceptance. See [docs/v0.6-status.md](docs/v0.6-status.md) for verified capabilities and remaining requirements.

## Requirements

- Python 3.12 or newer
- An OpenAI-compatible Chat Completions endpoint with function/tool calling
- Git on `PATH` or AgentDU's bundled Git for Git status, diff, and collaboration worktrees

The Python runtime has no third-party dependencies. `setuptools` is used only when building or installing the package.

## Configure a model

Set credentials and run directly from the source tree in PowerShell:

```powershell
$env:PYTHONPATH = "src"
$env:AGENTDU_API_KEY = "your-api-key"
$env:AGENTDU_MODEL = "your-model"
$env:AGENTDU_BASE_URL = "https://api.openai.com/v1"
python -m agentdu --workspace . "Inspect the project and summarize its structure"
```

Profiles can be defined in `%APPDATA%\AgentDU\config.toml` or supplied with `--config`:

```toml
default_profile = "openai"

[profiles.openai]
provider = "openai-compatible"
base_url = "https://api.openai.com/v1"
model = "your-model"
api_key_env = "OPENAI_API_KEY"
timeout = 120
temperature = 0
streaming = true
capabilities = ["implementation", "code review"]
stream_usage = true
input_cost_per_million = 2.0
output_cost_per_million = 8.0
```

Select a profile with `--profile openai`. `--model` and `--base-url` override profile values. Store API keys in a named environment variable configured by `api_key_env`, or use `AGENTDU_API_KEY` or `OPENAI_API_KEY`. The CLI does not accept raw API keys as command-line arguments. The supported provider protocol is OpenAI-compatible Chat Completions, including streamed tool-call deltas. Native provider protocols and their model-specific features are not implemented.

Token usage is recorded when the provider returns usage data. Cost is estimated only when both token counts and both configured per-million-token prices are available. Prices are user-maintained estimates, not billing data from the provider.

## Use AgentDU

```powershell
python -m agentdu --workspace C:\path\to\project --interactive
python -m agentdu --workspace C:\path\to\project --session "your-session-id" "Continue the previous task"
python -m agentdu --workspace C:\path\to\project --list-sessions
```

An interactive session accepts multiple prompts and exits with `/exit` or `/quit`. A session ID is printed when a run finishes. Session JSONL event logs are stored under `.agentdu/sessions/` in the workspace and retain the full transcript, including turns omitted from a model request to fit the configured context budget.

Built-in tools cover file listing and search, line-numbered reads, unique-block edits, atomic single-file writes, shell commands, and Git status/diff/log. Reads are allowed by default. Writes and shell commands require confirmation in an interactive terminal. `--yes` approves all writes and shell commands for the run. Shell commands execute as the current user. The workspace is their working directory, but it is not an operating-system sandbox and commands can access resources outside it.

## MCP, Skills, and Plugins

MCP stdio servers are configured in the model TOML file. The server working directory must remain inside the workspace, and configured environment values are copied from named environment variables.

```toml
[mcp.servers.repo]
command = "python"
args = ["-m", "my_mcp_server"]
cwd = "."
timeout = 60

[mcp.servers.repo.env]
ACCESS_TOKEN = "MCP_SERVER_TOKEN"
```

AgentDU asks before starting each configured server and before each MCP tool call. `--yes` approves these operations along with other execution and write permissions. The current client supports stdio transport and MCP tools. It omits image and audio tool-result payloads and does not implement remote Streamable HTTP, resources, prompts, or server sampling.

Skills in `.agents/skills/<name>/SKILL.md` are discovered automatically. Additional directories can be supplied with repeated `--skills-dir` options. The agent first sees skill names and descriptions, then loads instructions and text resources as needed. Frontmatter supports the required `name` and `description` fields as plain or quoted scalars and block descriptions. It is a constrained parser, not a complete YAML implementation.

Installed Python extensions use the `agentdu.extensions` entry-point group and must declare `api_version = 1`, a name, `register_tools(registry)`, and `on_event(event)`. An entry point may target an extension class, instance, or zero-argument factory. Enable one explicitly with `--plugin name`. AgentDU asks for execution permission before importing it. A plugin runs arbitrary Python code in the AgentDU process with the current user's permissions.

## Smart Autonomous Mode

With the default mode, AgentDU exposes collaboration tools when the configuration contains another profile whose model and endpoint differ from the main model. The main agent decides whether to work alone, consult a model, request a read-only review, or delegate an implementation task. Profile `capabilities` are selection hints supplied to the main model, not measured model guarantees. Use `--solo` to disable collaboration for a single-model baseline.

```powershell
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile main "Fix the failing tests"
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile main --solo "Fix the failing tests"
```

The scheduler defaults to two concurrent agents, eight tasks per user session, 80 collaborator model requests total, and 20 requests per task. Override those limits with `--collaboration-concurrency`, `--collaboration-max-tasks`, `--collaboration-max-model-calls`, and `--collaboration-task-model-calls`. These limits count provider requests, including retries. Configured prices provide post-run estimates, not a hard USD spending limit. The main agent has its separate `--max-steps` limit.

Implementation and review tasks require a clean Git worktree with an initial commit when they are dispatched. Each starts from that commit in an isolated Git worktree and a private model session. The main agent can continue editing after dispatch. Before integration, the main worktree must be clean, so the main agent needs to commit its own work first. Integration merges into a candidate worktree, runs the main agent's explicit acceptance commands there, and advances the original branch only after the merge and checks succeed. Merge conflicts and failed checks leave the main worktree unchanged. Dependency tasks wait for their prerequisites to complete and implementation results to be integrated.

Consultations receive only the question and context provided by the main agent and have no tools. Reviews use a read-only Git worktree. Implementation agents use the configured workspace permission policy and cannot create more agents. Task state and bounded progress history are stored under `.agentdu/agents/<session-id>/tasks.json`. Git worktrees are retained for audit. Shell commands still run as the current user, so a Git worktree is not an OS security sandbox.

## Model handoff

Define two named model profiles in the config file. The example uses separate environment variables for provider credentials:

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

Start a handoff from a clean Git worktree with an initial commit:

```powershell
python -m agentdu --workspace C:\path\to\project --config C:\path\to\config.toml --profile builder --handoff-profile reviewer --check "python -m unittest discover -s tests -v" --yes "Fix the failing tests and explain the change"
```

The source and receiving agents use separate Git worktrees, sessions, and model contexts. AgentDU passes the receiver a bounded handoff package containing the task, source report, commit IDs, changed-file list, diff hash, and recorded command evidence. The receiver gets a read-only acknowledgement phase and must explicitly return `Handoff: ACCEPT` before ownership transfers. It then inspects and continues the task, and reports acceptance. AgentDU runs every `--check` command, records the output hash and result, verifies that the receiver worktree remains clean, and fast-forwards the verified commit into the original branch. The resolved endpoint-and-model pairs must differ. Only OpenAI-compatible Chat Completions is supported.

Handoff records live under `.agentdu/handoffs/`, and worktrees and branches are retained for audit under the repository's Git common directory in `agentdu-worktrees/`. `--list-handoffs` shows persisted states. `--abandon-handoff ID` abandons an incomplete handoff under the repository lock. A later run reconciles a verified handoff left between verification and integration after a process interruption. A crash that leaves a nonterminal handoff blocks another handoff until the record is inspected or abandoned. Failed, rejected, and validation-failed results remain isolated and available for audit.

Git collaboration requires a clean worktree and an existing commit. During a handoff, external changes to the original branch make the result stale. Git worktrees isolate normal AgentDU file-tool writes, while shell commands still run as the current user and are not OS-sandboxed. Shell commands can access or modify other worktrees and paths outside the project, so the worktree boundary does not provide a security boundary.

## Verify

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m agentdu --help
python -m agentdu.evaluation --help
```

The paired evaluation command takes JSON cases with separate solo and collaborative workspaces. Each pair must be a clean Git repository with the same initial tree. Acceptance checks are argv arrays and run without a shell. The command writes run-level metrics and paired deltas as JSONL.

```json
[
  {
    "id": "fix-regression",
    "profile": "main",
    "prompt": "Fix the failing regression test and explain the change",
    "solo_workspace": "C:/eval/fix-regression-solo",
    "collaborative_workspace": "C:/eval/fix-regression-collaborative",
    "checks": [["python", "-m", "unittest", "discover", "-s", "tests"]]
  }
]
```

Run `agentdu-evaluate --cases cases.json --config config.toml --output results.jsonl`. Both runs use `--yes` so the agent can perform the task. The output records elapsed time, model requests, token usage, estimated cost when available, check results, and pass/fail status. This harness does not establish quality or performance claims without representative cases and live provider runs.

Tests use local HTTP endpoints and real filesystem, subprocess, and bundled Git operations. The orchestration integration test drives two parallel implementation agents and a dependent read-only reviewer through actual file writes, main-agent work, acceptance checks, candidate merges, and Git integration in a temporary repository. Additional tests cover consultation, provider-call budgets, failed acceptance rollback, and merge conflicts. These fixtures validate the orchestration protocol, not live compatibility, quality, latency, or cost across external model providers. No live multi-model comparison with the `--solo` baseline has been run in this environment.

See [docs/architecture.md](docs/architecture.md) for module contracts and [docs/v0.6-status.md](docs/v0.6-status.md) for the current reliability boundary, acceptance gaps, and upgrade route.

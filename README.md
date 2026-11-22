# AgentDU

AgentDU V0.4 is an independently implemented coding agent with a serial, verified two-model handoff. It can inspect a workspace, search and edit text files, run approved shell commands, inspect Git changes, persist conversations, and stream responses from OpenAI-compatible Chat Completions endpoints.

## Requirements

- Python 3.12 or newer
- An OpenAI-compatible Chat Completions endpoint with function/tool calling
- Git on `PATH` for Git status, diff, and log tools

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
```

Select a profile with `--profile openai`. `--model`, `--base-url`, and `--api-key` override profile values. API keys can be read from a named environment variable or supplied at invocation time. The supported provider protocol is OpenAI-compatible Chat Completions, including streamed tool-call deltas. Native provider protocols and their model-specific features are not implemented.

## Use AgentDU

```powershell
python -m agentdu --workspace C:\path\to\project --interactive
python -m agentdu --workspace C:\path\to\project --session "your-session-id" "Continue the previous task"
python -m agentdu --workspace C:\path\to\project --list-sessions
```

An interactive session accepts multiple prompts and exits with `/exit` or `/quit`. A session ID is printed when a run finishes. Session JSONL event logs are stored under `.agentdu/sessions/` in the workspace and retain the full transcript, including turns omitted from a model request to fit the configured context budget.

Built-in tools cover file listing and search, line-numbered reads, unique-block edits, atomic single-file writes, shell commands, and Git status/diff/log. Reads are allowed by default. Writes and shell commands require confirmation in an interactive terminal. `--yes` approves all writes and shell commands for the run. Shell commands execute as the current user. The workspace is their working directory, but it is not an operating-system sandbox and commands can access resources outside it.

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
```

Tests use local HTTP endpoints and real filesystem, subprocess, and bundled Git operations. The handoff integration test drives two distinct local OpenAI-compatible model fixtures through a real file edit, test execution, receiver inspection, and Git fast-forward in a temporary repository. This validates the handoff protocol, not live compatibility with two external model providers.

See [docs/architecture.md](docs/architecture.md) for module contracts, source-grounded decisions, runtime guarantees, and known limitations.

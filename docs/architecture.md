# AgentDU V0.3 Architecture

## Runtime Shape

```mermaid
flowchart TD
    CLI[CLI] --> Runtime[AgentRuntime]
    Runtime --> Context[ContextManager]
    Runtime --> Model[ChatModel]
    Settings[Model profiles] --> Provider[OpenAI-compatible adapter]
    Model --> Provider
    Runtime --> Tools[ToolRegistry]
    Tools --> Policy[PermissionPolicy]
    Runtime --> Events[EventBus]
    Runtime --> Store[JSONL SessionStore]
    Extensions[Extensions] --> Tools
    Extensions --> Events
```

`AgentRuntime.run(prompt, session_id)` is the execution entry point. It takes both an in-process lock and a per-session cross-process file lock, restores the transcript, closes tool calls left pending by a previous process without replaying them, appends the user message, selects a bounded request context, and calls one `ChatModel`. Assistant tool calls are persisted before serial execution. Each result is persisted before the next model request. The loop ends when the model returns without tool calls, returns a truncated response, fails, or reaches the configured step limit.

## Contracts

| Module | Contract |
| --- | --- |
| `models.py` | `ChatModel.complete(messages, tools)` returns a normalized `AssistantMessage`. Streaming providers emit `ModelStreamEvent` values. Provider-specific request and response types stay outside the loop. |
| `provider.py` | `OpenAICompatibleModel` maps the normalized contract to Chat Completions JSON or SSE, reassembles streamed tool calls, and classifies transient HTTP and transport errors for bounded retry. It uses Python's standard-library HTTP client. |
| `settings.py` | TOML profiles select an OpenAI-compatible base URL, model, environment variable for an API key, timeout, temperature, and streaming mode. CLI flags and environment variables provide overrides. |
| `tools.py` | `ToolRegistry` publishes JSON Schema function definitions, validates the supported schema subset, requests permission, and executes registered handlers. Built-ins list, search, read, and edit workspace files, write one file atomically, run shell commands, and inspect Git status/diff/log. |
| `permissions.py` | `PermissionPolicy.authorize(request)` is the policy boundary. The CLI allows reads and asks before writes or execution. `--yes` approves all built-in writes and shell commands for the run. |
| `sessions.py` | Each session is an append-only JSONL event file with contiguous sequence numbers. A per-session OS file lock serializes writers across processes. Incomplete final records are repaired before the next append. |
| `events.py` | Lifecycle events are persisted once and then delivered to extension observers. Streaming text deltas are delivered to observers but are not stored as separate events. Observer exceptions are logged and do not stop the agent run. |
| `context.py` | A conservative character estimate selects complete recent turns and accounts for tool schemas while retaining system instructions and matched tool-call/result groups. The canonical transcript remains intact. |
| `extensions.py` | An extension registers tools and subscribes to lifecycle events. Dynamic module loading and third-party code installation are outside this phase. |
| `collaboration.py` | `HandoffCoordinator` runs the source and receiving runtimes serially with separate profiles, sessions, and Git worktrees. It packages bounded task and evidence metadata, verifies the received commit, runs explicit acceptance commands, and integrates only a verified result. |
| `handoff.py` | `HandoffLedger` persists versioned state transitions and execution evidence as JSONL events. Expected versions and owners are checked before transitions. |
| `git_workspaces.py` | `GitWorkspaces` requires a clean initial baseline, creates separate source and receiver worktrees, hashes commit diffs, and fast-forwards a verified result only while the original baseline remains unchanged. A repository-level process lock serializes handoffs and CLI abandonment. |

Each `AgentRuntime` has one model. V0.4 creates two independent runtimes in sequence. The source agent's conversation remains in its private session. The receiver sees the original task and a bounded package containing the source report, source and base commit IDs, changed files, a SHA-256 digest of the source diff, command evidence, and explicit acceptance commands. The user-facing coordinator session records state changes and final reports without concatenating private transcripts.

The handoff state machine is `source_running -> offered -> accepted -> receiver_running -> verified -> integrated`, with `offered -> rejected` available when the receiving model declines or fails to acknowledge. The source owns the record until it offers its result. The receiver must explicitly return `Handoff: ACCEPT` during a read-only AgentRuntime turn before ownership transfers. The coordinator owns stale, abandoned, failed, rejected, and integrated terminal states. Every transition checks the expected state version and current owner. Evidence events are tied to the current owner and version. At least one explicit `--check` command is required, each must succeed, and any remaining change in the receiver worktree prevents integration.

Handoff starts from a clean Git worktree with an initial commit. Agents work in distinct Git worktrees. AgentDU hashes and rechecks each commit snapshot, and the coordinator fast-forwards only if the original branch still points at the baseline and remains clean. A persisted verified result is reconciled on the next run if a process stopped after Git integration but before the terminal ledger event. Incomplete non-verified records block another handoff until explicitly abandoned.

## Decisions And Evidence

The research library records source snapshots downloaded on 2026-11-22. Commit identifiers below come from the original ZIP comment or tar archive metadata. Paths are relative to `agent-learning-library/sources/`.

| Project and snapshot | Local source evidence | V0.2 decision |
| --- | --- | --- |
| Pi `9c4652688590abb836b40ebd1e4725ecf9a7c7d1` | `pi/packages/agent/src/agent-loop.ts` `runLoop`, `streamAssistantResponse`, and `executeToolCallsParallel`; tool schema validation and before/after hooks are in the same file. | Keep model I/O behind a contract and persist tool-call/result order. V0.2 executes calls serially while recovery semantics are established. |
| Codex CLI `1476a08f1b410e359e64becf2494bc248743e396` | `codex/codex-rs/core/src/exec_policy.rs` approval and sandbox policy; `codex/codex-rs/core/src/apply_patch.rs` delegates patch approval to tool runtime. | Treat approval as policy input to tool execution. AgentDU currently has path confinement and human approval, without an OS sandbox. |
| DeepSeek Harness `d98f5d68519215da48f3002da2cb9c9d2068d81d` | `deepseek-harness/packages/core/agent-loop/src/agent.ts` streams and settles assistant attempts around tool calls; `packages/session/session-persistence-jsonl/src/index.ts` uses write leases for session ownership. | Use an append-only event transcript, a per-session writer lock, and interruption results that do not replay uncertain actions. |
| OpenCode `5310e42203af41c3452ee44b24fc7d0c52aaa3cc` | `opencode/packages/core/src/session/runner/step.ts` consumes provider streams, schedules tools, and settles cancellation; `permission.ts` evaluates allow/ask/deny rules. | Keep permission decisions in a distinct policy module and stream assistant deltas. Provider cancellation before response headers and OS-level command isolation remain limitations. |
| Aider `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | `aider/aider/coders/base_coder.py` `check_tokens`, `send_message`, retry, and history summarization; `coders/editblock_coder.py` applies matched edits and can report partial success. | Preserve the full canonical transcript while bounding request context, retry transient provider failures, and use exact-match edits. Semantic summarization, Git commits, and transactional multi-file edits are not implemented. |
| Cline `8075d2e35cf5cad67c24f180c80a05d311b74a3e` | `cline/sdk/packages/agents/src/agent-runtime.ts` `run`, `executeToolCalls`, and `prepareToolExecution`; absent approval callback is an explicit denial. `sdk/ARCHITECTURE.md` assigns compaction to core. | Separate runtime, provider, tools, and context ownership. Require a live approval callback for interactive writes and command execution. |
| Goose `36b29113aff691e1a46a21a5c25900824caf9949` | `goose/crates/goose/src/agents/state_machine/session.rs` runs the state machine and tool hooks; `state_machine/ops_toolcalling.rs` owns tool-call operations. | Keep extension registration and event observers outside model transport. Full state-machine scheduling is not needed for the single-agent baseline. |
| OpenHands `7eb410802717996cf568deb67faee224863219ec` | `openhands/src/api/` is the Canvas API snapshot; the companion SDK `software-agent-sdk/openhands-sdk/openhands/sdk/conversation/event_store.py` `EventLog` indexes persistent events, and `conversation/impl/local_conversation.py` exposes run/message lifecycle. SDK snapshot `74f198df3650da6399c6a0a2dff6272a667bbace`. | Keep a durable event log as the session source of truth. AgentDU uses a smaller JSONL projection and does not copy OpenHands code. |

These are source-level design references, not claims that the upstream projects were executed in this environment. The reference projects are not runtime dependencies, and no third-party source code was copied into AgentDU.

## Runtime And Persistence Guarantees

- Assistant tool-call messages are persisted before any tool starts. Each tool result is persisted before the next model request.
- If the process stops after a tool call was recorded but before its result, resume appends an interruption result that tells the model the operation may have completed. AgentDU does not replay that action automatically.
- Same-session runs from `AgentRuntime` instances and CLI processes are serialized. Sequence numbers are refreshed after acquiring the process lock.
- Event observers run in registration order. An observer failure is logged and isolated from the execution loop.
- Tool outputs are limited to 16,000 characters. The context selector estimates ASCII as four JSON characters per token and each non-ASCII character as three tokens, counts the serialized tool schemas, and never splits a retained user turn or tool exchange. This is a conservative heuristic, not provider-tokenizer accounting.

## Known Boundaries

- The provider adapter supports only the OpenAI-compatible Chat Completions tool-calling shape. Tests use local HTTP fixtures. Compatibility with live OpenAI-compatible providers, their rate limits, and model-specific tool-call behavior has not been verified against external credentials.
- Context management drops old whole turns or complete assistant/tool exchanges and inserts an omission notice. It does not generate semantic summaries or use provider tokenizers. A latest turn or tool schema too large for the configured budget fails explicitly.
- The shell tool is not an OS sandbox. A command runs with the user's permissions and can leave the workspace after the user approves it. Path checks constrain file tools only.
- A crash during external command execution leaves its final effect uncertain. The session records that uncertainty and will not replay the call automatically.
- File writes use atomic replacement for one file. There is no multi-file transaction, diff review UI, Git checkpoint, or automatic validation stage. The agent must use command and Git tools to run tests and inspect changes.
- A shell command that exits after starting a child which keeps stdout open triggers process-tree cleanup and a warning. On Windows, descendants may survive if the shell already exited before cleanup could reach them.
- Streaming cancellation closes an established HTTP response. Cancellation while waiting for connection setup or response headers can wait for the configured network timeout. Approved tool execution is allowed to settle before session locks are released.
- V0.4 handoff is a fixed two-role serial workflow. It does not route among arbitrary model pools, schedule parallel agents, resume an interrupted model turn, or make autonomous delegation decisions.
- A Git worktree is a version-control workspace boundary, not an OS security boundary. An approved shell command can modify another worktree, the original checkout, Git metadata, or files outside the project. Concurrent or malicious out-of-worktree shell writes cannot be reliably detected by the current baseline checks.
- Multi-model mode requires an initial Git commit and a clean original worktree. It leaves handoff branches and worktrees for audit and does not automatically prune them.
- Handoff acceptance text is a model claim. AgentDU independently verifies commit lineage, diff hashes, clean state, and explicit shell acceptance commands, but it cannot prove that the receiver's qualitative code review is correct.
- Provider tests use local deterministic endpoints. No two live external providers or models have been validated with credentials in this environment.
- MCP, Skills discovery, dynamic extension loading, remote execution, and autonomous model routing are not implemented.
- Package installation uses `setuptools` as a build dependency. The source-tree CLI and tests run with Python's standard library.

## V0.4 Completion Assessment

The handoff protocol, separate model contexts, versioned record, Git snapshot verification, receiver acceptance, explicit command checks, and fast-forward integration are implemented and covered by local endpoint integration tests. V0.4 is ready for architecture review and further provider trials. It does not meet a strict security-isolation requirement for model-issued shell commands, and actual multi-provider model relay remains unverified until credentials and provider endpoints are configured. Those two conditions must be resolved or explicitly accepted before claiming complete real-world multi-model reliability.

The full V0.1 project-by-project reports were not present in the repository when V0.2 work began. This decision record captures source locations checked for the choices above; it does not mark the requested V0.1 research package complete.

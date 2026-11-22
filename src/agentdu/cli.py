from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from pathlib import Path

from .agent import AgentRuntime, build_default_runtime
from .collaboration import HandoffCoordinator
from .events import AgentEvent
from .git_workspaces import GitWorkspaceError, GitWorkspaces
from .handoff import HandoffLedger
from .permissions import ApproveWorkspaceActionsPolicy, PermissionRequest
from .orchestration import AutonomousCoordinator
from .provider import OpenAICompatibleModel
from .sessions import SessionStore
from .settings import ModelSettings, default_config_path, resolve_model_options


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentdu",
        description="Run the AgentDU coding agent in smart autonomous collaboration mode",
    )
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session", help="Resume a session by id")
    parser.add_argument("--list-sessions", action="store_true", help="List saved sessions in the workspace")
    parser.add_argument("--list-handoffs", action="store_true", help="List persisted multi-model handoffs")
    parser.add_argument("--abandon-handoff", help="Mark an incomplete handoff as abandoned")
    parser.add_argument("--interactive", action="store_true", help="Open a multi-turn coding session")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--profile", default=os.environ.get("AGENTDU_PROFILE"))
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENTDU_MODEL", os.environ.get("OPENAI_MODEL")),
        help="Override the model from the selected profile",
    )
    parser.add_argument(
        "--base-url",
        help="Override the selected profile's OpenAI-compatible API URL",
    )
    parser.add_argument("--api-key", help="Override the selected profile's API key")
    parser.add_argument("--handoff-profile", help="Run a second model as the handoff receiver")
    parser.add_argument(
        "--solo",
        action="store_true",
        help="Disable autonomous model collaboration for a single-model baseline",
    )
    parser.add_argument("--collaboration-concurrency", type=int, default=2)
    parser.add_argument("--collaboration-max-tasks", type=int, default=8)
    parser.add_argument("--collaboration-max-model-calls", type=int, default=80)
    parser.add_argument("--collaboration-task-model-calls", type=int, default=20)
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        help="Acceptance command to run in the receiver worktree. Repeat for multiple checks.",
    )
    parser.add_argument("--context-tokens", type=int, default=24_000)
    parser.add_argument("--max-steps", type=int, default=40)
    streaming = parser.add_mutually_exclusive_group()
    streaming.add_argument("--stream", dest="streaming", action="store_true")
    streaming.add_argument("--no-stream", dest="streaming", action="store_false")
    parser.set_defaults(streaming=None)
    parser.add_argument("--yes", action="store_true", help="Approve workspace writes and shell commands")
    return parser


def main() -> int:
    args = _parser().parse_args()
    interactive = args.interactive or (not args.prompt and sys.stdin.isatty())
    listing_or_admin = args.list_sessions or args.list_handoffs or args.abandon_handoff
    if not args.prompt and not interactive and not listing_or_admin:
        _parser().error("prompt is required when stdin is not interactive")
    if args.handoff_profile and (interactive or not args.prompt):
        _parser().error("--handoff-profile requires a single prompt and cannot be combined with --interactive")
    if args.handoff_profile and args.solo:
        _parser().error("--solo cannot be combined with --handoff-profile")
    if args.check and not args.handoff_profile:
        _parser().error("--check requires --handoff-profile")
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        _parser().error(f"workspace does not exist: {workspace}")
    if args.list_sessions:
        sessions = SessionStore(workspace / ".agentdu").list_sessions()
        for session in sessions:
            if "error" in session:
                print(f"{session['session_id']}\tERROR\t{session['error']}")
            else:
                print(
                    f"{session['session_id']}\t{session['modified_at']}\t"
                    f"{session['last_prompt']}"
                )
        return 0
    if args.list_handoffs:
        try:
            handoff_root = GitWorkspaces(workspace).root
        except GitWorkspaceError:
            handoff_root = workspace
        ledger = HandoffLedger(handoff_root / ".agentdu" / "handoffs")
        for handoff in ledger.list_states():
            print(
                f"{handoff.handoff_id}\t{handoff.status}\t{handoff.owner}\t"
                f"{handoff.source_profile} -> {handoff.receiver_profile}"
            )
        return 0
    if args.abandon_handoff:
        try:
            workspaces = GitWorkspaces(workspace)
        except GitWorkspaceError as error:
            print(f"AgentDU error: {error}", file=sys.stderr)
            return 1
        lock = workspaces.acquire_handoff_lock()
        try:
            ledger = HandoffLedger(workspaces.root / ".agentdu" / "handoffs")
            handoff = ledger.abandon(
                args.abandon_handoff, "abandoned from the AgentDU CLI"
            )
            print(f"{handoff.handoff_id}\t{handoff.status}")
            return 0
        except Exception as error:
            print(f"AgentDU error: {error}", file=sys.stderr)
            return 1
        finally:
            lock.release()
    policy = ApproveWorkspaceActionsPolicy() if args.yes else _InteractivePolicy()
    coordinator: AutonomousCoordinator | None = None
    try:
        settings = ModelSettings.load(args.config)
        if args.handoff_profile:
            source_profile = args.profile or settings.default_profile
            if not source_profile:
                raise ValueError("Set --profile or default_profile when using --handoff-profile")
            coordinator = HandoffCoordinator(
                workspace,
                settings,
                policy,
                context_tokens=args.context_tokens,
                max_steps=args.max_steps,
                event_handler=_terminal_output,
            )
            outcome = asyncio.run(
                coordinator.run(
                    args.prompt,
                    source_profile,
                    args.handoff_profile,
                    args.check,
                    args.session,
                )
            )
            print(outcome.report)
            session_id = outcome.coordinator_session_id
        else:
            options = resolve_model_options(
                settings,
                args.profile,
                args.model,
                args.base_url,
                args.api_key,
                args.streaming,
            )
            model = OpenAICompatibleModel(**options)
            runtime = build_default_runtime(
                workspace=workspace,
                model=model,
                permission_policy=policy,
                context_tokens=args.context_tokens,
                max_steps=args.max_steps,
            )
            if not args.solo:
                candidate_coordinator = AutonomousCoordinator(
                    workspace,
                    settings,
                    options,
                    policy,
                    context_tokens=args.context_tokens,
                    max_steps=args.max_steps,
                    max_concurrency=args.collaboration_concurrency,
                    max_tasks=args.collaboration_max_tasks,
                    total_model_calls=args.collaboration_max_model_calls,
                    task_model_calls=args.collaboration_task_model_calls,
                )
                if candidate_coordinator.available:
                    candidate_coordinator.install(runtime)
                    coordinator = candidate_coordinator
                else:
                    candidate_coordinator.close(wait=False)
            runtime.event_bus.subscribe(_terminal_output)
            session_id = asyncio.run(
                _run_with_coordinator(
                    runtime,
                    args.prompt,
                    args.session,
                    interactive,
                    coordinator,
                )
            )
    except KeyboardInterrupt:
        print("AgentDU interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"AgentDU error: {error}", file=sys.stderr)
        return 1
    if session_id:
        print(f"Session: {session_id}", file=sys.stderr)
    return 0


async def _run_prompts(
    runtime: AgentRuntime,
    prompt: str | None,
    session_id: str | None,
    interactive: bool,
) -> str | None:
    if not interactive:
        result = await runtime.run(prompt, session_id)
        return result.session_id
    current_session = session_id
    while True:
        try:
            current_prompt = await asyncio.to_thread(input, "agentdu> ")
        except EOFError:
            break
        if current_prompt.strip().lower() in {"/exit", "/quit"}:
            break
        if not current_prompt.strip():
            continue
        result = await runtime.run(current_prompt, current_session)
        current_session = result.session_id
    return current_session


async def _run_with_coordinator(
    runtime: AgentRuntime,
    prompt: str | None,
    session_id: str | None,
    interactive: bool,
    coordinator: AutonomousCoordinator | None,
) -> str | None:
    session_id = await _run_prompts(runtime, prompt, session_id, interactive)
    if coordinator is None or session_id is None:
        return session_id
    tasks = await asyncio.to_thread(coordinator.finish_session, session_id)
    pending = [
        task
        for task in tasks
        if task.get("status") in {"failed", "blocked", "interrupted"}
        or (task.get("kind") == "implementation" and task.get("status") == "completed")
    ]
    if pending:
        details = ", ".join(f"{task['task_id']}={task['status']}" for task in pending)
        print(f"\n[AgentDU] Collaboration follow-up required: {details}", file=sys.stderr)
    return session_id


def _terminal_output(event: AgentEvent) -> None:
    if event.kind == "assistant_delta":
        sys.stdout.write(event.payload["text"])
        sys.stdout.flush()
    elif event.kind == "run_completed":
        print()
    elif event.kind == "collaboration_progress":
        payload = event.payload
        detail = payload.get("tool_name") or payload.get("event") or payload.get("kind")
        print(f"\n[AgentDU task {payload['task_id']}] {detail}")


class _InteractivePolicy:
    def __init__(self) -> None:
        self._approval_lock = threading.Lock()

    def authorize(self, request: PermissionRequest) -> bool:
        if request.permission == "read":
            return True
        with self._approval_lock:
            if not sys.stdin.isatty():
                print(
                    f"Denied {request.tool_name}: approval requires an interactive terminal (or --yes).",
                    file=sys.stderr,
                )
                return False
            detail = (
                request.arguments.get("command")
                or request.arguments.get("task")
                or request.arguments.get("question")
                or request.arguments.get("task_id", "")
            )
            answer = input(f"Allow {request.tool_name} {detail!r}? [y/N] ")
            return answer.strip().lower() in {"y", "yes"}


if __name__ == "__main__":
    raise SystemExit(main())

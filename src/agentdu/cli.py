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
from .mcp import MCPToolsExtension
from .permissions import ApproveWorkspaceActionsPolicy, PermissionRequest
from .orchestration import AutonomousCoordinator
from .plugins import load_plugins
from .provider import OpenAICompatibleModel
from .sessions import SessionStore
from .skills import SkillsExtension
from .settings import (
    ModelSettings,
    default_config_path,
    resolve_model_options,
    resolve_profile_options,
)


class _ChineseHelpFormatter(argparse.HelpFormatter):
    def _format_usage(
        self,
        usage: str | None,
        actions: list[argparse.Action],
        groups: list[argparse._MutuallyExclusiveGroup],
        prefix: str | None,
    ) -> str:
        rendered = super()._format_usage(usage, actions, groups, prefix)
        if prefix is None:
            rendered = rendered.replace("usage:", "用法：", 1)
            rendered = rendered.replace("用法： ", "用法：", 1)
        return rendered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentdu",
        description="以智能自主协作模式运行 AgentDU 编程 Agent",
        add_help=False,
        formatter_class=_ChineseHelpFormatter,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")
    parser.add_argument("prompt", nargs="?", help="交给 Agent 的任务")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="项目工作目录")
    parser.add_argument("--session", help="按会话 ID 恢复会话")
    parser.add_argument("--list-sessions", action="store_true", help="列出工作区中的已保存会话")
    parser.add_argument("--list-handoffs", action="store_true", help="列出已保存的多模型交接")
    parser.add_argument("--abandon-handoff", help="将未完成的交接标记为已放弃")
    parser.add_argument("--interactive", action="store_true", help="启动多轮编程会话")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="模型配置文件")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        action="append",
        default=[],
        help="添加包含 Agent Skills 的目录",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="加载一个已安装且明确指定的 AgentDU 插件",
    )
    parser.add_argument("--profile", default=os.environ.get("AGENTDU_PROFILE"), help="模型档案名称")
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENTDU_MODEL", os.environ.get("OPENAI_MODEL")),
        help="覆盖所选档案中的模型名称",
    )
    parser.add_argument(
        "--base-url",
        help="覆盖所选档案中的 OpenAI 兼容 API 地址",
    )
    parser.add_argument("--handoff-profile", help="指定接收交接任务的第二个模型档案")
    parser.add_argument(
        "--solo",
        action="store_true",
        help="关闭自主模型协作，运行单模型基线",
    )
    parser.add_argument("--collaboration-concurrency", type=int, default=2, help="协作任务最大并发数")
    parser.add_argument("--collaboration-max-tasks", type=int, default=8, help="每个会话的协作任务上限")
    parser.add_argument("--collaboration-max-model-calls", type=int, default=80, help="每个会话的协作模型请求上限")
    parser.add_argument("--collaboration-task-model-calls", type=int, default=20, help="每个协作任务的模型请求上限")
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        help="在接收方 worktree 中运行的验收命令，可重复指定",
    )
    parser.add_argument("--context-tokens", type=int, default=24_000, help="上下文 Token 预算")
    parser.add_argument("--max-steps", type=int, default=40, help="主 Agent 最大模型轮次")
    streaming = parser.add_mutually_exclusive_group()
    streaming.add_argument("--stream", dest="streaming", action="store_true", help="启用流式响应")
    streaming.add_argument("--no-stream", dest="streaming", action="store_false", help="关闭流式响应")
    parser.set_defaults(streaming=None)
    parser.add_argument("--yes", action="store_true", help="自动批准工作区写入和 Shell 命令")
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
    runtime: AgentRuntime | None = None
    try:
        settings = ModelSettings.load(args.config)
        if args.handoff_profile:
            source_profile = args.profile or settings.default_profile
            if not source_profile:
                raise ValueError("使用 --handoff-profile 时，请设置 --profile 或 default_profile")
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
                None,
                args.streaming,
            )
            profile_secrets = set()
            for name, profile in settings.profiles.items():
                if profile.model:
                    key = resolve_profile_options(settings, name).get("api_key")
                    if isinstance(key, str) and len(key) >= 4:
                        profile_secrets.add(key)
            if isinstance(options.get("api_key"), str) and len(options["api_key"]) >= 4:
                profile_secrets.add(options["api_key"])
            model = OpenAICompatibleModel(**options)
            skill_roots = [workspace / ".agents" / "skills"]
            skill_roots.extend(args.skills_dir)
            extensions = [SkillsExtension(tuple(skill_roots))]
            extensions.extend(load_plugins(tuple(args.plugin), policy, workspace))
            if settings.mcp_servers:
                extensions.append(
                    MCPToolsExtension(
                        settings.mcp_servers,
                        workspace,
                        policy,
                        tuple(profile_secrets),
                    )
                )
            runtime = build_default_runtime(
                workspace=workspace,
                model=model,
                permission_policy=policy,
                context_tokens=args.context_tokens,
                max_steps=args.max_steps,
                extensions=extensions,
                secret_values=profile_secrets,
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
        print("AgentDU 已中断", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"AgentDU 错误：{error}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.close()
        if isinstance(coordinator, AutonomousCoordinator):
            coordinator.close(wait=False)
    if session_id:
        print(f"会话 ID：{session_id}", file=sys.stderr)
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
            current_prompt = await asyncio.to_thread(input, "AgentDU> ")
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
        print(f"\n[AgentDU] 协作任务需要后续处理：{details}", file=sys.stderr)
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
        localized = {
            "task_started": "任务已启动",
            "task_progress": "任务进行中",
            "task_completed": "任务已完成",
            "task_failed": "任务失败",
            "task_interrupted": "任务已中断",
            "model_request": "正在请求模型",
            "model_usage": "已记录模型用量",
            "provider_retry": "正在重试模型请求",
            "provider_error": "模型请求出错",
        }
        detail = localized.get(detail, detail)
        print(f"\n[AgentDU 任务 {payload['task_id']}] {detail}")


class _InteractivePolicy:
    def __init__(self) -> None:
        self._approval_lock = threading.Lock()

    def authorize(self, request: PermissionRequest) -> bool:
        if request.permission == "read":
            return True
        with self._approval_lock:
            if not sys.stdin.isatty():
                print(
                    f"已拒绝 {request.tool_name}：授权需要交互式终端（或使用 --yes）。",
                    file=sys.stderr,
                )
                return False
            detail = (
                request.arguments.get("command")
                or request.arguments.get("task")
                or request.arguments.get("question")
                or request.arguments.get("task_id", "")
            )
            answer = input(f"允许 {request.tool_name} 执行 {detail!r} 吗？[y/N] ")
            return answer.strip().lower() in {"y", "yes"}


if __name__ == "__main__":
    raise SystemExit(main())

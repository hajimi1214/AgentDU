from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .context import ContextManager
from .events import AgentEvent, EventBus
from .extensions import Extension
from .models import AssistantMessage, ChatModel, Message, ToolCall
from .permissions import PermissionPolicy
from .provider import ProviderError
from .sessions import SessionStore
from .tools import ToolRegistry, create_builtin_tools


DEFAULT_SYSTEM_PROMPT = (
    "You are AgentDU, an AI coding agent operating in the provided workspace. "
    "Inspect the repository and Git status before changing files. Read applicable AGENTS.md "
    "instructions, search for related code and tests, make focused edits, run relevant tests, "
    "and inspect the final Git diff. Continue fixing failures using actual tool output until "
    "the task is complete. File writes and command execution may require user approval."
)


class MaxStepsExceeded(RuntimeError):
    pass


class TruncatedResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    session_id: str
    content: str
    model_steps: int


class AgentRuntime:
    """Single-model tool loop with durable messages and observable lifecycle events."""

    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        sessions: SessionStore,
        context: ContextManager | None = None,
        event_bus: EventBus | None = None,
        extensions: Sequence[Extension] = (),
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 40,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.model = model
        self.tools = tools
        self.sessions = sessions
        self.context = context or ContextManager()
        self.event_bus = event_bus or EventBus()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        for tool in create_builtin_tools():
            if tool.name not in {schema["function"]["name"] for schema in tools.model_schemas()}:
                tools.register(tool)
        for extension in extensions:
            extension.register_tools(tools)
            self.event_bus.subscribe(extension.on_event)

    async def run(self, prompt: str, session_id: str | None = None) -> RunResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if session_id is None:
            session_id = self.sessions.create()
        elif not self.sessions.exists(session_id):
            try:
                self.sessions.create(session_id)
            except FileExistsError:
                pass

        lock = self._session_lock(session_id)
        acquire_task = asyncio.create_task(asyncio.to_thread(lock.acquire))
        try:
            await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            await acquire_task
            lock.release()
            raise
        process_lock = None
        try:
            process_lock_task = asyncio.create_task(
                asyncio.to_thread(self.sessions.acquire_process_lock, session_id)
            )
            try:
                process_lock = await asyncio.shield(process_lock_task)
            except asyncio.CancelledError:
                process_lock = await process_lock_task
                process_lock.release()
                process_lock = None
                raise
            self.sessions.refresh_sequence(session_id)
            self.sessions.recover_interrupted_tool_calls(session_id)
            for call in self.sessions.unfinished_tool_calls(session_id):
                await self._emit(
                    session_id,
                    "tool_finished",
                    {
                        "tool_call_id": call["tool_call_id"],
                        "tool_name": call["tool_name"],
                        "is_error": True,
                        "recovered": True,
                        "result_recorded": call["result_recorded"],
                    },
                )
            messages = self.sessions.read_messages(session_id)
            if not any(message.get("role") == "system" for message in messages):
                self.sessions.append_message(
                    session_id, {"role": "system", "content": self.system_prompt}
                )
            self.sessions.append_message(session_id, {"role": "user", "content": prompt})
            await self._emit(session_id, "run_started", {"prompt_chars": len(prompt)})

            for step in range(1, self.max_steps + 1):
                messages = self.sessions.read_messages(session_id)
                request_messages = self.context.prepare(messages, self.tools.model_schemas())
                answer = await self._request_model(session_id, step, request_messages)

                assistant_message = answer.to_message()
                if answer.finish_reason == "length":
                    await self._emit(
                        session_id,
                        "run_stopped",
                        {"reason": "response_truncated", "step": step},
                    )
                    raise TruncatedResponseError(
                        "The model stopped at its output limit. Increase the model output limit or reduce the request."
                    )
                self.sessions.append_message(session_id, assistant_message)
                await self._emit(
                    session_id,
                    "assistant_message",
                    {"step": step, "tool_calls": len(answer.tool_calls)},
                )
                if not answer.tool_calls:
                    content = answer.content or ""
                    await self._emit(
                        session_id,
                        "run_completed",
                        {"step": step, "response_chars": len(content)},
                    )
                    return RunResult(session_id, content, step)

                for call in answer.tool_calls:
                    await self._emit(
                        session_id,
                        "tool_started",
                        {"tool_call_id": call.id, "tool_name": call.name},
                    )
                    execution = asyncio.create_task(self._execute_tool(session_id, call))
                    try:
                        await asyncio.shield(execution)
                    except asyncio.CancelledError:
                        await execution
                        raise

            await self._emit(session_id, "run_stopped", {"reason": "max_steps", "limit": self.max_steps})
            raise MaxStepsExceeded(f"Agent stopped after {self.max_steps} model steps")
        finally:
            if process_lock is not None:
                process_lock.release()
            lock.release()

    def _session_lock(self, session_id: str) -> threading.Lock:
        key = self.sessions.lock_key(session_id)
        with _SESSION_LOCKS_GUARD:
            lock = _SESSION_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _SESSION_LOCKS[key] = lock
            return lock

    async def _emit(self, session_id: str, kind: str, payload: dict[str, object]) -> None:
        event = AgentEvent.create(session_id, kind, payload)
        if kind != "assistant_delta":
            self.sessions.append(
                session_id,
                kind,
                {"occurred_at": event.occurred_at, **payload},
            )
        await self.event_bus.publish(event)

    async def _request_model(
        self, session_id: str, step: int, messages: Sequence[Message]
    ) -> AssistantMessage:
        retries = 0
        while True:
            try:
                return await self._request_model_once(session_id, step, messages)
            except ProviderError as error:
                partial_output = getattr(error, "partial_output", False)
                if not error.retryable or partial_output or retries >= 2:
                    await self._emit(
                        session_id,
                        "provider_error",
                        {"error_type": type(error).__name__, "message": str(error)},
                    )
                    raise
                delay = 0.5 * (2**retries)
                retries += 1
                await self._emit(
                    session_id,
                    "provider_retry",
                    {"attempt": retries, "delay_seconds": delay},
                )
                await asyncio.sleep(delay)
            except Exception as error:
                await self._emit(
                    session_id,
                    "provider_error",
                    {"error_type": type(error).__name__, "message": str(error)},
                )
                raise

    async def _request_model_once(
        self, session_id: str, step: int, messages: Sequence[Message]
    ) -> AssistantMessage:
        await self._emit(session_id, "model_request", {"step": step})
        stream = getattr(self.model, "stream", None)
        if callable(stream) and getattr(self.model, "streaming_enabled", True):
            content: list[str] = []
            answer: AssistantMessage | None = None
            try:
                async for event in stream(messages, self.tools.model_schemas()):
                    if event.content_delta:
                        content.append(event.content_delta)
                        await self._emit(
                            session_id,
                            "assistant_delta",
                            {"step": step, "text": event.content_delta},
                        )
                    if event.message is not None:
                        answer = event.message
            except ProviderError as error:
                error.partial_output = bool(content)
                raise
            if answer is None:
                raise ProviderError("Model stream ended without a final assistant message")
            if answer.content is None and content:
                answer = AssistantMessage(
                    content="".join(content),
                    tool_calls=answer.tool_calls,
                    finish_reason=answer.finish_reason,
                )
            return answer

        answer = await self.model.complete(messages, self.tools.model_schemas())
        if answer.content:
            await self._emit(
                session_id,
                "assistant_delta",
                {"step": step, "text": answer.content},
            )
        return answer

    async def _execute_tool(self, session_id: str, call: ToolCall) -> None:
        result = await self.tools.execute(call.name, call.arguments)
        self.sessions.append_message(
            session_id,
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result.text,
            },
        )
        await self._emit(
            session_id,
            "tool_finished",
            {
                "tool_call_id": call.id,
                "tool_name": call.name,
                "is_error": result.is_error,
                "output_chars": len(result.text),
            },
        )


_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def build_default_runtime(
    workspace: Path,
    model: ChatModel,
    permission_policy: PermissionPolicy,
    data_dir: Path | None = None,
    context_tokens: int = 24_000,
    max_steps: int = 40,
    extensions: Sequence[Extension] = (),
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> AgentRuntime:
    if not callable(getattr(permission_policy, "authorize", None)):
        raise TypeError("permission_policy must implement authorize(request)")
    workspace = workspace.resolve()
    registry = ToolRegistry(workspace, permission_policy)
    return AgentRuntime(
        model=model,
        tools=registry,
        sessions=SessionStore(data_dir or workspace / ".agentdu"),
        context=ContextManager(context_tokens),
        extensions=extensions,
        system_prompt=_workspace_system_prompt(workspace, system_prompt),
        max_steps=max_steps,
    )


def _workspace_system_prompt(workspace: Path, base_prompt: str) -> str:
    instruction_files = [parent / "AGENTS.md" for parent in reversed(workspace.parents)]
    instruction_files.append(workspace / "AGENTS.md")
    sections: list[str] = []
    remaining = 48_000
    for path in instruction_files:
        if not path.is_file() or remaining <= 0:
            continue
        try:
            with path.open("r", encoding="utf-8") as file:
                content = file.read(remaining + 1)
        except (OSError, UnicodeDecodeError):
            continue
        if len(content) > remaining:
            content = content[:remaining] + "\n[remaining instructions omitted]"
        remaining -= len(content)
        sections.append(f"Instructions from {path}:\n{content}")
    if not sections:
        return base_prompt
    return f"{base_prompt}\n\nFollow these workspace instructions:\n\n" + "\n\n".join(sections)

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import re
import sys
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .agent import AgentRuntime, RunResult, build_default_runtime
from .events import AgentEvent
from .models import AssistantMessage, Message, ModelStreamEvent, ToolSchema
from .permissions import PermissionRequest, ReadOnlyPolicy
from .provider import OpenAICompatibleModel
from .settings import ModelSettings, default_config_path, resolve_model_options


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
MAX_REMEMBERED_REQUESTS = 512
RUNTIME_VERSION = "0.6.0"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
LOG = logging.getLogger("agentdu.desktop_host")
_EOF = object()


class ProtocolError(ValueError):
    pass


@dataclass
class PendingApproval:
    task_id: str
    event: threading.Event
    decision: bool | None = None


@dataclass
class ActiveTask:
    task_id: str
    session_id: str
    source: str
    runtime: AgentRuntime
    task: asyncio.Task[None]


class DesktopPermissionPolicy:
    """Bridge bounded file-write approvals and keep Shell execution unavailable."""

    def __init__(self, host: "RuntimeHost", task_id: str) -> None:
        self.host = host
        self.task_id = task_id

    def authorize(self, request: PermissionRequest) -> bool:
        if request.permission == "read":
            return True
        if request.permission == "execute":
            self.host.publish_from_worker(
                "security.capability_blocked",
                {
                    "taskId": self.task_id,
                    "toolName": request.tool_name,
                    "reason": "Windows OS-level command isolation has not passed D1 validation.",
                },
            )
            return False
        if request.permission != "write":
            return False

        approval_id = str(uuid.uuid4())
        pending = PendingApproval(self.task_id, threading.Event())
        with self.host._approval_lock:
            self.host._pending_approvals[approval_id] = pending

        payload = {
            "approvalId": approval_id,
            "taskId": self.task_id,
            "toolName": request.tool_name,
            "permission": request.permission,
            "workspace": str(request.workspace),
            "arguments": _approval_summary(request.arguments),
        }
        self.host.publish_from_worker("permission.requested", payload)
        resolved = pending.event.wait(timeout=120)
        with self.host._approval_lock:
            self.host._pending_approvals.pop(approval_id, None)
        if not resolved:
            self.host.publish_from_worker(
                "permission.resolved",
                {"approvalId": approval_id, "taskId": self.task_id, "decision": "timeout"},
            )
            return False
        self.host.publish_from_worker(
            "permission.resolved",
            {
                "approvalId": approval_id,
                "taskId": self.task_id,
                "decision": "allow" if pending.decision else "deny",
            },
        )
        return pending.decision is True


class ControlledStreamingModel:
    """Deterministic IPC fixture. It never makes a provider request."""

    streaming_enabled = True

    def __init__(self, delay_seconds: float = 0.12) -> None:
        self.delay_seconds = delay_seconds

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> AssistantMessage:
        del messages, tools
        return AssistantMessage(content="受控测试响应")

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> AsyncIterator[ModelStreamEvent]:
        del messages, tools
        chunks = (
            "AgentDU Desktop D1 已通过 stdio JSONL 接收事件。",
            "\n这是一条受控 Runtime 测试任务，不会调用外部模型。",
            "\n流式文本来自现有 AgentRuntime 的事件总线。",
            "\n任务可以由桌面发送取消请求，并由 Python 侧实际中断。",
        )
        content: list[str] = []
        for chunk in chunks:
            await asyncio.sleep(self.delay_seconds)
            content.append(chunk)
            yield ModelStreamEvent(content_delta=chunk)
        yield ModelStreamEvent(
            message=AssistantMessage(content="".join(content), finish_reason="stop")
        )


class RuntimeHost:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.input_queue: asyncio.Queue[dict[str, Any] | ProtocolError | object] | None = None
        self.workspace: Path | None = None
        self.initialized = False
        self.sequence = 0
        self._output_lock = threading.Lock()
        self._approval_lock = threading.Lock()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._tasks: dict[str, ActiveTask] = {}
        self._request_cache: OrderedDict[
            str, tuple[str, str, dict[str, Any]]
        ] = OrderedDict()

    async def serve(self) -> int:
        self.loop = asyncio.get_running_loop()
        self.input_queue = asyncio.Queue()
        reader = threading.Thread(
            target=self._read_stdin,
            args=(self.loop, self.input_queue),
            name="agentdu-stdio-reader",
            daemon=True,
        )
        reader.start()
        LOG.info("Python Runtime Host started with protocol version %s", PROTOCOL_VERSION)

        exit_code = 0
        while True:
            item = await self.input_queue.get()
            if item is _EOF:
                break
            if isinstance(item, ProtocolError):
                error = item
                self._write_event("protocol.error", {"message": str(error)})
                exit_code = 2
                break
            assert isinstance(item, dict)
            await self._handle_request(item)

        await self._stop_active_tasks()
        await asyncio.to_thread(reader.join, 1)
        LOG.info("Python Runtime Host stopped")
        return exit_code

    def _read_stdin(
        self,
        loop: asyncio.AbstractEventLoop,
        input_queue: asyncio.Queue[dict[str, Any] | ProtocolError | object],
    ) -> None:
        stream = sys.stdin.buffer
        while True:
            try:
                line = stream.readline(MAX_FRAME_BYTES + 1)
            except OSError:
                line = b""
            except ValueError:
                line = b""
            if not line:
                loop.call_soon_threadsafe(input_queue.put_nowait, _EOF)
                return
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                loop.call_soon_threadsafe(
                    input_queue.put_nowait,
                    ProtocolError("IPC frame exceeds the 1 MiB limit or is missing its newline delimiter."),
                )
                return
            try:
                request = _decode_request(line)
            except ProtocolError as error:
                loop.call_soon_threadsafe(input_queue.put_nowait, error)
                return
            loop.call_soon_threadsafe(input_queue.put_nowait, request)

    async def _handle_request(self, request: dict[str, Any]) -> None:
        request_id = request.get("requestId")
        method = request.get("method")
        params = request.get("params")
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            self._write_error(str(request_id or "invalid"), "INVALID_REQUEST", "Invalid request ID.")
            return
        if not isinstance(method, str) or not isinstance(params, dict):
            self._write_error(request_id, "INVALID_REQUEST", "Request method and params are required.")
            return

        try:
            fingerprint = json.dumps(
                {"method": method, "params": params},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            self._write_error(request_id, "INVALID_REQUEST", "Request params must be JSON values.")
            return

        previous = self._request_cache.get(request_id)
        if previous:
            prior_method, prior_fingerprint, prior_response = previous
            if prior_method == method and prior_fingerprint == fingerprint:
                self._write_message(prior_response)
            else:
                self._write_error(
                    request_id,
                    "REQUEST_ID_REUSED",
                    "A request ID cannot be reused with different contents.",
                )
            return

        if request.get("protocolVersion") != PROTOCOL_VERSION:
            self._remember_response(
                request_id,
                method,
                fingerprint,
                self._error_message(request_id, "UNSUPPORTED_PROTOCOL", "Protocol version is unsupported."),
            )
            return
        if not self.initialized and method != "host.initialize":
            response = self._error_message(
                request_id,
                "HOST_NOT_INITIALIZED",
                "Call host.initialize before other Runtime methods.",
            )
            self._remember_response(request_id, method, fingerprint, response)
            return

        try:
            result = await self._dispatch(method, params)
        except HostRequestError as error:
            response = self._error_message(request_id, error.code, error.message)
            self._remember_response(request_id, method, fingerprint, response)
            return
        except Exception:
            LOG.exception("Unhandled Runtime Host request failure for %s", method)
            response = self._error_message(
                request_id,
                "INTERNAL_ERROR",
                "The Runtime Host could not complete the request. See the local diagnostic log.",
            )
            self._remember_response(request_id, method, fingerprint, response)
            return

        response = {
            "type": "response",
            "protocolVersion": PROTOCOL_VERSION,
            "requestId": request_id,
            "result": result,
        }
        self._remember_response(request_id, method, fingerprint, response)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "host.initialize":
            if params:
                raise HostRequestError("INVALID_PARAMS", "host.initialize does not accept params.")
            self.initialized = True
            result = self._status_payload()
            self._write_event("host.ready", result)
            return result
        if method == "host.health":
            return self._status_payload()
        if method == "workspace.open":
            raw_path = params.get("path")
            if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 32_768:
                raise HostRequestError("INVALID_PARAMS", "A valid workspace path is required.")
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                raise HostRequestError("INVALID_WORKSPACE", "Workspace paths must be absolute.")
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise HostRequestError("INVALID_WORKSPACE", "The selected workspace is unavailable.") from error
            if not resolved.is_dir():
                raise HostRequestError("INVALID_WORKSPACE", "The selected workspace is not a directory.")
            self.workspace = resolved
            payload = {"path": str(resolved), "name": resolved.name}
            self._write_event("workspace.opened", payload)
            return payload
        if method == "test.start":
            if params:
                raise HostRequestError("INVALID_PARAMS", "test.start does not accept params.")
            task_workspace = self.data_dir / "ipc-test-workspaces" / str(uuid.uuid4())
            task_workspace.mkdir(parents=True, exist_ok=False)
            active = self._start_task(
                source="controlled-test",
                workspace=task_workspace,
                model=ControlledStreamingModel(),
                permission_policy=ReadOnlyPolicy(),
                prompt="Run the deterministic AgentDU desktop IPC streaming smoke test.",
            )
            return {"taskId": active.task_id, "sessionId": active.session_id}
        if method == "turn.start":
            prompt = params.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16_000:
                raise HostRequestError("INVALID_PARAMS", "Prompt must contain 1–16000 characters.")
            if self.workspace is None:
                raise HostRequestError("WORKSPACE_REQUIRED", "Open a local workspace before starting a model task.")
            try:
                settings = ModelSettings.load(default_config_path())
                options = resolve_model_options(settings, None, None, None, None)
            except (ValueError, OSError) as error:
                raise HostRequestError("PROVIDER_NOT_CONFIGURED", str(error)) from error
            if settings.default_profile and not options.get("api_key"):
                selected = settings.resolve(settings.default_profile)
                if selected and selected.api_key_env and selected.base_url.startswith(("https://", "http://")):
                    if not _is_local_endpoint(selected.base_url):
                        raise HostRequestError(
                            "PROVIDER_CREDENTIAL_MISSING",
                            "The selected provider credential is unavailable to the desktop Runtime Host.",
                        )
            model = OpenAICompatibleModel(**options)
            active = self._start_task(
                source="provider",
                workspace=self.workspace,
                model=model,
                permission_policy=None,
                prompt=prompt.strip(),
            )
            return {"taskId": active.task_id, "sessionId": active.session_id}
        if method == "turn.cancel":
            task_id = params.get("taskId")
            if not isinstance(task_id, str) or len(task_id) > 128:
                raise HostRequestError("INVALID_PARAMS", "A valid task ID is required.")
            return await self._cancel_task(task_id)
        if method == "permission.respond":
            approval_id = params.get("approvalId")
            decision = params.get("decision")
            if not isinstance(approval_id, str) or decision not in {"allow", "deny"}:
                raise HostRequestError("INVALID_PARAMS", "An approval ID and allow/deny decision are required.")
            with self._approval_lock:
                pending = self._pending_approvals.get(approval_id)
                if pending is None:
                    raise HostRequestError("APPROVAL_NOT_FOUND", "The approval has expired or was already handled.")
                pending.decision = decision == "allow"
                pending.event.set()
            return {"status": decision}
        if method == "host.shutdown":
            await self._stop_active_tasks()
            self._write_event("host.shutdown", {"status": "stopped"})
            return {"status": "stopped"}
        raise HostRequestError("METHOD_NOT_FOUND", "Runtime method is not supported.")

    def _start_task(
        self,
        source: str,
        workspace: Path,
        model: Any,
        permission_policy: Any,
        prompt: str,
    ) -> ActiveTask:
        if any(not item.task.done() for item in self._tasks.values()):
            raise HostRequestError("TASK_ALREADY_RUNNING", "The D1 Runtime Host supports one active task.")
        task_id = str(uuid.uuid4())
        if permission_policy is None:
            permission_policy = DesktopPermissionPolicy(self, task_id)
        runtime = build_default_runtime(
            workspace=workspace,
            model=model,
            permission_policy=permission_policy,
            data_dir=workspace / ".agentdu",
            context_tokens=8_000,
            max_steps=12,
        )
        session_id = runtime.sessions.create()
        task = asyncio.create_task(
            self._run_task(task_id, session_id, source, runtime, prompt),
            name=f"agentdu-desktop-{task_id}",
        )
        active = ActiveTask(task_id, session_id, source, runtime, task)
        self._tasks[task_id] = active
        runtime.event_bus.subscribe(
            lambda event: self._runtime_event(task_id, source, event)
        )
        self._write_event(
            "task.accepted",
            {"taskId": task_id, "sessionId": session_id, "source": source},
        )
        return active

    async def _run_task(
        self,
        task_id: str,
        session_id: str,
        source: str,
        runtime: AgentRuntime,
        prompt: str,
    ) -> None:
        try:
            result = await runtime.run(prompt, session_id)
        except asyncio.CancelledError:
            self._write_event(
                "task.cancelled",
                {"taskId": task_id, "sessionId": session_id, "source": source},
            )
            raise
        except Exception as error:
            safe_error = runtime.sessions.redact(
                {"message": f"{type(error).__name__}: {error}"}
            )["message"]
            self._write_event(
                "task.failed",
                {
                    "taskId": task_id,
                    "sessionId": session_id,
                    "source": source,
                    "message": str(safe_error)[:2000],
                },
            )
            LOG.exception("Agent task %s failed", task_id)
        else:
            self._write_event(
                "task.completed",
                _result_payload(task_id, source, result),
            )
        finally:
            runtime.close()

    def _runtime_event(self, task_id: str, source: str, event: AgentEvent) -> None:
        payload = {
            **event.payload,
            "taskId": task_id,
            "sessionId": event.session_id,
            "source": source,
            "occurred_at": event.occurred_at,
        }
        self._write_event(event.kind, payload)

    async def _cancel_task(self, task_id: str) -> dict[str, str]:
        active = self._tasks.get(task_id)
        if active is None:
            return {"status": "not_found"}
        if active.task.done():
            return {"status": "already_finished"}
        self._write_event("task.cancel_requested", {"taskId": task_id})
        self._resolve_pending_approvals(task_id, allow=False)
        active.task.cancel()
        try:
            await active.task
        except asyncio.CancelledError:
            return {"status": "cancelled"}
        return {"status": "completed"}

    def _resolve_pending_approvals(self, task_id: str, allow: bool) -> None:
        with self._approval_lock:
            for pending in self._pending_approvals.values():
                if pending.task_id == task_id:
                    pending.decision = allow
                    pending.event.set()

    async def _stop_active_tasks(self) -> None:
        active = [item for item in self._tasks.values() if not item.task.done()]
        for item in active:
            self._resolve_pending_approvals(item.task_id, allow=False)
            item.task.cancel()
        if active:
            await asyncio.gather(*(item.task for item in active), return_exceptions=True)
        for item in self._tasks.values():
            if not item.task.done():
                item.runtime.close()

    def publish_from_worker(self, event: str, payload: dict[str, Any]) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self._write_event, event, payload)

    def _status_payload(self) -> dict[str, Any]:
        try:
            settings = ModelSettings.load(default_config_path())
            profiles = []
            for name, profile in sorted(settings.profiles.items()):
                key = os.environ.get(profile.api_key_env) if profile.api_key_env else None
                key = key or os.environ.get("AGENTDU_API_KEY") or os.environ.get("OPENAI_API_KEY")
                profiles.append(
                    {
                        "name": name,
                        "model": profile.model,
                        "default": name == settings.default_profile,
                        "credentialAvailable": bool(key),
                    }
                )
            provider_configured = bool(
                settings.default_profile
                and settings.resolve(settings.default_profile)
                and settings.resolve(settings.default_profile).model
            )
            provider_state: dict[str, Any] = {
                "providerConfigured": provider_configured,
                "providerProfiles": profiles,
                "configPath": str(default_config_path()),
            }
        except Exception as error:
            LOG.warning("Unable to inspect provider settings: %s", error)
            provider_state = {
                "providerConfigured": False,
                "providerProfiles": [],
                "configPath": str(default_config_path()),
                "configError": type(error).__name__,
            }
        return {
            "runtimeVersion": RUNTIME_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "pythonVersion": sys.version.split()[0],
            "platform": sys.platform,
            "dataDirectory": str(self.data_dir),
            "workspace": str(self.workspace) if self.workspace else None,
            "activeTasks": sum(not item.task.done() for item in self._tasks.values()),
            **provider_state,
        }

    def _write_error(self, request_id: str, code: str, message: str) -> None:
        self._write_message(self._error_message(request_id, code, message))

    @staticmethod
    def _error_message(request_id: str, code: str, message: str) -> dict[str, Any]:
        return {
            "type": "response",
            "protocolVersion": PROTOCOL_VERSION,
            "requestId": request_id,
            "error": {"code": code, "message": message},
        }

    def _remember_response(
        self,
        request_id: str,
        method: str,
        fingerprint: str,
        response: dict[str, Any],
    ) -> None:
        self._request_cache[request_id] = (method, fingerprint, response)
        self._request_cache.move_to_end(request_id)
        while len(self._request_cache) > MAX_REMEMBERED_REQUESTS:
            self._request_cache.popitem(last=False)
        self._write_message(response)

    def _write_event(self, event: str, payload: dict[str, Any]) -> None:
        self.sequence += 1
        self._write_message(
            {
                "type": "event",
                "protocolVersion": PROTOCOL_VERSION,
                "eventId": str(uuid.uuid4()),
                "sequence": self.sequence,
                "event": event,
                "payload": payload,
            }
        )

    def _write_message(self, message: dict[str, Any]) -> None:
        try:
            frame = json.dumps(
                message,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            LOG.exception("Failed to encode a Runtime Host protocol frame")
            return
        if len(frame) > MAX_FRAME_BYTES:
            LOG.error("Refusing to write an oversized Runtime Host frame")
            return
        with self._output_lock:
            sys.stdout.buffer.write(frame + b"\n")
            sys.stdout.buffer.flush()


class HostRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _decode_request(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("IPC frame exceeds the 1 MiB limit.")
    if not line.endswith(b"\n"):
        raise ProtocolError("IPC frame is missing its newline delimiter.")
    try:
        value = json.loads(
            line[:-1].decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError("IPC frame is not valid UTF-8 JSON.") from error
    if not isinstance(value, dict) or value.get("type") != "request":
        raise ProtocolError("IPC frame must be a request object.")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant {value} is not allowed.")


def _approval_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    path = arguments.get("path")
    if isinstance(path, str):
        summary["path"] = path[:32_768]
    for field in ("content", "old_text", "new_text"):
        value = arguments.get(field)
        if isinstance(value, str):
            summary[f"{field}Chars"] = len(value)
    return summary


def _result_payload(task_id: str, source: str, result: RunResult) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "sessionId": result.session_id,
        "source": source,
        "modelSteps": result.model_steps,
        "modelCalls": result.model_calls,
        "response": result.content,
    }


def _is_local_endpoint(base_url: str) -> bool:
    return bool(re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/|$)", base_url))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", type=Path, required=False)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    args = _parse_args()
    data_dir = args.data_dir or Path(os.environ.get("AGENTDU_DESKTOP_DATA", Path.home() / "AppData" / "Local" / "AgentDU")) / "runtime"
    try:
        return asyncio.run(RuntimeHost(data_dir).serve())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

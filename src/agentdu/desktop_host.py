from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .agent import AgentRuntime, RunResult, build_default_runtime
from .events import AgentEvent
from .models import AssistantMessage, Message, ModelStreamEvent, ToolSchema
from .permissions import PermissionRequest, ReadOnlyPolicy
from .provider import OpenAICompatibleModel
from .settings import ModelSettings, default_config_path, resolve_profile_options
from .sessions import SessionStore
from .tools import _atomic_write, _git_command, _iter_workspace_files, _workspace_file
from .windows_sandbox import run_appcontainer_command


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
    """Bridge file and isolated-command approvals to the desktop UI."""

    def __init__(self, host: "RuntimeHost", task_id: str, operation_id: str | None = None) -> None:
        self.host = host
        self.task_id = task_id
        self.operation_id = operation_id

    def authorize(self, request: PermissionRequest) -> bool:
        if request.permission == "read":
            return True
        if request.permission == "execute" and os.name != "nt":
            self.host.publish_from_worker(
                "security.capability_blocked",
                {
                    "taskId": self.task_id,
                    "toolName": request.tool_name,
                    "reason": "Windows OS-level command isolation has not passed D1 validation.",
                },
            )
            return False
        if request.permission not in {"write", "execute"}:
            return False

        approval_id = str(uuid.uuid4())
        pending = PendingApproval(self.task_id, threading.Event())
        with self.host._approval_lock:
            self.host._pending_approvals[approval_id] = pending

        payload = {
            "approvalId": approval_id,
            "taskId": self.task_id,
            "operationId": self.operation_id,
            "toolName": request.tool_name,
            "permission": request.permission,
            "workspace": str(request.workspace),
            "arguments": _approval_summary(request.arguments),
            "operation": "运行 AppContainer 隔离命令" if request.permission == "execute" else "写入项目文件",
            "command": request.arguments.get("command") if isinstance(request.arguments.get("command"), str) else None,
            "targetFiles": [request.arguments["path"]] if isinstance(request.arguments.get("path"), str) else [],
            "scope": "当前项目工作区；运行期间临时授予 AppContainer 对项目文件的访问权" if request.permission == "execute" else "仅当前项目工作区",
            "networkRequired": False,
        }
        self.host.publish_from_worker("permission.requested", payload)
        resolved = pending.event.wait(timeout=120)
        with self.host._approval_lock:
            self.host._pending_approvals.pop(approval_id, None)
        if not resolved:
            self.host.publish_from_worker(
                "permission.resolved",
                {"approvalId": approval_id, "taskId": self.task_id, "operationId": self.operation_id, "decision": "timeout"},
            )
            return False
        self.host.publish_from_worker(
            "permission.resolved",
            {
                "approvalId": approval_id,
                "taskId": self.task_id,
                "operationId": self.operation_id,
                "decision": "allow" if pending.decision else "deny",
            },
        )
        return pending.decision is True


class ControlledStreamingModel:
    """Deterministic IPC fixture. It never makes a provider request."""

    streaming_enabled = True

    def __init__(self, delay_seconds: float = 0.75) -> None:
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
        self.recent_workspaces_path = self.data_dir / "recent-workspaces.json"
        self.initialized = False
        self.sequence = 0
        self._output_lock = threading.Lock()
        self._approval_lock = threading.Lock()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._tasks: dict[str, ActiveTask] = {}
        self._file_write_tasks: dict[str, asyncio.Task[None]] = {}
        self._terminal_tasks: dict[str, asyncio.Task[None]] = {}
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
            fingerprint = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
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
            if any(not item.task.done() for item in self._tasks.values()) or self._file_write_tasks or self._terminal_tasks:
                raise HostRequestError("TASK_ALREADY_RUNNING", "Finish the current task or file operation before switching projects.")
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
            self._remember_workspace(resolved)
            payload = {"path": str(resolved), "name": resolved.name}
            self._write_event("workspace.opened", payload)
            return payload
        if method == "workspace.list":
            return {"workspaces": self._recent_workspaces()}
        if method == "workspace.inspect":
            workspace = self._require_workspace()
            return await asyncio.to_thread(self._inspect_workspace, workspace)
        if method == "workspace.files":
            workspace = self._require_workspace()
            return self._workspace_files(workspace, params)
        if method == "file.read":
            workspace = self._require_workspace()
            raw_path = params.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise HostRequestError("INVALID_PARAMS", "A workspace-relative file path is required.")
            if Path(raw_path).is_absolute() or any(part.casefold() in {".git", ".agentdu"} for part in Path(raw_path).parts):
                raise HostRequestError("INVALID_FILE_PATH", "File reads must target ordinary project files using a workspace-relative path.")
            try:
                path = _workspace_file(workspace, raw_path)
                if path.is_symlink() or not path.is_file():
                    raise ValueError("File is not a regular file in the workspace")
                if path.stat().st_size > 1_000_000:
                    raise ValueError("File is larger than the 1 MiB viewer limit")
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as error:
                raise HostRequestError("FILE_READ_FAILED", str(error)) from error
            return {"path": path.relative_to(workspace).as_posix(), "content": content}
        if method == "file.write":
            workspace = self._require_workspace()
            raw_path = params.get("path")
            content = params.get("content")
            if not isinstance(raw_path, str) or not raw_path or not isinstance(content, str) or len(content) > 120_000:
                raise HostRequestError("INVALID_PARAMS", "A file path and content up to 120,000 characters are required.")
            if Path(raw_path).is_absolute() or any(part.casefold() in {".git", ".agentdu"} for part in Path(raw_path).parts):
                raise HostRequestError("INVALID_FILE_PATH", "File writes must target ordinary project files using a workspace-relative path.")
            if any(not item.task.done() for item in self._tasks.values()) or self._file_write_tasks or self._terminal_tasks:
                raise HostRequestError("TASK_ALREADY_RUNNING", "Finish the active task or workspace operation before writing a file.")
            try:
                path = _workspace_file(workspace, raw_path)
                if path.exists() and path.is_dir():
                    raise IsADirectoryError(str(path))
            except (OSError, ValueError) as error:
                raise HostRequestError("INVALID_FILE_PATH", str(error)) from error
            operation_id = str(uuid.uuid4())
            task_id = f"workspace-edit:{operation_id}"
            session_id = params.get("sessionId")
            if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9-]{16,64}", session_id) or not self._session_store(workspace).exists(session_id):
                raise HostRequestError("SESSION_REQUIRED", "File edits must be attached to a valid project session.")
            task = asyncio.create_task(
                self._write_workspace_file(workspace, path, content, session_id, operation_id, task_id),
                name=f"agentdu-desktop-write-{operation_id}",
            )
            self._file_write_tasks[operation_id] = task
            return {"path": str(path.relative_to(workspace)), "operationId": operation_id, "status": "pending"}
        if method == "terminal.run":
            workspace = self._require_workspace()
            command = params.get("command")
            session_id = params.get("sessionId")
            if not isinstance(command, str) or not command.strip() or len(command) > 16_000 or "\0" in command:
                raise HostRequestError("INVALID_PARAMS", "Command must contain 1–16,000 characters and no NUL byte.")
            if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9-]{16,64}", session_id) or not self._session_store(workspace).exists(session_id):
                raise HostRequestError("SESSION_REQUIRED", "Terminal commands must be attached to a valid project session.")
            if any(not item.task.done() for item in self._tasks.values()) or self._file_write_tasks or self._terminal_tasks:
                raise HostRequestError("TASK_ALREADY_RUNNING", "Finish the active task or file operation before starting a terminal command.")
            operation_id = str(uuid.uuid4())
            task_id = f"terminal:{operation_id}"
            task = asyncio.create_task(
                self._run_terminal_command(workspace, session_id, command.strip(), operation_id, task_id),
                name=f"agentdu-terminal-{operation_id}",
            )
            self._terminal_tasks[operation_id] = task
            return {"operationId": operation_id, "status": "pending"}
        if method == "git.status":
            return {"status": _git_command(self._require_workspace(), "status", "--short", "--branch")}
        if method == "git.diff":
            path = params.get("path")
            arguments = ["diff", "--no-ext-diff"]
            if params.get("staged") is True:
                arguments.append("--cached")
            arguments.append("--")
            if isinstance(path, str) and path:
                arguments.append(path)
            return {"diff": _git_command(self._require_workspace(), *arguments)}
        if method == "sessions.list":
            store = self._session_store(self._require_workspace())
            return {"sessions": store.list_sessions()}
        if method == "provider.test":
            profile_name = params.get("profile")
            if not isinstance(profile_name, str) or not profile_name:
                raise HostRequestError("INVALID_PARAMS", "A model profile is required.")
            try:
                settings = ModelSettings.load(default_config_path())
                options = resolve_profile_options(settings, profile_name)
                model = OpenAICompatibleModel(**options)
            except (ValueError, OSError) as error:
                raise HostRequestError("PROVIDER_NOT_CONFIGURED", str(error)) from error
            result = await model.test_connection()
            return {"profile": profile_name, "model": model.model, **result}
        if method == "session.create":
            store = self._session_store(self._require_workspace())
            session_id = store.create()
            return {"sessionId": session_id}
        if method == "session.read":
            workspace = self._require_workspace()
            session_id = params.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise HostRequestError("INVALID_PARAMS", "A session ID is required.")
            store = self._session_store(workspace)
            try:
                events = store.read_events(session_id)
                messages = store.read_messages(session_id)
            except (OSError, ValueError) as error:
                raise HostRequestError("SESSION_READ_FAILED", str(error)) from error
            return {"sessionId": session_id, "events": events[-500:], "messages": messages[-200:]}
        if method == "test.start":
            if params:
                raise HostRequestError("INVALID_PARAMS", "test.start does not accept params.")
            task_workspace = self.data_dir / "ipc-test-workspaces" / str(uuid.uuid4())
            task_workspace.mkdir(parents=True, exist_ok=False)
            active = self._start_task(
                source="controlled-test",
                workspace=task_workspace,
                model=ControlledStreamingModel(delay_seconds=6.0),
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
                profile_name = params.get("profile")
                if profile_name is not None and not isinstance(profile_name, str):
                    raise ValueError("Model profile name must be text")
                options = resolve_profile_options(settings, profile_name or settings.default_profile or "")
            except (ValueError, OSError) as error:
                raise HostRequestError("PROVIDER_NOT_CONFIGURED", str(error)) from error
            selected = settings.resolve(profile_name or settings.default_profile)
            if selected and not options.get("api_key") and selected.api_key_env:
                if not _is_local_endpoint(selected.base_url):
                    raise HostRequestError(
                        "PROVIDER_CREDENTIAL_MISSING",
                        "The selected provider credential is unavailable to the desktop Runtime Host.",
                    )
            model = OpenAICompatibleModel(**options)
            session_id = params.get("sessionId")
            if session_id is not None and not isinstance(session_id, str):
                raise HostRequestError("INVALID_PARAMS", "Session ID must be text.")
            active = self._start_task(
                source="provider",
                workspace=self.workspace,
                model=model,
                permission_policy=None,
                prompt=prompt.strip(),
                session_id=session_id,
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
        session_id: str | None = None,
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
            command_runner=run_appcontainer_command,
        )
        if session_id is None:
            session_id = runtime.sessions.create()
        elif not runtime.sessions.exists(session_id):
            runtime.close()
            raise HostRequestError("SESSION_NOT_FOUND", "The selected session does not exist in this workspace.")
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

    def _require_workspace(self) -> Path:
        if self.workspace is None:
            raise HostRequestError("WORKSPACE_REQUIRED", "Open a local workspace first.")
        return self.workspace

    @staticmethod
    def _session_store(workspace: Path) -> SessionStore:
        store = SessionStore(workspace / ".agentdu")
        store.add_redactions(RuntimeHost._known_secret_values())
        return store

    @staticmethod
    def _known_secret_values() -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for name, value in os.environ.items()
                if re.search(r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", name, re.IGNORECASE)
                or ("_PROFILE_" in name and name.endswith("_KEY"))
                if value
            )
        )

    def _remember_workspace(self, workspace: Path) -> None:
        items = self._recent_workspaces()
        key = str(workspace).casefold()
        items = [item for item in items if str(item.get("path", "")).casefold() != key]
        items.insert(0, {"path": str(workspace), "name": workspace.name, "openedAt": datetime.now(timezone.utc).isoformat()})
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.recent_workspaces_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(items[:20], ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.recent_workspaces_path)

    def _recent_workspaces(self) -> list[dict[str, str]]:
        try:
            items = json.loads(self.recent_workspaces_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(items, list):
            return []
        available = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = Path(item["path"])
            if path.is_dir():
                available.append({"path": str(path), "name": str(item.get("name") or path.name), "openedAt": str(item.get("openedAt") or "")})
        return available[:20]

    def _inspect_workspace(self, workspace: Path) -> dict[str, Any]:
        files = self._workspace_files(workspace, {"limit": 300})["files"]
        has_git_metadata = any(
            (parent / ".git").exists() for parent in (workspace, *workspace.parents)
        )
        if not has_git_metadata:
            return {
                "path": str(workspace),
                "name": workspace.name,
                "files": files,
                "gitStatus": "",
                "gitError": "当前工作区不在 Git 仓库中。",
            }

        try:
            git_status = _git_command(
                workspace,
                "status",
                "--short",
                "--branch",
                "--untracked-files=normal",
                "--",
                ".",
                timeout_seconds=5,
            )
            git_error = None
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            git_status = ""
            git_error = str(error)
        return {"path": str(workspace), "name": workspace.name, "files": files, "gitStatus": git_status, "gitError": git_error}

    @staticmethod
    def _workspace_files(workspace: Path, params: dict[str, Any]) -> dict[str, Any]:
        limit_value = params.get("limit", 300)
        limit = max(1, min(limit_value if isinstance(limit_value, int) and not isinstance(limit_value, bool) else 300, 1000))
        query = params.get("query", "")
        query = query.casefold() if isinstance(query, str) else ""
        files = []
        for path in _iter_workspace_files(workspace):
            relative = path.relative_to(workspace).as_posix()
            if query and query not in relative.casefold():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append({"path": relative, "size": size})
            if len(files) >= limit:
                break
        return {"files": files, "limit": limit}

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

    async def _write_workspace_file(
        self,
        workspace: Path,
        path: Path,
        content: str,
        session_id: str,
        operation_id: str,
        task_id: str,
    ) -> None:
        relative_path = path.relative_to(workspace).as_posix()
        session_store = self._session_store(workspace)
        audit = {"operationId": operation_id, "path": relative_path, "contentChars": len(content)}
        try:
            session_store.append(session_id, "desktop_file_write.requested", audit)
            request = PermissionRequest(
                permission="write",
                tool_name="workspace_editor",
                arguments={"path": relative_path, "content": content},
                workspace=workspace,
            )
            allowed = await asyncio.to_thread(
                DesktopPermissionPolicy(self, task_id, operation_id).authorize,
                request,
            )
            session_store.append(
                session_id,
                "desktop_file_write.approval",
                {**audit, "decision": "allow" if allowed else "deny"},
            )
            if not allowed:
                session_store.append(session_id, "desktop_file_write.result", {**audit, "status": "denied"})
                self._write_event("workspace.write.finished", {**audit, "sessionId": session_id, "status": "denied"})
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_atomic_write, path, content)
        except asyncio.CancelledError:
            self._write_event("workspace.write.finished", {**audit, "sessionId": session_id, "status": "cancelled"})
            raise
        except Exception as error:
            session_store.append(
                session_id,
                "desktop_file_write.result",
                {**audit, "status": "failed", "error": type(error).__name__},
            )
            self._write_event(
                "workspace.write.finished",
                {**audit, "sessionId": session_id, "status": "failed", "error": str(error)[:500]},
            )
        else:
            written_bytes = len(content.encode("utf-8"))
            session_store.append(
                session_id,
                "desktop_file_write.result",
                {**audit, "status": "completed", "bytes": written_bytes},
            )
            self._write_event(
                "workspace.write.finished",
                {**audit, "sessionId": session_id, "status": "completed", "bytes": written_bytes},
            )
        finally:
            self._file_write_tasks.pop(operation_id, None)

    async def _run_terminal_command(
        self,
        workspace: Path,
        session_id: str,
        command: str,
        operation_id: str,
        task_id: str,
    ) -> None:
        store = self._session_store(workspace)
        audit = {"operationId": operation_id, "command": command, "workingDirectory": str(workspace)}
        audit = store.redact(audit)
        try:
            store.append(session_id, "terminal.command.requested", audit)
            self._write_event("terminal.command.started", {"sessionId": session_id, **audit})
            request = PermissionRequest(
                permission="execute",
                tool_name="terminal.run",
                arguments={"command": command},
                workspace=workspace,
            )
            allowed = await asyncio.to_thread(
                DesktopPermissionPolicy(self, task_id, operation_id).authorize,
                request,
            )
            store.append(session_id, "terminal.command.approval", {"operationId": operation_id, "decision": "allow" if allowed else "deny"})
            if not allowed:
                store.append(session_id, "terminal.command.result", {"operationId": operation_id, "status": "denied"})
                self._write_event("terminal.command.finished", {"operationId": operation_id, "sessionId": session_id, "status": "denied"})
                return
            result = await asyncio.to_thread(
                run_appcontainer_command,
                {"command": command},
                workspace,
                self._known_secret_values(),
            )
            match = re.match(r"exit code: (-?\d+)", result.text)
            exit_code = int(match.group(1)) if match else None
            output = result.text.split("\n", 1)[1] if "\n" in result.text else result.text
            payload = {
                "operationId": operation_id,
                "status": "failed" if result.is_error else "completed",
                "exitCode": exit_code,
                "output": output[:16_000],
            }
            store.append(session_id, "terminal.command.result", payload)
            self._write_event("terminal.command.finished", {**payload, "sessionId": session_id})
        except asyncio.CancelledError:
            self._write_event("terminal.command.finished", {"operationId": operation_id, "sessionId": session_id, "status": "cancelled"})
            raise
        except Exception as error:
            store.append(session_id, "terminal.command.result", {"operationId": operation_id, "status": "failed", "error": type(error).__name__})
            self._write_event("terminal.command.finished", {"operationId": operation_id, "sessionId": session_id, "status": "failed", "error": str(error)[:500]})
        finally:
            self._terminal_tasks.pop(operation_id, None)

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
        file_writes = list(self._file_write_tasks.items())
        for operation_id, _task in file_writes:
            self._resolve_pending_approvals(f"workspace-edit:{operation_id}", allow=False)
        if active:
            await asyncio.gather(*(item.task for item in active), return_exceptions=True)
        if file_writes:
            await asyncio.gather(*(task for _, task in file_writes), return_exceptions=True)
        terminal_tasks = list(self._terminal_tasks.items())
        for operation_id, _task in terminal_tasks:
            self._resolve_pending_approvals(f"terminal:{operation_id}", allow=False)
        if terminal_tasks:
            await asyncio.gather(*(task for _, task in terminal_tasks), return_exceptions=True)
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
                        "baseUrl": profile.base_url,
                        "streaming": profile.streaming,
                        "wireApi": profile.wire_api,
                        "protocol": (
                            "OpenAI-compatible Responses API"
                            if profile.wire_api == "responses"
                            else "OpenAI-compatible Chat Completions"
                        ),
                        "configuredCapabilities": list(profile.capabilities),
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
            "activeTasks": (
                sum(not item.task.done() for item in self._tasks.values())
                + len(self._file_write_tasks)
                + len(self._terminal_tasks)
            ),
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

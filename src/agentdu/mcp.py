from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .events import AgentEvent
from .permissions import PermissionPolicy, PermissionRequest
from .security import redact_value, safe_child_environment
from .settings import MCPServerSettings
from .tools import ToolDefinition, ToolRegistry, ToolResult


_SUPPORTED_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
_MAX_MCP_MESSAGE_BYTES = 4_000_000
_MAX_TOOLS_PER_SERVER = 256
_MAX_PENDING_MCP_MESSAGES = 8


class MCPError(RuntimeError):
    pass


class MCPStdioClient:
    """MCP stdio client with bounded JSON-RPC waits and graceful process shutdown."""

    def __init__(
        self,
        settings: MCPServerSettings,
        workspace: Path,
        environment: dict[str, str],
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.settings = settings
        self.workspace = workspace.resolve()
        self.environment = environment
        self.secret_values = secret_values
        self.process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | None] = queue.Queue(maxsize=_MAX_PENDING_MCP_MESSAGES)
        self._reader_overflow = threading.Event()
        self._request_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self._next_id = 0

    def start(self) -> dict[str, Any]:
        if self.process is not None:
            raise MCPError(f"MCP server '{self.settings.name}' is already running")
        cwd = self._working_directory()
        try:
            process = subprocess.Popen(
                [self.settings.command, *self.settings.args],
                cwd=cwd,
                env=self.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            raise MCPError(f"Could not start MCP server '{self.settings.name}': {error}") from error
        self.process = process
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "AgentDU",
                        "title": "AgentDU",
                        "version": "0.6.0",
                    },
                },
            )
            version = result.get("protocolVersion")
            if version not in _SUPPORTED_VERSIONS:
                raise MCPError(
                    f"MCP server '{self.settings.name}' negotiated unsupported version {version!r}"
                )
            if not isinstance(result.get("capabilities"), dict):
                raise MCPError(f"MCP server '{self.settings.name}' returned invalid capabilities")
            self._notify("notifications/initialized")
            return result
        except BaseException:
            self.close()
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            page = result.get("tools")
            if not isinstance(page, list):
                raise MCPError(f"MCP server '{self.settings.name}' returned an invalid tools list")
            tools.extend(page)
            if len(tools) > _MAX_TOOLS_PER_SERVER:
                raise MCPError(
                    f"MCP server '{self.settings.name}' exposes more than {_MAX_TOOLS_PER_SERVER} tools"
                )
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise MCPError(f"MCP server '{self.settings.name}' returned an invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        blocks = result.get("content", [])
        if not isinstance(blocks, list):
            raise MCPError(f"MCP server '{self.settings.name}' returned invalid tool content")
        rendered: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                rendered.append(block["text"])
            elif block.get("type") == "image":
                rendered.append(f"[MCP image omitted: {block.get('mimeType', 'unknown type')}]")
            elif block.get("type") == "audio":
                rendered.append(f"[MCP audio omitted: {block.get('mimeType', 'unknown type')}]")
            elif block.get("type") == "resource_link":
                rendered.append(f"[MCP resource link: {block.get('uri', '')}]")
        structured = result.get("structuredContent")
        if not rendered and structured is not None:
            rendered.append(json.dumps(structured, ensure_ascii=False))
        if not rendered:
            rendered.append("MCP tool returned no text content")
        output = "\n".join(rendered)
        output = redact_value(output, self.secret_values)
        return ToolResult(output, is_error=result.get("isError") is True)

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._request_lock:
            process = self._require_process()
            self._next_id += 1
            request_id = self._next_id
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            self._write(message)
            deadline = time.monotonic() + self.settings.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"MCP request '{method}' timed out")
                try:
                    line = self._responses.get(timeout=remaining)
                except queue.Empty as error:
                    raise MCPError(f"MCP request '{method}' timed out") from error
                if line is None:
                    if self._reader_overflow.is_set():
                        raise MCPError(
                            f"MCP server '{self.settings.name}' exceeded the pending-message limit"
                        )
                    raise MCPError(self._process_ended(method, process))
                if len(line) > _MAX_MCP_MESSAGE_BYTES:
                    raise MCPError("MCP server message exceeded the 4 MB limit")
                try:
                    response = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise MCPError(f"MCP server sent invalid JSON-RPC data: {error}") from error
                if not isinstance(response, dict):
                    raise MCPError("MCP server response must be a JSON object")
                if response.get("id") != request_id:
                    self._handle_unexpected_message(response)
                    continue
                if isinstance(response.get("error"), dict):
                    error = response["error"]
                    raise MCPError(
                        f"MCP request '{method}' failed ({error.get('code')}): "
                        f"{error.get('message', 'unknown protocol error')}"
                    )
                result = response.get("result")
                if not isinstance(result, dict):
                    raise MCPError(f"MCP request '{method}' returned no result object")
                return result

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _write(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise MCPError("MCP server stdin is unavailable")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            process.stdin.write(encoded + b"\n")
            process.stdin.flush()
        except OSError as error:
            raise MCPError(f"Could not write to MCP server: {error}") from error

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := process.stdout.readline(_MAX_MCP_MESSAGE_BYTES + 1):
                try:
                    self._responses.put(line, timeout=0.1)
                except queue.Full:
                    self._reader_overflow.set()
                    if process.poll() is None:
                        try:
                            process.terminate()
                        except OSError:
                            pass
                    break
        finally:
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    pass
                self._responses.put_nowait(None)

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for raw_line in process.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            with self._stderr_lock:
                self._stderr_tail.append(line[:1_000])
                del self._stderr_tail[:-40]

    def _handle_unexpected_message(self, message: dict[str, Any]) -> None:
        if "method" not in message or "id" not in message:
            return
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not supported by AgentDU"},
                }
            )
        except MCPError:
            return

    def _process_ended(self, method: str, process: subprocess.Popen[bytes]) -> str:
        with self._stderr_lock:
            detail = "\n".join(self._stderr_tail[-8:])
        message = f"MCP server exited during '{method}' with code {process.poll()}"
        if detail:
            message += f": {detail}"
        return str(redact_value(message, self.secret_values))

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self.process is None or self.process.poll() is not None:
            raise MCPError(f"MCP server '{self.settings.name}' is not running")
        return self.process

    def _working_directory(self) -> Path:
        if self.settings.cwd is None:
            return self.workspace
        candidate = (self.workspace / self.settings.cwd).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as error:
            raise MCPError("MCP server working directory must stay inside the workspace") from error
        if not candidate.is_dir():
            raise MCPError(f"MCP server working directory does not exist: {candidate}")
        return candidate


class MCPToolsExtension:
    name = "mcp"

    def __init__(
        self,
        servers: tuple[MCPServerSettings, ...],
        workspace: Path,
        permission_policy: PermissionPolicy,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.servers = servers
        self.workspace = workspace.resolve()
        self.permission_policy = permission_policy
        self.clients: dict[str, MCPStdioClient] = {}
        self.secret_values: set[str] = {value for value in secret_values if len(value) >= 4}

    def register_tools(self, registry: ToolRegistry) -> None:
        try:
            for settings in self.servers:
                request = PermissionRequest(
                    permission="execute",
                    tool_name="start_mcp_server",
                    arguments={"server": settings.name, "command": settings.command, "args": list(settings.args)},
                    workspace=self.workspace,
                )
                if not self.permission_policy.authorize(request):
                    raise MCPError(f"Starting MCP server '{settings.name}' was not authorized")
                client, server_info = self._start_server(settings)
                tools = client.list_tools()
                for definition in tools:
                    self._register_tool(registry, settings.name, server_info, client, definition)
        except BaseException:
            self.close()
            raise

    async def on_event(self, event: AgentEvent) -> None:
        return

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def _start_server(
        self,
        settings: MCPServerSettings,
    ) -> tuple[MCPStdioClient, dict[str, Any]]:
        environment = safe_child_environment(tuple(self.secret_values))
        for child_name, source_name in (settings.env or {}).items():
            value = os.environ.get(source_name)
            if value is None:
                raise MCPError(
                    f"MCP server '{settings.name}' needs environment variable {source_name}"
                )
            environment[child_name] = value
            if len(value) >= 4:
                self.secret_values.add(value)
        client = MCPStdioClient(
            settings,
            self.workspace,
            environment,
            tuple(self.secret_values),
        )
        try:
            initialized = client.start()
            info = initialized.get("serverInfo", {})
            if not isinstance(info, dict):
                info = {}
            self.clients[settings.name] = client
            return client, info
        except BaseException:
            client.close()
            raise

    def _register_tool(
        self,
        registry: ToolRegistry,
        server_name: str,
        server_info: dict[str, Any],
        client: MCPStdioClient,
        definition: dict[str, Any],
    ) -> None:
        original_name = definition.get("name")
        schema = definition.get("inputSchema")
        description = definition.get("description", "")
        if (
            not isinstance(original_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", original_name)
            or not isinstance(schema, dict)
            or schema.get("type") != "object"
            or not isinstance(description, str)
        ):
            raise MCPError(f"MCP server '{server_name}' returned an unsupported tool definition")
        tool_name = _tool_name(server_name, original_name)
        vendor = server_info.get("name", server_name)
        full_description = f"MCP server {server_name} ({vendor}), tool {original_name}. {description}"
        parameters = dict(schema)
        parameters.setdefault("additionalProperties", False)

        def invoke(arguments: dict[str, Any], workspace: Path) -> ToolResult:
            return client.call_tool(original_name, arguments)

        registry.register(
            ToolDefinition(
                name=tool_name,
                description=full_description[:4_000],
                parameters=parameters,
                permission="execute",
                handler=invoke,
            )
        )


def _tool_name(server_name: str, tool_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server_name}_{tool_name}")
    if len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()[:8]
    return f"{normalized[:55]}_{digest}"

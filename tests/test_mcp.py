from __future__ import annotations

import asyncio
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdu.mcp import MCPError, MCPStdioClient, MCPToolsExtension
from agentdu.permissions import CallbackPolicy
from agentdu.settings import ConfigurationError, MCPServerSettings, ModelSettings
from agentdu.tools import ToolRegistry


_MCP_FIXTURE = r'''
import json
import sys

def send(message):
    print(json.dumps(message), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture"}
        }})
    elif method == "tools/list":
        cursor = request.get("params", {}).get("cursor")
        tool = "status" if cursor else "echo"
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        if tool == "echo":
            schema["properties"] = {"text": {"type": "string"}}
            schema["required"] = ["text"]
        result = {"tools": [{"name": tool, "description": "Fixture tool", "inputSchema": schema}]}
        if not cursor:
            result["nextCursor"] = "page-2"
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "tools/call":
        value = request["params"]["arguments"]["text"]
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": "echo: " + value}],
            "isError": False
        }})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
'''


class MCPTests(unittest.TestCase):
    def test_stdio_lifecycle_pagination_tool_call_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = MCPServerSettings(
                name="fixture",
                command=sys.executable,
                args=("-u", "-c", _MCP_FIXTURE),
                timeout=5,
            )
            requests = []
            extension = MCPToolsExtension(
                (settings,),
                workspace,
                CallbackPolicy(lambda request: requests.append(request) or True),
            )
            registry = ToolRegistry(workspace, CallbackPolicy(lambda request: requests.append(request) or True))

            extension.register_tools(registry)
            names = {
                schema["function"]["name"]
                for schema in registry.model_schemas()
            }
            self.assertIn("mcp_fixture_echo", names)
            self.assertIn("mcp_fixture_status", names)

            result = asyncio.run(registry.execute("mcp_fixture_echo", {"text": "hello"}))

            self.assertFalse(result.is_error, result.text)
            self.assertEqual(result.text, "echo: hello")
            self.assertEqual([request.tool_name for request in requests], ["start_mcp_server", "mcp_fixture_echo"])
            process = extension.clients["fixture"].process
            extension.close()
            self.assertIsNotNone(process.poll())

    def test_mcp_server_startup_is_denied_without_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            extension = MCPToolsExtension(
                (
                    MCPServerSettings(
                        name="fixture",
                        command="does-not-need-to-exist",
                    ),
                ),
                Path(directory),
                CallbackPolicy(lambda request: False),
            )

            with self.assertRaisesRegex(MCPError, "was not authorized"):
                extension.register_tools(ToolRegistry(Path(directory), CallbackPolicy(lambda request: True)))

    def test_model_secret_from_custom_environment_name_is_not_inherited(self) -> None:
        secret = "provider-secret-value-9876"
        with tempfile.TemporaryDirectory() as directory:
            extension = MCPToolsExtension(
                (
                    MCPServerSettings(
                        name="fixture",
                        command=sys.executable,
                        args=("-u", "-c", _MCP_FIXTURE),
                        timeout=5,
                    ),
                ),
                Path(directory),
                CallbackPolicy(lambda request: True),
                (secret,),
            )
            with patch.dict("os.environ", {"CUSTOM_PROVIDER_CREDENTIAL": secret}):
                extension.register_tools(ToolRegistry(Path(directory), CallbackPolicy(lambda request: True)))
            try:
                self.assertNotIn(secret, extension.clients["fixture"].environment.values())
                self.assertNotIn("CUSTOM_PROVIDER_CREDENTIAL", extension.clients["fixture"].environment)
            finally:
                extension.close()

    def test_server_output_queue_overflow_terminates_the_child(self) -> None:
        class NoisyStdout:
            def __init__(self) -> None:
                self.remaining = 100

            def readline(self, limit: int) -> bytes:
                if self.remaining == 0:
                    return b""
                self.remaining -= 1
                return b'{"jsonrpc":"2.0","method":"noise"}\n'

        class ChildProcess:
            def __init__(self) -> None:
                self.stdout = NoisyStdout()
                self.terminated = False

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

        client = MCPStdioClient(
            MCPServerSettings("noisy", sys.executable),
            Path.cwd(),
            {},
        )
        client._responses = queue.Queue(maxsize=1)
        child = ChildProcess()
        client.process = child

        client._read_stdout()

        self.assertTrue(client._reader_overflow.is_set())
        self.assertTrue(child.terminated)
        self.assertEqual(client._responses.get_nowait(), None)

    def test_mcp_cwd_rejects_parent_directory_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agentdu.toml"
            config.write_text(
                "[mcp.servers.fixture]\n"
                "command = 'python'\n"
                "cwd = 'safe/../outside'\n",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigurationError):
                ModelSettings.load(config)


if __name__ == "__main__":
    unittest.main()

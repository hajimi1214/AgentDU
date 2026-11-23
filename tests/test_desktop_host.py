from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


class RuntimeHostProcess:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "runtime-data"
        self.data_dir.mkdir(parents=True)
        self.config_path = root / "missing-config.toml"
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.backlog: list[dict[str, Any]] = []
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "APPDATA": str(root / "appdata"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", str(root)),
            "TMP": os.environ.get("TMP", str(root)),
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "AGENTDU_CONFIG": str(self.config_path),
        }
        self.process = subprocess.Popen(
            [
                os.environ.get("AGENTDU_TEST_PYTHON", sys.executable),
                "-X",
                "utf8",
                "-m",
                "agentdu.desktop_host",
                "--data-dir",
                str(self.data_dir),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_reader.start()
        self._request_id = 0
        self.send("host.initialize", {})
        response = self.wait_for(
            lambda item: item.get("type") == "response" and item.get("requestId") == "1"
        )
        self.initial_response = response
        if "error" in response:
            raise AssertionError(f"Host initialization failed: {response}")

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while line := self.process.stdout.readline():
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.messages.put({"type": "invalid-json", "line": line})
            else:
                self.messages.put(message)
        self.messages.put(None)

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while self.process.stderr.read(4096):
            pass

    def send(
        self,
        method: str,
        params: dict[str, Any],
        *,
        version: int = 1,
        request_id: str | None = None,
    ) -> str:
        self._request_id += 1
        identifier = request_id or str(self._request_id)
        message = {
            "type": "request",
            "protocolVersion": version,
            "requestId": identifier,
            "method": method,
            "params": params,
        }
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        self.process.stdin.flush()
        return identifier

    def send_raw(self, line: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        for index, message in enumerate(self.backlog):
            if predicate(message):
                return self.backlog.pop(index)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for Host message. Backlog: {self.backlog[-5:]}")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("Timed out waiting for Host message.") from error
            if message is None:
                raise EOFError(f"Runtime Host exited with code {self.process.poll()}.")
            if predicate(message):
                return message
            self.backlog.append(message)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        identifier = self.send(method, params or {})
        return self.wait_for(
            lambda item: item.get("type") == "response" and item.get("requestId") == identifier
        )

    def wait_event(self, event: str, timeout: float = 5.0) -> dict[str, Any]:
        return self.wait_for(lambda item: item.get("type") == "event" and item.get("event") == event, timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class DesktopHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="agentdu-host-test-")
        self.root = Path(self.temporary_directory.name)
        self.host = RuntimeHostProcess(self.root)

    def tearDown(self) -> None:
        self.host.close()
        self.temporary_directory.cleanup()

    def test_initialize_and_health_report_provider_and_protocol_state(self) -> None:
        initial = self.host.initial_response
        self.assertEqual(initial["result"]["protocolVersion"], 1)
        self.assertEqual(initial["result"]["runtimeVersion"], "0.6.0")
        self.assertFalse(initial["result"]["providerConfigured"])
        self.assertEqual(initial["result"]["providerProfiles"], [])

        health = self.host.request("host.health")
        self.assertEqual(health["result"]["activeTasks"], 0)

    def test_model_task_reports_missing_provider_configuration(self) -> None:
        workspace = self.root / "provider workspace"
        workspace.mkdir()
        self.host.request("workspace.open", {"path": str(workspace)})

        response = self.host.request("turn.start", {"prompt": "Check the missing provider configuration."})

        self.assertEqual(response["error"]["code"], "PROVIDER_NOT_CONFIGURED")

    def test_profile_without_provider_credentials_is_reported_and_rejected(self) -> None:
        self.host.config_path.write_text(
            'default_profile = "remote"\n\n'
            '[profiles.remote]\n'
            'model = "d1-test-model"\n'
            'base_url = "https://api.example.test/v1"\n'
            'api_key_env = "OPENAI_API_KEY"\n',
            encoding="utf-8",
        )
        health = self.host.request("host.health")
        profile = health["result"]["providerProfiles"][0]
        self.assertTrue(profile["credentialAvailable"] is False)

        workspace = self.root / "credential workspace"
        workspace.mkdir()
        self.host.request("workspace.open", {"path": str(workspace)})
        response = self.host.request("turn.start", {"prompt": "Check the missing provider credential."})

        self.assertEqual(response["error"]["code"], "PROVIDER_CREDENTIAL_MISSING")

    def test_host_shutdown_method_and_stdin_eof_exit_cleanly(self) -> None:
        response = self.host.request("host.shutdown")
        self.assertEqual(response["result"]["status"], "stopped")
        event = self.host.wait_event("host.shutdown")
        self.assertEqual(event["payload"]["status"], "stopped")

        assert self.host.process.stdin is not None
        self.host.process.stdin.close()
        self.assertEqual(self.host.process.wait(timeout=3), 0)

    def test_desktop_permission_policy_blocks_command_execution(self) -> None:
        from agentdu.desktop_host import DesktopPermissionPolicy, RuntimeHost
        from agentdu.permissions import PermissionRequest

        permission_host = RuntimeHost(self.root / "permission-runtime")
        blocked_events: list[tuple[str, dict[str, Any]]] = []

        def capture_blocked_event(event: str, payload: dict[str, Any]) -> None:
            blocked_events.append((event, payload))

        permission_host.publish_from_worker = capture_blocked_event  # type: ignore[method-assign]
        marker = self.root / "must-not-be-created.txt"
        policy = DesktopPermissionPolicy(permission_host, "task-shell-test")

        authorized = policy.authorize(
            PermissionRequest(
                permission="execute",
                tool_name="run_command",
                arguments={"command": f"echo blocked > {marker}"},
                workspace=self.root,
            )
        )

        self.assertFalse(authorized)
        self.assertFalse(marker.exists())
        self.assertEqual(blocked_events[0][0], "security.capability_blocked")
        self.assertEqual(blocked_events[0][1]["taskId"], "task-shell-test")

    def test_runtime_executes_controlled_stream_and_cancels_the_real_agent_task(self) -> None:
        started = self.host.request("test.start")
        task_id = started["result"]["taskId"]
        self.assertTrue(task_id)
        self.host.wait_event("task.accepted")

        first_delta = self.host.wait_event("assistant_delta")
        second_delta = self.host.wait_event("assistant_delta")
        self.assertIn("stdio JSONL", first_delta["payload"]["text"])
        self.assertIn("受控 Runtime", second_delta["payload"]["text"])

        cancelled = self.host.request("turn.cancel", {"taskId": task_id})
        self.assertEqual(cancelled["result"]["status"], "cancelled")
        cancelled_event = self.host.wait_event("task.cancelled")
        self.assertEqual(cancelled_event["payload"]["taskId"], task_id)
        self.assertFalse(
            any(
                item.get("type") == "event" and item.get("event") == "task.completed"
                for item in self.host.backlog
            )
        )

    def test_repeated_request_id_does_not_start_a_second_test_task(self) -> None:
        first_id = self.host.send("test.start", {}, request_id="stable-test")
        first = self.host.wait_for(
            lambda item: item.get("type") == "response" and item.get("requestId") == first_id
        )
        duplicate_id = self.host.send("test.start", {}, request_id="stable-test")
        duplicate = self.host.wait_for(
            lambda item: item.get("type") == "response" and item.get("requestId") == duplicate_id
        )
        self.assertEqual(first["result"]["taskId"], duplicate["result"]["taskId"])
        self.host.wait_event("task.accepted")
        self.assertFalse(
            any(
                item.get("type") == "event"
                and item.get("event") == "task.accepted"
                and item.get("payload", {}).get("taskId") != first["result"]["taskId"]
                for item in self.host.backlog
            )
        )

    def test_unsupported_version_and_unknown_method_return_structured_errors(self) -> None:
        wrong_version = self.host.send("host.health", {}, version=99)
        error = self.host.wait_for(
            lambda item: item.get("type") == "response" and item.get("requestId") == wrong_version
        )
        self.assertEqual(error["error"]["code"], "UNSUPPORTED_PROTOCOL")

        unknown_method = self.host.request("shell.execute", {"command": "whoami"})
        self.assertEqual(unknown_method["error"]["code"], "METHOD_NOT_FOUND")

    def test_workspace_path_is_required_to_be_absolute_and_a_directory(self) -> None:
        relative = self.host.request("workspace.open", {"path": "."})
        self.assertEqual(relative["error"]["code"], "INVALID_WORKSPACE")

        workspace = self.root / "project with spaces"
        workspace.mkdir()
        opened = self.host.request("workspace.open", {"path": str(workspace)})
        self.assertEqual(Path(opened["result"]["path"]), workspace.resolve())

    def test_invalid_json_frame_is_reported_then_host_exits(self) -> None:
        self.host.send_raw(b"{this is not json}\n")
        error = self.host.wait_event("protocol.error")
        self.assertIn("UTF-8 JSON", error["payload"]["message"])
        self.assertEqual(self.host.process.wait(timeout=3), 2)

    def test_nonstandard_json_numbers_are_protocol_errors(self) -> None:
        self.host.send_raw(
            b'{"type":"request","protocolVersion":1,"requestId":"bad-number",'
            b'"method":"host.health","params":{"value":NaN}}\n'
        )
        error = self.host.wait_event("protocol.error")
        self.assertIn("UTF-8 JSON", error["payload"]["message"])
        self.assertEqual(self.host.process.wait(timeout=3), 2)


if __name__ == "__main__":
    unittest.main()

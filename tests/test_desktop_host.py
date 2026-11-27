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
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = root / "missing-config.toml"
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.backlog: list[dict[str, Any]] = []
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "local-appdata"),
            "USERPROFILE": str(root),
            "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
            "HOMEPATH": os.environ.get("HOMEPATH", "\\Users\\AgentDU-Test"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
            "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
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

    def test_desktop_command_permission_requires_explicit_approval(self) -> None:
        from agentdu.desktop_host import DesktopPermissionPolicy, RuntimeHost
        from agentdu.permissions import PermissionRequest

        permission_host = RuntimeHost(self.root / "permission-runtime")
        permission_events: list[tuple[str, dict[str, Any]]] = []

        def approve_denial_for_test(event: str, payload: dict[str, Any]) -> None:
            permission_events.append((event, payload))
            if event == "permission.requested":
                pending = permission_host._pending_approvals[payload["approvalId"]]
                pending.decision = False
                pending.event.set()

        permission_host.publish_from_worker = approve_denial_for_test  # type: ignore[method-assign]
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
        self.assertEqual(permission_events[0][0], "permission.requested")
        self.assertEqual(permission_events[0][1]["taskId"], "task-shell-test")
        self.assertEqual(permission_events[0][1]["operation"], "运行 AppContainer 隔离命令")
        self.assertEqual(permission_events[0][1]["command"], f"echo blocked > {marker}")
        self.assertFalse(permission_events[0][1]["networkRequired"])

    def test_workspace_inspection_skips_git_for_non_repository_directory(self) -> None:
        app_data = Path(os.environ.get("APPDATA") or tempfile.gettempdir())
        with tempfile.TemporaryDirectory(prefix="agentdu-plain-workspace-", dir=app_data) as directory:
            workspace = Path(directory)
            (workspace / "main.py").write_text("print('ready')", encoding="utf-8")
            self.host.request("workspace.open", {"path": str(workspace)})

            request_id = self.host.send("workspace.inspect", {})
            response = self.host.wait_for(
                lambda item: item.get("type") == "response" and item.get("requestId") == request_id,
                timeout=4,
            )

        self.assertNotIn("error", response)
        result = response["result"]
        self.assertEqual([item["path"] for item in result["files"]], ["main.py"])
        self.assertEqual(result["gitStatus"], "")
        self.assertEqual(result["gitError"], "当前工作区不在 Git 仓库中。")

    def test_workspace_inspection_scopes_git_status_to_selected_directory(self) -> None:
        import shutil

        git_executable = shutil.which("git")
        if git_executable is None:
            self.skipTest("Git is unavailable")

        app_data = Path(os.environ.get("APPDATA") or tempfile.gettempdir())
        with tempfile.TemporaryDirectory(prefix="agentdu-git-workspace-", dir=app_data) as directory:
            repository = Path(directory)
            subprocess.run(
                [git_executable, "init", "--quiet", str(repository)],
                capture_output=True,
                timeout=10,
                check=True,
            )
            (repository / "outside-workspace.txt").write_text("outside", encoding="utf-8")
            workspace = repository / "selected"
            workspace.mkdir()
            main_file = workspace / "main.py"
            main_file.write_text("print('ready')", encoding="utf-8")
            git_identity = (("user.name", "AgentDU Test"), ("user.email", "agentdu-test@example.invalid"))
            for key, value in git_identity:
                subprocess.run(
                    [git_executable, "-C", str(repository), "config", key, value],
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
            subprocess.run(
                [
                    git_executable,
                    "-C",
                    str(repository),
                    "add",
                    "outside-workspace.txt",
                    "selected/main.py",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            subprocess.run(
                [
                    git_executable,
                    "-C",
                    str(repository),
                    "commit",
                    "--quiet",
                    "-m",
                    "workspace scope baseline",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            (repository / "outside-workspace.txt").write_text("updated outside", encoding="utf-8")
            main_file.write_text("print('updated')", encoding="utf-8")
            (workspace / "new.py").write_text("print('new')", encoding="utf-8")
            self.host.request("workspace.open", {"path": str(workspace)})

            request_id = self.host.send("workspace.inspect", {})
            response = self.host.wait_for(
                lambda item: item.get("type") == "response" and item.get("requestId") == request_id,
                timeout=8,
            )

        self.assertNotIn("error", response)
        result = response["result"]
        self.assertRegex(result["gitStatus"], r"(?m)^ M main\.py$")
        self.assertRegex(result["gitStatus"], r"(?m)^\?\? new\.py$")
        self.assertNotIn("outside-workspace.txt", result["gitStatus"])
        self.assertIsNone(result["gitError"])

    def test_workspace_inspection_keeps_files_when_git_status_times_out(self) -> None:
        from unittest.mock import patch

        from agentdu.desktop_host import RuntimeHost

        workspace = self.root / "slow git workspace"
        workspace.mkdir()
        (workspace / ".git").mkdir()
        (workspace / "main.py").write_text("print('ready')", encoding="utf-8")
        inspector = RuntimeHost(self.root / "inspector-runtime")

        with patch(
            "agentdu.desktop_host._git_command",
            side_effect=subprocess.TimeoutExpired(["git", "status"], timeout=5),
        ):
            result = inspector._inspect_workspace(workspace)

        self.assertEqual([item["path"] for item in result["files"]], ["main.py"])
        self.assertEqual(result["gitStatus"], "")
        self.assertIn("timed out", result["gitError"] or "")

    def test_runtime_executes_controlled_stream_and_cancels_the_real_agent_task(self) -> None:
        started = self.host.request("test.start")
        task_id = started["result"]["taskId"]
        self.assertTrue(task_id)
        self.host.wait_event("task.accepted")

        first_delta = self.host.wait_event("assistant_delta", timeout=8.0)
        second_delta = self.host.wait_event("assistant_delta", timeout=8.0)
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

        session_files = list(self.host.data_dir.rglob(f"{started['result']['sessionId']}.jsonl"))
        self.assertEqual(len(session_files), 1)
        persisted_events = [
            json.loads(line)
            for line in session_files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_kinds = [event["kind"] for event in persisted_events]
        self.assertIn("task.accepted", event_kinds)
        self.assertIn("task.cancel_requested", event_kinds)
        self.assertIn("task.cancelled", event_kinds)
        self.assertNotIn("task.completed", event_kinds)

        completed_task = self.host.request("test.start")
        completed_task_id = completed_task["result"]["taskId"]
        self.host.wait_event("task.accepted")
        completion_event = self.host.wait_event("task.completed", timeout=30.0)
        self.assertEqual(completion_event["payload"]["taskId"], completed_task_id)

        completed_session = completed_task["result"]["sessionId"]
        completed_session_file = next(
            self.host.data_dir.rglob(f"{completed_session}.jsonl")
        )
        completed_events = [
            json.loads(line)
            for line in completed_session_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        completed_record = next(
            event for event in completed_events if event["kind"] == "task.completed"
        )
        self.assertGreaterEqual(completed_record["payload"]["modelCalls"], 1)
        self.assertNotIn("response", completed_record["payload"])

    def test_runtime_host_crash_persists_interrupted_task_once_after_restart(self) -> None:
        started = self.host.request("test.start")["result"]
        task_id = started["taskId"]
        session_id = started["sessionId"]
        accepted = self.host.wait_event("task.accepted")
        workspace_path = accepted["payload"]["workspacePath"]
        self.host.wait_event("assistant_delta", timeout=8.0)

        crashed_host = self.host
        crashed_host.process.kill()
        crashed_host.process.wait(timeout=5)
        crashed_host.close()
        self.host = RuntimeHostProcess(self.root)

        recovered = self.host.request(
            "task.recoverInterrupted",
            {
                "taskId": task_id,
                "sessionId": session_id,
                "workspacePath": workspace_path,
            },
        )
        self.assertEqual(recovered["result"]["status"], "task.interrupted")
        interrupted_event = self.host.wait_event("task.interrupted")
        self.assertEqual(interrupted_event["payload"]["taskId"], task_id)
        self.assertTrue(interrupted_event["payload"]["recovered"])
        self.assertEqual(
            interrupted_event["payload"]["message"],
            "Runtime stopped before this task recorded a result. Review the workspace before retrying it.",
        )

        repeated = self.host.request(
            "task.recoverInterrupted",
            {
                "taskId": task_id,
                "sessionId": session_id,
                "workspacePath": workspace_path,
            },
        )
        self.assertEqual(repeated["result"]["status"], "already_terminal")

        restored = self.host.request("workspace.open", {"path": workspace_path})
        self.assertNotIn("error", restored)
        session = self.host.request("session.read", {"sessionId": session_id})
        self.assertNotIn("error", session)
        terminal_events = [
            event for event in session["result"]["events"]
            if event["payload"].get("taskId") == task_id
            and event["kind"] in {
                "task.completed", "task.failed", "task.cancelled", "task.interrupted"
            }
        ]
        self.assertEqual([event["kind"] for event in terminal_events], ["task.interrupted"])

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

    def test_project_files_and_session_can_be_listed_and_restored(self) -> None:
        workspace = self.root / "project with spaces"
        workspace.mkdir()
        source = workspace / "src" / "sample.py"
        source.parent.mkdir()
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")
        opened = self.host.request("workspace.open", {"path": str(workspace)})
        self.assertEqual(opened["result"]["name"], workspace.name)

        recent = self.host.request("workspace.list")
        self.assertEqual(recent["result"]["workspaces"][0]["path"], str(workspace.resolve()))
        listing = self.host.request("workspace.files", {"query": "sample"})
        self.assertEqual([item["path"] for item in listing["result"]["files"]], ["src/sample.py"])
        content = self.host.request("file.read", {"path": "src/sample.py"})
        self.assertIn("return 42", content["result"]["content"])
        self.assertEqual(self.host.request("file.read", {"path": "../outside.txt"})["error"]["code"], "FILE_READ_FAILED")

        created = self.host.request("session.create")
        session_id = created["result"]["sessionId"]
        listed = self.host.request("sessions.list")
        self.assertEqual(listed["result"]["sessions"][0]["session_id"], session_id)
        restored = self.host.request("session.read", {"sessionId": session_id})
        self.assertEqual(restored["result"]["sessionId"], session_id)
        self.assertEqual(restored["result"]["events"][0]["kind"], "session_created")

    def test_desktop_editor_write_requires_approval_and_persists_decision_and_result(self) -> None:
        workspace = self.root / "editor project"
        workspace.mkdir()
        file_path = workspace / "notes.txt"
        file_path.write_text("before", encoding="utf-8")
        self.host.request("workspace.open", {"path": str(workspace)})
        session_id = self.host.request("session.create")["result"]["sessionId"]

        request_id = self.host.send("file.write", {"path": "notes.txt", "content": "after", "sessionId": session_id})
        accepted = self.host.wait_for(lambda item: item.get("type") == "response" and item.get("requestId") == request_id)
        self.assertEqual(accepted["result"]["status"], "pending")
        operation_id = accepted["result"]["operationId"]
        approval = self.host.wait_event("permission.requested")
        self.assertEqual(approval["payload"]["targetFiles"], ["notes.txt"])
        self.assertEqual(approval["payload"]["operationId"], operation_id)
        self.assertFalse(approval["payload"]["networkRequired"])
        response = self.host.request("permission.respond", {"approvalId": approval["payload"]["approvalId"], "decision": "allow"})
        self.assertEqual(response["result"]["status"], "allow")
        result = self.host.wait_event("workspace.write.finished")
        self.assertEqual(result["payload"]["operationId"], operation_id)
        self.assertEqual(result["payload"]["status"], "completed", result["payload"])
        self.assertEqual(file_path.read_text(encoding="utf-8"), "after")

        records = self.host.request("session.read", {"sessionId": session_id})["result"]["events"]
        write_events = [item["kind"] for item in records if item["kind"].startswith("desktop_file_write.")]
        self.assertEqual(write_events, ["desktop_file_write.requested", "desktop_file_write.approval", "desktop_file_write.result"])
        self.assertEqual(records[-1]["payload"]["status"], "completed")

    def test_desktop_file_write_rejects_git_metadata_and_escape_paths(self) -> None:
        workspace = self.root / "bounded project"
        workspace.mkdir()
        self.host.request("workspace.open", {"path": str(workspace)})
        session_id = self.host.request("session.create")["result"]["sessionId"]
        for relative_path in (".git/config", "../outside.txt"):
            result = self.host.request("file.write", {"path": relative_path, "content": "blocked", "sessionId": session_id})
            self.assertEqual(result["error"]["code"], "INVALID_FILE_PATH")

    def test_desktop_file_write_is_rejected_while_agent_task_is_active(self) -> None:
        workspace = self.root / "serialized project"
        workspace.mkdir()
        source = workspace / "source.txt"
        source.write_text("original", encoding="utf-8")
        self.host.request("workspace.open", {"path": str(workspace)})
        session_id = self.host.request("session.create")["result"]["sessionId"]
        started = self.host.request("test.start")
        self.host.wait_event("task.accepted")

        result = self.host.request(
            "file.write",
            {"path": "source.txt", "content": "overwritten", "sessionId": session_id},
        )

        self.assertEqual(result["error"]["code"], "TASK_ALREADY_RUNNING")
        self.assertEqual(source.read_text(encoding="utf-8"), "original")
        self.assertFalse(any(item.get("type") == "event" and item.get("event") == "permission.requested" for item in self.host.backlog))
        cancelled = self.host.request("turn.cancel", {"taskId": started["result"]["taskId"]})
        self.assertEqual(cancelled["result"]["status"], "cancelled")

    @unittest.skipUnless(os.name == "nt", "Windows AppContainer integration test")
    def test_terminal_command_is_approved_executed_and_persisted(self) -> None:
        workspace = self.root / "terminal project"
        workspace.mkdir()
        self.host.request("workspace.open", {"path": str(workspace)})
        session_id = self.host.request("session.create")["result"]["sessionId"]
        command = "echo AgentDU-terminal-verified"

        accepted = self.host.request("terminal.run", {"command": command, "sessionId": session_id})
        operation_id = accepted["result"]["operationId"]
        self.assertEqual(accepted["result"]["status"], "pending")
        approval = self.host.wait_event("permission.requested")
        self.assertEqual(approval["payload"]["command"], command)
        self.assertEqual(approval["payload"]["operationId"], operation_id)
        self.assertEqual(approval["payload"]["workspace"], str(workspace.resolve()))
        self.assertFalse(approval["payload"]["networkRequired"])
        self.host.request(
            "permission.respond",
            {"approvalId": approval["payload"]["approvalId"], "decision": "allow"},
        )

        result = self.host.wait_event("terminal.command.finished", timeout=30)
        self.assertEqual(result["payload"]["operationId"], operation_id)
        self.assertEqual(result["payload"]["status"], "completed", result["payload"])
        self.assertEqual(result["payload"]["exitCode"], 0)
        self.assertIn("AgentDU-terminal-verified", result["payload"]["output"])

        events = self.host.request("session.read", {"sessionId": session_id})["result"]["events"]
        terminal_events = [item for item in events if item["kind"].startswith("terminal.command.")]
        self.assertEqual(
            [item["kind"] for item in terminal_events],
            ["terminal.command.requested", "terminal.command.approval", "terminal.command.result"],
        )
        self.assertEqual(terminal_events[-1]["payload"]["exitCode"], 0)

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

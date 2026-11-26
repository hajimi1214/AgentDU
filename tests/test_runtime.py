from __future__ import annotations

import json
import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

from agentdu.agent import AgentRuntime, TruncatedResponseError
from agentdu.collaboration import HandoffCoordinator, HandoffValidationError
from agentdu.context import ContextManager, ContextOverflow
from agentdu.events import AgentEvent
from agentdu.handoff import HandoffConflict, HandoffLedger
from agentdu.models import AssistantMessage, Message, ModelStreamEvent, ToolCall, ToolSchema
from agentdu.permissions import AllowAllPolicy, PermissionRequest, ReadOnlyPolicy
from agentdu.provider import OpenAICompatibleModel, ProviderError, _parse_responses_message
from agentdu.sessions import SessionProcessLock, SessionStore
from agentdu.settings import ConfigurationError, ModelSettings, resolve_model_options
from agentdu.tools import MAX_TOOL_OUTPUT_CHARS, ToolDefinition, ToolRegistry, create_builtin_tools


class QueueModel:
    def __init__(self, answers: list[AssistantMessage]) -> None:
        self.answers = answers
        self.requests: list[list[Message]] = []

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> AssistantMessage:
        self.requests.append(list(messages))
        return self.answers.pop(0)


class RuntimeTests(unittest.TestCase):
    def test_handoff_decisions_reject_conflicting_or_duplicate_lines(self) -> None:
        self.assertTrue(HandoffCoordinator._report_accepts("Reviewed.\nAcceptance: PASS"))
        self.assertFalse(
            HandoffCoordinator._report_accepts(
                "Acceptance: PASS\nAcceptance: FAIL"
            )
        )
        self.assertFalse(
            HandoffCoordinator._report_accepts(
                "Acceptance: PASS\nAcceptance: PASS"
            )
        )
        self.assertTrue(
            HandoffCoordinator._has_exclusive_decision(
                "Handoff: ACCEPT", "HANDOFF: ACCEPT", "HANDOFF: REJECT"
            )
        )
        self.assertFalse(
            HandoffCoordinator._has_exclusive_decision(
                "Handoff: ACCEPT\nHandoff: REJECT",
                "HANDOFF: ACCEPT",
                "HANDOFF: REJECT",
            )
        )

    def test_session_lock_supports_nonblocking_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.lock"
            first = SessionProcessLock(path)
            second = SessionProcessLock(path)
            self.assertTrue(first.try_acquire())
            try:
                self.assertFalse(second.try_acquire())
            finally:
                first.release()
            self.assertTrue(second.try_acquire())
            second.release()

    def test_waiting_for_a_lock_can_be_cancelled(self) -> None:
        async def wait_and_cancel() -> None:
            task = asyncio.create_task(HandoffCoordinator._wait_for_lock(lambda: None))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)

        asyncio.run(wait_and_cancel())

    def test_retryable_provider_errors_use_bounded_backoff(self) -> None:
        class FlakyModel:
            calls = 0

            async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> AssistantMessage:
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("temporary overload", retryable=True)
                return AssistantMessage(content="Recovered")

        async def observe(event: AgentEvent) -> None:
            event_kinds.append(event.kind)

        with tempfile.TemporaryDirectory() as directory:
            event_kinds: list[str] = []
            root = Path(directory)
            model = FlakyModel()
            runtime = AgentRuntime(
                model,
                ToolRegistry(root, ReadOnlyPolicy()),
                SessionStore(root / ".agentdu"),
            )
            runtime.event_bus.subscribe(observe)
            result = asyncio.run(runtime.run("continue"))
            self.assertEqual(result.content, "Recovered")
            self.assertEqual(model.calls, 2)
            self.assertEqual(event_kinds.count("provider_retry"), 1)
            self.assertEqual(event_kinds.count("model_request"), 2)

    def test_project_tools_navigate_search_edit_and_show_git_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text("def combine(left, right):\n    return left - right\n", encoding="utf-8")
            nested = root / "pkg"
            nested.mkdir()
            (nested / "helper.py").write_text("TARGET = True\n", encoding="utf-8")
            registry = ToolRegistry(root, AllowAllPolicy())
            for tool in create_builtin_tools():
                registry.register(tool)

            git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
            if not git_executable.is_file():
                self.skipTest("Git is unavailable for the Git tool test")
            git_environment = os.environ.copy()
            git_environment["GIT_CONFIG_COUNT"] = "0"
            git_init = subprocess.run(
                [str(git_executable), "-C", str(root), "init"],
                capture_output=True,
                text=True,
                env=git_environment,
            )
            self.assertEqual(git_init.returncode, 0, git_init.stderr)
            subprocess.run(
                [str(git_executable), "-C", str(root), "add", "app.py", "pkg/helper.py"],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            subprocess.run(
                [
                    str(git_executable),
                    "-C",
                    str(root),
                    "-c",
                    "user.name=AgentDU Test",
                    "-c",
                    "user.email=agentdu-test@example.invalid",
                    "commit",
                    "-m",
                    "baseline",
                ],
                check=True,
                capture_output=True,
                env=git_environment,
            )

            files = asyncio.run(registry.execute("list_files", {"pattern": "**/*.py"}))
            limited_files = asyncio.run(
                registry.execute("list_files", {"pattern": "**/*.py", "limit": 1})
            )
            search = asyncio.run(registry.execute("search_text", {"pattern": "target"}))
            read = asyncio.run(registry.execute("read_file", {"path": "app.py"}))
            edit = asyncio.run(
                registry.execute(
                    "edit_file",
                    {
                        "path": "app.py",
                        "old_text": "return left - right",
                        "new_text": "return left + right",
                    },
                )
            )
            with patch.dict(
                os.environ,
                {"AGENTDU_GIT_EXECUTABLE": str(git_executable), "GIT_CONFIG_COUNT": "0"},
            ):
                diff = asyncio.run(registry.execute("git_diff", {"path": "app.py"}))

            self.assertIn("app.py", files.text)
            self.assertIn("pkg/helper.py", files.text)
            self.assertIn("showing 1 of 2 files", limited_files.text)
            self.assertIn("pkg/helper.py:1: TARGET = True", search.text)
            self.assertIn("2:     return left - right", read.text)
            self.assertFalse(edit.is_error)
            self.assertIn("-    return left - right", diff.text)
            self.assertIn("+    return left + right", diff.text)

    def test_agent_repairs_a_real_project_after_a_failing_test(self) -> None:
        class RepairModel:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> AssistantMessage:
                self.calls += 1
                calls = {
                    1: ("list_files", {"pattern": "**/*.py"}),
                    2: ("git_status", {}),
                    3: ("read_file", {"path": "test_app.py"}),
                    4: ("run_command", {"command": subprocess.list2cmdline([sys.executable, "-m", "unittest", "-v"])}),
                    5: ("read_file", {"path": "app.py"}),
                    6: (
                        "edit_file",
                        {
                            "path": "app.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    ),
                    7: ("read_file", {"path": "README.md"}),
                    8: (
                        "edit_file",
                        {
                            "path": "README.md",
                            "old_text": "subtracts the right operand",
                            "new_text": "adds the right operand",
                        },
                    ),
                    9: ("run_command", {"command": subprocess.list2cmdline([sys.executable, "-m", "unittest", "-v"])}),
                    10: ("git_diff", {}),
                }
                if self.calls == 11:
                    return AssistantMessage(content="Fixed the implementation and verified the test suite.")
                name, arguments = calls[self.calls]
                call_id = f"project-call-{self.calls}"
                return AssistantMessage(tool_calls=(ToolCall(call_id, name, arguments),))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
            if not git_executable.is_file():
                self.skipTest("Bundled Git is unavailable for the project integration test")
            git_environment = os.environ.copy()
            git_environment.update(
                {
                    "AGENTDU_GIT_EXECUTABLE": str(git_executable),
                    "GIT_CONFIG_COUNT": "0",
                }
            )
            subprocess.run(
                [str(git_executable), "-C", str(root), "init"],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            (root / "app.py").write_text(
                "def combine(left, right):\n    return left - right\n", encoding="utf-8"
            )
            (root / "test_app.py").write_text(
                "import unittest\nfrom app import combine\n\n"
                "class CombineTests(unittest.TestCase):\n"
                "    def test_combine_adds_values(self):\n"
                "        self.assertEqual(combine(2, 3), 5)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Example app\n\ncombine(left, right) subtracts the right operand.\n",
                encoding="utf-8",
            )
            subprocess.run(
                [str(git_executable), "-C", str(root), "add", "app.py", "test_app.py", "README.md"],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            subprocess.run(
                [
                    str(git_executable),
                    "-C",
                    str(root),
                    "-c",
                    "user.name=AgentDU Test",
                    "-c",
                    "user.email=agentdu-test@example.invalid",
                    "commit",
                    "-m",
                    "baseline",
                ],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            with patch.dict(
                os.environ,
                {
                    "AGENTDU_GIT_EXECUTABLE": str(git_executable),
                    "GIT_CONFIG_COUNT": "0",
                },
            ):
                model = RepairModel()
                registry = ToolRegistry(root, AllowAllPolicy())
                for tool in create_builtin_tools():
                    registry.register(tool)
                runtime = AgentRuntime(model, registry, SessionStore(root / ".agentdu"))

                result = asyncio.run(runtime.run("Find and fix the failing test"))

                messages = runtime.sessions.read_messages(result.session_id)
                tool_outputs = [
                    message["content"] for message in messages if message.get("role") == "tool"
                ]
                self.assertIn("FAILED (failures=1)", tool_outputs[3])
                self.assertIn("Ran 1 test", tool_outputs[8])
                self.assertIn("OK", tool_outputs[8])
                self.assertIn("+    return left + right", tool_outputs[9])
                self.assertIn("+combine(left, right) adds the right operand", tool_outputs[9])
                self.assertEqual(
                    (root / "app.py").read_text(encoding="utf-8"),
                    "def combine(left, right):\n    return left + right\n",
                )
                self.assertEqual(
                    (root / "README.md").read_text(encoding="utf-8"),
                    "# Example app\n\ncombine(left, right) adds the right operand.\n",
                )
                self.assertIn("verified the test suite", result.content)

    def test_extension_can_register_tool_and_observe_events(self) -> None:
        class EchoExtension:
            name = "echo"

            def __init__(self) -> None:
                self.events: list[str] = []

            def register_tools(self, registry: ToolRegistry) -> None:
                registry.register(
                    ToolDefinition(
                        "echo",
                        "Return the provided text",
                        {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                        "read",
                        lambda arguments, workspace: arguments["text"],
                    )
                )

            async def on_event(self, event: AgentEvent) -> None:
                self.events.append(event.kind)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension = EchoExtension()
            model = QueueModel(
                [
                    AssistantMessage(tool_calls=(ToolCall("echo-1", "echo", {"text": "hello"}),)),
                    AssistantMessage(content="hello"),
                ]
            )
            runtime = AgentRuntime(
                model,
                ToolRegistry(root, ReadOnlyPolicy()),
                SessionStore(root / ".agentdu"),
                extensions=[extension],
            )
            result = asyncio.run(runtime.run("Repeat hello"))
            self.assertEqual(result.content, "hello")
            self.assertIn("tool_finished", extension.events)

    def test_tool_loop_persists_messages_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = QueueModel(
                [
                    AssistantMessage(
                        tool_calls=(ToolCall("call-1", "write_file", {"path": "result.txt", "content": "done"}),)
                    ),
                    AssistantMessage(content="File written."),
                ]
            )
            registry = ToolRegistry(root, AllowAllPolicy())
            runtime = AgentRuntime(model, registry, SessionStore(root / ".agentdu"))

            result = asyncio.run(runtime.run("Create the result file"))

            self.assertEqual(result.content, "File written.")
            self.assertEqual(result.model_steps, 2)
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "done")
            messages = runtime.sessions.read_messages(result.session_id)
            self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "tool", "assistant"])
            event_kinds = [event["kind"] for event in runtime.sessions.read_events(result.session_id)]
            self.assertIn("tool_started", event_kinds)
            self.assertIn("tool_finished", event_kinds)
            self.assertEqual(event_kinds[-1], "run_completed")

    def test_permission_policy_denies_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(root, ReadOnlyPolicy())
            result = asyncio.run(registry.execute("write_file", {"path": "blocked.txt", "content": "x"}))
            self.assertTrue(result.is_error)
            self.assertFalse((root / "blocked.txt").exists())

    def test_workspace_paths_reject_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry(root, AllowAllPolicy())
            for tool in create_builtin_tools():
                registry.register(tool)
            result = asyncio.run(registry.execute("read_file", {"path": "../outside.txt"}))
            self.assertTrue(result.is_error)
            self.assertIn("inside the workspace", result.text)

    def test_context_drops_old_complete_turns(self) -> None:
        messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "old " * 600},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
        ]
        prepared = ContextManager(max_tokens=256).prepare(messages)
        self.assertTrue(any("omitted" in item.get("content", "") for item in prepared))
        self.assertEqual([item["role"] for item in prepared[-3:]], ["user", "assistant", "tool"])
        self.assertNotIn("old ", json.dumps(prepared))

    def test_context_budget_counts_non_ascii_conservatively(self) -> None:
        with self.assertRaises(ContextOverflow):
            ContextManager(max_tokens=256).prepare(
                [
                    {"role": "system", "content": "instructions"},
                    {"role": "user", "content": "请" * 300},
                ]
            )

    def test_context_budget_includes_tool_definitions(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "large_extension_tool",
                    "description": "x" * 1200,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with self.assertRaises(ContextOverflow):
            ContextManager(max_tokens=256).prepare(
                [{"role": "system", "content": "instructions"}, {"role": "user", "content": "help"}],
                tools,
            )

    def test_context_compacts_old_tool_exchanges_inside_a_long_turn(self) -> None:
        messages: list[Message] = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "Fix the failing tests and preserve behavior."},
        ]
        for index in range(20):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {"name": "run_command", "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"call-{index}",
                        "name": "run_command",
                        "content": f"output-{index} " * 180,
                    },
                ]
            )
        prepared = ContextManager(max_tokens=1800).prepare(messages)
        prepared_json = json.dumps(prepared)
        self.assertIn("Fix the failing tests", prepared_json)
        self.assertIn("earlier assistant/tool exchange(s)", prepared_json)
        self.assertIn("output-19", prepared_json)
        assistant_calls = {
            call["id"]
            for message in prepared
            for call in message.get("tool_calls", [])
        }
        tool_results = {
            message["tool_call_id"] for message in prepared if message.get("role") == "tool"
        }
        self.assertEqual(assistant_calls, tool_results)

    def test_interrupted_tool_call_is_closed_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session_id = store.create()
            store.append_message(session_id, {"role": "user", "content": "do work"})
            store.append_message(
                session_id,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call-1", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}
                    ],
                },
            )
            recovered = store.recover_interrupted_tool_calls(session_id)
            self.assertEqual(len(recovered), 1)
            self.assertIn("may have completed", recovered[0]["content"])
            self.assertEqual(store.recover_interrupted_tool_calls(session_id), [])

    def test_session_listing_and_cross_process_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / ".agentdu")
            session_id = store.create()
            store.append_message(session_id, {"role": "user", "content": "fix the parser"})
            listing = store.list_sessions()
            self.assertEqual(listing[0]["session_id"], session_id)
            self.assertEqual(listing[0]["last_prompt"], "fix the parser")

            lock = store.acquire_process_lock(session_id)
            source_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(source_root / "src")
            script = (
                "from pathlib import Path; "
                "from agentdu.sessions import SessionStore; "
                f"store=SessionStore(Path(r'{root / '.agentdu'}')); "
                "print('waiting', flush=True); "
                f"lock=store.acquire_process_lock('{session_id}'); "
                f"store.refresh_sequence('{session_id}'); "
                f"store.append('{session_id}', 'child_write', {{}}); "
                "print('locked', flush=True); lock.release()"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=source_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "waiting")
                time.sleep(0.25)
                early_exit = child.poll()
                if early_exit is not None:
                    stdout, stderr = child.communicate()
                    self.fail(f"lock subprocess exited before acquiring the lock ({early_exit}): {stdout}{stderr}")
            finally:
                lock.release()
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "locked")
            store.refresh_sequence(session_id)
            store.append(session_id, "parent_write", {})
            events = store.read_events(session_id)
            self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
            self.assertEqual([event["kind"] for event in events][-2:], ["child_write", "parent_write"])

    def test_recovered_call_gets_a_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / ".agentdu")
            session_id = store.create()
            store.append_message(session_id, {"role": "user", "content": "continue"})
            store.append_message(
                session_id,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "pending-call", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}
                    ],
                },
            )
            store.append(session_id, "tool_started", {"tool_call_id": "pending-call"})
            store.append_message(
                session_id,
                {
                    "role": "tool",
                    "tool_call_id": "pending-call",
                    "name": "run_command",
                    "content": "command completed before the process stopped",
                },
            )
            runtime = AgentRuntime(
                QueueModel([AssistantMessage(content="Recovered safely")]),
                ToolRegistry(root, AllowAllPolicy()),
                store,
            )

            asyncio.run(runtime.run("inspect before retry", session_id))

            recovered_finishes = [
                event
                for event in store.read_events(session_id)
                if event["kind"] == "tool_finished" and event["payload"].get("recovered")
            ]
            self.assertEqual(len(recovered_finishes), 1)
            self.assertTrue(recovered_finishes[0]["payload"]["result_recorded"])

    def test_incomplete_final_event_is_repaired_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_store = SessionStore(Path(directory))
            session_id = first_store.create()
            first_store.append(session_id, "checkpoint", {"ok": True})
            path = first_store.sessions_dir / f"{session_id}.jsonl"
            with path.open("ab") as file:
                file.write(b'{"seq":3,"payload":')

            resumed_store = SessionStore(Path(directory))
            resumed_store.append(session_id, "resumed", {"ok": True})

            events = resumed_store.read_events(session_id)
            self.assertEqual([event["seq"] for event in events], [1, 2, 3])
            self.assertEqual(events[-1]["kind"], "resumed")

    def test_incomplete_utf8_event_tail_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session_id = store.create()
            path = store.sessions_dir / f"{session_id}.jsonl"
            with path.open("ab") as file:
                file.write(b'{"seq":2,"kind":"broken","payload":{"text":"\xe4\xb8')

            reopened = SessionStore(Path(directory))
            reopened.append(session_id, "resumed", {"ok": True})
            self.assertEqual([event["kind"] for event in reopened.read_events(session_id)], ["session_created", "resumed"])

    def test_truncated_model_response_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / ".agentdu")
            session_id = store.create()
            runtime = AgentRuntime(
                QueueModel([AssistantMessage(content="partial", finish_reason="length")]),
                ToolRegistry(root, AllowAllPolicy()),
                store,
            )
            with self.assertRaises(TruncatedResponseError):
                asyncio.run(runtime.run("answer", session_id))
            event_kinds = [event["kind"] for event in runtime.sessions.read_events(session_id)]
            self.assertEqual(event_kinds[-1], "run_stopped")

    def test_command_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = subprocess.list2cmdline(
                [sys.executable, "-c", f"print('x' * {MAX_TOOL_OUTPUT_CHARS * 10})"]
            )
            registry = ToolRegistry(Path(directory), AllowAllPolicy())
            for tool in create_builtin_tools():
                registry.register(tool)
            result = asyncio.run(registry.execute("run_command", {"command": command}))
            self.assertIn("was truncated", result.text)
            self.assertLessEqual(len(result.text), MAX_TOOL_OUTPUT_CHARS + 100)

    def test_command_returning_with_a_background_child_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            parent_script = (
                "import subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
                "open(sys.argv[2],'w').write(str(child.pid))"
            )
            arguments = [
                sys.executable,
                "-c",
                parent_script,
                "import time; time.sleep(10)",
                str(pid_file),
            ]
            command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
            registry = ToolRegistry(root, AllowAllPolicy())
            for tool in create_builtin_tools():
                registry.register(tool)

            started_at = time.monotonic()
            result = asyncio.run(registry.execute("run_command", {"command": command}))
            elapsed = time.monotonic() - started_at

            try:
                self.assertLess(elapsed, 7)
                self.assertTrue(result.is_error)
                self.assertIn("background process retained stdout", result.text)
            finally:
                if pid_file.exists():
                    child_pid = int(pid_file.read_text(encoding="utf-8"))
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(child_pid)],
                            capture_output=True,
                            timeout=5,
                            check=False,
                        )
                    else:
                        try:
                            os.kill(child_pid, 9)
                        except ProcessLookupError:
                            pass

    def test_nonzero_command_exit_is_reported_as_tool_error(self) -> None:
        command = subprocess.list2cmdline([sys.executable, "-c", "raise SystemExit(7)"])
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(Path(directory), AllowAllPolicy())
            for tool in create_builtin_tools():
                registry.register(tool)
            result = asyncio.run(registry.execute("run_command", {"command": command}))
            self.assertTrue(result.is_error)
            self.assertIn("exit code: 7", result.text)

    def test_same_session_runs_are_serialized_across_runtime_instances(self) -> None:
        class BlockingModel:
            def __init__(self) -> None:
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> AssistantMessage:
                self.calls += 1
                call_number = self.calls
                if call_number == 1:
                    self.started.set()
                    await self.release.wait()
                return AssistantMessage(content=f"answer-{call_number}")

        async def scenario(root: Path) -> tuple[str, str, int]:
            model = BlockingModel()
            store = SessionStore(root / ".agentdu")
            session_id = store.create()
            runtimes = [
                AgentRuntime(model, ToolRegistry(root, ReadOnlyPolicy()), store),
                AgentRuntime(model, ToolRegistry(root, ReadOnlyPolicy()), SessionStore(root / ".agentdu")),
            ]
            first = asyncio.create_task(runtimes[0].run("first", session_id))
            await model.started.wait()
            second = asyncio.create_task(runtimes[1].run("second", session_id))
            await asyncio.sleep(0.03)
            calls_while_first_is_active = model.calls
            model.release.set()
            first_result, second_result = await asyncio.gather(first, second)
            return first_result.content, second_result.content, calls_while_first_is_active

        with tempfile.TemporaryDirectory() as directory:
            first, second, calls = asyncio.run(scenario(Path(directory)))
            self.assertEqual(calls, 1)
            self.assertEqual(first, "answer-1")
            self.assertEqual(second, "answer-2")

    def test_openai_compatible_http_tool_round_trip(self) -> None:
        state: dict[str, list[dict[str, object]]] = {"requests": []}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                state["requests"].append(json.loads(self.rfile.read(size)))
                if len(state["requests"]) == 1:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "http-call-1",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"http.txt","content":"via http"}',
                                },
                            }
                        ],
                    }
                    finish_reason = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "HTTP flow complete."}
                    finish_reason = "stop"
                response = json.dumps(
                    {"choices": [{"message": message, "finish_reason": finish_reason}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = OpenAICompatibleModel(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "test-model",
                    "test-key",
                    streaming=False,
                )
                runtime = AgentRuntime(
                    model,
                    ToolRegistry(root, AllowAllPolicy()),
                    SessionStore(root / ".agentdu"),
                )
                result = asyncio.run(runtime.run("Write a file"))
                self.assertEqual(result.content, "HTTP flow complete.")
                self.assertEqual((root / "http.txt").read_text(encoding="utf-8"), "via http")
                self.assertEqual(len(state["requests"]), 2)
                sent_messages = state["requests"][1]["messages"]
                self.assertEqual(sent_messages[-1]["role"], "tool")
                self.assertEqual(sent_messages[-1]["tool_call_id"], "http-call-1")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_responses_api_http_tool_round_trip(self) -> None:
        state: dict[str, list[dict[str, object]]] = {"requests": []}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                state["requests"].append(json.loads(self.rfile.read(size)))
                if len(state["requests"]) == 1:
                    output = [
                        {
                            "type": "function_call",
                            "id": "fc_write",
                            "call_id": "call-write",
                            "name": "write_file",
                            "arguments": '{"path":"responses.txt","content":"via responses"}',
                        }
                    ]
                else:
                    output = [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Responses flow complete."}],
                        }
                    ]
                response = json.dumps(
                    {
                        "status": "completed",
                        "output": output,
                        "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = OpenAICompatibleModel(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "test-model",
                    "test-key",
                    streaming=False,
                    wire_api="responses",
                )
                runtime = AgentRuntime(
                    model,
                    ToolRegistry(root, AllowAllPolicy()),
                    SessionStore(root / ".agentdu"),
                )
                result = asyncio.run(runtime.run("Write a file"))
                self.assertEqual(result.content, "Responses flow complete.")
                self.assertEqual((root / "responses.txt").read_text(encoding="utf-8"), "via responses")
                first_request, second_request = state["requests"]
                self.assertEqual(first_request["model"], "test-model")
                self.assertEqual(first_request["input"][-1], {"role": "user", "content": "Write a file"})
                self.assertEqual(first_request["tools"][0]["type"], "function")
                self.assertEqual(first_request["tools"][0]["name"], "edit_file")
                self.assertEqual(second_request["input"][-2]["type"], "function_call")
                self.assertEqual(second_request["input"][-1]["type"], "function_call_output")
                self.assertEqual(second_request["input"][-1]["call_id"], "call-write")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_responses_stream_returns_text_deltas_and_final_tool_calls(self) -> None:
        state: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                state["path"] = self.path
                events = [
                    {
                        "type": "response.output_text.delta",
                        "delta": "Inspecting ",
                    },
                    {
                        "type": "response.output_text.delta",
                        "delta": "the project.",
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": "Inspecting the project."}],
                                },
                                {
                                    "type": "function_call",
                                    "id": "fc_read",
                                    "call_id": "call-read",
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            ],
                            "usage": {"input_tokens": 7, "output_tokens": 5},
                        },
                    },
                ]
                body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            model = OpenAICompatibleModel(
                f"http://127.0.0.1:{server.server_port}/v1",
                "test-model",
                timeout=3,
                wire_api="responses",
            )

            async def collect() -> list[ModelStreamEvent]:
                return [event async for event in model.stream([], [])]

            events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
            self.assertEqual(state["path"], "/v1/responses")
            self.assertEqual([event.content_delta for event in events[:-1]], ["Inspecting ", "the project."])
            final = events[-1].message
            self.assertEqual(final.content, "Inspecting the project.")
            self.assertEqual(final.tool_calls[0].id, "call-read")
            self.assertEqual(final.tool_calls[0].arguments, {"path": "README.md"})
            self.assertEqual(final.usage, {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_responses_incomplete_filter_result_is_not_treated_as_success(self) -> None:
        with self.assertRaisesRegex(ProviderError, r"incomplete response \(content_filter\)"):
            _parse_responses_message(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "content_filter"},
                    "output": [],
                }
            )

        truncated = _parse_responses_message(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            }
        )
        self.assertEqual(truncated.finish_reason, "length")

    def test_cli_runs_against_a_local_openai_compatible_endpoint(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                response = (
                    "data: "
                    + json.dumps({"choices": [{"delta": {"content": "CLI runtime verified."}}]})
                    + "\n\n"
                    + "data: "
                    + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                    + "\n\n"
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_root = Path(__file__).resolve().parents[1]
                environment = os.environ.copy()
                for key in (
                    "AGENTDU_API_KEY",
                    "AGENTDU_BASE_URL",
                    "AGENTDU_MODEL",
                    "AGENTDU_PROFILE",
                ):
                    environment.pop(key, None)
                python_path = str(source_root / "src")
                environment["PYTHONPATH"] = os.pathsep.join(
                    item for item in [python_path, environment.get("PYTHONPATH", "")] if item
                )
                base_url = f"http://127.0.0.1:{server.server_port}/v1"
                config_path = root / "agentdu.toml"
                config_path.write_text(
                    "default_profile = 'local'\n"
                    "[profiles.local]\n"
                    "provider = 'openai-compatible'\n"
                    "model = 'test-model'\n"
                    f"base_url = '{base_url}'\n"
                    "streaming = true\n",
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agentdu",
                        "--workspace",
                        str(root),
                        "--config",
                        str(config_path),
                        "--profile",
                        "local",
                        "Inspect this empty workspace",
                    ],
                    cwd=source_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("CLI runtime verified.", completed.stdout)
                self.assertTrue((root / ".agentdu" / "sessions").is_dir())

                interactive = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agentdu",
                        "--workspace",
                        str(root),
                        "--config",
                        str(config_path),
                        "--profile",
                        "local",
                        "--interactive",
                    ],
                    cwd=source_root,
                    env=environment,
                    input="first interactive prompt\nsecond interactive prompt\n/exit\n",
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(interactive.returncode, 0, interactive.stderr)
                session_paths = list((root / ".agentdu" / "sessions").glob("*.jsonl"))
                self.assertEqual(len(session_paths), 2)
                interactive_session = next(
                    path
                    for path in session_paths
                    if any(
                        json.loads(line).get("payload", {}).get("message", {}).get("content")
                        == "first interactive prompt"
                        for line in path.read_text(encoding="utf-8").splitlines()
                    )
                )
                session_events = [
                    json.loads(line)
                    for line in interactive_session.read_text(encoding="utf-8").splitlines()
                ]
                stored_users = [
                    event["payload"]["message"]["content"]
                    for event in session_events
                    if event["kind"] == "message"
                    and event["payload"]["message"].get("role") == "user"
                ]
                self.assertEqual(
                    stored_users,
                    ["first interactive prompt", "second interactive prompt"],
                )

                resumed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agentdu",
                        "--workspace",
                        str(root),
                        "--session",
                        interactive_session.stem,
                        "--config",
                        str(config_path),
                        "--profile",
                        "local",
                        "third resumed prompt",
                    ],
                    cwd=source_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertIn("CLI runtime verified.", resumed.stdout)

                listing = subprocess.run(
                    [sys.executable, "-m", "agentdu", "--workspace", str(root), "--list-sessions"],
                    cwd=source_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn("third resumed prompt", listing.stdout)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_openai_stream_reassembles_fragmented_tool_calls(self) -> None:
        request_received = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request_received.set()
                chunks = [
                    {"choices": [{"delta": {"content": "Inspecting."}}]},
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-",
                                            "function": {"name": "read_", "arguments": '{"path":"'},
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "1",
                                            "function": {"name": "file", "arguments": 'README.md"}'},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "total_tokens": 10,
                        },
                    },
                ]
                body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                body += "data: [DONE]\n\n"
                response = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                self.wfile.flush()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            model = OpenAICompatibleModel(
                f"http://127.0.0.1:{server.server_port}/v1", "test-model", timeout=3
            )

            async def collect() -> list[ModelStreamEvent]:
                return [event async for event in model.stream([], [])]

            try:
                events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
            except TimeoutError:
                self.assertTrue(request_received.is_set(), "stream request did not reach the local endpoint")
                raise
            self.assertEqual(events[0].content_delta, "Inspecting.")
            final = events[-1].message
            self.assertEqual(final.tool_calls[0].id, "call-1")
            self.assertEqual(final.tool_calls[0].name, "read_file")
            self.assertEqual(final.tool_calls[0].arguments, {"path": "README.md"})
            self.assertEqual(final.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_model_profiles_resolve_endpoint_and_environment_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "default_profile = 'deepseek'\n"
                "[profiles.deepseek]\n"
                "provider = 'openai-compatible'\n"
                "model = 'deepseek-coder'\n"
                "base_url = 'https://example.invalid/v1'\n"
                "api_key_env = 'TEST_AGENTDU_KEY'\n"
                "wire_api = 'responses'\n"
                "streaming = false\n",
                encoding="utf-8",
            )
            settings = ModelSettings.load(path)
            with patch.dict(os.environ, {"TEST_AGENTDU_KEY": "secret-value"}, clear=False):
                options = resolve_model_options(settings, None, None, None, None)
            self.assertEqual(options["model"], "deepseek-coder")
            self.assertEqual(options["base_url"], "https://example.invalid/v1")
            self.assertEqual(options["api_key"], "secret-value")
            self.assertFalse(options["streaming"])
            self.assertEqual(options["wire_api"], "responses")

    def test_model_profile_rejects_non_finite_numeric_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[profiles.example]\n"
                "model = 'test-model'\n"
                "temperature = nan\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "finite number"):
                ModelSettings.load(path)

    def test_model_usage_and_configured_cost_are_accumulated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("usage test\n", encoding="utf-8")
            model = QueueModel(
                [
                    AssistantMessage(
                        content="Reading the file",
                        tool_calls=(ToolCall("usage-read", "read_file", {"path": "README.md"}),),
                        usage={"input_tokens": 1_000, "output_tokens": 500, "total_tokens": 1_500},
                    ),
                    AssistantMessage(
                        content="Second response",
                        usage={"input_tokens": 2_000, "output_tokens": 1_000, "total_tokens": 3_000},
                    ),
                ]
            )
            model.input_cost_per_million = 0.5
            model.output_cost_per_million = 1.0
            runtime = AgentRuntime(
                model,
                ToolRegistry(root, ReadOnlyPolicy()),
                SessionStore(root / ".agentdu"),
            )

            result = asyncio.run(runtime.run("Count provider usage"))

            self.assertEqual(
                result.model_usage,
                {"input_tokens": 3_000, "output_tokens": 1_500, "total_tokens": 4_500},
            )
            self.assertAlmostEqual(result.estimated_cost_usd, 0.003)

    def test_missing_usage_makes_cost_estimate_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("usage test\n", encoding="utf-8")
            model = QueueModel(
                [
                    AssistantMessage(
                        content="Reading the file",
                        tool_calls=(ToolCall("usage-read", "read_file", {"path": "README.md"}),),
                        usage=None,
                    ),
                    AssistantMessage(
                        content="Second response",
                        usage={"input_tokens": 2_000, "output_tokens": 1_000},
                    ),
                ]
            )
            model.input_cost_per_million = 0.5
            model.output_cost_per_million = 1.0
            runtime = AgentRuntime(
                model,
                ToolRegistry(root, ReadOnlyPolicy()),
                SessionStore(root / ".agentdu"),
            )

            result = asyncio.run(runtime.run("Count provider usage"))

            self.assertIsNone(result.estimated_cost_usd)
            events = runtime.sessions.read_events(result.session_id)
            usage_events = [event for event in events if event["kind"] == "model_usage"]
            self.assertIsNone(usage_events[0]["payload"]["usage"])
            self.assertIsNotNone(usage_events[-1]["payload"]["usage"])
            self.assertIsNone(usage_events[-1]["payload"]["estimated_cost_usd"])

    def test_handoff_ledger_enforces_owner_and_version_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = HandoffLedger(Path(directory))
            state = ledger.create("builder", "reviewer", {"base_commit": "abc"})
            self.assertEqual((state.status, state.version, state.owner), ("source_running", 1, "builder"))

            offered = ledger.transition(
                state.handoff_id,
                state.version,
                "source_running",
                "offered",
                "builder",
                {"source_commit": "def"},
            )
            with self.assertRaises(HandoffConflict):
                ledger.transition(
                    offered.handoff_id,
                    offered.version,
                    "offered",
                    "accepted",
                    "builder",
                )

            accepted = ledger.transition(
                offered.handoff_id,
                offered.version,
                "offered",
                "accepted",
                "reviewer",
            )
            ledger.record_evidence(
                accepted.handoff_id,
                accepted.version,
                "reviewer",
                {"command": "python -m unittest", "passed": True},
            )
            with self.assertRaises(HandoffConflict):
                ledger.transition(
                    accepted.handoff_id,
                    offered.version,
                    "offered",
                    "accepted",
                    "reviewer",
                )
            self.assertEqual(ledger.read(accepted.handoff_id).version, accepted.version)

            stale = ledger.create("builder", "reviewer", {"base_commit": "abc"})
            stale = ledger.transition(
                stale.handoff_id,
                stale.version,
                "source_running",
                "stale",
                "coordinator",
                {"stale_reason": "The original project changed"},
            )
            self.assertEqual(stale.status, "stale")
            self.assertEqual(stale.owner, "coordinator")

    def test_two_profiles_complete_a_verified_git_handoff(self) -> None:
        def create_endpoint(responses: list[dict[str, object]]):
            state = {"requests": [], "responses": list(responses)}

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    size = int(self.headers["Content-Length"])
                    state["requests"].append(json.loads(self.rfile.read(size)))
                    scripted = state["responses"].pop(0)
                    if "content" in scripted:
                        message = {"role": "assistant", "content": scripted["content"]}
                        finish_reason = "stop"
                    else:
                        index = len(state["requests"])
                        message = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"{scripted['prefix']}-{index}",
                                    "type": "function",
                                    "function": {
                                        "name": scripted["name"],
                                        "arguments": json.dumps(scripted["arguments"]),
                                    },
                                }
                            ],
                        }
                        finish_reason = "tool_calls"
                    body = json.dumps(
                        {"choices": [{"message": message, "finish_reason": finish_reason}]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format: str, *args: object) -> None:
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            return server, worker, state

        source_server, source_thread, source_state = create_endpoint(
            [
                {"prefix": "source", "name": "read_file", "arguments": {"path": "app.py"}},
                {
                    "prefix": "source",
                    "name": "run_command",
                    "arguments": {
                        "command": subprocess.list2cmdline(
                            [sys.executable, "-m", "unittest", "test_app", "-v"]
                        )
                    },
                },
                {
                    "prefix": "source",
                    "name": "edit_file",
                    "arguments": {
                        "path": "app.py",
                        "old_text": "return left - right",
                        "new_text": "return left + right",
                    },
                },
                {
                    "prefix": "source",
                    "name": "run_command",
                    "arguments": {
                        "command": subprocess.list2cmdline(
                            [sys.executable, "-m", "unittest", "test_app", "-v"]
                        )
                    },
                },
                {
                    "content": "Changed app.py. The first test run failed as expected, then the test passed. No unresolved issues.",
                },
            ]
        )
        receiver_server, receiver_thread, receiver_state = create_endpoint(
            [
                {
                    "prefix": "receiver-ack",
                    "name": "edit_file",
                    "arguments": {
                        "path": "app.py",
                        "old_text": "return left + right",
                        "new_text": "return left - right",
                    },
                },
                {"content": "Handoff: ACCEPT"},
                {"prefix": "receiver", "name": "read_file", "arguments": {"path": "app.py"}},
                {"prefix": "receiver", "name": "git_diff", "arguments": {}},
                {
                    "content": (
                        "I inspected the committed change and confirmed the source evidence. "
                        "No further changes are needed.\nAcceptance: PASS"
                    )
                },
            ]
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
                if not git_executable.is_file():
                    self.skipTest("Bundled Git is unavailable for the handoff integration test")
                git_environment = os.environ.copy()
                git_environment["AGENTDU_GIT_EXECUTABLE"] = str(git_executable)
                git_environment["GIT_CONFIG_COUNT"] = "0"
                (root / ".gitignore").write_text("__pycache__/\n*.py[cod]\n.agentdu/\n", encoding="utf-8")
                (root / "app.py").write_text(
                    "def combine(left, right):\n    return left - right\n", encoding="utf-8"
                )
                (root / "test_app.py").write_text(
                    "import unittest\nfrom app import combine\n\n"
                    "class CombineTests(unittest.TestCase):\n"
                    "    def test_combine_adds_values(self):\n"
                    "        self.assertEqual(combine(2, 3), 5)\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    [str(git_executable), "-C", str(root), "init"],
                    check=True,
                    capture_output=True,
                    env=git_environment,
                )
                subprocess.run(
                    [str(git_executable), "-C", str(root), "add", "."],
                    check=True,
                    capture_output=True,
                    env=git_environment,
                )
                subprocess.run(
                    [
                        str(git_executable),
                        "-C",
                        str(root),
                        "-c",
                        "user.name=AgentDU Test",
                        "-c",
                        "user.email=agentdu-test@example.invalid",
                        "commit",
                        "-m",
                        "baseline",
                    ],
                    check=True,
                    capture_output=True,
                    env=git_environment,
                )
                config = root / ".agentdu" / "agentdu.toml"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(
                    "[profiles.builder]\n"
                    "provider = 'openai-compatible'\n"
                    "model = 'writer-model'\n"
                    f"base_url = 'http://127.0.0.1:{source_server.server_port}/v1'\n"
                    "streaming = false\n\n"
                    "[profiles.reviewer]\n"
                    "provider = 'openai-compatible'\n"
                    "model = 'reviewer-model'\n"
                    f"base_url = 'http://127.0.0.1:{receiver_server.server_port}/v1'\n"
                    "streaming = false\n",
                    encoding="utf-8",
                )
                from agentdu.settings import ModelSettings

                with patch.dict(
                    os.environ,
                    {"AGENTDU_GIT_EXECUTABLE": str(git_executable), "GIT_CONFIG_COUNT": "0"},
                ):
                    coordinator = HandoffCoordinator(
                        root,
                        ModelSettings.load(config),
                        AllowAllPolicy(),
                    )
                    outcome = asyncio.run(
                        coordinator.run(
                            "Fix combine so it adds values and verify the change.",
                            "builder",
                            "reviewer",
                            [
                                subprocess.list2cmdline(
                                    [sys.executable, "-m", "unittest", "test_app", "-v"]
                                )
                            ],
                        )
                    )
                    self.assertEqual(outcome.base_commit != outcome.source_commit, True)
                    self.assertEqual(outcome.source_commit, outcome.result_commit)
                    self.assertIn("app.py", outcome.changed_files)
                    self.assertEqual(len(outcome.acceptance_checks), 1)
                    self.assertTrue(outcome.acceptance_checks[0]["passed"])
                    self.assertEqual(
                        (root / "app.py").read_text(encoding="utf-8"),
                        "def combine(left, right):\n    return left + right\n",
                    )
                    self.assertEqual(
                        coordinator.ledger.read(outcome.handoff_id).status,
                        "integrated",
                    )
                    coordinator_session_path = (
                        root
                        / ".agentdu"
                        / "sessions"
                        / f"{outcome.coordinator_session_id}.jsonl"
                    )
                    coordinator_events = coordinator_session_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    last_event = json.loads(coordinator_events[-1])
                    self.assertEqual(last_event["kind"], "collaboration_completed")
                    coordinator_session_path.write_text(
                        "\n".join(coordinator_events[:-1]) + "\n", encoding="utf-8"
                    )
                    coordinator.coordinator_sessions.refresh_sequence(
                        outcome.coordinator_session_id
                    )
                    asyncio.run(coordinator._ensure_completion_events(""))
                    recovered_events = coordinator.coordinator_sessions.read_events(
                        outcome.coordinator_session_id
                    )
                    completion_events = [
                        event
                        for event in recovered_events
                        if event["kind"] == "collaboration_completed"
                        and event["payload"].get("handoff_id") == outcome.handoff_id
                    ]
                    self.assertEqual(len(completion_events), 1)
                    self.assertTrue(completion_events[0]["payload"]["recovered_after_restart"])
                    source_evidence = coordinator.ledger.read(outcome.handoff_id).data[
                        "source_package"
                    ]["execution_evidence"]
                    self.assertEqual(len(source_evidence), 2)
                    self.assertIn("FAILED (failures=1)", source_evidence[0]["result_excerpt"])
                    self.assertIn("OK", source_evidence[1]["result_excerpt"])
                    self.assertEqual(
                        [request["model"] for request in source_state["requests"]],
                        ["writer-model"] * 5,
                    )
                    self.assertEqual(
                        [request["model"] for request in receiver_state["requests"]],
                        ["reviewer-model"] * 5,
                    )
                    receiver_messages = receiver_state["requests"][0]["messages"]
                    self.assertFalse(any(message["role"] == "tool" for message in receiver_messages))
                    self.assertIn(outcome.handoff_id, receiver_messages[-1]["content"])
                    receiver_session = coordinator.ledger.read(outcome.handoff_id).data[
                        "receiver_session_id"
                    ]
                    acknowledgement_messages = SessionStore(
                        root
                        / ".agentdu"
                        / "agents"
                        / outcome.handoff_id
                        / "receiver"
                    ).read_messages(receiver_session)
                    self.assertTrue(
                        any(
                            message.get("role") == "tool"
                            and "Permission denied" in message.get("content", "")
                            for message in acknowledgement_messages
                        )
                    )
        finally:
            source_server.shutdown()
            source_server.server_close()
            source_thread.join(timeout=2)
            receiver_server.shutdown()
            receiver_server.server_close()
            receiver_thread.join(timeout=2)

    def test_handoff_validation_failure_does_not_integrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = HandoffLedger(Path(directory))
            state = ledger.create("builder", "reviewer", {"base_commit": "abc"})
            offered = ledger.transition(
                state.handoff_id,
                state.version,
                "source_running",
                "offered",
                "builder",
            )
            accepted = ledger.transition(
                offered.handoff_id,
                offered.version,
                "offered",
                "accepted",
                "reviewer",
            )
            running = ledger.transition(
                accepted.handoff_id,
                accepted.version,
                "accepted",
                "receiver_running",
                "reviewer",
            )
            failed = ledger.transition(
                running.handoff_id,
                running.version,
                "receiver_running",
                "validation_failed",
                "reviewer",
                {"acceptance_evidence": [{"command": "test", "passed": False}]},
            )
            self.assertEqual(failed.status, "validation_failed")
            self.assertEqual(ledger.active_states(), [])

            pending = ledger.create("builder", "reviewer", {"base_commit": "abc"})
            rejected = ledger.transition(
                pending.handoff_id,
                pending.version,
                "source_running",
                "offered",
                "builder",
            )
            rejected = ledger.transition(
                rejected.handoff_id,
                rejected.version,
                "offered",
                "rejected",
                "reviewer",
                {"rejection_reason": "Receiver declined"},
            )
            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(rejected.owner, "coordinator")

    def test_handoff_cli_resolves_repository_root_from_subdirectory(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        git_executable = source_root / ".tools" / "mingit" / "cmd" / "git.exe"
        if not git_executable.is_file():
            self.skipTest("Bundled Git is unavailable for the handoff CLI test")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            (root / "README.md").write_text("handoff cli test\n", encoding="utf-8")
            git_environment = os.environ.copy()
            git_environment["AGENTDU_GIT_EXECUTABLE"] = str(git_executable)
            git_environment["GIT_CONFIG_COUNT"] = "0"
            subprocess.run(
                [str(git_executable), "-C", str(root), "init"],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            subprocess.run(
                [str(git_executable), "-C", str(root), "add", "README.md"],
                check=True,
                capture_output=True,
                env=git_environment,
            )
            subprocess.run(
                [
                    str(git_executable),
                    "-C",
                    str(root),
                    "-c",
                    "user.name=AgentDU Test",
                    "-c",
                    "user.email=agentdu-test@example.invalid",
                    "commit",
                    "-m",
                    "baseline",
                ],
                check=True,
                capture_output=True,
                env=git_environment,
            )

            state = HandoffLedger(root / ".agentdu" / "handoffs").create(
                "builder", "reviewer", {"base_commit": "baseline"}
            )
            environment = git_environment.copy()
            environment["PYTHONPATH"] = str(source_root / "src")
            listing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentdu",
                    "--workspace",
                    str(nested),
                    "--list-handoffs",
                ],
                cwd=source_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn(state.handoff_id, listing.stdout)

            abandoned = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentdu",
                    "--workspace",
                    str(nested),
                    "--abandon-handoff",
                    state.handoff_id,
                ],
                cwd=source_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            self.assertEqual(
                HandoffLedger(root / ".agentdu" / "handoffs").read(state.handoff_id).status,
                "abandoned",
            )
            api_state = HandoffLedger(root / ".agentdu" / "handoffs").create(
                "builder", "reviewer", {"base_commit": "baseline"}
            )
            with patch.dict(
                os.environ,
                {"AGENTDU_GIT_EXECUTABLE": str(git_executable), "GIT_CONFIG_COUNT": "0"},
            ):
                coordinator = HandoffCoordinator(
                    root,
                    ModelSettings(profiles={}, default_profile=None),
                    AllowAllPolicy(),
                )
                api_abandoned = asyncio.run(
                    coordinator.abandon_handoff(api_state.handoff_id, "test cleanup")
                )
            self.assertEqual(api_abandoned.status, "abandoned")

    def test_model_configuration_rejects_unsupported_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[profiles.example]\nprovider = 'native-anthropic'\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                ModelSettings.load(path)


if __name__ == "__main__":
    unittest.main()

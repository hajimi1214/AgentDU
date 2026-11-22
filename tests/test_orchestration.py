from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agentdu.agent import ModelCallBudgetExceeded, build_default_runtime
from agentdu.git_workspaces import GitWorkspaceError, GitWorkspaces
from agentdu.models import AssistantMessage, ToolCall
from agentdu.orchestration import AutonomousCoordinator, TaskStateStore
from agentdu.permissions import AllowAllPolicy
from agentdu.provider import OpenAICompatibleModel
from agentdu.settings import ModelProfile, ModelSettings


class QueueModel:
    def __init__(self, answers: list[AssistantMessage]) -> None:
        self.answers = answers
        self.requests: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantMessage:
        self.requests.append(messages)
        return self.answers.pop(0)


class OrchestrationTests(unittest.TestCase):
    @staticmethod
    def _initialize_git_repository(root: Path, git_executable: Path) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "AGENTDU_GIT_EXECUTABLE": str(git_executable),
                "GIT_CONFIG_COUNT": "0",
            }
        )
        (root / ".gitignore").write_text(".agentdu/\n", encoding="utf-8")
        (root / "base.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(
            [str(git_executable), "-C", str(root), "init"],
            check=True,
            capture_output=True,
            env=environment,
        )
        subprocess.run(
            [str(git_executable), "-C", str(root), "add", "."],
            check=True,
            capture_output=True,
            env=environment,
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
            env=environment,
        )

    def test_model_call_budget_stops_before_an_extra_provider_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
            model = QueueModel(
                [
                    AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                "read-1",
                                "read_file",
                                {"path": "sample.py"},
                            ),
                        )
                    ),
                    AssistantMessage(content="This must not be requested."),
                ]
            )
            runtime = build_default_runtime(
                workspace,
                model,
                AllowAllPolicy(),
                max_model_calls=1,
            )
            with self.assertRaises(ModelCallBudgetExceeded):
                asyncio.run(runtime.run("Read sample.py"))
            self.assertEqual(len(model.requests), 1)

    def test_dependencies_wait_for_implementation_integration(self) -> None:
        records = {
            "implementation": {"kind": "implementation", "status": "completed"},
            "consultation": {"kind": "consultation", "status": "completed"},
            "review": {"kind": "review", "status": "completed"},
        }
        self.assertEqual(
            AutonomousCoordinator._dependency_state(["implementation"], records),
            "waiting",
        )
        records["implementation"]["status"] = "integrated"
        self.assertEqual(
            AutonomousCoordinator._dependency_state(["implementation"], records),
            "ready",
        )
        self.assertEqual(
            AutonomousCoordinator._dependency_state(["consultation", "review"], records),
            "ready",
        )

    def test_scheduler_reserves_remaining_calls_and_releases_unused_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            endpoint = "http://127.0.0.1:1/v1"
            settings = ModelSettings(
                profiles={
                    "main": ModelProfile("main", "main", endpoint),
                    "worker": ModelProfile("worker", "worker", endpoint),
                },
                default_profile="main",
            )
            manager = AutonomousCoordinator(
                Path(directory),
                settings,
                {"model": "main", "base_url": endpoint},
                AllowAllPolicy(),
                total_model_calls=5,
                task_model_calls=3,
            )
            session_id = "d" * 32
            store = manager._store(session_id)
            store.add(
                {
                    "task_id": "e" * 12,
                    "status": "running",
                    "model_calls": 1,
                    "model_call_limit": 3,
                }
            )
            store.add(
                {
                    "task_id": "f" * 12,
                    "status": "completed",
                    "model_calls": 2,
                    "model_call_limit": 2,
                }
            )
            self.assertEqual(manager._available_model_calls(session_id), 0)
            store.update("e" * 12, status="completed")
            self.assertEqual(manager._available_model_calls(session_id), 2)
            manager.close(wait=False)

    def test_consultation_uses_bounded_context_without_tools(self) -> None:
        state: dict[str, Any] = {"main_requests": 0, "consult_tools": None}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                if request["model"] == "advisor":
                    state["consult_tools"] = request.get("tools", [])
                    message = {
                        "role": "assistant",
                        "content": "The supplied evidence favors the simpler option.",
                    }
                    finish = "stop"
                else:
                    state["main_requests"] += 1
                    if state["main_requests"] == 1:
                        message = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "consult-1",
                                    "type": "function",
                                    "function": {
                                        "name": "consult_model",
                                        "arguments": json.dumps(
                                            {
                                                "profile": "advisor_profile",
                                                "question": "Which option is simpler?",
                                                "context": "Option A has one component. Option B has four.",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                        finish = "tool_calls"
                    else:
                        message = {"role": "assistant", "content": "I used the model consultation."}
                        finish = "stop"
                body = json.dumps({"choices": [{"message": message, "finish_reason": finish}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                base_url = f"http://127.0.0.1:{server.server_port}/v1"
                settings = ModelSettings(
                    profiles={
                        "main": ModelProfile("main", "main", base_url, streaming=False),
                        "advisor_profile": ModelProfile(
                            "advisor_profile", "advisor", base_url, streaming=False
                        ),
                    },
                    default_profile="main",
                )
                main_options = {
                    "model": "main",
                    "base_url": base_url,
                    "api_key": None,
                    "timeout": 5,
                    "temperature": 0,
                    "streaming": False,
                }
                runtime = build_default_runtime(
                    workspace,
                    OpenAICompatibleModel(**main_options),
                    AllowAllPolicy(),
                )
                coordinator = AutonomousCoordinator(
                    workspace,
                    settings,
                    main_options,
                    AllowAllPolicy(),
                    total_model_calls=4,
                    task_model_calls=2,
                )
                coordinator.install(runtime)
                result = asyncio.run(runtime.run("Choose between two designs."))
                tasks = coordinator.finish_session(result.session_id)
                self.assertEqual(tasks[0]["status"], "completed")
                self.assertIn("simpler option", tasks[0]["report"])
                self.assertEqual(tasks[0]["model_calls"], 1)
                self.assertEqual(state["consult_tools"], [])
                self.assertIn("consultation", result.content)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_task_state_marks_running_work_interrupted_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            session_id = "a" * 32
            store = TaskStateStore(workspace, session_id)
            store.add(
                {
                    "task_id": "b" * 12,
                    "status": "running",
                    "updated_at": "now",
                    "events": [],
                }
            )
            reopened = TaskStateStore(workspace, session_id)
            record = reopened.get("b" * 12)
            self.assertEqual(record["status"], "interrupted")
            self.assertIn("process stopped", record["error"])

    def test_failed_acceptance_check_does_not_change_main_worktree(self) -> None:
        git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
        if not git_executable.is_file():
            self.skipTest("Bundled Git is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_git_repository(root, git_executable)
            with patch.dict(os.environ, {"AGENTDU_GIT_EXECUTABLE": str(git_executable)}):
                git = GitWorkspaces(root)
                base = git.require_clean()
                worker = git.add_worktree("a" * 12, "worker", "agentdu/test-failed-check", base)
                (worker / "result.txt").write_text("unaccepted\n", encoding="utf-8")
                result_commit = git.commit_changes(worker, "test implementation")
                candidate_commits: list[str] = []

                with self.assertRaises(GitWorkspaceError):
                    git.merge_verified_result(
                        base,
                        result_commit,
                        git.changed_files(base, result_commit),
                        git.diff_digest(base, result_commit),
                        lambda candidate: [
                            {"command": "false", "passed": False, "output": "rejected"}
                        ],
                        "AgentDU test merge",
                        "integration-failed-check",
                        candidate_commits.append,
                    )

                self.assertEqual(git.head(), base)
                self.assertEqual(git.status(), "")
                self.assertFalse((root / "result.txt").exists())
                self.assertEqual(candidate_commits, [])

    def test_conflicting_task_result_is_rejected_without_main_changes(self) -> None:
        git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
        if not git_executable.is_file():
            self.skipTest("Bundled Git is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_git_repository(root, git_executable)
            (root / "shared.txt").write_text("baseline\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["GIT_CONFIG_COUNT"] = "0"
            subprocess.run(
                [str(git_executable), "-C", str(root), "add", "shared.txt"],
                check=True,
                capture_output=True,
                env=environment,
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
                    "add shared file",
                ],
                check=True,
                capture_output=True,
                env=environment,
            )
            with patch.dict(os.environ, {"AGENTDU_GIT_EXECUTABLE": str(git_executable)}):
                git = GitWorkspaces(root)
                base = git.head()
                worker = git.add_worktree("c" * 12, "worker", "agentdu/test-conflict", base)
                (worker / "shared.txt").write_text("worker result\n", encoding="utf-8")
                result_commit = git.commit_changes(worker, "worker conflict")
                (root / "shared.txt").write_text("main result\n", encoding="utf-8")
                subprocess.run(
                    [str(git_executable), "-C", str(root), "add", "shared.txt"],
                    check=True,
                    capture_output=True,
                    env=environment,
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
                        "main conflict",
                    ],
                    check=True,
                    capture_output=True,
                    env=environment,
                )
                main_head = git.head()
                checks_called = False

                def check_should_not_run(candidate: Path) -> list[dict[str, Any]]:
                    nonlocal checks_called
                    checks_called = True
                    return [{"command": "unused", "passed": True}]

                with self.assertRaises(GitWorkspaceError):
                    git.merge_verified_result(
                        base,
                        result_commit,
                        git.changed_files(base, result_commit),
                        git.diff_digest(base, result_commit),
                        check_should_not_run,
                        "AgentDU conflicting test merge",
                        "integration-conflict-test",
                        lambda commit: None,
                    )

                self.assertFalse(checks_called)
                self.assertEqual(git.head(), main_head)
                self.assertEqual(git.status(), "")
                self.assertEqual(
                    (root / "shared.txt").read_text(encoding="utf-8"),
                    "main result\n",
                )

    def test_main_brain_continues_during_parallel_agents_then_integrates(self) -> None:
        git_executable = Path(__file__).resolve().parents[1] / ".tools" / "mingit" / "cmd" / "git.exe"
        if not git_executable.is_file():
            self.skipTest("Bundled Git is unavailable")

        workers_started: set[str] = set()
        workers_lock = threading.Lock()
        both_workers_started = threading.Event()
        release_workers = threading.Event()
        state: dict[str, Any] = {
            "main_requests": 0,
            "worker_requests": {},
            "review_requests": 0,
            "parallel_confirmed": False,
            "main_committed_before_release": False,
        }

        def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }

        def parse_task_summaries(messages: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
            records = []
            for message in messages:
                if message.get("role") != "tool" or message.get("name") != tool_name:
                    continue
                try:
                    records.append(json.loads(message["content"]))
                except (KeyError, json.JSONDecodeError):
                    continue
            return records

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                model = request["model"]
                messages = request["messages"]

                if model in {"writer-a", "writer-b"}:
                    with workers_lock:
                        workers_started.add(model)
                        if len(workers_started) == 2:
                            both_workers_started.set()
                        number = state["worker_requests"].get(model, 0) + 1
                        state["worker_requests"][model] = number
                    release_workers.wait(timeout=15)
                    if number == 1:
                        filename = "alpha.txt" if model == "writer-a" else "beta.txt"
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                tool_call(
                                    f"{model}-write",
                                    "write_file",
                                    {"path": filename, "content": f"{filename}\n"},
                                )
                            ],
                        }
                        finish = "tool_calls"
                    else:
                        answer = {
                            "role": "assistant",
                            "content": f"Created {model} output and completed the assigned task.",
                        }
                        finish = "stop"
                elif model == "reviewer":
                    state["review_requests"] += 1
                    if state["review_requests"] == 1:
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                tool_call(
                                    "review-write",
                                    "write_file",
                                    {"path": "review.txt", "content": "must be denied"},
                                )
                            ],
                        }
                        finish = "tool_calls"
                    else:
                        answer = {
                            "role": "assistant",
                            "content": "Reviewed the integrated alpha change. No actionable findings.",
                        }
                        finish = "stop"
                else:
                    state["main_requests"] += 1
                    request_number = state["main_requests"]
                    if request_number == 1:
                        calls = [
                            tool_call(
                                "delegate-a",
                                "delegate_agent_task",
                                {
                                    "profile": "writer_a",
                                    "task": "Create alpha.txt containing alpha.txt followed by a newline.",
                                    "acceptance_criteria": "The file exists with the requested content.",
                                },
                            ),
                            tool_call(
                                "delegate-b",
                                "delegate_agent_task",
                                {
                                    "profile": "writer_b",
                                    "task": "Create beta.txt containing beta.txt followed by a newline.",
                                    "acceptance_criteria": "The file exists with the requested content.",
                                },
                            ),
                        ]
                        answer = {"role": "assistant", "content": None, "tool_calls": calls}
                        finish = "tool_calls"
                    elif request_number == 2:
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                tool_call(
                                    "main-write",
                                    "write_file",
                                    {"path": "main.txt", "content": "main brain\n"},
                                )
                            ],
                        }
                        finish = "tool_calls"
                    elif request_number == 3:
                        git = subprocess.list2cmdline([str(git_executable)])
                        command = (
                            f"{git} -c user.name=AgentDU-Test -c user.email=test@example.invalid "
                            "add main.txt && "
                            f"{git} -c user.name=AgentDU-Test -c user.email=test@example.invalid "
                            "commit -m main-brain-work"
                        )
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call("main-commit", "run_command", {"command": command})],
                        }
                        finish = "tool_calls"
                    elif request_number == 4:
                        parallel = both_workers_started.wait(timeout=5)
                        state["parallel_confirmed"] = parallel
                        task_records = parse_task_summaries(messages, "delegate_agent_task")
                        state["delegate_tool_results"] = [
                            message.get("content")
                            for message in messages
                            if message.get("role") == "tool"
                            and message.get("name") == "delegate_agent_task"
                        ]
                        state["main_commit_results"] = [
                            message.get("content")
                            for message in messages
                            if message.get("role") == "tool"
                            and message.get("name") == "run_command"
                        ]
                        if task_records:
                            head_result = subprocess.run(
                                [
                                    str(git_executable),
                                    "-C",
                                    str(state["root"]),
                                    "log",
                                    "-1",
                                    "--format=%s",
                                ],
                                capture_output=True,
                                check=False,
                                env=git_env,
                            )
                            head_message = head_result.stdout.decode().strip()
                            state["main_committed_before_release"] = (
                                head_message == "main-brain-work"
                            )
                            state["head_message_at_release"] = head_message
                            state["head_error_at_release"] = head_result.stderr.decode().strip()
                        release_workers.set()
                        first_task = task_records[0]["task_id"]
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                tool_call(
                                    "dependent-review",
                                    "review_agent_task",
                                    {
                                        "profile": "review",
                                        "task": "Review the alpha implementation for defects.",
                                        "depends_on": [first_task],
                                    },
                                )
                            ],
                        }
                        finish = "tool_calls"
                    elif request_number in {5, 7}:
                        name = "wait_for_agent_tasks"
                        task_tool_name = (
                            "delegate_agent_task" if request_number == 5 else "review_agent_task"
                        )
                        requested_tasks = parse_task_summaries(messages, task_tool_name)
                        answer = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                tool_call(
                                    f"wait-{request_number}",
                                    name,
                                    {
                                        "task_ids": [task["task_id"] for task in requested_tasks],
                                        "timeout_seconds": 10,
                                    },
                                )
                            ],
                        }
                        finish = "tool_calls"
                    elif request_number == 6:
                        tasks = parse_task_summaries(messages, "delegate_agent_task")
                        calls = []
                        for index, task in enumerate(tasks):
                            filename = "alpha.txt" if index == 0 else "beta.txt"
                            check = (
                                "python -c \"from pathlib import Path; "
                                f"assert Path('{filename}').is_file()\""
                            )
                            calls.append(
                                tool_call(
                                    f"integrate-{index}",
                                    "integrate_agent_task",
                                    {"task_id": task["task_id"], "checks": [check]},
                                )
                            )
                        answer = {"role": "assistant", "content": None, "tool_calls": calls}
                        finish = "tool_calls"
                    else:
                        answer = {"role": "assistant", "content": "The parallel task was verified and integrated."}
                        finish = "stop"

                body = json.dumps(
                    {"choices": [{"message": answer, "finish_reason": finish}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state["root"] = root
                git_env = os.environ.copy()
                git_env.update(
                    {
                        "AGENTDU_GIT_EXECUTABLE": str(git_executable),
                        "GIT_CONFIG_COUNT": "0",
                    }
                )
                (root / ".gitignore").write_text(
                    ".agentdu/\n__pycache__/\n", encoding="utf-8"
                )
                (root / "base.txt").write_text("baseline\n", encoding="utf-8")
                subprocess.run(
                    [str(git_executable), "-C", str(root), "init"],
                    check=True,
                    capture_output=True,
                    env=git_env,
                )
                subprocess.run(
                    [str(git_executable), "-C", str(root), "add", "."],
                    check=True,
                    capture_output=True,
                    env=git_env,
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
                    env=git_env,
                )
                base_url = f"http://127.0.0.1:{server.server_port}/v1"
                settings = ModelSettings(
                    profiles={
                        "main": ModelProfile("main", "main-model", base_url, streaming=False),
                        "writer_a": ModelProfile(
                            "writer_a", "writer-a", base_url, streaming=False, capabilities=("small fixes",)
                        ),
                        "writer_b": ModelProfile(
                            "writer_b", "writer-b", base_url, streaming=False, capabilities=("parallel work",)
                        ),
                        "review": ModelProfile(
                            "review", "reviewer", base_url, streaming=False, capabilities=("code review",)
                        ),
                    },
                    default_profile="main",
                )
                main_options = {
                    "model": "main-model",
                    "base_url": base_url,
                    "api_key": None,
                    "timeout": 10,
                    "temperature": 0,
                    "streaming": False,
                }
                with patch.dict(
                    os.environ,
                    {
                        "AGENTDU_GIT_EXECUTABLE": str(git_executable),
                        "GIT_CONFIG_COUNT": "0",
                    },
                ):
                    runtime = build_default_runtime(
                        root,
                        OpenAICompatibleModel(**main_options),
                        AllowAllPolicy(),
                        max_steps=20,
                    )
                    coordinator = AutonomousCoordinator(
                        root,
                        settings,
                        main_options,
                        AllowAllPolicy(),
                        max_steps=8,
                        max_concurrency=2,
                        max_tasks=5,
                        total_model_calls=20,
                        task_model_calls=4,
                    )
                    coordinator.install(runtime)
                    delegation_schema = next(
                        schema
                        for schema in runtime.tools.model_schemas()
                        if schema["function"]["name"] == "delegate_agent_task"
                    )
                    self.assertIn(
                        "small fixes",
                        delegation_schema["function"]["parameters"]["properties"]["profile"]["description"],
                    )
                    progress_events: list[str] = []
                    runtime.event_bus.subscribe(lambda event: progress_events.append(event.kind))
                    result = asyncio.run(runtime.run("Create and verify independent outputs."))
                    coordinator.close()

                self.assertTrue(state["parallel_confirmed"], state)
                self.assertTrue(state["main_committed_before_release"], state)
                task_store = TaskStateStore(root, result.session_id)
                tasks = task_store.list()
                self.assertTrue((root / "alpha.txt").is_file(), {"state": state, "tasks": tasks})
                self.assertTrue((root / "beta.txt").is_file(), {"state": state, "tasks": tasks})
                self.assertTrue((root / "main.txt").is_file())
                self.assertIn("verified and integrated", result.content)
                self.assertIn("collaboration_progress", progress_events)
                implementation_tasks = [task for task in tasks if task["kind"] == "implementation"]
                review_tasks = [task for task in tasks if task["kind"] == "review"]
                self.assertEqual(len(implementation_tasks), 2)
                self.assertTrue(all(task["status"] == "integrated" for task in implementation_tasks))
                self.assertTrue(all(task["acceptance_checks"][0]["passed"] for task in implementation_tasks))
                self.assertEqual(len(review_tasks), 1)
                self.assertEqual(review_tasks[0]["status"], "completed")
                self.assertEqual(review_tasks[0]["dependencies"], [implementation_tasks[0]["task_id"]])
                self.assertFalse((Path(review_tasks[0]["workspace"]) / "review.txt").exists())
                self.assertEqual({task["owner"] for task in implementation_tasks}, {"AgentDU"})
        finally:
            release_workers.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

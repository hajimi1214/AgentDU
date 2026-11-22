from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import DEFAULT_SYSTEM_PROMPT, AgentRuntime, build_default_runtime
from .events import AgentEvent, EventBus
from .git_workspaces import GitWorkspaceError, GitWorkspaces
from .models import ChatModel
from .permissions import PermissionPolicy, PermissionRequest, ReadOnlyPolicy
from .provider import OpenAICompatibleModel
from .settings import ModelSettings, resolve_profile_options
from .tools import ToolDefinition, ToolRegistry, ToolResult, _run_command


AUTONOMOUS_INSTRUCTIONS = """\
You are the AgentDU main brain. You remain the owner of the user's task and can complete it
yourself with the ordinary coding tools. Choose collaboration only when it is likely to improve
quality, shorten a critical path, provide a useful independent review, or reduce a concrete risk.
Prefer direct work for small or tightly coupled changes. Delegated tasks run asynchronously in
separate model contexts and Git worktrees, so continue useful work while they run. Use task status
and result tools before relying on another agent's claims. Only integrate implementation results
through integrate_agent_task, with checks that fit the project. Dependencies start only after
their prerequisite results are complete and implementation changes are integrated. Never delegate
to another agent from a child task. Account for relevant pending work before delivering the final
answer, and state when a task remains pending or could not be integrated."""

_TASK_ID = re.compile(r"^[a-f0-9]{12}$")
_TERMINAL_STATES = {"completed", "failed", "blocked", "interrupted", "integrated"}
_FAILED_STATES = {"failed", "blocked", "interrupted"}
_MAX_TASK_OUTPUT_CHARS = 12_000


class TaskStateStore:
    """Atomic per-session task state with a bounded progress history."""

    def __init__(self, workspace: Path, session_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9-]{16,64}", session_id):
            raise ValueError("Invalid AgentDU session id")
        self.path = workspace.resolve() / ".agentdu" / "agents" / session_id / "tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                content = json.loads(self.path.read_text(encoding="utf-8"))
                records = content["tasks"]
                if not isinstance(records, dict):
                    raise ValueError("tasks must be an object")
                self.tasks = records
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise RuntimeError(f"Could not read task state {self.path}: {error}") from error

        changed = False
        for record in self.tasks.values():
            if record.get("status") in {"running", "queued"}:
                record["status"] = "interrupted"
                record["error"] = "The process stopped before this task reached a terminal state"
                record["updated_at"] = _now()
                changed = True
        if changed:
            self._save()

    def add(self, record: dict[str, Any]) -> None:
        with self.lock:
            task_id = record["task_id"]
            if task_id in self.tasks:
                raise ValueError(f"Task already exists: {task_id}")
            self.tasks[task_id] = record
            self._save()

    def get(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.tasks.get(task_id)
            if record is None:
                raise ValueError(f"Unknown task id: {task_id}")
            return json.loads(json.dumps(record, ensure_ascii=False))

    def update(self, task_id: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            record = self.tasks.get(task_id)
            if record is None:
                raise ValueError(f"Unknown task id: {task_id}")
            previous_status = record.get("status")
            record.update(values)
            if "status" in values and previous_status != values["status"]:
                record["version"] = int(record.get("version", 0)) + 1
            record["updated_at"] = _now()
            self._save()
            return json.loads(json.dumps(record, ensure_ascii=False))

    def record_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self.lock:
            record = self.tasks.get(task_id)
            if record is None:
                return
            events = record.setdefault("events", [])
            events.append({"at": _now(), "kind": kind, "payload": payload})
            del events[:-100]
            record["updated_at"] = _now()
            self._save()

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                json.loads(json.dumps(record, ensure_ascii=False))
                for record in self.tasks.values()
            ]

    def _save(self) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "tasks": self.tasks}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class AutonomousCoordinator:
    """Tool-driven bounded scheduler for non-recursive model collaboration."""

    def __init__(
        self,
        workspace: Path,
        settings: ModelSettings,
        main_options: dict[str, Any],
        permission_policy: PermissionPolicy,
        context_tokens: int = 24_000,
        max_steps: int = 40,
        max_concurrency: int = 2,
        max_tasks: int = 8,
        total_model_calls: int = 80,
        task_model_calls: int = 20,
    ) -> None:
        if max_concurrency < 1 or max_tasks < 1 or total_model_calls < 1 or task_model_calls < 1:
            raise ValueError("Collaboration limits must be positive")
        self.workspace = workspace.resolve()
        self.settings = settings
        self.permission_policy = permission_policy
        self.context_tokens = context_tokens
        self.max_steps = max_steps
        self.max_concurrency = max_concurrency
        self.max_tasks = max_tasks
        self.total_model_calls = total_model_calls
        self.task_model_calls = task_model_calls
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stores: dict[str, TaskStateStore] = {}
        self._futures: dict[str, Any] = {}
        self._parent_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="agentdu-agent",
        )
        self._parent_events: EventBus | None = None
        self._git: GitWorkspaces | None = None
        try:
            self._git = GitWorkspaces(self.workspace)
        except GitWorkspaceError:
            pass

        main_identity = (
            str(main_options.get("base_url", "")).rstrip("/"),
            main_options.get("model"),
        )
        self.profiles: dict[str, dict[str, Any]] = {}
        for name, profile in settings.profiles.items():
            if not profile.model:
                continue
            options = resolve_profile_options(settings, name)
            identity = (str(options["base_url"]).rstrip("/"), options["model"])
            if identity != main_identity:
                self.profiles[name] = options

    @property
    def available(self) -> bool:
        return bool(self.profiles)

    def install(self, runtime: AgentRuntime) -> None:
        if not self.available:
            raise ValueError("At least one distinct collaborator profile is required")
        self._parent_events = runtime.event_bus
        runtime.system_prompt = f"{runtime.system_prompt}\n\n{AUTONOMOUS_INSTRUCTIONS}"
        runtime.tools.register(
            self._tool(
                "delegate_agent_task",
                "Start an implementation task asynchronously in an isolated Git worktree. Select a suitable configured profile and include a bounded, independently verifiable task. Use for useful parallel work, not to transfer ownership of the whole user request. Results require explicit acceptance checks and integration.",
                self._dispatch_schema("implementation"),
                "execute",
                self._delegate,
            )
        )
        runtime.tools.register(
            self._tool(
                "consult_model",
                "Ask a distinct configured model for bounded advice using only the supplied context. The consultation has no file or shell tools and runs asynchronously.",
                self._consult_schema(),
                "execute",
                self._consult,
            )
        )
        runtime.tools.register(
            self._tool(
                "review_agent_task",
                "Start an independent read-only code review in a separate Git worktree. To review another task's implementation, depend on that task so the review starts after its result is integrated.",
                self._dispatch_schema("review"),
                "execute",
                self._review,
            )
        )
        runtime.tools.register(
            self._tool(
                "list_agent_tasks",
                "List task states, dependencies, model-call use, and concise results for this user session.",
                _object_schema({}, []),
                "read",
                self._list_tasks,
            )
        )
        runtime.tools.register(
            self._tool(
                "get_agent_task_result",
                "Read a completed task report and its verified Git diff. Diff output is bounded.",
                _object_schema(
                    {"task_id": {"type": "string"}, "include_diff": {"type": "boolean"}},
                    ["task_id"],
                ),
                "read",
                self._get_result,
            )
        )
        runtime.tools.register(
            self._tool(
                "wait_for_agent_tasks",
                "Wait for all listed tasks to finish, or for any task to finish when task_ids is omitted. Waiting is optional and blocks the main model turn, so continue independent work when possible.",
                _object_schema(
                    {
                        "task_ids": {"type": "array", "items": {"type": "string"}},
                        "timeout_seconds": {"type": "integer"},
                    },
                    [],
                ),
                "read",
                self._wait_for_tasks,
            )
        )
        if self._git is not None:
            runtime.tools.register(
                self._tool(
                    "integrate_agent_task",
                    "Integrate a completed implementation task after running one or more explicit acceptance commands against an isolated candidate merge. Conflicts, failed checks, dirty main worktrees, and stale baselines are rejected without changing the main worktree.",
                    _object_schema(
                        {
                            "task_id": {"type": "string"},
                            "checks": {"type": "array", "items": {"type": "string"}},
                        },
                        ["task_id", "checks"],
                    ),
                    "write",
                    self._integrate_task,
                )
            )

    def close(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def finish_session(self, session_id: str) -> list[dict[str, Any]]:
        store = self._store(session_id)
        with self._changed:
            while True:
                self._launch_queued(session_id)
                records = store.list()
                if not any(record.get("status") == "running" for record in records):
                    for record in records:
                        if record.get("status") == "queued":
                            self._update(
                                store,
                                record["task_id"],
                                status="blocked",
                                owner="AgentDU",
                                error="The session ended before its dependencies were integrated",
                            )
                    break
                self._changed.wait()
        self._executor.shutdown(wait=True)
        return store.list()

    def _tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        permission: str,
        handler: Any,
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            permission=permission,
            handler=_missing_context,
            context_handler=handler,
        )

    def _dispatch_schema(self, kind: str) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "profile": {
                "type": "string",
                "enum": sorted(self.profiles),
                "description": self._profile_description(),
            },
            "task": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
        }
        required = ["profile", "task"]
        if kind == "implementation":
            properties["acceptance_criteria"] = {"type": "string"}
        return _object_schema(properties, required)

    def _profile_description(self) -> str:
        descriptions = []
        for name in sorted(self.profiles):
            profile = self.settings.profiles[name]
            capabilities = ", ".join(profile.capabilities) or "no capability tags configured"
            descriptions.append(f"{name}: {profile.model}, {capabilities}")
        return "Choose based on the configured model and capability hints. " + "; ".join(descriptions)

    def _consult_schema(self) -> dict[str, Any]:
        return _object_schema(
            {
                "profile": {"type": "string", "enum": sorted(self.profiles)},
                "question": {"type": "string"},
                "context": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
            },
            ["profile", "question", "context"],
        )

    def _delegate(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        return self._submit(
            session_id,
            "implementation",
            arguments["profile"],
            arguments["task"],
            arguments.get("depends_on", []),
            parent_loop,
            acceptance_criteria=arguments.get("acceptance_criteria", ""),
        )

    def _consult(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        prompt = arguments["question"]
        context = arguments["context"]
        if len(context) > 16_000:
            return ToolResult("Consultation context must be 16,000 characters or shorter", True)
        return self._submit(
            session_id,
            "consultation",
            arguments["profile"],
            prompt,
            arguments.get("depends_on", []),
            parent_loop,
            supplied_context=context,
        )

    def _review(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        return self._submit(
            session_id,
            "review",
            arguments["profile"],
            arguments["task"],
            arguments.get("depends_on", []),
            parent_loop,
        )

    def _submit(
        self,
        session_id: str,
        kind: str,
        profile: str,
        prompt: str,
        dependencies: list[str],
        parent_loop: asyncio.AbstractEventLoop,
        acceptance_criteria: str = "",
        supplied_context: str = "",
    ) -> str:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 20_000:
            return ToolResult("Task text must be between 1 and 20,000 characters", True)
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            return ToolResult("depends_on must be an array of task ids", True)
        if len(set(dependencies)) != len(dependencies):
            return ToolResult("depends_on cannot contain duplicate task ids", True)
        if profile not in self.profiles:
            return ToolResult(f"Profile is not available for collaboration: {profile}", True)

        with self._changed:
            self._parent_loops[session_id] = parent_loop
            store = self._store(session_id)
            records = {record["task_id"]: record for record in store.list()}
            if len(records) >= self.max_tasks:
                return ToolResult(f"Session task limit reached ({self.max_tasks})", True)
            for dependency in dependencies:
                if dependency not in records:
                    return ToolResult(f"Unknown dependency task id: {dependency}", True)
                if records[dependency].get("status") in _FAILED_STATES:
                    return ToolResult(
                        f"Dependency {dependency} has status {records[dependency]['status']}", True
                    )

            task_id = uuid.uuid4().hex[:12]
            record = {
                "task_id": task_id,
                "session_id": session_id,
                "kind": kind,
                "profile": profile,
                "prompt": prompt,
                "supplied_context": supplied_context,
                "acceptance_criteria": acceptance_criteria,
                "dependencies": list(dependencies),
                "status": "queued",
                "owner": "AgentDU",
                "version": 0,
                "created_at": _now(),
                "updated_at": _now(),
                "base_commit": None,
                "result_commit": None,
                "changed_files": [],
                "diff_digest": None,
                "workspace": None,
                "branch": None,
                "report": "",
                "error": None,
                "model_calls": 0,
                "model_call_limit": 0,
                "acceptance_checks": [],
                "integration_commit": None,
                "events": [],
            }
            store.add(record)
            self._event(store, task_id, "task_queued", {"kind": kind, "profile": profile})
            self._launch_queued(session_id)
            current = store.get(task_id)
            return _render_task_summary(current)

    def _launch_queued(self, session_id: str) -> None:
        with self._changed:
            store = self._store(session_id)
            records = store.list()
            by_id = {record["task_id"]: record for record in records}
            for record in records:
                if record.get("status") != "queued":
                    continue
                dependency_state = self._dependency_state(record["dependencies"], by_id)
                if dependency_state == "failed":
                    self._update(
                        store,
                        record["task_id"],
                        status="blocked",
                        owner="AgentDU",
                        error="A prerequisite task failed or was interrupted",
                    )
                    continue
                if dependency_state == "waiting":
                    continue
                if self._running_count() >= self.max_concurrency:
                    break

                remaining = self._available_model_calls(session_id)
                if remaining < 1:
                    if not any(item.get("status") == "running" for item in records):
                        self._update(
                            store,
                            record["task_id"],
                            status="blocked",
                            owner="AgentDU",
                            error="The session model-call budget is exhausted",
                        )
                    continue
                call_limit = min(self.task_model_calls, remaining)
                try:
                    workspace, base_commit, branch = self._prepare_workspace(record)
                except (GitWorkspaceError, OSError) as error:
                    self._update(
                        store,
                        record["task_id"],
                        status="failed",
                        owner="AgentDU",
                        error=str(error),
                    )
                    continue

                self._update(
                    store,
                    record["task_id"],
                    status="running",
                    owner=record["profile"],
                    workspace=str(workspace),
                    base_commit=base_commit,
                    branch=branch,
                    model_call_limit=call_limit,
                    error=None,
                )
                self._event(
                    store,
                    record["task_id"],
                    "task_started",
                    {"model_call_limit": call_limit, "workspace": str(workspace)},
                )
                future = self._executor.submit(
                    self._run_task,
                    session_id,
                    record["task_id"],
                )
                self._futures[record["task_id"]] = future
                self._changed.notify_all()

    def _prepare_workspace(self, record: dict[str, Any]) -> tuple[Path, str | None, str | None]:
        if record["kind"] == "consultation":
            return self.workspace, None, None
        if self._git is None:
            raise GitWorkspaceError("Implementation and review tasks require a Git repository")
        with self._git.handoff_lock():
            base_commit = self._git.require_clean()
            task_id = record["task_id"]
            if record["kind"] == "implementation":
                branch = f"agentdu/task-{task_id}"
                workspace = self._git.add_worktree(task_id, "worker", branch, base_commit)
            else:
                branch = None
                workspace = self._git.add_readonly_worktree(task_id, "review", base_commit)
        return workspace, base_commit, branch

    def _run_task(self, session_id: str, task_id: str) -> None:
        try:
            asyncio.run(self._execute_task(session_id, task_id))
        except Exception as error:
            store = self._store(session_id)
            self._update(
                store,
                task_id,
                status="failed",
                owner="AgentDU",
                error=f"{type(error).__name__}: {error}"[:2_000],
            )
            self._publish_progress(
                session_id,
                task_id,
                "task_failed",
                {"error": f"{type(error).__name__}: {error}"[:500]},
            )
        finally:
            self._launch_queued(session_id)

    async def _execute_task(self, session_id: str, task_id: str) -> None:
        store = self._store(session_id)
        task = store.get(task_id)
        profile = task["profile"]
        workspace = Path(task["workspace"])
        kind = task["kind"]
        if kind == "consultation":
            worker_policy: PermissionPolicy = ReadOnlyPolicy()
            instructions = (
                "You are an independent model consultant. Answer only the supplied question using "
                "the supplied context. No tools or additional project access are available. "
                "Separate verified context from inference and state uncertainty."
            )
        elif kind == "review":
            worker_policy = ReadOnlyPolicy()
            instructions = (
                "You are an independent read-only code reviewer. Inspect the assigned project "
                "snapshot and report only actionable findings with severity and file/line evidence. "
                "Do not change files or claim tests that you did not run."
            )
        else:
            worker_policy = self.permission_policy
            instructions = (
                "You are an implementation subagent working on one bounded task. Work only in "
                "your assigned worktree. Do not create other agents, switch branches, create "
                "worktrees, reset, or commit. Inspect actual code, make focused changes, run "
                "relevant checks when permitted, and report changed files, decisions, evidence, "
                "and unresolved issues. The main brain owns the overall task and final integration."
            )

        runtime = build_default_runtime(
            workspace=workspace,
            model=OpenAICompatibleModel(**self.profiles[profile]),
            permission_policy=worker_policy,
            data_dir=(
                self.workspace
                / ".agentdu"
                / "agents"
                / session_id
                / "tasks"
                / task_id
            ),
            context_tokens=self.context_tokens,
            max_steps=self.max_steps,
            max_model_calls=task["model_call_limit"],
            system_prompt=f"{DEFAULT_SYSTEM_PROMPT}\n\n{instructions}",
            include_builtin_tools=kind != "consultation",
        )

        async def forward_event(event: AgentEvent) -> None:
            payload: dict[str, Any] = {
                "event": event.kind,
                "profile": profile,
            }
            for key in ("step", "tool_name", "is_error", "reason", "limit"):
                if key in event.payload:
                    payload[key] = event.payload[key]
            if event.kind == "model_request":
                current = store.get(task_id)
                self._update(store, task_id, model_calls=current["model_calls"] + 1)
            if event.kind in {
                "run_started",
                "model_request",
                "tool_started",
                "tool_finished",
                "provider_retry",
                "provider_error",
                "run_completed",
                "run_stopped",
            }:
                self._event(store, task_id, event.kind, payload)
                await self._publish_progress_async(session_id, task_id, "task_progress", payload)

        runtime.event_bus.subscribe(forward_event)
        prompt = self._task_prompt(task, store.list())
        result = await runtime.run(prompt)
        report = result.content[:8_000]
        changes: dict[str, Any] = {
            "report": report,
            "model_calls": result.model_calls,
            "error": None,
        }
        if kind == "implementation":
            if self._git is None:
                raise GitWorkspaceError("Git support is unavailable")
            with self._git.handoff_lock():
                result_commit = self._git.commit_changes(
                    workspace,
                    f"AgentDU task {task_id}",
                )
                self._git.verify_snapshot(
                    task["base_commit"],
                    result_commit,
                    self._git.changed_files(task["base_commit"], result_commit),
                    self._git.diff_digest(task["base_commit"], result_commit),
                )
                if self._git.status(workspace):
                    raise GitWorkspaceError("Implementation worktree is not clean after snapshot")
                changes.update(
                    result_commit=result_commit,
                    changed_files=self._git.changed_files(task["base_commit"], result_commit),
                    diff_digest=self._git.diff_digest(task["base_commit"], result_commit),
                )
        elif kind == "review":
            if self._git is None or self._git.status(workspace):
                raise GitWorkspaceError("Read-only review changed its worktree")
            changes.update(result_commit=self._git.head(workspace), changed_files=[])

        self._update(store, task_id, status="completed", owner="AgentDU", **changes)
        self._event(store, task_id, "task_completed", {"model_calls": result.model_calls})
        self._publish_progress(
            session_id,
            task_id,
            "task_completed",
            {"profile": profile, "model_calls": result.model_calls},
        )

    def _task_prompt(self, task: dict[str, Any], records: list[dict[str, Any]]) -> str:
        by_id = {record["task_id"]: record for record in records}
        dependency_reports = [
            {
                "task_id": dependency,
                "kind": by_id[dependency]["kind"],
                "status": by_id[dependency]["status"],
                "report": by_id[dependency].get("report", "")[:4_000],
                "result_commit": by_id[dependency].get("result_commit"),
            }
            for dependency in task["dependencies"]
            if dependency in by_id
        ]
        if task["kind"] == "consultation":
            return (
                f"Question\n{task['prompt']}\n\nSupplied context\n"
                f"{task['supplied_context']}\n\nCompleted task context\n"
                f"{json.dumps(dependency_reports, ensure_ascii=False, indent=2)}"
            )
        if task["kind"] == "review":
            return (
                f"Review assignment\n{task['prompt']}\n\nCompleted task context\n"
                f"{json.dumps(dependency_reports, ensure_ascii=False, indent=2)}"
            )
        return (
            f"Implementation assignment\n{task['prompt']}\n\nAcceptance criteria\n"
            f"{task['acceptance_criteria'] or 'Report how the main brain can verify the result.'}"
            f"\n\nIntegrated prerequisite task context\n"
            f"{json.dumps(dependency_reports, ensure_ascii=False, indent=2)}"
        )

    def _store(self, session_id: str) -> TaskStateStore:
        with self._lock:
            store = self._stores.get(session_id)
            if store is None:
                store = TaskStateStore(self.workspace, session_id)
                self._stores[session_id] = store
                self._recover_integrations(store)
            return store

    def _recover_integrations(self, store: TaskStateStore) -> None:
        if self._git is None:
            return
        for task in store.list():
            commit = task.get("integration_commit")
            if (
                task.get("status") == "integrating"
                and commit
                and self._git.is_ancestor(commit, self._git.head())
                and not self._git.status()
            ):
                self._update(
                    store,
                    task["task_id"],
                    status="integrated",
                    owner="AgentDU",
                    error=None,
                    integration_recovered=True,
                )
            elif task.get("status") == "integrating":
                self._update(store, task["task_id"], status="completed")

    def _dependencies_ready(
        self,
        dependencies: list[str],
        records: dict[str, dict[str, Any]],
    ) -> bool:
        return self._dependency_state(dependencies, records) == "ready"

    @staticmethod
    def _dependency_state(
        dependencies: list[str],
        records: dict[str, dict[str, Any]],
    ) -> str:
        waiting = False
        for dependency in dependencies:
            record = records.get(dependency)
            if record is None or record.get("status") in _FAILED_STATES:
                return "failed"
            required = "integrated" if record.get("kind") == "implementation" else "completed"
            if record.get("status") != required:
                waiting = True
        return "waiting" if waiting else "ready"

    def _running_count(self) -> int:
        return sum(
            record.get("status") == "running"
            for store in self._stores.values()
            for record in store.list()
        )

    def _available_model_calls(self, session_id: str) -> int:
        store = self._store(session_id)
        records = store.list()
        spent = sum(int(record.get("model_calls", 0)) for record in records)
        reserved = sum(
            max(0, int(record.get("model_call_limit", 0)) - int(record.get("model_calls", 0)))
            for record in records
            if record.get("status") == "running"
        )
        return self.total_model_calls - spent - reserved

    def _update(self, store: TaskStateStore, task_id: str, **values: Any) -> None:
        with self._changed:
            store.update(task_id, **values)
            self._changed.notify_all()

    def _event(
        self,
        store: TaskStateStore,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        store.record_event(task_id, kind, payload)

    def _publish_progress(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if self._parent_events is None:
            return
        parent_loop = self._parent_loops.get(session_id)
        if parent_loop is None or not parent_loop.is_running():
            return
        event = AgentEvent.create(
            session_id,
            "collaboration_progress",
            {"task_id": task_id, "kind": kind, **payload},
        )
        try:
            future = asyncio.run_coroutine_threadsafe(self._parent_events.publish(event), parent_loop)
            future.result(timeout=2)
        except (RuntimeError, TimeoutError):
            pass

    async def _publish_progress_async(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if self._parent_events is None:
            return
        parent_loop = self._parent_loops.get(session_id)
        if parent_loop is None or not parent_loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._parent_events.publish(AgentEvent.create(
                session_id,
                "collaboration_progress",
                {"task_id": task_id, "kind": kind, **payload},
            )),
            parent_loop,
        )
        await asyncio.wrap_future(future)

    def _list_tasks(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        store = self._store(session_id)
        self._launch_queued(session_id)
        return _render_tasks(store.list())

    def _get_result(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        task_id = arguments["task_id"]
        if not _TASK_ID.fullmatch(task_id):
            return ToolResult("Invalid task id", True)
        store = self._store(session_id)
        task = store.get(task_id)
        if task["status"] not in {"completed", "integrated"}:
            return ToolResult(f"Task {task_id} is {task['status']}", True)
        result: dict[str, Any] = {
            "task_id": task_id,
            "kind": task["kind"],
            "profile": task["profile"],
            "status": task["status"],
            "model_calls": task["model_calls"],
            "report": task["report"],
            "changed_files": task["changed_files"],
            "base_commit": task["base_commit"],
            "result_commit": task["result_commit"],
        }
        if task["kind"] == "implementation":
            try:
                self._verify_completed_snapshot(task)
                if arguments.get("include_diff", True):
                    assert self._git is not None
                    diff = self._git.git(
                        self._git.root,
                        "diff",
                        "--no-ext-diff",
                        "--no-renames",
                        task["base_commit"],
                        task["result_commit"],
                        "--",
                    ).stdout.decode("utf-8", errors="replace")
                    result["diff"] = diff[:_MAX_TASK_OUTPUT_CHARS]
                    if len(diff) > _MAX_TASK_OUTPUT_CHARS:
                        result["diff"] += "\n[diff truncated]"
            except GitWorkspaceError as error:
                self._update(store, task_id, status="failed", error=str(error))
                return ToolResult(f"Task result verification failed: {error}", True)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _wait_for_tasks(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        store = self._store(session_id)
        timeout = max(0, min(30, arguments.get("timeout_seconds", 10)))
        records = {task["task_id"]: task for task in store.list()}
        selected_ids = arguments.get("task_ids", [])
        if any(task_id not in records for task_id in selected_ids):
            return ToolResult("task_ids contains an unknown task id", True)
        before = {task_id: task["status"] for task_id, task in records.items()}

        def finished() -> bool:
            current = {task["task_id"]: task["status"] for task in store.list()}
            if selected_ids:
                return all(current.get(task_id) in _TERMINAL_STATES for task_id in selected_ids)
            return any(
                status in _TERMINAL_STATES and before.get(task_id) not in _TERMINAL_STATES
                for task_id, status in current.items()
            )

        with self._changed:
            self._changed.wait_for(finished, timeout=timeout)
        return _render_tasks(store.list())

    def _integrate_task(
        self,
        arguments: dict[str, Any],
        workspace: Path,
        session_id: str,
        parent_loop: asyncio.AbstractEventLoop,
    ) -> str:
        task_id = arguments["task_id"]
        checks = arguments["checks"]
        if not _TASK_ID.fullmatch(task_id):
            return ToolResult("Invalid task id", True)
        if not isinstance(checks, list) or not checks or len(checks) > 10:
            return ToolResult("Provide between 1 and 10 acceptance commands", True)
        if any(not isinstance(command, str) or not command.strip() or len(command) > 2_000 for command in checks):
            return ToolResult("Each acceptance command must be a non-empty string up to 2,000 characters", True)
        if self._git is None:
            return ToolResult("Git integration is unavailable", True)

        self._parent_loops[session_id] = parent_loop
        store = self._store(session_id)
        task = store.get(task_id)
        if task["kind"] != "implementation" or task["status"] not in {"completed", "integrating"}:
            return ToolResult("Only a completed implementation task can be integrated", True)
        try:
            self._verify_completed_snapshot(task)
            with self._git.handoff_lock():
                current_head = self._git.head()
                candidate_commit = task.get("integration_commit")
                if (
                    candidate_commit
                    and self._git.is_ancestor(candidate_commit, current_head)
                    and not self._git.status()
                ):
                    self._update(
                        store,
                        task_id,
                        status="integrated",
                        error=None,
                        integration_recovered=True,
                    )
                    return f"Task {task_id} was already integrated at {current_head}."

                attempt_id = f"integration-{task_id}-{uuid.uuid4().hex[:8]}"
                self._update(
                    store,
                    task_id,
                    status="integrating",
                    owner="AgentDU",
                    integration_commit=None,
                    integration_attempt=attempt_id,
                    acceptance_checks=[],
                    error=None,
                )

                def check_candidate(candidate: Path) -> list[dict[str, Any]]:
                    evidence = self._run_acceptance_checks(checks, candidate)
                    self._update(store, task_id, acceptance_checks=evidence)
                    return evidence

                def record_candidate(commit: str) -> None:
                    self._update(store, task_id, integration_commit=commit)

                integrated_commit, evidence, recovered = self._git.merge_verified_result(
                    task["base_commit"],
                    task["result_commit"],
                    task["changed_files"],
                    task["diff_digest"],
                    check_candidate,
                    f"AgentDU: integrate task {task_id}",
                    attempt_id,
                    record_candidate,
                )
                self._update(
                    store,
                    task_id,
                    status="integrated",
                    owner="AgentDU",
                    integration_commit=integrated_commit,
                    acceptance_checks=evidence,
                    integration_recovered=recovered,
                    error=None,
                )
                self._event(
                    store,
                    task_id,
                    "task_integrated",
                    {"commit": integrated_commit, "checks": evidence},
                )
            self._publish_progress(
                session_id,
                task_id,
                "task_integrated",
                {"commit": integrated_commit, "recovered": recovered},
            )
            self._launch_queued(session_id)
            return (
                f"Integrated task {task_id} at {integrated_commit}. "
                f"Acceptance checks passed: {len(evidence)}."
            )
        except (GitWorkspaceError, OSError, ValueError) as error:
            self._update(
                store,
                task_id,
                status="completed",
                owner="AgentDU",
                error=f"Integration rejected: {error}"[:2_000],
            )
            return ToolResult(f"Integration rejected: {error}", True)

    def _verify_completed_snapshot(self, task: dict[str, Any]) -> None:
        if self._git is None:
            raise GitWorkspaceError("Git support is unavailable")
        workspace = Path(task["workspace"])
        if self._git.status(workspace):
            raise GitWorkspaceError("The completed task worktree is no longer clean")
        if self._git.head(workspace) != task["result_commit"]:
            raise GitWorkspaceError("The completed task branch moved after its result was recorded")
        self._git.verify_snapshot(
            task["base_commit"],
            task["result_commit"],
            task["changed_files"],
            task["diff_digest"],
        )

    def _run_acceptance_checks(self, checks: list[str], workspace: Path) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for command in checks:
            request = PermissionRequest(
                permission="execute",
                tool_name="agentdu_acceptance_check",
                arguments={"command": command},
                workspace=workspace,
            )
            if not self.permission_policy.authorize(request):
                evidence.append(
                    {"command": command, "passed": False, "output": "Execution was not authorized"}
                )
                break
            result = _run_command({"command": command}, workspace)
            evidence.append(
                {
                    "command": command,
                    "passed": not result.is_error,
                    "output": result.text[:4_000],
                    "output_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                }
            )
            if result.is_error:
                break
        return evidence


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _missing_context(arguments: dict[str, Any], workspace: Path) -> ToolResult:
    return ToolResult("This collaboration tool requires an AgentDU session", True)


def _render_tasks(records: list[dict[str, Any]]) -> str:
    summaries = [_render_task_summary(record) for record in records]
    return "\n".join(summaries) if summaries else "No collaboration tasks for this session."


def _render_task_summary(record: dict[str, Any]) -> str:
    result = {
        "task_id": record["task_id"],
        "kind": record["kind"],
        "profile": record["profile"],
        "status": record["status"],
        "dependencies": record["dependencies"],
        "model_calls": record["model_calls"],
        "model_call_limit": record["model_call_limit"],
        "changed_files": record["changed_files"],
    }
    if record.get("error"):
        result["error"] = record["error"]
    if record.get("report"):
        result["report"] = record["report"][:1_000]
    return json.dumps(result, ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

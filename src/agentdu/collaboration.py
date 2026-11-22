from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agent import DEFAULT_SYSTEM_PROMPT, AgentRuntime, build_default_runtime
from .events import AgentEvent
from .git_workspaces import GitWorkspaceError, GitWorkspaces
from .handoff import HandoffLedger, HandoffState
from .permissions import PermissionPolicy, ReadOnlyPolicy
from .provider import OpenAICompatibleModel
from .sessions import SessionProcessLock, SessionStore
from .settings import ModelSettings, resolve_profile_options


class CollaborationError(RuntimeError):
    pass


class HandoffValidationError(CollaborationError):
    pass


@dataclass(frozen=True)
class HandoffOutcome:
    coordinator_session_id: str
    handoff_id: str
    base_commit: str
    source_commit: str
    result_commit: str
    changed_files: tuple[str, ...]
    acceptance_checks: tuple[dict[str, Any], ...]
    report: str


class HandoffCoordinator:
    """Runs a serial implementation and verification handoff on separate worktrees."""

    def __init__(
        self,
        workspace: Path,
        settings: ModelSettings,
        permission_policy: PermissionPolicy,
        context_tokens: int = 24_000,
        max_steps: int = 40,
        event_handler: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.workspaces = GitWorkspaces(workspace)
        self.root = self.workspaces.root
        self.settings = settings
        self.permission_policy = permission_policy
        self.context_tokens = context_tokens
        self.max_steps = max_steps
        self.event_handler = event_handler
        self.coordinator_sessions = SessionStore(self.root / ".agentdu")
        self.ledger = HandoffLedger(self.root / ".agentdu" / "handoffs")

    async def run(
        self,
        prompt: str,
        source_profile: str,
        receiver_profile: str,
        acceptance_checks: list[str],
        session_id: str | None = None,
    ) -> HandoffOutcome:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        checks = [check.strip() for check in acceptance_checks if check.strip()]
        if not checks:
            raise ValueError("Multi-model runs require at least one explicit --check command")
        source_options = resolve_profile_options(self.settings, source_profile)
        receiver_options = resolve_profile_options(self.settings, receiver_profile)
        if (
            source_options["model"] == receiver_options["model"]
            and source_options["base_url"].rstrip("/") == receiver_options["base_url"].rstrip("/")
        ):
            raise ValueError("The source and receiver profiles resolve to the same model endpoint")

        coordinator_session_id = session_id or uuid.uuid4().hex
        repository_lock = await self._acquire_repository_lock()
        session_lock: SessionProcessLock | None = None
        handoff_id: str | None = None
        state: HandoffState | None = None
        try:
            session_lock = await self._acquire_session_lock(coordinator_session_id)
            await self._reconcile_verified_handoffs(coordinator_session_id)
            active = self.ledger.active_states()
            if active:
                outstanding = ", ".join(f"{item.handoff_id} ({item.status})" for item in active)
                raise CollaborationError(
                    f"An earlier handoff is incomplete: {outstanding}. Inspect it with --list-handoffs."
                )
            base_commit = self.workspaces.require_clean()
            self._begin_coordinator_session(coordinator_session_id, prompt)

            handoff_id = uuid.uuid4().hex
            source_session_id, receiver_session_id = self._create_agent_sessions(handoff_id)
            source_branch = f"agentdu/{handoff_id}/source"
            receiver_branch = f"agentdu/{handoff_id}/receiver"
            source_worktree = self.workspaces.worktree_path(handoff_id, "source")
            receiver_worktree = self.workspaces.worktree_path(handoff_id, "receiver")
            state = self.ledger.create(
                source_profile,
                receiver_profile,
                {
                    "coordinator_session_id": coordinator_session_id,
                    "original_task": prompt,
                    "acceptance_checks": checks,
                    "base_commit": base_commit,
                    "source_branch": source_branch,
                    "receiver_branch": receiver_branch,
                    "source_worktree": str(source_worktree),
                    "receiver_worktree": str(receiver_worktree),
                    "source_session_id": source_session_id,
                    "receiver_session_id": receiver_session_id,
                },
                handoff_id,
            )
            self._record_coordinator_state(coordinator_session_id, state)

            source_workspace = self.workspaces.add_worktree(
                handoff_id, "source", source_branch, base_commit
            )
            source_runtime = self._build_agent(
                source_profile,
                source_workspace,
                handoff_id,
                "source",
                self._source_instructions(checks),
            )
            source_result = await source_runtime.run(
                self._source_prompt(prompt, checks), source_session_id
            )
            source_commit = self.workspaces.commit_changes(
                source_workspace, f"AgentDU handoff {handoff_id[:12]} source"
            )
            source_files = self.workspaces.changed_files(base_commit, source_commit)
            if not source_files:
                raise CollaborationError("The implementation agent produced no committed project changes")
            source_digest = self.workspaces.diff_digest(base_commit, source_commit)
            source_evidence = self._command_evidence(source_runtime.sessions, source_session_id)
            self.workspaces.verify_snapshot(base_commit, source_commit, source_files, source_digest)
            self._require_original_baseline(base_commit, handoff_id, state)

            source_package = {
                "handoff_id": handoff_id,
                "source_profile": source_profile,
                "receiver_profile": receiver_profile,
                "base_commit": base_commit,
                "source_commit": source_commit,
                "diff_sha256": source_digest,
                "changed_files": source_files,
                "source_report": source_result.content[:8_000],
                "execution_evidence": source_evidence,
                "acceptance_checks": checks,
                "source_session_id": source_session_id,
            }
            state = self.ledger.transition(
                handoff_id,
                state.version,
                "source_running",
                "offered",
                source_profile,
                {"source_package": source_package},
            )
            self._record_coordinator_state(coordinator_session_id, state)
            self.coordinator_sessions.append_message(
                coordinator_session_id,
                {
                    "role": "assistant",
                    "content": f"Implementation agent {source_profile} completed.\n\n{source_result.content[:8_000]}",
                },
            )

            self._verify_source_package(source_package)
            self._require_original_baseline(base_commit, handoff_id, state)
            receiver_workspace = self.workspaces.add_worktree(
                handoff_id, "receiver", receiver_branch, source_commit
            )
            if self.workspaces.head(receiver_workspace) != source_commit:
                raise GitWorkspaceError("Receiver worktree did not start from the offered source commit")
            receiver_runtime = self._build_agent(
                receiver_profile,
                receiver_workspace,
                handoff_id,
                "receiver",
                self._receiver_instructions(),
            )
            receiver_runtime.tools.permission_policy = ReadOnlyPolicy()
            acknowledgement = await receiver_runtime.run(
                self._receiver_offer_prompt(prompt, source_package), receiver_session_id
            )
            acknowledged = self._has_exclusive_decision(
                acknowledgement.content, "HANDOFF: ACCEPT", "HANDOFF: REJECT"
            )
            if not acknowledged:
                state = self.ledger.transition(
                    handoff_id,
                    state.version,
                    "offered",
                    "rejected",
                    receiver_profile,
                    {
                        "receiver_acknowledgement": acknowledgement.content[:2_000],
                        "rejection_reason": "The receiver did not return Handoff: ACCEPT",
                    },
                )
                self._record_coordinator_state(coordinator_session_id, state)
                raise HandoffValidationError(
                    "The receiving model rejected or failed to acknowledge the handoff"
                )
            if self.workspaces.status(receiver_workspace):
                state = self.ledger.transition(
                    handoff_id,
                    state.version,
                    "offered",
                    "rejected",
                    receiver_profile,
                    {
                        "receiver_acknowledgement": acknowledgement.content[:2_000],
                        "rejection_reason": "The read-only acknowledgement phase changed the receiver worktree",
                    },
                )
                self._record_coordinator_state(coordinator_session_id, state)
                raise HandoffValidationError(
                    "The acknowledgement phase left project changes. The handoff was rejected."
                )
            state = self.ledger.transition(
                handoff_id,
                state.version,
                "offered",
                "accepted",
                receiver_profile,
                {
                    "accepted_source_commit": source_commit,
                    "receiver_acknowledgement": acknowledgement.content[:2_000],
                },
            )
            self._record_coordinator_state(coordinator_session_id, state)
            receiver_runtime.tools.permission_policy = self.permission_policy
            state = self.ledger.transition(
                handoff_id,
                state.version,
                "accepted",
                "receiver_running",
                receiver_profile,
                {"receiver_base_commit": source_commit},
            )
            self._record_coordinator_state(coordinator_session_id, state)
            receiver_result = await receiver_runtime.run(
                self._receiver_prompt(prompt, source_package), receiver_session_id
            )
            self.coordinator_sessions.append_message(
                coordinator_session_id,
                {
                    "role": "assistant",
                    "content": f"Receiving agent {receiver_profile} reported.\n\n{receiver_result.content[:8_000]}",
                },
            )
            if not self._report_accepts(receiver_result.content):
                state = self.ledger.transition(
                    handoff_id,
                    state.version,
                    "receiver_running",
                    "validation_failed",
                    receiver_profile,
                    {"validation_failure": "The receiving agent did not report Acceptance: PASS"},
                )
                self._record_coordinator_state(coordinator_session_id, state)
                raise HandoffValidationError("The receiving agent did not accept the handoff result")

            result_commit = self.workspaces.commit_changes(
                receiver_workspace, f"AgentDU handoff {handoff_id[:12]} receiver"
            )
            if not self.workspaces.is_ancestor(source_commit, result_commit):
                raise GitWorkspaceError("Receiver result is not based on the accepted source commit")

            check_evidence = await self._run_acceptance_checks(
                receiver_runtime,
                handoff_id,
                state,
                receiver_profile,
                checks,
            )
            failed_checks = [item for item in check_evidence if not item["passed"]]
            if failed_checks:
                state = self.ledger.transition(
                    handoff_id,
                    state.version,
                    "receiver_running",
                    "validation_failed",
                    receiver_profile,
                    {"acceptance_evidence": check_evidence},
                )
                self._record_coordinator_state(coordinator_session_id, state)
                raise HandoffValidationError(
                    "One or more explicit acceptance checks failed. The result remains isolated."
                )
            post_check_changes = self.workspaces.status(receiver_workspace)
            if post_check_changes:
                state = self.ledger.transition(
                    handoff_id,
                    state.version,
                    "receiver_running",
                    "validation_failed",
                    receiver_profile,
                    {
                        "acceptance_evidence": check_evidence,
                        "validation_failure": "Acceptance checks left project changes outside the verified commit",
                    },
                )
                self._record_coordinator_state(coordinator_session_id, state)
                raise HandoffValidationError(
                    "Acceptance checks left uncommitted project changes. The result remains isolated.\n"
                    + post_check_changes
                )

            result_files = self.workspaces.changed_files(base_commit, result_commit)
            result_digest = self.workspaces.diff_digest(base_commit, result_commit)
            self.workspaces.verify_snapshot(base_commit, result_commit, result_files, result_digest)
            final_package = {
                "result_commit": result_commit,
                "result_diff_sha256": result_digest,
                "changed_files": result_files,
                "receiver_report": receiver_result.content[:8_000],
                "acceptance_evidence": check_evidence,
                "receiver_session_id": receiver_session_id,
            }
            state = self.ledger.transition(
                handoff_id,
                state.version,
                "receiver_running",
                "verified",
                receiver_profile,
                final_package,
            )
            self._record_coordinator_state(coordinator_session_id, state)

            self._require_original_baseline(base_commit, handoff_id, state)
            self.workspaces.integrate(base_commit, result_commit)
            state = self.ledger.transition(
                handoff_id,
                state.version,
                "verified",
                "integrated",
                "coordinator",
                {"integrated_head": result_commit},
            )
            self._record_coordinator_state(coordinator_session_id, state)
            final_report = self._final_report(state)
            self.coordinator_sessions.append_message(
                coordinator_session_id,
                {"role": "assistant", "content": final_report},
            )
            self.coordinator_sessions.append(
                coordinator_session_id,
                "collaboration_completed",
                {"handoff_id": handoff_id, "result_commit": result_commit},
            )
            return HandoffOutcome(
                coordinator_session_id=coordinator_session_id,
                handoff_id=handoff_id,
                base_commit=base_commit,
                source_commit=source_commit,
                result_commit=result_commit,
                changed_files=tuple(result_files),
                acceptance_checks=tuple(check_evidence),
                report=final_report,
            )
        except BaseException as error:
            if handoff_id is not None:
                try:
                    current = self.ledger.read(handoff_id)
                    if current.status not in {
                        "integrated",
                        "rejected",
                        "validation_failed",
                        "failed",
                        "stale",
                        "abandoned",
                        "verified",
                    }:
                        state = self.ledger.transition(
                            handoff_id,
                            current.version,
                            current.status,
                            "failed",
                            "coordinator",
                            {"failure": f"{type(error).__name__}: {error}"[:2_000]},
                        )
                        self._record_coordinator_state(coordinator_session_id, state)
                except Exception:
                    pass
            if session_lock is not None and self.coordinator_sessions.exists(coordinator_session_id):
                self.coordinator_sessions.append(
                    coordinator_session_id,
                    "collaboration_error",
                    {
                        "handoff_id": handoff_id,
                        "error_type": type(error).__name__,
                        "message": str(error)[:2_000],
                    },
                )
            raise
        finally:
            if session_lock is not None:
                session_lock.release()
            repository_lock.release()

    def list_handoffs(self) -> list[HandoffState]:
        return self.ledger.list_states()

    async def abandon_handoff(self, handoff_id: str, reason: str) -> HandoffState:
        lock = await self._acquire_repository_lock()
        try:
            return self.ledger.abandon(handoff_id, reason)
        finally:
            lock.release()

    async def _acquire_session_lock(self, session_id: str) -> SessionProcessLock:
        return await self._wait_for_lock(
            lambda: self.coordinator_sessions.try_acquire_process_lock(session_id)
        )

    async def _acquire_repository_lock(self) -> SessionProcessLock:
        return await self._wait_for_lock(self.workspaces.try_acquire_handoff_lock)

    @staticmethod
    async def _wait_for_lock(
        acquire: Callable[[], SessionProcessLock | None],
    ) -> SessionProcessLock:
        while True:
            task = asyncio.create_task(asyncio.to_thread(acquire))
            try:
                lock = await asyncio.shield(task)
            except asyncio.CancelledError:
                lock = await task
                if lock is not None:
                    lock.release()
                raise
            if lock is not None:
                return lock
            await asyncio.sleep(0.05)

    def _begin_coordinator_session(self, session_id: str, prompt: str) -> None:
        self.coordinator_sessions.refresh_sequence(session_id)
        if not self.coordinator_sessions.exists(session_id):
            self.coordinator_sessions.create(session_id)
        messages = self.coordinator_sessions.read_messages(session_id)
        if not any(message.get("role") == "system" for message in messages):
            self.coordinator_sessions.append_message(
                session_id,
                {
                    "role": "system",
                    "content": "This is the user-facing AgentDU collaboration session. Agent work remains in isolated worktrees and is summarized through verified handoff records.",
                },
            )
        self.coordinator_sessions.append_message(session_id, {"role": "user", "content": prompt})

    def _create_agent_sessions(self, handoff_id: str) -> tuple[str, str]:
        agent_root = self.root / ".agentdu" / "agents" / handoff_id
        source_session = SessionStore(agent_root / "source").create()
        receiver_session = SessionStore(agent_root / "receiver").create()
        return source_session, receiver_session

    def _build_agent(
        self,
        profile_name: str,
        workspace: Path,
        handoff_id: str,
        role: str,
        role_instructions: str,
    ) -> AgentRuntime:
        options = resolve_profile_options(self.settings, profile_name)
        runtime = build_default_runtime(
            workspace=workspace,
            model=OpenAICompatibleModel(**options),
            permission_policy=self.permission_policy,
            data_dir=self.root / ".agentdu" / "agents" / handoff_id / role,
            context_tokens=self.context_tokens,
            max_steps=self.max_steps,
            system_prompt=f"{DEFAULT_SYSTEM_PROMPT}\n\n{role_instructions}",
        )
        if self.event_handler is not None:
            runtime.event_bus.subscribe(self.event_handler)
        return runtime

    @staticmethod
    def _source_instructions(checks: list[str]) -> str:
        return (
            "You are the implementation owner for a two-model handoff. Work only in this agent's "
            "Git worktree. Complete the user's task, inspect and test the project, and avoid Git "
            "branch, worktree, reset, or commit operations because AgentDU records the immutable "
            "handoff commit. Finish with a concise report covering changed files, design decisions, "
            "commands and test outcomes, and unresolved issues. The coordinator will pass this "
            f"report and verified source evidence to the receiver. Required acceptance commands are {checks!r}."
        )

    @staticmethod
    def _receiver_instructions() -> str:
        return (
            "You are the receiving and verification owner. Your context contains a bounded handoff "
            "package rather than the source agent's transcript. On the initial delivery request, "
            "inspect the package and respond with exactly one standalone line 'Handoff: ACCEPT' "
            "or 'Handoff: REJECT'. This phase is read-only. After acceptance, the coordinator will "
            "send a separate task request. Then inspect the actual code in this separate worktree, "
            "treat the source report as a claim rather than proof, continue or fix the task, run "
            "relevant checks, inspect the final diff, and report decisions and unresolved issues. "
            "End that task with exactly one standalone line 'Acceptance: PASS' or 'Acceptance: FAIL'. "
            "Use PASS only when the result is ready for the coordinator's explicit acceptance "
            "commands. Do not perform Git branch, worktree, reset, or commit operations."
        )

    @staticmethod
    def _receiver_offer_prompt(prompt: str, package: dict[str, Any]) -> str:
        return (
            "A source agent has offered a task package. Inspect the supplied metadata and confirm "
            "that you can take ownership. Do not edit files or run commands in this acknowledgement "
            "phase. Reply with exactly 'Handoff: ACCEPT' or 'Handoff: REJECT' on its own line.\n\n"
            f"Original user task\n{prompt}\n\n"
            f"Handoff package\n{json.dumps(package, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _source_prompt(prompt: str, checks: list[str]) -> str:
        return (
            f"User task\n{prompt}\n\n"
            "Complete the task in this isolated worktree. Report implementation changes, design "
            "decisions, actual command and test outcomes, and unresolved issues."
            f"\n\nRequired acceptance commands\n{json.dumps(checks, ensure_ascii=False)}"
        )

    @staticmethod
    def _receiver_prompt(prompt: str, package: dict[str, Any]) -> str:
        encoded_package = json.dumps(package, ensure_ascii=False, indent=2)
        return (
            f"Original user task\n{prompt}\n\n"
            "Continue this task from the accepted source commit. Inspect the project files and "
            "verify claims against tool output. The JSON package is bounded handoff metadata and "
            "evidence, not a copy of the previous conversation.\n\n"
            f"Handoff package\n{encoded_package}"
        )

    @staticmethod
    def _command_evidence(sessions: SessionStore, session_id: str) -> list[dict[str, Any]]:
        messages = sessions.read_messages(session_id)
        events = sessions.read_events(session_id)
        commands: dict[str, str] = {}
        results: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    function = call.get("function", {})
                    if function.get("name") != "run_command":
                        continue
                    try:
                        arguments = json.loads(function.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        continue
                    command = arguments.get("command")
                    if isinstance(command, str):
                        commands[call.get("id", "")] = command
            elif message.get("role") == "tool" and message.get("name") == "run_command":
                content = message.get("content", "")
                call_id = message.get("tool_call_id", "")
                finished = next(
                    (
                        event["payload"]
                        for event in reversed(events)
                        if event["kind"] == "tool_finished"
                        and event["payload"].get("tool_call_id") == call_id
                    ),
                    {},
                )
                results.append(
                    {
                        "command": commands.get(call_id, ""),
                        "is_error": bool(finished.get("is_error", True)),
                        "result_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "result_excerpt": content[-1_000:],
                    }
                )
        return results[-12:]

    async def _run_acceptance_checks(
        self,
        runtime: AgentRuntime,
        handoff_id: str,
        state: HandoffState,
        receiver_profile: str,
        checks: list[str],
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for command in checks:
            result = await runtime.tools.execute("run_command", {"command": command})
            item = {
                "command": command,
                "passed": not result.is_error,
                "result_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                "result_excerpt": result.text[-2_000:],
            }
            evidence.append(item)
            self.ledger.record_evidence(
                handoff_id,
                state.version,
                receiver_profile,
                item,
            )
        return evidence

    def _verify_source_package(self, package: dict[str, Any]) -> None:
        self.workspaces.verify_snapshot(
            package["base_commit"],
            package["source_commit"],
            package["changed_files"],
            package["diff_sha256"],
        )

    def _require_original_baseline(
        self,
        base_commit: str,
        handoff_id: str,
        state: HandoffState,
    ) -> None:
        if self.workspaces.head() == base_commit and not self.workspaces.status():
            return
        stale = self.ledger.transition(
            handoff_id,
            state.version,
            state.status,
            "stale",
            "coordinator",
            {
                "stale_reason": "The original worktree changed while the handoff was in progress",
                "observed_head": self.workspaces.head(),
                "observed_status": self.workspaces.status(),
            },
        )
        self._record_coordinator_state(state.data["coordinator_session_id"], stale)
        raise GitWorkspaceError("The original project changed during handoff. The result remains isolated.")

    async def _reconcile_verified_handoffs(self, held_session_id: str) -> None:
        for state in self.ledger.active_states():
            if state.status != "verified":
                continue
            data = state.data
            base_commit = data["base_commit"]
            result_commit = data["result_commit"]
            self.workspaces.verify_snapshot(
                base_commit,
                result_commit,
                data["changed_files"],
                data["result_diff_sha256"],
            )
            current_head = self.workspaces.head()
            if current_head == result_commit or self.workspaces.is_ancestor(result_commit, current_head):
                integrated_head = current_head
            elif current_head == base_commit and not self.workspaces.status():
                self.workspaces.integrate(base_commit, result_commit)
                integrated_head = result_commit
            else:
                stale = self.ledger.transition(
                    state.handoff_id,
                    state.version,
                    "verified",
                    "stale",
                    "coordinator",
                    {"stale_reason": "The original project advanced before handoff recovery"},
                )
                await self._record_reconciled_state(
                    data["coordinator_session_id"], stale, held_session_id
                )
                continue
            integrated = self.ledger.transition(
                state.handoff_id,
                state.version,
                "verified",
                "integrated",
                "coordinator",
                {"integrated_head": integrated_head, "recovered_after_restart": True},
            )
            await self._record_reconciled_state(
                data["coordinator_session_id"], integrated, held_session_id
            )
        await self._ensure_completion_events(held_session_id)

    async def _record_reconciled_state(
        self,
        session_id: str,
        state: HandoffState,
        held_session_id: str,
    ) -> None:
        lock = await self._lock_for_reconciled_session(session_id, held_session_id)
        try:
            self.coordinator_sessions.refresh_sequence(session_id)
            self._record_coordinator_state(session_id, state)
        finally:
            if lock is not None:
                lock.release()

    async def _ensure_completion_events(self, held_session_id: str) -> None:
        for state in self.ledger.list_states():
            if state.status != "integrated":
                continue
            session_id = state.data.get("coordinator_session_id")
            if not session_id or not self.coordinator_sessions.exists(session_id):
                continue
            lock = await self._lock_for_reconciled_session(session_id, held_session_id)
            try:
                self.coordinator_sessions.refresh_sequence(session_id)
                events = self.coordinator_sessions.read_events(session_id)
                if any(
                    event["kind"] == "collaboration_completed"
                    and event["payload"].get("handoff_id") == state.handoff_id
                    for event in events
                ):
                    continue
                messages = self.coordinator_sessions.read_messages(session_id)
                report_prefix = f"Handoff {state.handoff_id} integrated at "
                if not any(
                    message.get("role") == "assistant"
                    and isinstance(message.get("content"), str)
                    and message["content"].startswith(report_prefix)
                    for message in messages
                ):
                    self.coordinator_sessions.append_message(
                        session_id,
                        {"role": "assistant", "content": self._final_report(state)},
                    )
                self.coordinator_sessions.append(
                    session_id,
                    "collaboration_completed",
                    {
                        "handoff_id": state.handoff_id,
                        "result_commit": state.data["result_commit"],
                        "recovered_after_restart": True,
                    },
                )
            finally:
                if lock is not None:
                    lock.release()

    async def _lock_for_reconciled_session(
        self,
        session_id: str,
        held_session_id: str,
    ) -> SessionProcessLock | None:
        if session_id == held_session_id:
            return None
        return await self._wait_for_lock(
            lambda: self.coordinator_sessions.try_acquire_process_lock(session_id)
        )

    def _begin_coordinator_session(self, session_id: str, prompt: str) -> None:
        self.coordinator_sessions.refresh_sequence(session_id)
        if not self.coordinator_sessions.exists(session_id):
            self.coordinator_sessions.create(session_id)
        messages = self.coordinator_sessions.read_messages(session_id)
        if not any(message.get("role") == "system" for message in messages):
            self.coordinator_sessions.append_message(
                session_id,
                {
                    "role": "system",
                    "content": "This is the user-facing AgentDU collaboration session. Agent work is summarized through versioned handoff records and verified Git state.",
                },
            )
        self.coordinator_sessions.append_message(session_id, {"role": "user", "content": prompt})

    def _record_coordinator_state(self, session_id: str, state: HandoffState) -> None:
        self.coordinator_sessions.append(
            session_id,
            "handoff_state",
            {
                "handoff_id": state.handoff_id,
                "version": state.version,
                "status": state.status,
                "owner": state.owner,
            },
        )

    @staticmethod
    def _report_accepts(report: str) -> bool:
        return HandoffCoordinator._has_exclusive_decision(
            report, "ACCEPTANCE: PASS", "ACCEPTANCE: FAIL"
        )

    @staticmethod
    def _has_exclusive_decision(report: str, accepted: str, rejected: str) -> bool:
        decisions = [
            line.strip().upper()
            for line in report.splitlines()
            if line.strip().upper() in {accepted, rejected}
        ]
        return decisions == [accepted]

    @staticmethod
    def _final_report(state: HandoffState) -> str:
        files = state.data.get("changed_files", [])
        checks = state.data.get("acceptance_evidence", [])
        return (
            f"Handoff {state.handoff_id} integrated at {state.data['result_commit']}.\n"
            f"Changed files: {', '.join(files)}\n"
            f"Acceptance checks passed: {len(checks)}\n"
            f"Source report: {state.data.get('source_report', '')[:2_000]}\n"
            f"Receiver report: {state.data.get('receiver_report', '')[:2_000]}"
        )

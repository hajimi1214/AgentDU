from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .sessions import SessionProcessLock


class GitWorkspaceError(RuntimeError):
    pass


class GitWorkspaces:
    """Creates isolated worktrees and verifies immutable Git snapshots."""

    def __init__(self, workspace: Path) -> None:
        requested_workspace = workspace.resolve()
        executable = os.environ.get("AGENTDU_GIT_EXECUTABLE") or shutil.which("git")
        if executable is None:
            bundled_git = next(
                (
                    parent / ".tools" / "mingit" / "cmd" / "git.exe"
                    for parent in (requested_workspace, *requested_workspace.parents)
                    if (parent / ".tools" / "mingit" / "cmd" / "git.exe").is_file()
                ),
                None,
            )
            if bundled_git is not None:
                executable = str(bundled_git)
        if executable is None:
            raise GitWorkspaceError("Git was not found on PATH")
        self.executable = executable
        self.root = Path(self.git(requested_workspace, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
        self.workspace_relative = requested_workspace.relative_to(self.root)
        common_dir = Path(self.git(self.root, "rev-parse", "--git-common-dir").stdout.decode().strip())
        self.common_dir = (self.root / common_dir).resolve() if not common_dir.is_absolute() else common_dir

    def acquire_handoff_lock(self) -> SessionProcessLock:
        return SessionProcessLock(self.common_dir / "agentdu-handoff.lock").acquire()

    @contextmanager
    def handoff_lock(self) -> Iterator[None]:
        lock = self.acquire_handoff_lock()
        try:
            yield
        finally:
            lock.release()

    def try_acquire_handoff_lock(self) -> SessionProcessLock | None:
        lock = SessionProcessLock(self.common_dir / "agentdu-handoff.lock")
        return lock if lock.try_acquire() else None

    def worktree_path(self, handoff_id: str, role: str) -> Path:
        return self.common_dir / "agentdu-worktrees" / handoff_id / role

    def git(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment["GIT_CONFIG_COUNT"] = "0"
        result = subprocess.run(
            [self.executable, *arguments],
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            command = " ".join(arguments)
            raise GitWorkspaceError(
                f"Git command failed ({command}): "
                f"{detail or f'exit status {result.returncode}'}"
            )
        return result

    def head(self, cwd: Path | None = None) -> str:
        return self.git(cwd or self.root, "rev-parse", "HEAD").stdout.decode().strip()

    def status(self, cwd: Path | None = None) -> str:
        result = self.git(
            cwd or self.root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).agentdu",
        )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def require_clean(self) -> str:
        try:
            base_commit = self.head()
        except GitWorkspaceError as error:
            raise GitWorkspaceError(
                "Multi-model runs require a Git repository with an initial commit"
            ) from error
        status = self.status()
        if status:
            raise GitWorkspaceError(
                "Multi-model runs require a clean Git worktree. Commit or move current changes first.\n"
                + status
            )
        return base_commit

    def add_worktree(self, handoff_id: str, role: str, branch: str, commit: str) -> Path:
        worktree = self.worktree_path(handoff_id, role)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "worktree", "add", "--quiet", "-b", branch, str(worktree), commit)
        return worktree / self.workspace_relative

    def add_readonly_worktree(self, task_id: str, role: str, commit: str) -> Path:
        worktree = self.worktree_path(task_id, role)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "worktree", "add", "--quiet", "--detach", str(worktree), commit)
        return worktree / self.workspace_relative

    def commit_changes(self, cwd: Path, message: str) -> str:
        self.git(cwd, "add", "-A")
        staged = self.git(cwd, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 1:
            self.git(
                cwd,
                "-c",
                "user.name=AgentDU Handoff",
                "-c",
                "user.email=agentdu@localhost",
                "commit",
                "-m",
                message,
            )
        elif staged.returncode != 0:
            raise GitWorkspaceError("Could not inspect staged changes before handoff commit")
        if self.status(cwd):
            raise GitWorkspaceError(f"Agent worktree still has uncommitted changes:\n{self.status(cwd)}")
        return self.head(cwd)

    def changed_files(self, base_commit: str, result_commit: str) -> list[str]:
        result = self.git(
            self.root,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            base_commit,
            result_commit,
            "--",
        )
        return [
            os.fsdecode(path)
            for path in result.stdout.split(b"\0")
            if path
        ]

    def diff_digest(self, base_commit: str, result_commit: str) -> str:
        result = self.git(
            self.root,
            "diff",
            "--binary",
            "--no-ext-diff",
            base_commit,
            result_commit,
            "--",
        )
        return hashlib.sha256(result.stdout).hexdigest()

    def is_ancestor(self, ancestor: str, commit: str) -> bool:
        result = self.git(
            self.root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            commit,
            check=False,
        )
        return result.returncode == 0

    def verify_snapshot(
        self,
        base_commit: str,
        result_commit: str,
        expected_files: list[str],
        expected_digest: str,
    ) -> None:
        if not self.is_ancestor(base_commit, result_commit):
            raise GitWorkspaceError("Handoff commit is not based on the accepted project revision")
        if self.changed_files(base_commit, result_commit) != expected_files:
            raise GitWorkspaceError("Handoff changed-file list does not match its recorded evidence")
        if self.diff_digest(base_commit, result_commit) != expected_digest:
            raise GitWorkspaceError("Handoff diff digest does not match its recorded evidence")

    def integrate(self, base_commit: str, result_commit: str) -> None:
        if self.head() != base_commit:
            raise GitWorkspaceError("The project HEAD changed during handoff")
        if self.status():
            raise GitWorkspaceError("The project worktree became dirty during handoff")
        if not self.is_ancestor(base_commit, result_commit):
            raise GitWorkspaceError("The verified result is not a descendant of the project baseline")
        self.git(self.root, "merge", "--ff-only", "--no-edit", result_commit)
        if self.head() != result_commit:
            raise GitWorkspaceError("Git did not advance to the verified handoff commit")

    def merge_verified_result(
        self,
        base_commit: str,
        result_commit: str,
        expected_files: list[str],
        expected_digest: str,
        acceptance_check: Callable[[Path], list[dict[str, Any]]],
        message: str,
        attempt_id: str,
        on_candidate_commit: Callable[[str], None],
    ) -> tuple[str, list[dict[str, Any]], bool]:
        self.verify_snapshot(base_commit, result_commit, expected_files, expected_digest)
        if self.status():
            raise GitWorkspaceError("The main worktree must be clean before result integration")
        current_head = self.head()
        if not self.is_ancestor(base_commit, current_head):
            raise GitWorkspaceError("The task baseline is no longer an ancestor of the main branch")
        if self.is_ancestor(result_commit, current_head):
            before_checks = self.status()
            evidence = acceptance_check(self.root)
            if not evidence or any(not item.get("passed", False) for item in evidence):
                raise GitWorkspaceError("Acceptance checks did not all pass")
            if self.status() != before_checks:
                raise GitWorkspaceError("Acceptance checks changed the main worktree")
            return current_head, evidence, True

        candidate = self.add_readonly_worktree(attempt_id, "integration", current_head)
        merge = self.git(
            candidate,
            "-c",
            "user.name=AgentDU Integration",
            "-c",
            "user.email=agentdu@localhost",
            "merge",
            "--no-commit",
            "--no-ff",
            "--no-edit",
            result_commit,
            check=False,
        )
        if merge.returncode != 0:
            detail = merge.stderr.decode("utf-8", errors="replace").strip()
            self.git(candidate, "merge", "--abort", check=False)
            raise GitWorkspaceError(detail or "Git could not merge the task result cleanly")

        try:
            before_checks = self.status(candidate)
            evidence = acceptance_check(candidate)
            if not evidence or any(not item.get("passed", False) for item in evidence):
                raise GitWorkspaceError("Acceptance checks did not all pass")
            if self.status(candidate) != before_checks:
                raise GitWorkspaceError(
                    "Acceptance checks changed the integration worktree"
                )
            candidate_commit = self.commit_changes(candidate, message)
            if (
                not self.is_ancestor(result_commit, candidate_commit)
                or not self.is_ancestor(current_head, candidate_commit)
                or self.status(candidate)
            ):
                raise GitWorkspaceError("Git did not leave a clean candidate containing the result")
            on_candidate_commit(candidate_commit)
            if self.head() != current_head or self.status():
                raise GitWorkspaceError("The main branch changed during task integration")
            self.git(self.root, "merge", "--ff-only", "--no-edit", candidate_commit)
            integrated_commit = self.head()
            if integrated_commit != candidate_commit or self.status():
                raise GitWorkspaceError("Git did not advance cleanly to the verified task result")
            return integrated_commit, evidence, False
        except BaseException:
            self.git(candidate, "merge", "--abort", check=False)
            raise

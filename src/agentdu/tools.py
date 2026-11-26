from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import signal
import shutil
import subprocess
import stat
import tempfile
import threading
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .permissions import PermissionPolicy, PermissionRequest
from .security import redact_value, safe_child_environment


MAX_TOOL_OUTPUT_CHARS = 16_000
IGNORED_DIRECTORIES = {
    ".agentdu",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str
    handler: Callable[[dict[str, Any], Path], str | ToolResult]
    context_handler: Callable[
        [dict[str, Any], Path, str, asyncio.AbstractEventLoop], str | ToolResult
    ] | None = None

    def to_model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, workspace: Path, permission_policy: PermissionPolicy) -> None:
        self.workspace = workspace.resolve()
        self.permission_policy = permission_policy
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def model_schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].to_model_schema() for name in sorted(self._tools)]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            _validate_arguments(tool.parameters, arguments)
        except ValueError as error:
            return ToolResult(f"Invalid arguments: {error}", is_error=True)

        request = PermissionRequest(
            permission=tool.permission,
            tool_name=tool.name,
            arguments=arguments,
            workspace=self.workspace,
        )
        authorized = await asyncio.to_thread(self.permission_policy.authorize, request)
        if not authorized:
            return ToolResult(f"Permission denied for tool '{name}'", is_error=True)

        try:
            if tool.context_handler is not None:
                if session_id is None:
                    return ToolResult(
                        f"Tool '{name}' requires an AgentDU session context",
                        is_error=True,
                    )
                parent_loop = asyncio.get_running_loop()
                output = await asyncio.to_thread(
                    tool.context_handler,
                    arguments,
                    self.workspace,
                    session_id,
                    parent_loop,
                )
            else:
                output = await asyncio.to_thread(tool.handler, arguments, self.workspace)
        except Exception as error:
            output = f"{type(error).__name__}: {error}"
            return ToolResult(_limit_output(output), is_error=True)
        if isinstance(output, ToolResult):
            return ToolResult(_limit_output(output.text), is_error=output.is_error)
        return ToolResult(_limit_output(output))


def create_builtin_tools(
    secret_values: Collection[str] = (),
    command_runner: Callable[[dict[str, Any], Path, Collection[str]], ToolResult] | None = None,
) -> list[ToolDefinition]:
    def run_command(arguments: dict[str, Any], workspace: Path) -> ToolResult:
        if command_runner is not None:
            return command_runner(arguments, workspace, secret_values)
        return _run_command(arguments, workspace, secret_values)

    return [
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file with line numbers. Use line ranges to inspect source efficiently.",
            parameters=_object_schema({"path": {"type": "string"}}, ["path"]),
            permission="read",
            handler=_read_file,
        ),
        ToolDefinition(
            name="list_files",
            description="List workspace files matching a glob pattern. Common generated and VCS directories are skipped.",
            parameters=_object_schema(
                {"pattern": {"type": "string"}, "limit": {"type": "integer"}}, []
            ),
            permission="read",
            handler=_list_files,
        ),
        ToolDefinition(
            name="search_text",
            description="Search text files in the workspace with a regular expression and return matching lines with paths and line numbers.",
            parameters=_object_schema(
                {
                    "pattern": {"type": "string"},
                    "file_pattern": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                },
                ["pattern"],
            ),
            permission="read",
            handler=_search_text,
        ),
        ToolDefinition(
            name="edit_file",
            description="Replace one unique exact text block in a workspace file. Read the file first and include enough surrounding lines to make the match unique.",
            parameters=_object_schema(
                {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                ["path", "old_text", "new_text"],
            ),
            permission="write",
            handler=_edit_file,
        ),
        ToolDefinition(
            name="write_file",
            description="Create or replace a UTF-8 text file inside the workspace.",
            parameters=_object_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            permission="write",
            handler=_write_file,
        ),
        ToolDefinition(
            name="run_command",
            description="Run a shell command in the workspace. This tool requires explicit approval.",
            parameters=_object_schema({"command": {"type": "string"}}, ["command"]),
            permission="execute",
            handler=run_command,
        ),
        ToolDefinition(
            name="git_status",
            description="Show the Git working tree status for the workspace.",
            parameters=_object_schema({}, []),
            permission="read",
            handler=_git_status,
        ),
        ToolDefinition(
            name="git_diff",
            description="Show the Git diff for the workspace, including unstaged and staged changes.",
            parameters=_object_schema(
                {"path": {"type": "string"}, "staged": {"type": "boolean"}}, []
            ),
            permission="read",
            handler=_git_diff,
        ),
        ToolDefinition(
            name="git_log",
            description="Show the five most recent Git commits for the workspace.",
            parameters=_object_schema({}, []),
            permission="read",
            handler=_git_log,
        ),
    ]


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _validate_arguments(schema: dict[str, Any], value: dict[str, Any]) -> None:
    def validate(definition: dict[str, Any], item: Any, location: str) -> None:
        expected = definition.get("type")
        checks = {
            "string": lambda candidate: isinstance(candidate, str),
            "integer": lambda candidate: isinstance(candidate, int) and not isinstance(candidate, bool),
            "number": lambda candidate: isinstance(candidate, (int, float)) and not isinstance(candidate, bool),
            "boolean": lambda candidate: isinstance(candidate, bool),
            "object": lambda candidate: isinstance(candidate, dict),
            "array": lambda candidate: isinstance(candidate, list),
        }
        check = checks.get(expected)
        if check is not None and not check(item):
            raise ValueError(f"{location} must be {expected}")
        if "enum" in definition and item not in definition["enum"]:
            raise ValueError(f"{location} must be one of {definition['enum']}")
        if expected == "object":
            properties = definition.get("properties", {})
            for key in definition.get("required", []):
                if key not in item:
                    raise ValueError(f"{location} is missing required field '{key}'")
            if definition.get("additionalProperties") is False:
                extra = item.keys() - properties.keys()
                if extra:
                    raise ValueError(f"{location} has unexpected field(s): {', '.join(sorted(extra))}")
            for key, child in item.items():
                if key in properties:
                    validate(properties[key], child, f"{location}.{key}")
        if expected == "array" and "items" in definition:
            for index, child in enumerate(item):
                validate(definition["items"], child, f"{location}[{index}]")

    validate(schema, value, "arguments")


def _workspace_file(workspace: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as error:
        raise ValueError("path must stay inside the workspace") from error
    return resolved


def _read_file(arguments: dict[str, Any], workspace: Path) -> str:
    path = _workspace_file(workspace, arguments["path"])
    with path.open("r", encoding="utf-8") as file:
        content = file.read(MAX_TOOL_OUTPUT_CHARS + 1)
    numbered = "\n".join(f"{number}: {line}" for number, line in enumerate(content.splitlines(), 1))
    if len(content) > MAX_TOOL_OUTPUT_CHARS:
        numbered += "\n[read output truncated]"
    return numbered


def _iter_workspace_files(workspace: Path) -> Iterator[Path]:
    for root, directories, filenames in os.walk(workspace, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_DIRECTORIES
            and not (Path(root) / directory).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(root) / filename
            if path.is_symlink():
                continue
            yield path


def _matches_pattern(relative_path: str, pattern: str) -> bool:
    alternatives = [pattern]
    if pattern.startswith("**/"):
        alternatives.append(pattern[3:])
    return any(fnmatch.fnmatchcase(relative_path, candidate) for candidate in alternatives)


def _list_files(arguments: dict[str, Any], workspace: Path) -> str:
    pattern = arguments.get("pattern", "**/*")
    limit = max(1, min(arguments.get("limit", 200), 1000))
    shown: list[str] = []
    match_count = 0
    for path in _iter_workspace_files(workspace):
        relative_path = path.relative_to(workspace).as_posix()
        if not _matches_pattern(relative_path, pattern):
            continue
        match_count += 1
        if len(shown) < limit:
            shown.append(relative_path)
    result = "\n".join(shown) if shown else "No files matched."
    if match_count > limit:
        result += f"\n[showing {limit} of {match_count} files]"
    return result


def _search_text(arguments: dict[str, Any], workspace: Path) -> str:
    try:
        expression = re.compile(
            arguments["pattern"],
            0 if arguments.get("case_sensitive", False) else re.IGNORECASE,
        )
    except re.error as error:
        raise ValueError(f"invalid regular expression: {error}") from error
    file_pattern = arguments.get("file_pattern", "**/*")
    matches: list[str] = []
    for path in _iter_workspace_files(workspace):
        relative_path = path.relative_to(workspace).as_posix()
        if not _matches_pattern(relative_path, file_pattern):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            with path.open("r", encoding="utf-8") as file:
                for line_number, line in enumerate(file, 1):
                    if expression.search(line):
                        matches.append(f"{relative_path}:{line_number}: {line.rstrip()}")
                        if len(matches) >= 200:
                            return "\n".join(matches) + "\n[showing first 200 matches]"
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(matches) if matches else "No matches found."


def _edit_file(arguments: dict[str, Any], workspace: Path) -> str:
    path = _workspace_file(workspace, arguments["path"])
    content = path.read_text(encoding="utf-8")
    old_text = arguments["old_text"]
    occurrences = content.count(old_text)
    if not old_text or occurrences != 1:
        raise ValueError(f"old_text must match exactly once, found {occurrences} matches")
    updated = content.replace(old_text, arguments["new_text"], 1)
    _atomic_write(path, updated)
    return f"Edited {path.relative_to(workspace)}"


def _write_file(arguments: dict[str, Any], workspace: Path) -> str:
    path = _workspace_file(workspace, arguments["path"])
    if path.exists() and path.is_dir():
        raise IsADirectoryError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, arguments["content"])
    return f"Wrote {path.relative_to(workspace)}"


def _atomic_write(path: Path, content: str) -> None:
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=".agentdu-", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if existing_mode is not None:
            os.chmod(temporary_name, existing_mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _git_command(
    workspace: Path,
    *arguments: str,
    timeout_seconds: float = 20,
) -> str:
    executable = os.environ.get("AGENTDU_GIT_EXECUTABLE") or shutil.which("git")
    if executable is None:
        bundled_git = next(
            (
                parent / ".tools" / "mingit" / "cmd" / "git.exe"
                for parent in (workspace, *workspace.parents)
                if (parent / ".tools" / "mingit" / "cmd" / "git.exe").is_file()
            ),
            None,
        )
        if bundled_git is not None:
            executable = str(bundled_git)
    if executable is None:
        raise FileNotFoundError("Git was not found on PATH")
    result = subprocess.run(
        [executable, "-C", str(workspace), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"git exited with status {result.returncode}")
    return output or "(no output)"


def _git_status(arguments: dict[str, Any], workspace: Path) -> str:
    return _git_command(workspace, "status", "--short", "--branch")


def _git_diff(arguments: dict[str, Any], workspace: Path) -> str:
    command = ["diff", "--no-ext-diff"]
    if arguments.get("staged", False):
        command.append("--cached")
    command.append("--")
    if arguments.get("path"):
        command.append(arguments["path"])
    return _git_command(workspace, *command)


def _git_log(arguments: dict[str, Any], workspace: Path) -> str:
    return _git_command(workspace, "log", "-5", "--oneline", "--decorate")


def _run_command(
    arguments: dict[str, Any],
    workspace: Path,
    secret_values: tuple[str, ...] = (),
) -> ToolResult:
    command = arguments["command"]
    timed_out = False
    output_capped = False
    captured = bytearray()
    captured_lock = threading.Lock()
    capture_finished = threading.Event()
    process_options: dict[str, Any] = {
        "shell": True,
        "cwd": workspace,
        "env": safe_child_environment(secret_values),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if os.name != "nt":
        process_options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        **process_options,
    )

    def capture_output() -> None:
        nonlocal output_capped
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(4096):
                with captured_lock:
                    if capture_finished.is_set():
                        continue
                    remaining = MAX_TOOL_OUTPUT_CHARS - len(captured)
                    captured.extend(chunk[: max(0, remaining)])
                    if len(chunk) > remaining:
                        output_capped = True
        finally:
            process.stdout.close()

    reader = threading.Thread(target=capture_output, daemon=True)
    reader.start()
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        process.wait()
    finally:
        reader.join(timeout=2)
        background_process = reader.is_alive()
        capture_finished.set()
        if background_process:
            _kill_process_tree(process)
            reader.join(timeout=2)

    status = "Command timed out after 60 seconds." if timed_out else f"exit code: {process.returncode}"
    if output_capped:
        status = "Command output exceeded the limit and was truncated."
    if background_process:
        status += " A background process retained stdout after the command ended; cleanup was attempted."
    with captured_lock:
        output = captured.decode("utf-8", errors="replace")
    safe_output = redact_value(output, secret_values)
    return ToolResult(
        f"{status}\n{safe_output}",
        is_error=timed_out or background_process or process.returncode != 0,
    )


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _limit_output(value: str) -> str:
    if len(value) <= MAX_TOOL_OUTPUT_CHARS:
        return value
    marker = "\n[tool output truncated]"
    return value[: MAX_TOOL_OUTPUT_CHARS - len(marker)] + marker

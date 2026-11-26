from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import uuid
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .security import redact_value, safe_child_environment
from .tools import MAX_TOOL_OUTPUT_CHARS, ToolResult


def run_appcontainer_command(
    arguments: dict[str, Any],
    workspace: Path,
    secret_values: Collection[str],
) -> ToolResult:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 16_000 or "\0" in command:
        return ToolResult("Command must contain 1–16,000 characters and no NUL byte.", is_error=True)
    if os.name != "nt":
        return ToolResult("Desktop command execution requires the validated Windows AppContainer launcher.", is_error=True)

    windows_directory = os.environ.get("WINDIR", r"C:\Windows")
    powershell = Path(windows_directory) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    launcher = Path(__file__).with_name("windows_appcontainer_command.ps1")
    if not powershell.is_file() or not launcher.is_file():
        return ToolResult("The Windows AppContainer command launcher is unavailable.", is_error=True)

    run_id = uuid.uuid4().hex
    temporary_directory = workspace / ".agentdu" / "command-runs" / run_id
    request = {
        "workspace": str(workspace.resolve()),
        "command": command,
        "timeoutSeconds": 60,
        "runId": run_id,
    }
    environment = safe_child_environment(secret_values)
    environment["TEMP"] = str(temporary_directory)
    environment["TMP"] = str(temporary_directory)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
            ],
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, stderr = process.communicate(
            json.dumps(request, ensure_ascii=False).encode("utf-8"),
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate_runner_tree(process)
        return ToolResult("AppContainer launcher did not finish within 180 seconds. Workspace permissions may require inspection.", is_error=True)
    except OSError as error:
        return ToolResult(f"AppContainer launcher could not start: {type(error).__name__}", is_error=True)

    if os.name == "nt":
        output_code_page = ctypes.windll.kernel32.GetOEMCP()
        stderr_encoding = f"cp{output_code_page}"
    else:
        stderr_encoding = "utf-8"
    safe_stderr = redact_value(stderr.decode(stderr_encoding, errors="replace"), secret_values)
    if process.returncode != 0:
        detail = str(safe_stderr).strip()[:2_000]
        return ToolResult(f"AppContainer launch failed with exit code {process.returncode}. {detail}", is_error=True)
    try:
        result = json.loads(stdout.decode("utf-8"))
        if not isinstance(result, dict) or result.get("isAppContainer") is not True:
            raise ValueError("launcher did not verify AppContainer identity")
        output = base64.b64decode(result.get("outputBase64", ""), validate=True).decode("utf-8", errors="replace")
        exit_code = result.get("exitCode")
        timed_out = result.get("timedOut") is True
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError("launcher returned an invalid exit code")
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        detail = str(safe_stderr).strip()[:1_000]
        return ToolResult(f"AppContainer returned an invalid result: {error}. {detail}", is_error=True)

    safe_output = str(redact_value(output, secret_values))
    if len(safe_output) > MAX_TOOL_OUTPUT_CHARS:
        safe_output = safe_output[:MAX_TOOL_OUTPUT_CHARS] + "\n[tool output truncated]"
    status = "Command timed out after 60 seconds." if timed_out else f"exit code: {exit_code}"
    return ToolResult(f"{status}\n{safe_output}", is_error=timed_out or exit_code != 0)


def _terminate_runner_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)

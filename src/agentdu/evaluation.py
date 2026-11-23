from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .git_workspaces import GitWorkspaceError, GitWorkspaces
from .settings import ModelSettings, default_config_path, resolve_profile_options


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agentdu-evaluate",
        description="Run paired solo and collaborative AgentDU evaluation cases",
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-timeout", type=float, default=1_800)
    parser.add_argument("--check-timeout", type=float, default=300)
    arguments = parser.parse_args()

    try:
        if (
            not math.isfinite(arguments.run_timeout)
            or arguments.run_timeout <= 0
            or not math.isfinite(arguments.check_timeout)
            or arguments.check_timeout <= 0
        ):
            raise ValueError("Run and check timeouts must be positive finite numbers")
        settings = ModelSettings.load(arguments.config)
        cases = _load_cases(arguments.cases)
        output_path = arguments.output.resolve()
        if output_path == arguments.cases.resolve():
            raise ValueError("The output file must not overwrite the case file")
        if any(
            _is_inside(output_path, workspace)
            for case in cases
            for workspace in (case["solo_workspace"], case["collaborative_workspace"])
        ):
            raise ValueError("The output file must be outside all evaluated workspaces")
        records: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []
        for case in cases:
            _validate_profiles(settings, case["profile"])
            _validate_workspaces(case)
            solo = _run_case(
                case,
                "solo",
                case["solo_workspace"],
                arguments.config,
                arguments.run_timeout,
                arguments.check_timeout,
            )
            collaborative = _run_case(
                case,
                "collaborative",
                case["collaborative_workspace"],
                arguments.config,
                arguments.run_timeout,
                arguments.check_timeout,
            )
            records.extend((solo, collaborative))
            comparisons.append(_compare_pair(solo, collaborative))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
            for record in records:
                output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            for comparison in comparisons:
                output_file.write(json.dumps(comparison, ensure_ascii=False) + "\n")
        print(f"Wrote {len(records)} run records and {len(comparisons)} paired comparisons to {output_path}")
        return 0 if all(record["passed"] for record in records) else 1
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"AgentDU evaluation error: {error}", file=sys.stderr)
        return 2


def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("The case file must contain a non-empty JSON array")
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    used_workspaces: set[Path] = set()
    required = {"id", "profile", "prompt", "solo_workspace", "collaborative_workspace", "checks"}
    for item in data:
        if not isinstance(item, dict) or not required <= item.keys():
            raise ValueError(f"Each case must define {', '.join(sorted(required))}")
        if (
            not isinstance(item["id"], str)
            or not item["id"].strip()
            or item["id"] in ids
            or not isinstance(item["profile"], str)
            or not isinstance(item["prompt"], str)
            or not item["prompt"].strip()
        ):
            raise ValueError("Case IDs must be unique, and profile and prompt must be non-empty strings")
        ids.add(item["id"])
        checks = item["checks"]
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"Case '{item['id']}' must include at least one acceptance command")
        if any(
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) for argument in command)
            for command in checks
        ):
            raise ValueError(f"Case '{item['id']}' checks must be arrays of command arguments")
        workspaces = [Path(item[key]).expanduser().resolve() for key in ("solo_workspace", "collaborative_workspace")]
        if workspaces[0] == workspaces[1]:
            raise ValueError(f"Case '{item['id']}' needs separate solo and collaborative workspaces")
        if _is_inside(workspaces[0], workspaces[1]) or _is_inside(workspaces[1], workspaces[0]):
            raise ValueError(f"Case '{item['id']}' workspaces must not contain one another")
        if any(workspace in used_workspaces for workspace in workspaces):
            raise ValueError("Each evaluation run must use its own workspace copy")
        used_workspaces.update(workspaces)
        if any(not workspace.is_dir() for workspace in workspaces):
            raise ValueError(f"Case '{item['id']}' workspaces must already exist")
        cases.append({**item, "solo_workspace": workspaces[0], "collaborative_workspace": workspaces[1]})
    return cases


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _validate_workspaces(case: dict[str, Any]) -> None:
    try:
        solo = GitWorkspaces(case["solo_workspace"])
        collaborative = GitWorkspaces(case["collaborative_workspace"])
        solo_tree = solo.git(solo.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        collaborative_tree = collaborative.git(
            collaborative.root,
            "rev-parse",
            "HEAD^{tree}",
        ).stdout.decode().strip()
        solo.require_clean()
        collaborative.require_clean()
    except (GitWorkspaceError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Case '{case['id']}' needs clean Git workspaces with an initial commit: {error}") from error
    if solo_tree != collaborative_tree:
        raise ValueError(f"Case '{case['id']}' baseline Git trees differ")


def _validate_profiles(settings: ModelSettings, profile_name: str) -> None:
    if settings.resolve(profile_name) is None:
        raise ValueError(f"Evaluation profile is not defined: {profile_name}")
    main = resolve_profile_options(settings, profile_name)
    main_identity = (main["base_url"].rstrip("/"), main["model"])
    if not any(
        (options["base_url"].rstrip("/"), options["model"]) != main_identity
        for name in settings.profiles
        if name != profile_name
        for options in [resolve_profile_options(settings, name)]
    ):
        raise ValueError("Collaborative evaluation requires another distinct configured model profile")


def _run_case(
    case: dict[str, Any],
    mode: str,
    workspace: Path,
    config: Path,
    run_timeout: float,
    check_timeout: float,
) -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    command = [
        sys.executable,
        "-m",
        "agentdu",
        "--workspace",
        str(workspace),
        "--config",
        str(config.resolve()),
        "--profile",
        case["profile"],
        "--session",
        session_id,
        "--yes",
    ]
    if mode == "solo":
        command.append("--solo")
    command.append("--")
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            [*command, case["prompt"]],
            capture_output=True,
            text=True,
            timeout=run_timeout,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        return_code = None
        stdout = _as_text(error.stdout)
        stderr = _as_text(error.stderr)
    elapsed = time.monotonic() - started

    checks = [_run_check(argv, workspace, check_timeout) for argv in case["checks"]]
    events_path = workspace / ".agentdu" / "sessions" / f"{session_id}.jsonl"
    events = _read_events(events_path)
    usage = _summarize_usage(events)
    return {
        "case_id": case["id"],
        "mode": mode,
        "profile": case["profile"],
        "workspace": str(workspace),
        "session_id": session_id,
        "elapsed_seconds": round(elapsed, 3),
        "timed_out": timed_out,
        "agent_exit_code": return_code,
        "model_requests": usage["model_requests"],
        "usage": usage["usage"],
        "usage_complete": usage["usage_complete"],
        "estimated_cost_usd": usage["estimated_cost_usd"],
        "acceptance_checks": checks,
        "passed": return_code == 0 and not timed_out and all(check["passed"] for check in checks),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _summarize_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    model_requests = 0
    usage_calls: list[dict[str, int] | None] = []
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        child_event = payload.get("event") if kind == "collaboration_progress" else None
        if kind == "model_request" or child_event == "model_request":
            model_requests += 1
        if kind == "model_usage" or child_event == "model_usage":
            call_usage = payload.get("usage")
            if not isinstance(call_usage, dict):
                usage_calls.append(None)
                continue
            input_tokens = call_usage.get("input_tokens")
            output_tokens = call_usage.get("output_tokens")
            if not all(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0
                for count in (input_tokens, output_tokens)
            ):
                usage_calls.append(None)
                continue
            total_tokens = call_usage.get("total_tokens")
            if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
                total_tokens = input_tokens + output_tokens
            usage_calls.append(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                }
            )

    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for call_usage in usage_calls:
        if call_usage is None:
            continue
        for name in totals:
            totals[name] += call_usage[name]
    usage_complete = (
        model_requests > 0
        and model_requests == len(usage_calls)
        and all(item is not None for item in usage_calls)
    )
    cost_calls: list[float] = []
    for event in events:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        is_usage_event = event.get("kind") == "model_usage" or (
            event.get("kind") == "collaboration_progress" and payload.get("event") == "model_usage"
        )
        if is_usage_event:
            call_cost = payload.get("call_cost_usd")
            if (
                isinstance(call_cost, (int, float))
                and not isinstance(call_cost, bool)
                and math.isfinite(call_cost)
                and call_cost >= 0
            ):
                cost_calls.append(float(call_cost))
            else:
                cost_calls.append(None)
    cost = (
        sum(cost_calls)
        if usage_complete
        and len(cost_calls) == model_requests
        and all(item is not None for item in cost_calls)
        else None
    )
    return {
        "model_requests": model_requests,
        "usage": totals,
        "usage_complete": usage_complete,
        "estimated_cost_usd": cost,
    }


def _run_check(command: list[str], workspace: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
        return_code: int | None = result.returncode
    except subprocess.TimeoutExpired as error:
        output = _as_text(error.stdout) + _as_text(error.stderr)
        passed = False
        return_code = None
    return {
        "command_sha256": hashlib.sha256(
            json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "passed": passed,
        "exit_code": return_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _compare_pair(solo: dict[str, Any], collaborative: dict[str, Any]) -> dict[str, Any]:
    solo_tokens = solo["usage"]["total_tokens"]
    collaborative_tokens = collaborative["usage"]["total_tokens"]
    solo_cost = solo["estimated_cost_usd"]
    collaborative_cost = collaborative["estimated_cost_usd"]
    return {
        "record_type": "paired_comparison",
        "case_id": solo["case_id"],
        "solo_passed": solo["passed"],
        "collaborative_passed": collaborative["passed"],
        "elapsed_delta_seconds": round(
            collaborative["elapsed_seconds"] - solo["elapsed_seconds"], 3
        ),
        "model_requests_delta": collaborative["model_requests"] - solo["model_requests"],
        "total_tokens_delta": collaborative_tokens - solo_tokens,
        "estimated_cost_delta_usd": (
            collaborative_cost - solo_cost
            if isinstance(collaborative_cost, (int, float))
            and isinstance(solo_cost, (int, float))
            else None
        ),
    }


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


if __name__ == "__main__":
    raise SystemExit(main())

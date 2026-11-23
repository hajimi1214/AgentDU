from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentdu.evaluation import _compare_pair, _summarize_usage, main
from agentdu.git_workspaces import GitWorkspaces


class EvaluationTests(unittest.TestCase):
    def test_usage_summary_counts_main_and_collaborator_events(self) -> None:
        summary = _summarize_usage(
            [
                {"kind": "model_request", "payload": {}},
                {
                    "kind": "model_usage",
                    "payload": {
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        "call_cost_usd": 0.002,
                    },
                },
                {"kind": "collaboration_progress", "payload": {"event": "model_request"}},
                {
                    "kind": "collaboration_progress",
                    "payload": {
                        "event": "model_usage",
                        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                        "call_cost_usd": 0.004,
                    },
                },
            ]
        )

        self.assertEqual(summary["model_requests"], 2)
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["usage"]["total_tokens"], 45)
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.006)

    def test_missing_usage_disables_paired_cost_delta(self) -> None:
        solo = {
            "case_id": "fix",
            "passed": True,
            "elapsed_seconds": 8.0,
            "model_requests": 2,
            "usage": {"total_tokens": 100},
            "estimated_cost_usd": 0.01,
        }
        collaborative = {
            **solo,
            "passed": False,
            "elapsed_seconds": 9.5,
            "model_requests": 4,
            "usage": {"total_tokens": 160},
            "estimated_cost_usd": None,
        }

        comparison = _compare_pair(solo, collaborative)

        self.assertEqual(comparison["elapsed_delta_seconds"], 1.5)
        self.assertEqual(comparison["total_tokens_delta"], 60)
        self.assertIsNone(comparison["estimated_cost_delta_usd"])

    def test_partial_token_usage_is_not_reported_as_complete(self) -> None:
        summary = _summarize_usage(
            [
                {"kind": "model_request", "payload": {}},
                {
                    "kind": "model_usage",
                    "payload": {"usage": {"input_tokens": 12}, "call_cost_usd": 0.001},
                },
            ]
        )

        self.assertFalse(summary["usage_complete"])
        self.assertIsNone(summary["estimated_cost_usd"])

    def test_evaluator_runs_paired_cli_tasks_and_records_results(self) -> None:
        state: dict[str, object] = {"counts": {}}
        state_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                model = request["model"]
                available_tools = {
                    item["function"]["name"] for item in request.get("tools", [])
                }
                counter = (
                    "main_collaborative"
                    if model == "main" and "delegate_agent_task" in available_tools
                    else "main_solo"
                    if model == "main"
                    else model
                )
                with state_lock:
                    counts = state["counts"]
                    assert isinstance(counts, dict)
                    counts[counter] = counts.get(counter, 0) + 1
                    request_number = counts[counter]
                delegated_task_id = state.get("task_id")
                if counter == "main_collaborative" and request_number >= 2:
                    for message in request.get("messages", []):
                        if message.get("role") != "tool":
                            continue
                        try:
                            result = json.loads(message.get("content", ""))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(result, dict) and isinstance(result.get("task_id"), str):
                            delegated_task_id = result["task_id"]
                            state["task_id"] = delegated_task_id
                            break
                if model == "helper" or counter == "main_solo":
                    tool_name = "write_file"
                    arguments = {"path": "artifact.txt", "content": "ok\n"}
                    use_tool = request_number == 1
                elif request_number == 1:
                    tool_name = "delegate_agent_task"
                    arguments = {
                        "profile": "helper",
                        "task": "Write artifact.txt with the text ok followed by a newline",
                        "acceptance_criteria": "artifact.txt contains ok and a trailing newline",
                        "depends_on": [],
                    }
                    use_tool = True
                elif request_number == 2:
                    tool_name = "wait_for_agent_tasks"
                    arguments = {
                        "task_ids": [delegated_task_id],
                        "timeout_seconds": 10,
                    }
                    use_tool = True
                elif request_number == 3:
                    tool_name = "integrate_agent_task"
                    arguments = {
                        "task_id": delegated_task_id,
                        "checks": [
                            "python -c \"from pathlib import Path; assert Path('artifact.txt').exists()\""
                        ],
                    }
                    use_tool = True
                else:
                    use_tool = False
                    tool_name = ""
                    arguments = {}
                if use_tool:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"{model}-{request_number}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                    finish_reason = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "Task completed."}
                    finish_reason = "stop"
                body = json.dumps(
                    {
                        "choices": [{"message": message, "finish_reason": finish_reason}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 6,
                            "total_tokens": 18,
                        },
                    }
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
            source_root = Path(__file__).resolve().parents[1]
            git_executable = GitWorkspaces(source_root).executable
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                solo_workspace = root / "solo"
                collaborative_workspace = root / "collaborative"
                for workspace in (solo_workspace, collaborative_workspace):
                    workspace.mkdir()
                    (workspace / ".gitignore").write_text(".agentdu/\n", encoding="utf-8")
                    (workspace / "baseline.txt").write_text("baseline\n", encoding="utf-8")
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "AGENTDU_GIT_EXECUTABLE": git_executable,
                            "GIT_CONFIG_COUNT": "0",
                        }
                    )
                    subprocess.run(
                        [git_executable, "-C", str(workspace), "init"],
                        check=True,
                        capture_output=True,
                        env=environment,
                    )
                    subprocess.run(
                        [git_executable, "-C", str(workspace), "add", "."],
                        check=True,
                        capture_output=True,
                        env=environment,
                    )
                    subprocess.run(
                        [
                            git_executable,
                            "-C",
                            str(workspace),
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

                base_url = f"http://127.0.0.1:{server.server_port}/v1"
                config_path = root / "config.toml"
                config_path.write_text(
                    "\n".join(
                        (
                            "[profiles.main]",
                            'model = "main"',
                            f'base_url = "{base_url}"',
                            "streaming = false",
                            "input_cost_per_million = 1.0",
                            "output_cost_per_million = 2.0",
                            "",
                            "[profiles.helper]",
                            'model = "helper"',
                            f'base_url = "{base_url}"',
                            "streaming = false",
                            "input_cost_per_million = 1.0",
                            "output_cost_per_million = 2.0",
                            "",
                        )
                    ),
                    encoding="utf-8",
                )
                cases_path = root / "cases.json"
                cases_path.write_text(
                    json.dumps(
                        [
                            {
                                "id": "paired-local-fixture",
                                "profile": "main",
                                "prompt": "--help",
                                "solo_workspace": str(solo_workspace),
                                "collaborative_workspace": str(collaborative_workspace),
                                "checks": [
                                    [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; assert Path('artifact.txt').read_text(encoding='utf-8') == 'ok\\n'",
                                    ]
                                ],
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                output_path = root / "results.jsonl"
                environment = {
                    "PYTHONPATH": str(source_root / "src"),
                    "AGENTDU_GIT_EXECUTABLE": git_executable,
                }
                arguments = [
                    "agentdu-evaluate",
                    "--cases",
                    str(cases_path),
                    "--config",
                    str(config_path),
                    "--output",
                    str(output_path),
                ]
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(sys, "argv", arguments), patch.dict(os.environ, environment):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = main()

                self.assertEqual(
                    exit_code,
                    0,
                    stdout.getvalue() + stderr.getvalue() + output_path.read_text(),
                )
                records = [json.loads(line) for line in output_path.read_text().splitlines()]
                runs = [record for record in records if "mode" in record]
                self.assertEqual({record["mode"] for record in runs}, {"solo", "collaborative"})
                self.assertTrue(all(record["passed"] for record in runs))
                self.assertTrue(all(record["usage_complete"] for record in runs))
                self.assertTrue(all(record["estimated_cost_usd"] > 0 for record in runs))
                self.assertEqual(records[-1]["record_type"], "paired_comparison")
                self.assertIn("helper", state["counts"])
                self.assertEqual(
                    (collaborative_workspace / "artifact.txt").read_text(),
                    "ok\n",
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

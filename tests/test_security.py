from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from io import StringIO
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdu.cli import _parser
from agentdu.permissions import AllowAllPolicy
from agentdu.agent import build_default_runtime
from agentdu.orchestration import AutonomousCoordinator
from agentdu.sessions import SessionStore
from agentdu.settings import ModelProfile, ModelSettings
from agentdu.tools import ToolRegistry, create_builtin_tools


class SecurityTests(unittest.TestCase):
    def test_cli_rejects_raw_api_key_arguments(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args(["--api-key", "provider-secret-value", "task"])

    def test_shell_tool_does_not_inherit_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            command = subprocess.list2cmdline(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('AGENTDU_TEST_API_KEY', 'missing')); "
                    "print(os.getenv('CUSTOM_CREDENTIAL_VALUE', 'missing'))",
                ]
            )
            registry = ToolRegistry(workspace, AllowAllPolicy())
            for tool in create_builtin_tools(("custom-secret-value-1234",)):
                registry.register(tool)

            with patch.dict(
                os.environ,
                {
                    "AGENTDU_TEST_API_KEY": "provider-secret-value-5678",
                    "CUSTOM_CREDENTIAL_VALUE": "custom-secret-value-1234",
                    "AGENTDU_TEST_VISIBLE_SETTING": "visible",
                },
            ):
                result = asyncio.run(
                    registry.execute("run_command", {"command": command})
                )

            self.assertFalse(result.is_error, result.text)
            self.assertIn("missing", result.text)
            self.assertNotIn("provider-secret-value-5678", result.text)
            self.assertNotIn("custom-secret-value-1234", result.text)

    def test_installed_coordinator_adds_custom_named_profile_secrets(self) -> None:
        class Model:
            async def complete(self, messages, tools):
                raise AssertionError("the model should not be called")

        secret = "collaborator-secret-value-1234"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = ModelSettings(
                profiles={
                    "main": ModelProfile("main", "main-model", "http://main.invalid/v1"),
                    "collaborator": ModelProfile(
                        "collaborator",
                        "collaborator-model",
                        "http://collaborator.invalid/v1",
                        api_key_env="DEEPSEEK_KEY",
                    ),
                },
                default_profile="main",
            )
            runtime = build_default_runtime(workspace, Model(), AllowAllPolicy())
            coordinator = None
            command = subprocess.list2cmdline(
                [sys.executable, "-c", "import os; print(os.getenv('DEEPSEEK_KEY', 'missing'))"]
            )
            try:
                with patch.dict(os.environ, {"DEEPSEEK_KEY": secret}):
                    coordinator = AutonomousCoordinator(
                        workspace,
                        settings,
                        {
                            "model": "main-model",
                            "base_url": "http://main.invalid/v1",
                            "api_key": None,
                            "timeout": 1,
                            "temperature": 0,
                            "streaming": False,
                        },
                        AllowAllPolicy(),
                    )
                    coordinator.install(runtime)
                    result = asyncio.run(
                        runtime.tools.execute("run_command", {"command": command})
                    )
                self.assertFalse(result.is_error, result.text)
                self.assertIn("missing", result.text)
                self.assertNotIn(secret, result.text)
            finally:
                if coordinator is not None:
                    coordinator.close()
                runtime.close()

    def test_session_transcript_redacts_known_provider_secrets(self) -> None:
        secret = "provider-secret-value-5678"
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.add_redactions([secret])
            session_id = store.create()
            store.append_message(
                session_id,
                {"role": "assistant", "content": f"The command printed {secret}."},
            )
            store.append(
                session_id,
                "tool_finished",
                {"output": f"Authorization token {secret}"},
            )

            serialized = (store.sessions_dir / f"{session_id}.jsonl").read_text(
                encoding="utf-8"
            )
            messages = store.read_messages(session_id)
            events = store.read_events(session_id)

            self.assertNotIn(secret, serialized)
            self.assertIn("[REDACTED]", messages[-1]["content"])
            self.assertIn("[REDACTED]", events[-1]["payload"]["output"])


if __name__ == "__main__":
    unittest.main()

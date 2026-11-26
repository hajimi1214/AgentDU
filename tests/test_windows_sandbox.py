from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from agentdu.windows_sandbox import run_appcontainer_command


@unittest.skipUnless(os.name == "nt", "Windows AppContainer integration test")
class WindowsSandboxTests(unittest.TestCase):
    def test_command_runs_in_appcontainer_with_project_access_and_private_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentdu-appcontainer-") as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / ".agentdu" / "sessions").mkdir(parents=True)
            private_session = workspace / ".agentdu" / "sessions" / "private.jsonl"
            private_session.write_text("session-private-data", encoding="utf-8")
            protected_file = root / "outside.txt"
            protected_file.write_text("outside-private-data", encoding="utf-8")

            command = "echo appcontainer-output > result.txt & type result.txt & echo outside-write > ..\\outside.txt"
            result = run_appcontainer_command({"command": command}, workspace, ())

            self.assertFalse(result.is_error, result.text)
            self.assertIn("appcontainer-output", result.text)
            self.assertEqual((workspace / "result.txt").read_text(encoding="utf-8").strip(), "appcontainer-output")
            self.assertEqual(private_session.read_text(encoding="utf-8"), "session-private-data")
            self.assertEqual(protected_file.read_text(encoding="utf-8"), "outside-private-data")
            command_runs = workspace / ".agentdu" / "command-runs"
            self.assertFalse(command_runs.exists() and any(command_runs.iterdir()))

    def test_nested_process_cannot_reach_loopback_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentdu-appcontainer-network-") as temporary:
            workspace = Path(temporary) / "project"
            workspace.mkdir()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            probe = workspace / "network_probe.ps1"
            probe.write_text(
                "$client = New-Object System.Net.Sockets.TcpClient\n"
                f"try {{ $client.Connect('127.0.0.1', {port}); Write-Output 'NETWORK_CONNECTED'; exit 9 }} "
                "catch { Write-Output 'NETWORK_BLOCKED'; exit 0 }\n",
                encoding="utf-8",
            )
            try:
                result = run_appcontainer_command(
                    {"command": "powershell.exe -NoProfile -NonInteractive -File network_probe.ps1"},
                    workspace,
                    (),
                )
                self.assertFalse(result.is_error, result.text)
                self.assertIn("NETWORK_BLOCKED", result.text)
            finally:
                listener.close()


if __name__ == "__main__":
    unittest.main()

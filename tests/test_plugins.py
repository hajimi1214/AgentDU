from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from agentdu.permissions import CallbackPolicy
from agentdu.plugins import PLUGIN_API_VERSION, PluginError, load_plugins


class _Extension:
    api_version = PLUGIN_API_VERSION
    name = "fixture"

    def register_tools(self, registry) -> None:
        return

    async def on_event(self, event) -> None:
        return


class _EntryPoint:
    name = "fixture"
    dist = None

    def __init__(self) -> None:
        self.loaded = False

    def load(self):
        self.loaded = True
        return _Extension


class PluginTests(unittest.TestCase):
    def test_plugin_is_loaded_only_after_explicit_permission(self) -> None:
        entry = _EntryPoint()
        with patch("agentdu.plugins._entry_points", return_value=[entry]):
            with self.assertRaisesRegex(PluginError, "not authorized"):
                load_plugins(("fixture",), CallbackPolicy(lambda request: False), Path.cwd())
            self.assertFalse(entry.loaded)

            requests = []
            loaded = load_plugins(
                ("fixture",),
                CallbackPolicy(lambda request: requests.append(request) or True),
                Path.cwd(),
            )

        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0], _Extension)
        self.assertEqual(requests[0].tool_name, "load_agentdu_plugin")
        self.assertEqual(requests[0].arguments["name"], "fixture")

    def test_plugin_factory_entry_point_is_instantiated(self) -> None:
        entry = _EntryPoint()
        entry.load = lambda: (lambda: _Extension())
        with patch("agentdu.plugins._entry_points", return_value=[entry]):
            loaded = load_plugins(("fixture",), CallbackPolicy(lambda request: True), Path.cwd())

        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0], _Extension)

    def test_plugin_api_version_is_checked(self) -> None:
        class IncompatibleExtension(_Extension):
            api_version = 999

        entry = _EntryPoint()
        entry.load = lambda: IncompatibleExtension
        with patch("agentdu.plugins._entry_points", return_value=[entry]):
            with self.assertRaisesRegex(PluginError, "API version 1"):
                load_plugins(("fixture",), CallbackPolicy(lambda request: True), Path.cwd())


if __name__ == "__main__":
    unittest.main()

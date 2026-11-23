from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any

from .permissions import PermissionPolicy, PermissionRequest


PLUGIN_GROUP = "agentdu.extensions"
PLUGIN_API_VERSION = 1


class PluginError(RuntimeError):
    pass


def load_plugins(
    names: tuple[str, ...],
    permission_policy: PermissionPolicy,
    workspace: Path,
) -> list[Any]:
    if len(set(names)) != len(names):
        raise PluginError("Plugin names must not be repeated")
    available = _entry_points()
    loaded: list[Any] = []
    try:
        for name in names:
            matches = [entry for entry in available if entry.name == name]
            if len(matches) != 1:
                raise PluginError(
                    f"Expected one installed AgentDU plugin named '{name}', found {len(matches)}"
                )
            entry = matches[0]
            request = PermissionRequest(
                permission="execute",
                tool_name="load_agentdu_plugin",
                arguments={"name": entry.name, "distribution": _distribution_name(entry)},
                workspace=workspace,
            )
            if not permission_policy.authorize(request):
                raise PluginError(f"Loading AgentDU plugin '{name}' was not authorized")
            extension = _instantiate(entry)
            if getattr(extension, "api_version", None) != PLUGIN_API_VERSION:
                raise PluginError(
                    f"Plugin '{name}' must declare AgentDU extension API version {PLUGIN_API_VERSION}"
                )
            if not isinstance(getattr(extension, "name", None), str) or not extension.name:
                raise PluginError(f"Plugin '{name}' must provide a non-empty extension name")
            if not callable(getattr(extension, "register_tools", None)) or not callable(
                getattr(extension, "on_event", None)
            ):
                raise PluginError(f"Plugin '{name}' does not implement the AgentDU extension interface")
            loaded.append(extension)
        return loaded
    except BaseException:
        for extension in reversed(loaded):
            close = getattr(extension, "close", None)
            if callable(close):
                close()
        raise


def _entry_points() -> list[EntryPoint]:
    discovered = entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=PLUGIN_GROUP))
    return list(discovered.get(PLUGIN_GROUP, []))


def _instantiate(entry: EntryPoint) -> Any:
    try:
        factory = entry.load()
    except Exception as error:
        raise PluginError(f"Could not load AgentDU plugin '{entry.name}': {error}") from error
    try:
        if isinstance(factory, type) or (
            callable(factory) and not callable(getattr(factory, "register_tools", None))
        ):
            extension = factory()
        else:
            extension = factory
    except Exception as error:
        raise PluginError(f"Could not initialize AgentDU plugin '{entry.name}': {error}") from error
    return extension


def _distribution_name(entry: EntryPoint) -> str | None:
    distribution = getattr(entry, "dist", None)
    return getattr(distribution, "name", None)

from __future__ import annotations

from typing import Protocol

from .events import AgentEvent
from .tools import ToolRegistry


class Extension(Protocol):
    name: str

    def register_tools(self, registry: ToolRegistry) -> None: ...

    async def on_event(self, event: AgentEvent) -> None: ...

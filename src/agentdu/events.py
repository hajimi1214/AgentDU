from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class AgentEvent:
    session_id: str
    kind: str
    payload: dict[str, Any]
    occurred_at: str

    @classmethod
    def create(cls, session_id: str, kind: str, payload: dict[str, Any]) -> "AgentEvent":
        return cls(
            session_id=session_id,
            kind=kind,
            payload=payload,
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )


EventHandler = Callable[[AgentEvent], None | Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: AgentEvent) -> None:
        for handler in tuple(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logging.getLogger("agentdu.extensions").exception(
                    "Event extension failed for %s", event.kind
                )

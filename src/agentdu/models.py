from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, Sequence


Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantMessage:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None

    def to_message(self) -> Message:
        call_ids = [call.id for call in self.tool_calls]
        if any(not call_id for call_id in call_ids) or len(set(call_ids)) != len(call_ids):
            raise ValueError("tool call ids must be non-empty and unique")
        if any(not call.name for call in self.tool_calls):
            raise ValueError("tool call names must be non-empty")
        message: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                    },
                }
                for call in self.tool_calls
            ]
        return message


@dataclass(frozen=True)
class ModelStreamEvent:
    content_delta: str | None = None
    message: AssistantMessage | None = None


class ChatModel(Protocol):
    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage: ...


class StreamingChatModel(Protocol):
    streaming_enabled: bool

    def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[ModelStreamEvent]: ...

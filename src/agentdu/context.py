from __future__ import annotations

import json
from typing import Sequence

from .models import Message, ToolSchema


class ContextOverflow(RuntimeError):
    pass


class ContextManager:
    """Selects complete recent user turns using a conservative character estimate."""

    def __init__(self, max_tokens: int = 24_000) -> None:
        if max_tokens < 256:
            raise ValueError("max_tokens must be at least 256")
        self.max_tokens = max_tokens

    def prepare(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema] = (),
    ) -> list[Message]:
        tool_tokens = self._estimate(tools) if tools else 0
        request_budget = self.max_tokens - tool_tokens
        if request_budget < 1:
            raise ContextOverflow("Tool definitions exceed the configured context budget")
        system = [message for message in messages if message.get("role") in {"system", "developer"}]
        conversation = [message for message in messages if message.get("role") not in {"system", "developer"}]
        turns: list[list[Message]] = []
        current: list[Message] = []
        for message in conversation:
            if message.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(message)
        if current:
            turns.append(current)

        selected: list[list[Message]] = []
        for turn in reversed(turns):
            candidate_turns = [turn, *selected]
            omitted = len(turns) - len(candidate_turns)
            candidate = list(system)
            if omitted:
                candidate.append(self._omission_note(omitted))
            candidate.extend(item for group in candidate_turns for item in group)
            if self._estimate(candidate) > request_budget:
                if not selected:
                    compacted = self._compact_latest_turn(system, turn, request_budget)
                    if compacted is None or self._estimate([*system, *compacted]) > request_budget:
                        raise ContextOverflow(
                            "System instructions and the latest user turn exceed the context budget"
                        )
                    selected = [compacted]
                    break
                break
            selected = candidate_turns

        result = list(system)
        if len(selected) < len(turns):
            result.append(self._omission_note(len(turns) - len(selected)))
        result.extend(item for group in selected for item in group)
        return result

    def _compact_latest_turn(
        self,
        system: list[Message],
        turn: list[Message],
        request_budget: int,
    ) -> list[Message] | None:
        if not turn or turn[0].get("role") != "user" or not isinstance(turn[0].get("content"), str):
            return None
        blocks: list[list[Message]] = []
        for message in turn[1:]:
            if message.get("role") == "assistant":
                blocks.append([message])
            elif blocks:
                blocks[-1].append(message)
        omitted = 0
        while True:
            user_message = dict(turn[0])
            if omitted:
                user_message["content"] += (
                    f"\n\n[AgentDU omitted {omitted} earlier assistant/tool exchange(s) "
                    "from this request to fit context. Their full results remain in the session. "
                    "Re-read files or rerun commands to verify relevant details.]"
                )
            candidate = [*system, user_message, *(item for block in blocks for item in block)]
            if self._estimate(candidate) <= request_budget:
                return [user_message, *(item for block in blocks for item in block)]
            removable = next(
                (
                    index
                    for index, block in enumerate(blocks)
                    if any(message.get("tool_calls") for message in block)
                ),
                None,
            )
            if removable is None:
                return None
            blocks.pop(removable)
            omitted += 1

    @staticmethod
    def _omission_note(count: int) -> Message:
        return {
            "role": "system",
            "content": (
                f"{count} earlier conversation turn(s) were omitted to fit the context budget. "
                "Re-read relevant workspace files when needed."
            ),
        }

    @staticmethod
    def _estimate(messages: Sequence[Message]) -> int:
        encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        ascii_characters = sum(character.isascii() for character in encoded)
        non_ascii_characters = len(encoded) - ascii_characters
        return max(1, (ascii_characters + 3) // 4 + 3 * non_ascii_characters)

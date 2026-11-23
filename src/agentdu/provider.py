from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any, Sequence

from .models import AssistantMessage, Message, ModelStreamEvent, ToolCall, ToolSchema


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenAICompatibleModel:
    """Chat Completions adapter for OpenAI-compatible hosted and local APIs."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 120,
        temperature: float = 0,
        streaming: bool = True,
        stream_usage: bool = True,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        self.endpoint = self._endpoint(base_url)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.streaming_enabled = streaming
        self.stream_usage = stream_usage
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AssistantMessage:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        return await asyncio.to_thread(self._request, payload)

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[ModelStreamEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        if self.stream_usage:
            payload["stream_options"] = {"include_usage": True}

        events: asyncio.Queue[ModelStreamEvent | BaseException | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        response_ref: dict[str, Any] = {}

        def produce() -> None:
            try:
                for event in self._stream_request(payload, cancelled, response_ref):
                    if not cancelled.is_set():
                        loop.call_soon_threadsafe(events.put_nowait, event)
            except BaseException as error:
                if not cancelled.is_set():
                    loop.call_soon_threadsafe(events.put_nowait, error)
            finally:
                if not cancelled.is_set():
                    loop.call_soon_threadsafe(events.put_nowait, None)

        worker = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                event = await events.get()
                if event is None:
                    await worker
                    break
                if isinstance(event, BaseException):
                    raise event
                yield event
        finally:
            cancelled.set()
            response = response_ref.get("response")
            if response is not None:
                response.close()
            if not worker.done():
                await asyncio.shield(worker)

    def _stream_request(
        self,
        payload: dict[str, Any],
        cancelled: threading.Event,
        response_ref: dict[str, Any],
    ) -> Iterator[ModelStreamEvent]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
        )
        content: list[str] = []
        tool_fragments: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: dict[str, int] | None = None
        saw_done = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_ref["response"] = response
                if cancelled.is_set():
                    return
                data_lines: list[str] = []
                while line := response.readline():
                    if cancelled.is_set():
                        return
                    if line in {b"\n", b"\r\n"}:
                        event_data = "\n".join(data_lines)
                        data_lines.clear()
                        if not event_data:
                            continue
                        if event_data.strip() == "[DONE]":
                            saw_done = True
                            break
                        try:
                            chunk = json.loads(event_data)
                        except json.JSONDecodeError as error:
                            raise ProviderError(f"Model API sent invalid SSE JSON: {error}") from error
                        usage = _normalize_usage(chunk.get("usage")) or usage
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            text_delta = delta.get("content")
                            if isinstance(text_delta, str) and text_delta:
                                content.append(text_delta)
                                yield ModelStreamEvent(content_delta=text_delta)
                            for fragment in delta.get("tool_calls", []):
                                index = fragment.get("index")
                                if not isinstance(index, int):
                                    raise ProviderError("Streamed tool call is missing its index")
                                target = tool_fragments.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if fragment.get("id"):
                                    target["id"] += fragment["id"]
                                function = fragment.get("function", {})
                                target["name"] += function.get("name", "")
                                target["arguments"] += function.get("arguments", "")
                            if choice.get("finish_reason") is not None:
                                finish_reason = choice["finish_reason"]
                    elif line.startswith(b"data:"):
                        data_lines.append(line[5:].decode("utf-8").strip())
                if data_lines:
                    final_data = "\n".join(data_lines)
                    if final_data.strip() == "[DONE]":
                        saw_done = True
                    elif final_data.strip():
                        raise ProviderError("SSE stream ended before its event delimiter")
        except urllib.error.HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise ProviderError(
                f"Model API returned HTTP {error.code}: {detail}",
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if cancelled.is_set():
                return
            raise ProviderError(f"Model stream failed: {error}", retryable=True) from error

        if cancelled.is_set():
            return
        if not saw_done:
            raise ProviderError("Model SSE stream ended without a [DONE] frame", retryable=True)
        calls: list[ToolCall] = []
        for index in sorted(tool_fragments):
            fragment = tool_fragments[index]
            try:
                arguments = json.loads(fragment["arguments"] or "{}")
            except json.JSONDecodeError as error:
                raise ProviderError(f"Model streamed invalid tool arguments: {error}") from error
            if not isinstance(arguments, dict):
                raise ProviderError("Model streamed tool arguments that were not an object")
            calls.append(ToolCall(fragment["id"], fragment["name"], arguments))
        message = AssistantMessage(
            content="".join(content) or None,
            tool_calls=tuple(calls),
            finish_reason=finish_reason,
            usage=usage,
        )
        try:
            message.to_message()
        except ValueError as error:
            raise ProviderError(f"Model streamed invalid tool calls: {error}") from error
        yield ModelStreamEvent(message=message)

    def _request(self, payload: dict[str, Any]) -> AssistantMessage:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise ProviderError(
                f"Model API returned HTTP {error.code}: {detail}",
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"Model request failed: {error}", retryable=True) from error

        try:
            choice = result["choices"][0]
            message = choice["message"]
            calls = tuple(
                ToolCall(
                    id=item["id"],
                    name=item["function"]["name"],
                    arguments=json.loads(item["function"].get("arguments") or "{}"),
                )
                for item in message.get("tool_calls", [])
            )
            if len({call.id for call in calls}) != len(calls):
                raise ValueError("tool call ids must be unique within a response")
            if any(not isinstance(call.arguments, dict) for call in calls):
                raise TypeError("tool arguments must be JSON objects")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise TypeError("assistant content must be text or null")
            return AssistantMessage(
                content=content,
                tool_calls=calls,
                finish_reason=choice.get("finish_reason"),
                usage=_normalize_usage(result.get("usage")),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError(f"Model API returned an invalid chat completion: {error}") from error

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _endpoint(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}/chat/completions"
        return f"{normalized}/v1/chat/completions"


def _normalize_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, int] = {}
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        count = value.get(source)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            normalized[target] = count
    if "total_tokens" not in normalized and {
        "input_tokens",
        "output_tokens",
    } <= normalized.keys():
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized or None

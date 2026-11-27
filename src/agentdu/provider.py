from __future__ import annotations

import asyncio
import json
import select
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any, Sequence

from .models import AssistantMessage, Message, ModelStreamEvent, ToolCall, ToolSchema


def _prepare_stream_socket(response: Any) -> Any:
    response_file = getattr(response, "fp", None)
    raw_socket = getattr(getattr(response_file, "raw", None), "_sock", None)
    if raw_socket is None:
        raise ProviderError("Model stream transport does not expose a cancellable socket")
    # urllib's blocking line reader cannot observe cancellation during an idle SSE stream.
    raw_socket.setblocking(False)
    return raw_socket


def _iter_stream_lines(
    response: Any,
    raw_socket: Any,
    cancelled: threading.Event,
    timeout: float,
) -> Iterator[bytes]:
    pending = bytearray()
    for chunk in _iter_http_body(response, raw_socket, cancelled, timeout):
        if cancelled.is_set():
            return
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            yield bytes(pending[: newline + 1])
            del pending[: newline + 1]
    if pending and not cancelled.is_set():
        yield bytes(pending)


def _iter_http_body(
    response: Any,
    raw_socket: Any,
    cancelled: threading.Event,
    timeout: float,
) -> Iterator[bytes]:
    response_file = response.fp
    raw_chunks = _iter_raw_http_chunks(response_file, raw_socket, cancelled, timeout)
    if response.chunked:
        yield from _iter_chunked_body(raw_chunks, cancelled)
        return

    remaining = response.length
    if remaining is None:
        yield from raw_chunks
        return

    for chunk in raw_chunks:
        if cancelled.is_set():
            return
        body_chunk = chunk[:remaining]
        if body_chunk:
            yield body_chunk
            remaining -= len(body_chunk)
        if remaining == 0:
            return
    if remaining and not cancelled.is_set():
        raise ProviderError("Model HTTP response ended before its content length")


def _iter_raw_http_chunks(
    response_file: Any,
    raw_socket: Any,
    cancelled: threading.Event,
    timeout: float,
) -> Iterator[bytes]:
    deadline = time.monotonic() + timeout
    while not cancelled.is_set():
        try:
            buffered = response_file.peek(1)
        except ssl.SSLWantReadError:
            _wait_for_stream_socket(raw_socket, deadline)
            continue
        except ssl.SSLWantWriteError:
            _wait_for_stream_socket(raw_socket, deadline, writable=True)
            continue
        except BlockingIOError:
            _wait_for_stream_socket(raw_socket, deadline)
            continue

        if buffered:
            chunk = _read_buffered_stream_chunk(response_file, raw_socket, deadline)
        else:
            readable, _, _ = select.select(
                [raw_socket], [], [], _poll_timeout(deadline)
            )
            if not readable:
                continue
            chunk = _read_buffered_stream_chunk(response_file, raw_socket, deadline)

        if chunk is None:
            continue
        if not chunk:
            return
        deadline = time.monotonic() + timeout
        yield chunk


def _read_buffered_stream_chunk(
    response_file: Any,
    raw_socket: Any,
    deadline: float,
) -> bytes | None:
    try:
        return response_file.read1(8192)
    except ssl.SSLWantReadError:
        _wait_for_stream_socket(raw_socket, deadline)
    except ssl.SSLWantWriteError:
        _wait_for_stream_socket(raw_socket, deadline, writable=True)
    except BlockingIOError:
        _wait_for_stream_socket(raw_socket, deadline)
    return None


def _iter_chunked_body(
    raw_chunks: Iterator[bytes],
    cancelled: threading.Event,
) -> Iterator[bytes]:
    buffered = bytearray()

    def read_line() -> bytes | None:
        while True:
            if cancelled.is_set():
                return None
            newline = buffered.find(b"\r\n")
            if newline >= 0:
                line = bytes(buffered[:newline])
                del buffered[: newline + 2]
                return line
            try:
                buffered.extend(next(raw_chunks))
            except StopIteration:
                if cancelled.is_set():
                    return None
                raise ProviderError("Model HTTP chunked response ended unexpectedly")

    def read_exact(length: int) -> bytes | None:
        while len(buffered) < length:
            if cancelled.is_set():
                return None
            try:
                buffered.extend(next(raw_chunks))
            except StopIteration:
                if cancelled.is_set():
                    return None
                raise ProviderError("Model HTTP chunked response ended unexpectedly")
        result = bytes(buffered[:length])
        del buffered[:length]
        return result

    while not cancelled.is_set():
        size_line = read_line()
        if size_line is None:
            return
        size_text = size_line.split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_text, 16)
        except ValueError as error:
            raise ProviderError("Model HTTP response contained an invalid chunk size") from error
        if chunk_size < 0:
            raise ProviderError("Model HTTP response contained a negative chunk size")
        if chunk_size == 0:
            while trailer := read_line():
                if cancelled.is_set():
                    return
            return

        chunk = read_exact(chunk_size)
        if chunk is None:
            return
        delimiter = read_exact(2)
        if delimiter is None:
            return
        if delimiter != b"\r\n":
            raise ProviderError("Model HTTP response contained an invalid chunk delimiter")
        yield chunk


def _wait_for_stream_socket(
    raw_socket: Any,
    deadline: float,
    *,
    writable: bool = False,
) -> None:
    timeout = _poll_timeout(deadline)
    if writable:
        select.select([], [raw_socket], [], timeout)
    else:
        select.select([raw_socket], [], [], timeout)


def _poll_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Model stream read timed out")
    return min(0.1, remaining)

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
        wire_api: str = "chat_completions",
    ) -> None:
        if wire_api not in {"chat_completions", "responses"}:
            raise ValueError("wire_api must be 'chat_completions' or 'responses'")
        self.wire_api = wire_api
        self.endpoint = self._endpoint(base_url, wire_api)
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
        payload = self._request_payload(messages, tools)
        return await asyncio.to_thread(self._request, payload)

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> AsyncIterator[ModelStreamEvent]:
        payload = self._request_payload(messages, tools, streaming=True)

        events: asyncio.Queue[ModelStreamEvent | BaseException | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()

        def produce() -> None:
            try:
                for event in self._stream_request(payload, cancelled):
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
            if not worker.done():
                await asyncio.shield(worker)

    def _request_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        streaming: bool = False,
    ) -> dict[str, Any]:
        if self.wire_api == "responses":
            payload: dict[str, Any] = {
                "model": self.model,
                "input": _responses_input(messages),
            }
            if streaming:
                payload["stream"] = True
            if tools:
                payload["tools"] = [_responses_tool(tool) for tool in tools]
                payload["tool_choice"] = "auto"
            return payload

        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
        }
        if streaming:
            payload["stream"] = True
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        if streaming and self.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _stream_request(
        self,
        payload: dict[str, Any],
        cancelled: threading.Event,
    ) -> Iterator[ModelStreamEvent]:
        if self.wire_api == "responses":
            yield from self._stream_responses_request(payload, cancelled)
            return

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
                if cancelled.is_set():
                    return
                raw_socket = _prepare_stream_socket(response)
                data_lines: list[str] = []
                for line in _iter_stream_lines(response, raw_socket, cancelled, self.timeout):
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

    def _stream_responses_request(
        self,
        payload: dict[str, Any],
        cancelled: threading.Event,
    ) -> Iterator[ModelStreamEvent]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
        )
        final_message: AssistantMessage | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if cancelled.is_set():
                    return
                raw_socket = _prepare_stream_socket(response)
                data_lines: list[str] = []
                for line in _iter_stream_lines(response, raw_socket, cancelled, self.timeout):
                    if cancelled.is_set():
                        return
                    if line in {b"\n", b"\r\n"}:
                        delta, message = self._consume_responses_event(data_lines)
                        if delta:
                            yield ModelStreamEvent(content_delta=delta)
                        if message is not None:
                            final_message = message
                        data_lines.clear()
                        continue
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].decode("utf-8").strip())
                if data_lines:
                    delta, message = self._consume_responses_event(data_lines)
                    if delta:
                        yield ModelStreamEvent(content_delta=delta)
                    if message is not None:
                        final_message = message
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
        if final_message is None:
            raise ProviderError("Responses stream ended without a completed response", retryable=True)
        yield ModelStreamEvent(message=final_message)

    def _consume_responses_event(
        self,
        data_lines: list[str],
    ) -> tuple[str | None, AssistantMessage | None]:
        event_data = "\n".join(data_lines)
        if not event_data or event_data.strip() == "[DONE]":
            return None, None
        try:
            event = json.loads(event_data)
        except json.JSONDecodeError as error:
            raise ProviderError(f"Responses stream sent invalid SSE JSON: {error}") from error
        if not isinstance(event, dict):
            raise ProviderError("Responses stream sent a non-object event")
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                content_delta = delta
            else:
                content_delta = None
        else:
            content_delta = None
        if event_type in {"error", "response.failed"}:
            error = event.get("error")
            if not isinstance(error, dict):
                response = event.get("response")
                error = response.get("error") if isinstance(response, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise ProviderError(
                message if isinstance(message, str) else "Responses API reported a failed response"
            )
        if event_type in {"response.completed", "response.incomplete"}:
            response = event.get("response")
            if not isinstance(response, dict):
                raise ProviderError("Responses stream completed without a response object")
            return content_delta, _parse_responses_message(response)
        return content_delta, None

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

        if self.wire_api == "responses":
            try:
                return _parse_responses_message(result)
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ProviderError(f"Responses API returned an invalid response: {error}") from error

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

    async def test_connection(self) -> dict[str, Any]:
        def request_models() -> dict[str, Any]:
            suffix = "/responses" if self.wire_api == "responses" else "/chat/completions"
            models_endpoint = self.endpoint.removesuffix(suffix) + "/models"
            request = urllib.request.Request(models_endpoint, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return {"reachable": 200 <= response.status < 300, "statusCode": response.status}
            except urllib.error.HTTPError as error:
                return {"reachable": False, "statusCode": error.code}
            except (urllib.error.URLError, TimeoutError, OSError):
                return {"reachable": False, "statusCode": None}

        return await asyncio.to_thread(request_models)

    @staticmethod
    def _endpoint(base_url: str, wire_api: str = "chat_completions") -> str:
        normalized = base_url.rstrip("/")
        suffix = "/responses" if wire_api == "responses" else "/chat/completions"
        if normalized.endswith(suffix):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}{suffix}"
        return f"{normalized}/v1{suffix}"


def _responses_input(messages: Sequence[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("tool output is missing its tool_call_id")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content if isinstance(content, str) else json.dumps(content),
                }
            )
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"Responses API does not support message role {role!r}")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("Responses API input currently requires text messages")
            items.append({"role": role, "content": content})
        if role == "assistant":
            tool_calls = message.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                raise ValueError("assistant tool_calls must be an array")
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    raise ValueError("assistant tool call must be an object")
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    raise ValueError("assistant tool call is missing its function")
                call_id = tool_call.get("id")
                name = function.get("name")
                arguments = function.get("arguments", "{}")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise ValueError("assistant tool call needs a call id and function name")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
    return items


def _responses_tool(tool: ToolSchema) -> dict[str, Any]:
    function = tool.get("function")
    if not isinstance(function, dict):
        raise ValueError("tool schema is missing its function definition")
    name = function.get("name")
    parameters = function.get("parameters")
    if not isinstance(name, str) or not name or not isinstance(parameters, dict):
        raise ValueError("tool schema needs a function name and object parameters")
    result: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": parameters,
        "strict": False,
    }
    description = function.get("description")
    if isinstance(description, str) and description:
        result["description"] = description
    return result


def _parse_responses_message(response: dict[str, Any]) -> AssistantMessage:
    status = response.get("status")
    if status == "failed":
        error = response.get("error")
        detail = error.get("message") if isinstance(error, dict) else None
        raise ProviderError(detail if isinstance(detail, str) else "Responses API reported a failed response")
    if status == "incomplete":
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason != "max_output_tokens":
            raise ProviderError(
                f"Responses API returned an incomplete response ({reason or 'unknown reason'})"
            )
    elif status != "completed":
        raise ProviderError(f"Responses API returned an unexpected response status ({status or 'missing'})")
    output = response.get("output", [])
    if not isinstance(output, list):
        raise TypeError("response output must be an array")
    content: list[str] = []
    calls: list[ToolCall] = []
    for item in output:
        if not isinstance(item, dict):
            raise TypeError("response output item must be an object")
        if item.get("type") == "message":
            parts = item.get("content", [])
            if not isinstance(parts, list):
                raise TypeError("response message content must be an array")
            for part in parts:
                if not isinstance(part, dict):
                    raise TypeError("response content part must be an object")
                if part.get("type") in {"output_text", "refusal"}:
                    text = part.get("text") or part.get("refusal")
                    if isinstance(text, str):
                        content.append(text)
        elif item.get("type") == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            raw_arguments = item.get("arguments", "{}")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise ValueError("response function call needs a call id and function name")
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments or "{}")
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                raise TypeError("response function arguments must be an object")
            calls.append(ToolCall(call_id, name, arguments))
    if not content and isinstance(response.get("output_text"), str):
        content.append(response["output_text"])
    call_ids = [call.id for call in calls]
    if len(set(call_ids)) != len(call_ids):
        raise ValueError("response function call ids must be unique")
    usage = _normalize_responses_usage(response.get("usage"))
    finish_reason: str | None
    if status == "completed":
        finish_reason = "stop"
    elif status == "incomplete":
        finish_reason = "length"
    else:
        finish_reason = "stop"
    return AssistantMessage(
        content="".join(content) or None,
        tool_calls=tuple(calls),
        finish_reason=finish_reason,
        usage=usage,
    )


def _normalize_responses_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        count = value.get(name)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            normalized[name] = count
    if "total_tokens" not in normalized and {"input_tokens", "output_tokens"} <= normalized.keys():
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized or None


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

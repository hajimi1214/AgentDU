from __future__ import annotations

import errno
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Message


_SESSION_ID = re.compile(r"^[a-f0-9-]{16,64}$")


class SessionCorruptionError(RuntimeError):
    pass


class SessionProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def acquire(self) -> "SessionProcessLock":
        while not self.try_acquire():
            time.sleep(0.05)
        return self

    def try_acquire(self) -> bool:
        if self._file is not None:
            raise RuntimeError("Lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt

            if self._file.seek(0, os.SEEK_END) == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError as error:
                if error.winerror not in {33, 36} and error.errno not in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                }:
                    self._file.close()
                    self._file = None
                    raise
                self._file.close()
                self._file = None
                return False
        else:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                self._file.close()
                self._file = None
                return False
            except BaseException:
                self._file.close()
                self._file = None
                raise

    def release(self) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


class SessionStore:
    """Append-only JSONL event store with recoverable message projection."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.sessions_dir = self.data_dir / "sessions"
        self._lock = threading.RLock()
        self._next_sequence: dict[str, int] = {}

    def create(self, session_id: str | None = None) -> str:
        session_id = session_id or uuid.uuid4().hex
        path = self._path(session_id)
        with self._lock:
            if path.exists():
                raise FileExistsError(f"Session already exists: {session_id}")
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
            self.append(
                session_id,
                "session_created",
                {"created_at": datetime.now(timezone.utc).isoformat()},
            )
        return session_id

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    def lock_key(self, session_id: str) -> str:
        return str(self._path(session_id))

    def acquire_process_lock(self, session_id: str) -> SessionProcessLock:
        return SessionProcessLock(self.sessions_dir / ".locks" / f"{session_id}.lock").acquire()

    def try_acquire_process_lock(self, session_id: str) -> SessionProcessLock | None:
        lock = SessionProcessLock(self.sessions_dir / ".locks" / f"{session_id}.lock")
        return lock if lock.try_acquire() else None

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.sessions_dir.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            session_id = path.stem
            try:
                messages = self.read_messages(session_id)
                last_user = next(
                    (message for message in reversed(messages) if message.get("role") == "user"),
                    {},
                )
                prompt = last_user.get("content", "")
                sessions.append(
                    {
                        "session_id": session_id,
                        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                        "message_count": len(messages),
                        "last_prompt": prompt[:160] if isinstance(prompt, str) else "",
                    }
                )
            except (OSError, ValueError, SessionCorruptionError) as error:
                sessions.append({"session_id": session_id, "error": str(error)})
        return sorted(sessions, key=lambda item: item.get("modified_at", ""), reverse=True)

    def refresh_sequence(self, session_id: str) -> None:
        with self._lock:
            self._next_sequence.pop(session_id, None)

    def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = self._path(session_id)
            if not path.exists():
                raise FileNotFoundError(f"No session found: {session_id}")
            if session_id not in self._next_sequence:
                self._remove_incomplete_tail(path)
                self._next_sequence[session_id] = len(self.read_events(session_id)) + 1
            record = {
                "seq": self._next_sequence[session_id],
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "payload": payload,
            }
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            with path.open("ab") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            self._next_sequence[session_id] += 1
            return record

    def append_message(self, session_id: str, message: Message) -> None:
        self.append(session_id, "message", {"message": message})

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            path = self._path(session_id)
            if not path.exists():
                raise FileNotFoundError(f"No session found: {session_id}")
            raw_lines = path.read_bytes().splitlines(keepends=True)
            records: list[dict[str, Any]] = []
            for index, raw in enumerate(raw_lines):
                final_unterminated_line = index == len(raw_lines) - 1 and not raw.endswith(b"\n")
                try:
                    record = json.loads(raw)
                    if record["seq"] != len(records) + 1:
                        raise ValueError("event sequence is not contiguous")
                    if not isinstance(record.get("payload"), dict):
                        raise ValueError("event payload must be an object")
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as error:
                    if final_unterminated_line and isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
                        break
                    raise SessionCorruptionError(
                        f"Invalid event {index + 1} in session {session_id}: {error}"
                    ) from error
                records.append(record)
            return records

    def read_messages(self, session_id: str) -> list[Message]:
        return [
            record["payload"]["message"]
            for record in self.read_events(session_id)
            if record["kind"] == "message"
        ]

    def recover_interrupted_tool_calls(self, session_id: str) -> list[Message]:
        messages = self.read_messages(session_id)
        pending: dict[str, str] = {}
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    pending[call["id"]] = call["function"]["name"]
            elif message.get("role") == "tool":
                pending.pop(message.get("tool_call_id"), None)
        recovered: list[Message] = []
        for call_id, name in pending.items():
            message = {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": (
                    "AgentDU stopped before recording this tool result. The operation may have "
                    "completed. Inspect the workspace before deciding whether to retry it."
                ),
            }
            self.append_message(session_id, message)
            recovered.append(message)
        return recovered

    def unfinished_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        events = self.read_events(session_id)
        messages = self.read_messages(session_id)
        starts: dict[str, dict[str, Any]] = {}
        finished: set[str] = set()
        result_ids = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "tool"
        }
        for event in events:
            payload = event["payload"]
            call_id = payload.get("tool_call_id")
            if not isinstance(call_id, str):
                continue
            if event["kind"] == "tool_started":
                starts[call_id] = payload
            elif event["kind"] == "tool_finished":
                finished.add(call_id)
        return [
            {
                "tool_call_id": call_id,
                "tool_name": payload.get("tool_name", "unknown"),
                "result_recorded": call_id in result_ids,
            }
            for call_id, payload in starts.items()
            if call_id not in finished
        ]

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid session id")
        return self.sessions_dir / f"{session_id}.jsonl"

    @staticmethod
    def _remove_incomplete_tail(path: Path) -> None:
        content = path.read_bytes()
        if content and not content.endswith(b"\n"):
            last_newline = content.rfind(b"\n")
            final_record = content[last_newline + 1 :]
            try:
                json.loads(final_record.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            else:
                with path.open("ab") as file:
                    file.write(b"\n")
                return
            with path.open("r+b") as file:
                file.truncate(last_newline + 1)

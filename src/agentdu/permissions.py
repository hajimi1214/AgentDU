from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class PermissionRequest:
    permission: str
    tool_name: str
    arguments: dict[str, object]
    workspace: Path


class PermissionPolicy(Protocol):
    def authorize(self, request: PermissionRequest) -> bool: ...


class ReadOnlyPolicy:
    def authorize(self, request: PermissionRequest) -> bool:
        return request.permission == "read"


class AllowAllPolicy:
    def authorize(self, request: PermissionRequest) -> bool:
        return True


class ApproveWorkspaceActionsPolicy:
    def authorize(self, request: PermissionRequest) -> bool:
        return request.permission in {"read", "write", "execute"}


class CallbackPolicy:
    def __init__(self, callback: Callable[[PermissionRequest], bool]) -> None:
        self.callback = callback

    def authorize(self, request: PermissionRequest) -> bool:
        return self.callback(request)

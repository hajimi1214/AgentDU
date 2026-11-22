from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sessions import SessionStore


_TRANSITIONS = {
    "source_running": {"offered", "failed", "stale", "abandoned"},
    "offered": {"accepted", "rejected", "failed", "stale", "abandoned"},
    "accepted": {"receiver_running", "failed", "stale", "abandoned"},
    "receiver_running": {"verified", "validation_failed", "failed", "stale", "abandoned"},
    "verified": {"integrated", "stale", "abandoned"},
}
_TERMINAL_STATES = {"integrated", "rejected", "validation_failed", "failed", "stale", "abandoned"}


class HandoffConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class HandoffState:
    handoff_id: str
    version: int
    status: str
    owner: str
    source_profile: str
    receiver_profile: str
    data: dict[str, Any]


class HandoffLedger:
    """Append-only handoff state with versioned, ownership-checked transitions."""

    def __init__(self, data_dir: Path) -> None:
        self.sessions = SessionStore(data_dir)

    def create(
        self,
        source_profile: str,
        receiver_profile: str,
        data: dict[str, Any],
        handoff_id: str | None = None,
    ) -> HandoffState:
        if not source_profile or not receiver_profile or source_profile == receiver_profile:
            raise ValueError("A handoff needs two distinct model profiles")
        handoff_id = handoff_id or uuid.uuid4().hex
        self.sessions.create(handoff_id)
        self.sessions.append(
            handoff_id,
            "handoff_state",
            {
                "version": 1,
                "status": "source_running",
                "owner": source_profile,
                "source_profile": source_profile,
                "receiver_profile": receiver_profile,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "data": data,
            },
        )
        return self.read(handoff_id)

    def read(self, handoff_id: str) -> HandoffState:
        state_events = [
            event
            for event in self.sessions.read_events(handoff_id)
            if event["kind"] == "handoff_state"
        ]
        if not state_events:
            raise FileNotFoundError(f"No handoff found: {handoff_id}")
        payload = state_events[-1]["payload"]
        return HandoffState(
            handoff_id=handoff_id,
            version=payload["version"],
            status=payload["status"],
            owner=payload["owner"],
            source_profile=payload["source_profile"],
            receiver_profile=payload["receiver_profile"],
            data=payload["data"],
        )

    def transition(
        self,
        handoff_id: str,
        expected_version: int,
        expected_status: str,
        new_status: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> HandoffState:
        lock = self.sessions.acquire_process_lock(handoff_id)
        try:
            self.sessions.refresh_sequence(handoff_id)
            current = self.read(handoff_id)
            if current.version != expected_version or current.status != expected_status:
                raise HandoffConflict(
                    f"Handoff {handoff_id} changed while being transferred "
                    f"(expected {expected_status} v{expected_version}, "
                    f"found {current.status} v{current.version})"
                )
            if new_status not in _TRANSITIONS.get(current.status, set()):
                raise HandoffConflict(
                    f"Handoff {handoff_id} cannot move from {current.status} to {new_status}"
                )
            if actor != current.owner and actor != "coordinator" and not (
                new_status in {"accepted", "rejected"}
                and actor == current.receiver_profile
            ):
                raise HandoffConflict(
                    f"Handoff {handoff_id} is owned by {current.owner}, not {actor}"
                )
            self._validate_actor(current, new_status, actor)

            owner = current.owner
            if new_status in {"accepted", "receiver_running", "verified", "validation_failed"}:
                owner = current.receiver_profile
            elif new_status in {"integrated", "failed", "stale", "abandoned", "rejected"}:
                owner = "coordinator"
            updated = {
                "version": current.version + 1,
                "status": new_status,
                "owner": owner,
                "source_profile": current.source_profile,
                "receiver_profile": current.receiver_profile,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "data": {**current.data, **(payload or {})},
            }
            self.sessions.append(handoff_id, "handoff_state", updated)
            return self.read(handoff_id)
        finally:
            lock.release()

    def list_states(self) -> list[HandoffState]:
        if not self.sessions.sessions_dir.exists():
            return []
        states: list[HandoffState] = []
        for path in self.sessions.sessions_dir.glob("*.jsonl"):
            try:
                states.append(self.read(path.stem))
            except (FileNotFoundError, KeyError, ValueError):
                continue
        return sorted(states, key=lambda state: state.handoff_id)

    def record_evidence(
        self,
        handoff_id: str,
        expected_version: int,
        actor: str,
        evidence: dict[str, Any],
    ) -> None:
        lock = self.sessions.acquire_process_lock(handoff_id)
        try:
            self.sessions.refresh_sequence(handoff_id)
            current = self.read(handoff_id)
            if current.version != expected_version or current.owner != actor:
                raise HandoffConflict(
                    f"Handoff {handoff_id} is no longer owned by {actor} at version {expected_version}"
                )
            self.sessions.append(
                handoff_id,
                "handoff_evidence",
                {
                    "version": current.version,
                    "actor": actor,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "evidence": evidence,
                },
            )
        finally:
            lock.release()

    def active_states(self) -> list[HandoffState]:
        return [state for state in self.list_states() if state.status not in _TERMINAL_STATES]

    def abandon(self, handoff_id: str, reason: str) -> HandoffState:
        current = self.read(handoff_id)
        if current.status in _TERMINAL_STATES:
            raise HandoffConflict(f"Handoff {handoff_id} is already {current.status}")
        return self.transition(
            handoff_id,
            current.version,
            current.status,
            "abandoned",
            "coordinator",
            {"abandon_reason": reason},
        )

    @staticmethod
    def _validate_actor(current: HandoffState, new_status: str, actor: str) -> None:
        expected_actor = {
            "offered": current.source_profile,
            "accepted": current.receiver_profile,
            "rejected": current.receiver_profile,
            "receiver_running": current.receiver_profile,
            "verified": current.receiver_profile,
            "integrated": "coordinator",
            "abandoned": "coordinator",
        }.get(new_status)
        if expected_actor is not None and actor != expected_actor:
            raise HandoffConflict(
                f"Only {expected_actor} can perform the {new_status} transition"
            )

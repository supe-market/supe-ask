from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from itertools import count
from threading import Lock
from time import monotonic
from typing import Any

from ..event_bus import event_bus
from ..repository import repository

STDOUT_TAIL_MAX_LINES = 100
STDOUT_TAIL_MAX_BYTES = 32 * 1024
STREAM_STATE_FLUSH_INTERVAL_SECONDS = 1.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _append_chunk(current: str, delta: str) -> str:
    next_chunk = str(delta or "").strip()
    if not next_chunk:
        return current
    return f"{current} {next_chunk}".strip() if current else next_chunk


def _stdout_bytes(lines: list[str]) -> int:
    return len("\n".join(lines).encode("utf-8"))


def _truncate_line_tail(line: str, max_bytes: int) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= max_bytes:
        return line
    return raw[-max_bytes:].decode("utf-8", errors="ignore")


def _trim_stdout_tail(lines: list[str]) -> list[str]:
    tail = [str(line) for line in lines][-STDOUT_TAIL_MAX_LINES:]
    while tail and _stdout_bytes(tail) > STDOUT_TAIL_MAX_BYTES:
        if len(tail) == 1:
            tail[0] = _truncate_line_tail(tail[0], STDOUT_TAIL_MAX_BYTES)
            break
        tail.pop(0)
    return tail


def normalize_stream_state(value: Any) -> dict[str, Any]:
    state = {
        "thinking": None,
        "planningText": "",
        "codeBuffer": "",
        "stdoutTail": [],
        "updatedAt": None,
    }
    if not isinstance(value, dict):
        return state

    thinking = value.get("thinking")
    if isinstance(thinking, dict):
        stage = str(thinking.get("stage") or "").strip()
        message = str(thinking.get("message") or "").strip()
        if stage or message:
            state["thinking"] = {"stage": stage, "message": message}

    state["planningText"] = str(value.get("planningText") or "")
    state["codeBuffer"] = str(value.get("codeBuffer") or "")
    stdout_tail = value.get("stdoutTail")
    if isinstance(stdout_tail, list):
        state["stdoutTail"] = _trim_stdout_tail([str(line) for line in stdout_tail if line is not None])
    updated_at = value.get("updatedAt")
    state["updatedAt"] = str(updated_at) if updated_at else None
    return state


class RunStreamService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._event_ids = count(1)
        self._states: dict[str, dict[str, Any]] = {}
        self._dirty_runs: set[str] = set()
        self._last_flushed_at: dict[str, float] = {}

    def next_event_id(self) -> str:
        with self._lock:
            return str(next(self._event_ids))

    def publish_live_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "id": self.next_event_id(),
            "eventType": event_type,
            "payload": dict(payload or {}),
            "createdAt": _now_iso(),
        }
        event_bus.publish(run_id, event)
        return event

    def get_current_stream_state(self, tenant_id: str, run_id: str, persisted: Any | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._states.get(run_id)
            if cached is not None:
                return deepcopy(cached)
        return self._load_state(tenant_id, run_id, persisted)

    def update_stream_state(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        force_flush: bool = False,
    ) -> dict[str, Any]:
        now = monotonic()
        snapshot: dict[str, Any] | None = None

        with self._lock:
            state = self._states.get(run_id)
            if state is None:
                state = self._load_state(tenant_id, run_id)
                self._states[run_id] = state

            changed = self._apply_event(state, event_type, payload)
            if changed:
                state["updatedAt"] = _now_iso()
                self._dirty_runs.add(run_id)

            should_flush = (
                run_id in self._dirty_runs
                and (force_flush or now - self._last_flushed_at.get(run_id, 0.0) >= STREAM_STATE_FLUSH_INTERVAL_SECONDS)
            )
            if should_flush:
                snapshot = deepcopy(state)
                self._dirty_runs.discard(run_id)
                self._last_flushed_at[run_id] = now

        if snapshot is not None:
            repository.update_run(run_id, stream_state=snapshot)
            return snapshot

        with self._lock:
            return deepcopy(self._states.get(run_id) or normalize_stream_state(None))

    def emit(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        force_flush: bool = False,
    ) -> dict[str, Any]:
        self.update_stream_state(tenant_id, run_id, event_type, payload, force_flush=force_flush)
        return self.publish_live_event(run_id, event_type, payload)

    def _load_state(self, tenant_id: str, run_id: str, persisted: Any | None = None) -> dict[str, Any]:
        if persisted is None:
            run = repository.get_run(tenant_id, run_id)
            persisted = (run or {}).get("stream_state")
        return normalize_stream_state(persisted)

    def _apply_event(self, state: dict[str, Any], event_type: str, payload: dict[str, Any]) -> bool:
        if event_type == "run.thinking":
            stage = str(payload.get("stage") or "").strip()
            message = str(payload.get("message") or "").strip()
            next_value = {"stage": stage, "message": message} if stage or message else None
            if state.get("thinking") == next_value:
                return False
            state["thinking"] = next_value
            return True

        if event_type == "run.planning.delta":
            next_value = _append_chunk(str(state.get("planningText") or ""), str(payload.get("delta") or ""))
            if next_value == state.get("planningText"):
                return False
            state["planningText"] = next_value
            return True

        if event_type == "run.codegen.delta":
            delta = str(payload.get("delta") or "")
            if not delta:
                return False
            state["codeBuffer"] = f"{state.get('codeBuffer') or ''}{delta}"
            return True

        if event_type == "run.codegen.completed":
            python_code = payload.get("pythonCode")
            if python_code is None:
                return False
            next_value = str(python_code or "")
            if next_value == state.get("codeBuffer"):
                return False
            state["codeBuffer"] = next_value
            return True

        if event_type == "run.execution.stdout":
            line = str(payload.get("line") or "")
            if not line:
                return False
            state["stdoutTail"] = _trim_stdout_tail([*(state.get("stdoutTail") or []), line])
            return True

        if event_type in {"run.completed", "run.failed", "run.cancelled"}:
            if state.get("thinking") is None:
                return False
            state["thinking"] = None
            return True

        return False


run_stream_service = RunStreamService()


def publish_live_event(run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return run_stream_service.publish_live_event(run_id, event_type, payload)


def update_stream_state(
    tenant_id: str,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    force_flush: bool = False,
) -> dict[str, Any]:
    return run_stream_service.update_stream_state(tenant_id, run_id, event_type, payload, force_flush=force_flush)


def emit_live_run_event(
    tenant_id: str,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    force_flush: bool = False,
) -> dict[str, Any]:
    return run_stream_service.emit(tenant_id, run_id, event_type, payload, force_flush=force_flush)

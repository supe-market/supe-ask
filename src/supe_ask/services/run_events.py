from __future__ import annotations

from typing import Any

from ..event_bus import event_bus
from ..repository import repository


def emit_run_event(tenant_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    stored = repository.append_run_event(tenant_id, run_id, event_type, payload)
    event_bus.publish(
        run_id,
        {
            "id": stored["id"],
            "eventType": event_type,
            "payload": dict(stored["payload"] or {}),
            "createdAt": stored["created_at"].isoformat() if stored.get("created_at") else None,
        },
    )
    return stored

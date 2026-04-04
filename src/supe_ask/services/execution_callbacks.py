"""Handle callbacks from isolated execution runners.

The callback path is the only channel through which the runner can update Ask
state. It verifies the signed token, ignores duplicate deliveries, persists the
event/artifact, and emits the corresponding live SSE event.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..repository import repository
from ..security import verify_callback_token
from .artifacts import artifact_service
from .run_events import emit_run_event


class ExecutionCallbackService:
    """Translate runner callbacks into durable Ask state transitions."""

    def handle_callback(self, run_id: str, token: str, callback_type: str, sequence: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Verify and apply a single runner callback."""
        execution = repository.get_run_execution(run_id)
        if not execution:
            raise HTTPException(status_code=404, detail="Run execution not found")
        if not verify_callback_token(token, execution.get("callback_token_hash")):
            raise HTTPException(status_code=403, detail="Invalid callback token")
        if int(execution.get("last_callback_sequence") or 0) >= int(sequence):
            return {"success": True, "ignored": True}

        tenant_id = str(execution["tenant_id"])
        run = repository.get_run(tenant_id, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.get("status") == "cancelled" and callback_type != "heartbeat":
            # Once a run is cancelled, the control plane stops accepting new work
            # from the runner except for heartbeats that help reconciliation.
            repository.update_run_execution(
                run_id,
                status="cancelled",
                last_callback_sequence=sequence,
                touch_heartbeat=True,
            )
            return {"success": True, "ignored": True}

        repository.update_run_execution(
            run_id,
            last_callback_sequence=sequence,
            touch_heartbeat=True,
            runner_started=callback_type in {"heartbeat", "progress", "artifact", "completed", "failed"},
            status="running" if callback_type in {"heartbeat", "progress", "artifact"} else None,
        )

        if callback_type == "heartbeat":
            return {"success": True}

        if callback_type == "progress":
            emit_run_event(tenant_id, run_id, "run.execution.progress", {"message": str(payload.get("message") or "")})
            return {"success": True}

        if callback_type == "artifact":
            # The runner already decided whether the full body had to move to S3.
            # The control plane persists the preview plus storage metadata only.
            storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else None
            artifact = artifact_service.persist_artifact(
                tenant_id,
                run_id,
                str(payload.get("artifactType") or "unknown"),
                str(payload.get("title") or "Artifact"),
                preview_payload=dict(payload.get("previewPayload") or {}),
                storage=storage,
                metadata={"source": "ecs_callback"},
            )
            emit_run_event(tenant_id, run_id, "run.artifact", {"artifact": artifact})
            return {"success": True}

        if callback_type == "completed":
            repository.update_run_execution(run_id, status="completed", runner_completed=True)
            repository.update_run(run_id, status="completed", completed=True)
            emit_run_event(tenant_id, run_id, "run.completed", {"status": "completed", "summary": payload.get("summary")})
            return {"success": True}

        if callback_type == "failed":
            message = str(payload.get("message") or "Execution failed")
            stage = str(payload.get("stage") or "execution")
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=message)
            repository.update_run(run_id, status="failed", error_message=message, completed=True)
            emit_run_event(
                tenant_id,
                run_id,
                "run.failed",
                {
                    "message": message,
                    "traceback": payload.get("traceback"),
                    "stage": stage,
                },
            )
            return {"success": True}

        raise HTTPException(status_code=400, detail="Unsupported callback type")


execution_callback_service = ExecutionCallbackService()

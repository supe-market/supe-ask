"""Warm-pool execution backend for Ask-generated Python.

Code runs in pre-warmed Python subprocesses on the same host as the control
plane — no ECS tasks, no SQS queues, no S3 manifests required.

Set ``ASK_RUNNER_BACKEND=warm_pool`` (or leave it as the default) to use this
backend.  Tune pool size with ``ASK_CODEBOX_WARM_POOL_SIZE`` (default 1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Thread
from typing import Any

from ..aws_clients import s3_storage
from ..config import settings
from ..repository import repository

logger = logging.getLogger(__name__)


@dataclass
class RunnerLaunchResult:
    """Result returned when the control plane hands execution to a backend."""

    backend: str
    completion_mode: str
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    task_arn: str | None = None
    dispatch_mode: str = "warm_pool"
    queue_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionBootstrapError(RuntimeError):
    """Raised when execution cannot be handed off to the runner backend."""


class WarmPoolRunner:
    """Run Ask execution in-process via a pre-warmed Python subprocess pool.

    ``launch()`` spawns a daemon thread so the orchestrator's pipeline thread
    returns immediately.  The daemon thread blocks on ``pool.run()``, streams
    events directly into ``emit_live_run_event``, and writes the terminal run
    state when done.  No HTTP callbacks, no S3 manifest, no SQS.
    """

    name = "warm_pool"

    def __init__(self) -> None:
        from .codebox_pool import WarmProcessPool

        self._pool = WarmProcessPool(
            pool_size=settings.codebox_warm_pool_size,
            max_uses=settings.codebox_warm_pool_max_uses,
            ready_timeout_seconds=settings.codebox_warm_ready_timeout_seconds,
        )

    def warm_up(self) -> None:
        """Pre-spawn warm workers in the background so the first run is fast."""
        self._pool.warm_up(wait=False)

    def shutdown(self) -> None:
        """Terminate all warm workers on service shutdown."""
        self._pool.shutdown()

    def launch(self, run_id: str, tenant_id: str, code: str) -> RunnerLaunchResult:
        """Dispatch execution to the warm pool on a daemon thread and return immediately."""
        repository.upsert_run_execution(
            tenant_id,
            run_id,
            self.name,
            "running",
            metadata={"mode": "sync", "dispatchMode": "warm_pool"},
        )
        Thread(
            target=self._execute,
            args=(run_id, tenant_id, code),
            daemon=True,
        ).start()
        return RunnerLaunchResult(
            backend=self.name,
            completion_mode="sync",
            dispatch_mode="warm_pool",
        )

    def cancel(self, run_id: str) -> bool:
        """No-op — the pool's timeout mechanism will kill any overrunning subprocess."""
        return False

    # ------------------------------------------------------------------
    # Internal execution thread
    # ------------------------------------------------------------------

    def _execute(self, run_id: str, tenant_id: str, code: str) -> None:
        from ..artifact_utils import build_preview_payload, should_offload_artifact
        from .artifacts import artifact_service
        from .run_stream import emit_live_run_event

        def on_event(event: dict[str, Any]) -> None:
            event_type = str(event.get("type") or "")
            payload = dict(event.get("payload") or {})

            if event_type == "progress":
                emit_live_run_event(
                    tenant_id, run_id,
                    "run.execution.progress",
                    {"message": str(payload.get("message") or "")},
                )
            elif event_type == "stdout":
                emit_live_run_event(
                    tenant_id, run_id,
                    "run.execution.stdout",
                    {"line": str(payload.get("line") or "")},
                )
            elif event_type == "artifact":
                artifact_type = str(payload.get("artifact_type") or "unknown")
                title = str(payload.get("title") or artifact_type.title())
                full_payload = dict(payload.get("payload") or {})
                preview = build_preview_payload(artifact_type, full_payload)
                storage = None
                if settings.runner_artifact_bucket and should_offload_artifact(artifact_type, full_payload):
                    object_key = f"ask-runs/{tenant_id}/{run_id}/artifacts/{artifact_type}.json"
                    stored = s3_storage.put_json(settings.runner_artifact_bucket, object_key, full_payload)
                    storage = {
                        "storageBackend": "s3",
                        "objectKey": stored.key,
                        "contentType": stored.content_type,
                        "byteSize": stored.byte_size,
                    }
                artifact = artifact_service.persist_artifact(
                    tenant_id, run_id, artifact_type, title,
                    preview_payload=preview,
                    storage=storage,
                    metadata={"source": "warm_pool"},
                )
                emit_live_run_event(tenant_id, run_id, "run.artifact", {"artifact": artifact}, force_flush=True)
            elif event_type == "error":
                message = str((dict(event.get("payload") or {})).get("message") or "Execution error")
                logger.warning("Warm pool error event", extra={"run_id": run_id, "message": message})

        try:
            return_code, logs = self._pool.run(run_id, tenant_id, code, on_event)

            if logs:
                artifact = artifact_service.persist_artifact(
                    tenant_id, run_id, "log", "Execution log",
                    preview_payload=build_preview_payload("log", {"lines": logs}),
                    storage=None,
                    metadata={"source": "warm_pool"},
                )
                emit_live_run_event(tenant_id, run_id, "run.artifact", {"artifact": artifact}, force_flush=True)

            if return_code != 0:
                msg = "Runner exited with a non-zero status"
                repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=msg)
                repository.update_run(run_id, status="failed", error_message=msg, completed=True)
                emit_live_run_event(tenant_id, run_id, "run.failed", {"message": msg, "stage": "execution"}, force_flush=True)
                return

            repository.update_run_execution(run_id, status="completed", runner_completed=True)
            repository.update_run(run_id, status="completed", completed=True)
            emit_live_run_event(tenant_id, run_id, "run.completed", {"status": "completed"}, force_flush=True)

        except Exception as error:
            logger.exception("Warm pool execution failed", extra={"run_id": run_id, "tenant_id": tenant_id})
            message = str(error)
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=message)
            repository.update_run(run_id, status="failed", error_message=message, completed=True)
            emit_live_run_event(tenant_id, run_id, "run.failed", {"message": message, "stage": "execution"}, force_flush=True)


active_runner = WarmPoolRunner()

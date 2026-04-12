from __future__ import annotations

from threading import Event, Thread

from ..aws_clients import ecs_service
from ..config import settings
from ..repository import repository
from .run_events import emit_run_event


class ExecutionReconciler:
    def _resolve_stop_reason(self, execution: dict) -> str:
        """Surface the ECS task stop reason when the task dies before callbacks arrive."""
        task_arn = str(execution.get("task_arn") or "")
        if not task_arn or not settings.ecs_cluster:
            return "ECS runner stopped sending heartbeats before completion"
        try:
            response = ecs_service.describe_tasks(cluster=settings.ecs_cluster, tasks=[task_arn])
        except Exception:
            return "ECS runner stopped sending heartbeats before completion"
        tasks = response.get("tasks") or []
        if not tasks:
            return "ECS runner stopped sending heartbeats before completion"
        task = tasks[0] or {}
        stopped_reason = str(task.get("stoppedReason") or "").strip()
        if stopped_reason:
            return stopped_reason
        containers = task.get("containers") or []
        for container in containers:
            reason = str((container or {}).get("reason") or "").strip()
            if reason:
                return reason
        return "ECS runner stopped sending heartbeats before completion"

    def __init__(self) -> None:
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if settings.runner_backend != "ecs":
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop_event.wait(settings.runner_callback_heartbeat_seconds):
            self.reconcile_once()

    def reconcile_once(self) -> None:
        stale = repository.list_stale_run_executions(settings.runner_reconcile_stale_seconds)
        for execution in stale:
            run_id = str(execution["run_id"])
            tenant_id = str(execution["tenant_id"])
            run = repository.get_run(tenant_id, run_id)
            if not run or run.get("status") in {"completed", "failed", "cancelled"}:
                continue
            message = self._resolve_stop_reason(execution)
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=message)
            repository.update_run(run_id, status="failed", error_message=message, completed=True)
            emit_run_event(tenant_id, run_id, "run.failed", {"message": message, "stage": "execution"})


execution_reconciler = ExecutionReconciler()

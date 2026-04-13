from __future__ import annotations

from threading import Event, Thread

from ..aws_clients import ecs_service
from ..config import settings
from ..repository import repository
from .run_stream import emit_live_run_event


class ExecutionReconciler:
    def _dispatch_mode(self, execution: dict) -> str:
        metadata = execution.get("metadata")
        if isinstance(metadata, dict):
            value = str(metadata.get("dispatchMode") or "").strip()
            if value:
                return value
        return "task"

    def _normalize_stop_reason(self, reason: str) -> str:
        """Rewrite common ECS bootstrap failures into actionable operator messages."""
        normalized = reason.strip()
        lower = normalized.lower()
        if "does not contain descriptor matching platform 'linux/amd64'" in lower:
            return (
                "Runner image does not include a linux/amd64 manifest. "
                "Rebuild and push the ECS runner image for linux/amd64 or a multi-arch manifest before retrying."
            )
        if "unable to pull secrets or registry auth" in lower and "getauthorizationtoken" in lower:
            return (
                "Runner task could not reach Amazon ECR to pull registry auth. "
                "Check task egress, public IP assignment, or NAT/VPC endpoint configuration."
            )
        return normalized

    def _resolve_stop_reason(self, execution: dict) -> str:
        """Surface the ECS task stop reason when the task dies before callbacks arrive."""
        if self._dispatch_mode(execution) == "codebox":
            return (
                "Codebox worker stopped sending heartbeats before completion. "
                "Check the ECS service health, CloudWatch logs, and SQS visibility timeout."
            )
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
            return self._normalize_stop_reason(stopped_reason)
        containers = task.get("containers") or []
        for container in containers:
            reason = str((container or {}).get("reason") or "").strip()
            if reason:
                return self._normalize_stop_reason(reason)
        return "ECS runner stopped sending heartbeats before completion"

    def __init__(self) -> None:
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not settings.ecs_cluster and not settings.codebox_queue_url:
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
            emit_live_run_event(
                tenant_id,
                run_id,
                "run.failed",
                {"message": message, "stage": "execution"},
                force_flush=True,
            )

        if not settings.codebox_queue_url:
            return

        stale_queued = repository.list_stale_queued_run_executions(settings.codebox_queue_stale_seconds)
        for execution in stale_queued:
            run_id = str(execution["run_id"])
            tenant_id = str(execution["tenant_id"])
            run = repository.get_run(tenant_id, run_id)
            if not run or run.get("status") in {"completed", "failed", "cancelled"}:
                continue
            message = (
                "Codebox job stayed queued without pickup. "
                "Check ASK_CODEBOX_QUEUE_URL, the ECS service desired count, and worker IAM/network access."
            )
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=message)
            repository.update_run(run_id, status="failed", error_message=message, completed=True)
            emit_live_run_event(
                tenant_id,
                run_id,
                "run.failed",
                {"message": message, "stage": "execution"},
                force_flush=True,
            )


execution_reconciler = ExecutionReconciler()

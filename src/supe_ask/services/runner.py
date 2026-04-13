"""ECS execution backend for Ask-generated Python."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..aws_clients import ecs_service, s3_storage, sqs_service
from ..config import settings
from ..repository import repository
from ..security import generate_callback_token, hash_callback_token
from .runner_network import build_callback_url

logger = logging.getLogger(__name__)


@dataclass
class RunnerLaunchResult:
    """Result returned when the control plane hands execution to a backend."""

    backend: str
    completion_mode: str
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    task_arn: str | None = None
    dispatch_mode: str = "task"
    queue_message_id: str | None = None
    callback_token: str | None = None
    input_object_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionBootstrapError(RuntimeError):
    """Raised when execution cannot be handed off to the runner backend."""


class EcsRunner:
    """Launch Ask execution inside an isolated ECS task."""

    name = "ecs"

    def launch(self, run_id: str, tenant_id: str, code: str) -> RunnerLaunchResult:
        """Upload the manifest, persist execution state, and dispatch the run."""
        dispatch_mode = "codebox" if settings.codebox_queue_url else "task"
        repository.upsert_run_execution(
            tenant_id,
            run_id,
            self.name,
            "preparing",
            metadata={"mode": "async", "dispatchMode": dispatch_mode},
        )
        try:
            if not settings.control_plane_internal_url:
                raise ExecutionBootstrapError("ASK_CONTROL_PLANE_INTERNAL_URL must be configured for the ECS runner backend")
            if not settings.runner_input_bucket:
                raise ExecutionBootstrapError("ASK_RUNNER_INPUT_BUCKET must be configured for the ECS runner backend")
            if not settings.runner_artifact_bucket:
                raise ExecutionBootstrapError("ASK_RUNNER_ARTIFACT_BUCKET must be configured for the ECS runner backend")
            subnets: list[str] = []
            security_groups: list[str] = []
            if dispatch_mode == "task":
                if not settings.ecs_cluster or not settings.ecs_task_definition:
                    raise ExecutionBootstrapError(
                        "ASK_ECS_CLUSTER and ASK_ECS_TASK_DEFINITION must be configured for the ECS runner backend"
                    )
                subnets = [item.strip() for item in settings.ecs_subnets.split(",") if item.strip()]
                security_groups = [item.strip() for item in settings.ecs_security_groups.split(",") if item.strip()]
                if not subnets or not security_groups:
                    raise ExecutionBootstrapError(
                        "ASK_ECS_SUBNETS and ASK_ECS_SECURITY_GROUPS must be configured for the ECS runner backend"
                    )
            elif not settings.codebox_queue_url:
                raise ExecutionBootstrapError("ASK_CODEBOX_QUEUE_URL must be configured for the codebox runner backend")

            callback_token = generate_callback_token()
            try:
                callback_url = build_callback_url(settings.control_plane_internal_url, run_id)
            except ValueError as error:
                raise ExecutionBootstrapError(str(error)) from error
            input_object_key = f"ask-runs/{tenant_id}/{run_id}/input.json"
            manifest = {
                "runId": run_id,
                "tenantId": str(tenant_id),
                "pythonCode": code,
                "artifactThresholdBytes": settings.artifact_s3_threshold_bytes,
                "maxTableRows": settings.max_table_rows,
            }
            logger.info("Uploading Ask execution manifest", extra={"run_id": run_id, "tenant_id": tenant_id})
            # The manifest contains only run input. Writable Ask state stays in the
            # control plane database and never moves into the runner container.
            s3_storage.put_json(settings.runner_input_bucket, input_object_key, manifest)
            repository.update_run_execution(
                run_id,
                status="queueing" if dispatch_mode == "codebox" else "launching",
                input_object_key=input_object_key,
                metadata={"callbackUrl": callback_url, "dispatchMode": dispatch_mode},
            )
            repository.upsert_run_execution(
                tenant_id,
                run_id,
                self.name,
                "queueing" if dispatch_mode == "codebox" else "launching",
                callback_token_hash=hash_callback_token(callback_token),
                input_object_key=input_object_key,
                metadata={"callbackUrl": callback_url, "dispatchMode": dispatch_mode},
            )

            input_s3_uri = f"s3://{settings.runner_input_bucket}/{input_object_key}"
            if dispatch_mode == "codebox":
                logger.info("Queueing Ask codebox job", extra={"run_id": run_id, "tenant_id": tenant_id})
                response = sqs_service.send_message(
                    settings.codebox_queue_url,
                    {
                        "runId": run_id,
                        "tenantId": str(tenant_id),
                        "callbackUrl": callback_url,
                        "callbackToken": callback_token,
                        "inputS3Uri": input_s3_uri,
                    },
                )
                message_id = str(response.get("MessageId") or "")
                repository.update_run_execution(
                    run_id,
                    status="queued",
                    metadata={"queueMessageId": message_id, "dispatchMode": dispatch_mode},
                )
                return RunnerLaunchResult(
                    backend=self.name,
                    completion_mode="async",
                    dispatch_mode=dispatch_mode,
                    queue_message_id=message_id or None,
                    callback_token=callback_token,
                    input_object_key=input_object_key,
                    metadata={"dispatchMode": dispatch_mode, "queueMessageId": message_id},
                )

            overrides = {
                "containerOverrides": [
                    {
                        "name": settings.ecs_container_name,
                        "environment": [
                            {"name": "RUN_ID", "value": run_id},
                            {"name": "CALLBACK_URL", "value": callback_url},
                            {"name": "CALLBACK_TOKEN", "value": callback_token},
                            {"name": "INPUT_S3_URI", "value": input_s3_uri},
                            {"name": "ASK_RUN_TIMEOUT_SECONDS", "value": str(settings.run_timeout_seconds)},
                            {"name": "ASK_RUNNER_ARTIFACT_BUCKET", "value": settings.runner_artifact_bucket},
                            {"name": "ASK_RUNNER_CALLBACK_HEARTBEAT_SECONDS", "value": str(settings.runner_callback_heartbeat_seconds)},
                            {"name": "ASK_MAX_TABLE_ROWS", "value": str(settings.max_table_rows)},
                            {"name": "ASK_ARTIFACT_S3_THRESHOLD_BYTES", "value": str(settings.artifact_s3_threshold_bytes)},
                            {"name": "AWS_REGION", "value": settings.aws_region},
                            {"name": "S3_ENDPOINT", "value": settings.s3_endpoint},
                            {"name": "S3_ACCESS_KEY_ID", "value": settings.s3_access_key_id},
                            {"name": "S3_SECRET_ACCESS_KEY", "value": settings.s3_secret_access_key},
                            {"name": "S3_FORCE_PATH_STYLE", "value": "true" if settings.s3_force_path_style else "false"},
                        ],
                    }
                ]
            }
            logger.info("Launching Ask ECS task", extra={"run_id": run_id, "tenant_id": tenant_id})
            response = ecs_service.run_task(
                cluster=settings.ecs_cluster,
                taskDefinition=settings.ecs_task_definition,
                launchType="FARGATE",
                count=1,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": security_groups,
                        "assignPublicIp": "ENABLED" if settings.ecs_assign_public_ip else "DISABLED",
                    }
                },
                overrides=overrides,
            )
            failures = response.get("failures") or []
            if failures:
                raise ExecutionBootstrapError(str(failures[0].get("reason") or "ECS task launch failed"))
            tasks = response.get("tasks") or []
            if not tasks:
                raise ExecutionBootstrapError("ECS task launch returned no tasks")
            task_arn = str(tasks[0].get("taskArn") or "")
            repository.update_run_execution(
                run_id,
                task_arn=task_arn,
                metadata={"launchResponse": {"taskArn": task_arn}, "dispatchMode": dispatch_mode},
            )
            return RunnerLaunchResult(
                backend=self.name,
                completion_mode="async",
                task_arn=task_arn,
                dispatch_mode=dispatch_mode,
                callback_token=callback_token,
                input_object_key=input_object_key,
                metadata={"dispatchMode": dispatch_mode, "taskArn": task_arn},
            )
        except Exception as error:
            logger.exception("Ask ECS bootstrap failed", extra={"run_id": run_id, "tenant_id": tenant_id})
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=str(error))
            if isinstance(error, ExecutionBootstrapError):
                raise
            raise ExecutionBootstrapError(str(error)) from error

    def cancel(self, run_id: str) -> bool:
        """Stop a launched ECS task for the given run, if the ARN is known."""
        execution = repository.get_run_execution(run_id)
        task_arn = str((execution or {}).get("task_arn") or "")
        if not task_arn:
            return False
        ecs_service.stop_task(
            cluster=settings.ecs_cluster,
            task=task_arn,
            reason="Cancelled by user",
        )
        return True


active_runner = EcsRunner()

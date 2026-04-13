"""Launch an ECS runner preflight task using the configured Ask runner settings."""

from __future__ import annotations

import json
import os
import time

from .aws_clients import ecs_service
from .config import settings
from .services.runner_network import normalize_control_plane_internal_url


def _env_overrides() -> list[dict[str, str]]:
    overrides = [
        {"name": "ASK_CONTROL_PLANE_INTERNAL_URL", "value": normalize_control_plane_internal_url(settings.control_plane_internal_url)},
        {"name": "ASK_RUNNER_INPUT_BUCKET", "value": settings.runner_input_bucket},
        {"name": "ASK_RUNNER_ARTIFACT_BUCKET", "value": settings.runner_artifact_bucket},
        {"name": "ASK_RUNNER_CALLBACK_HEARTBEAT_SECONDS", "value": str(settings.runner_callback_heartbeat_seconds)},
        {"name": "AWS_REGION", "value": settings.aws_region},
        {"name": "S3_ENDPOINT", "value": settings.s3_endpoint},
        {"name": "S3_ACCESS_KEY_ID", "value": settings.s3_access_key_id},
        {"name": "S3_SECRET_ACCESS_KEY", "value": settings.s3_secret_access_key},
        {"name": "S3_FORCE_PATH_STYLE", "value": "true" if settings.s3_force_path_style else "false"},
    ]
    secret_probe_id = os.getenv("ASK_RUNNER_SECRET_PROBE_ID", "").strip()
    if secret_probe_id:
        overrides.append({"name": "ASK_RUNNER_SECRET_PROBE_ID", "value": secret_probe_id})
    return overrides


def _require_ecs_settings() -> tuple[list[str], list[str]]:
    if not settings.ecs_cluster or not settings.ecs_task_definition:
        raise RuntimeError("ASK_ECS_CLUSTER and ASK_ECS_TASK_DEFINITION must be configured")
    if not settings.runner_input_bucket:
        raise RuntimeError("ASK_RUNNER_INPUT_BUCKET must be configured")
    if not settings.runner_artifact_bucket:
        raise RuntimeError("ASK_RUNNER_ARTIFACT_BUCKET must be configured")
    subnets = [item.strip() for item in settings.ecs_subnets.split(",") if item.strip()]
    security_groups = [item.strip() for item in settings.ecs_security_groups.split(",") if item.strip()]
    if not subnets or not security_groups:
        raise RuntimeError("ASK_ECS_SUBNETS and ASK_ECS_SECURITY_GROUPS must be configured")
    return subnets, security_groups


def main() -> int:
    subnets, security_groups = _require_ecs_settings()
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
        overrides={
            "containerOverrides": [
                {
                    "name": settings.ecs_container_name,
                    "command": ["python", "-m", "supe_ask.runner_preflight"],
                    "environment": _env_overrides(),
                }
            ]
        },
    )
    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(str(failures[0].get("reason") or "ECS smoke task launch failed"))

    tasks = response.get("tasks") or []
    if not tasks:
        raise RuntimeError("ECS smoke task launch returned no tasks")

    task_arn = str(tasks[0].get("taskArn") or "")
    print(json.dumps({"status": "launched", "taskArn": task_arn}, ensure_ascii=True), flush=True)

    timeout_seconds = int(os.getenv("ASK_ECS_SMOKE_WAIT_SECONDS", "300"))
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        described = ecs_service.describe_tasks(cluster=settings.ecs_cluster, tasks=[task_arn])
        task = (described.get("tasks") or [{}])[0]
        current_status = str(task.get("lastStatus") or "")
        if current_status and current_status != last_status:
            print(json.dumps({"status": current_status, "taskArn": task_arn}, ensure_ascii=True), flush=True)
            last_status = current_status
        if current_status == "STOPPED":
            container = next(
                (item for item in (task.get("containers") or []) if str(item.get("name") or "") == settings.ecs_container_name),
                (task.get("containers") or [{}])[0],
            )
            raw_exit_code = container.get("exitCode")
            exit_code = int(raw_exit_code) if raw_exit_code is not None else 1
            summary = {
                "status": "stopped",
                "taskArn": task_arn,
                "exitCode": exit_code,
                "stoppedReason": str(task.get("stoppedReason") or ""),
                "containerReason": str(container.get("reason") or ""),
            }
            print(json.dumps(summary, ensure_ascii=True), flush=True)
            return 0 if exit_code == 0 else exit_code
        time.sleep(5)

    raise RuntimeError(f"ECS smoke task did not stop within {timeout_seconds} seconds")


if __name__ == "__main__":
    raise SystemExit(main())

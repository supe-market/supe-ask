"""Execution backends for Ask-generated Python.

`LocalRunner` is used for local development and tests. `EcsRunner` is the
production-oriented backend that launches an isolated task and lets the control
plane observe it through signed callbacks.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

from ..aws_clients import ecs_service, s3_storage
from ..config import settings
from ..repository import repository
from ..security import generate_callback_token, hash_callback_token

EVENT_PREFIX = "__SUPE_ASK_EVENT__"
ERROR_PREFIX = "__SUPE_ASK_ERROR__"
logger = logging.getLogger(__name__)


WRAPPER_TEMPLATE = """
import json
import traceback

from supe_lib.runtime import execute_user_code

USER_CODE = {code_json}

try:
    execute_user_code(USER_CODE)
except Exception as error:
    payload = {{
        "message": str(error),
        "traceback": traceback.format_exc(),
    }}
    print("{error_prefix}" + json.dumps(payload), flush=True)
    raise
"""


class LocalRunner:
    """Execute generated code inside a local subprocess."""

    name = "local"

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = Lock()

    def run(self, run_id: str, tenant_id: str, code: str, on_event: Callable[[dict], None]) -> tuple[int, list[str]]:
        """Run generated Python locally and stream runtime events to `on_event`."""
        stdout_logs: list[str] = []
        with tempfile.TemporaryDirectory(prefix=f"supe-ask-{run_id[:8]}-") as temp_dir:
            wrapper_path = Path(temp_dir) / "runner_wrapper.py"
            wrapper_path.write_text(
                WRAPPER_TEMPLATE.format(code_json=json.dumps(code), error_prefix=ERROR_PREFIX),
                encoding="utf-8",
            )
            env = os.environ.copy()
            project_src = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = f"{project_src}:{env.get('PYTHONPATH', '')}".strip(":")
            env["SUPE_ASK_EVENT_PREFIX"] = EVENT_PREFIX
            # The local runner talks to the analytics database directly, mirroring
            # the read-only credentials the isolated ECS runner receives at runtime.
            env["SUPE_ASK_DB_HOST"] = os.getenv("ASK_DB_HOST", os.getenv("DB_HOST", "localhost"))
            env["SUPE_ASK_DB_PORT"] = os.getenv("ASK_DB_PORT", os.getenv("DB_PORT", "5432"))
            env["SUPE_ASK_DB_NAME"] = os.getenv("ASK_DB_NAME", os.getenv("DB_NAME", "supe_analytics"))
            env["SUPE_ASK_DB_USER"] = os.getenv("ASK_DB_USER", os.getenv("DB_USER", "postgres"))
            env["SUPE_ASK_DB_PASSWORD"] = os.getenv("ASK_DB_PASSWORD", os.getenv("DB_PASSWORD", "postgres"))
            env["SUPE_ASK_DB_SSL"] = os.getenv("DB_SSL", "false")
            env["SUPE_ASK_TENANT_ID"] = str(tenant_id)

            process = subprocess.Popen(
                [sys.executable, str(wrapper_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            with self._lock:
                self._processes[run_id] = process
            try:
                started_at = time.monotonic()
                while True:
                    if process.poll() is not None and process.stdout and process.stdout.closed:
                        break
                    if (time.monotonic() - started_at) > settings.run_timeout_seconds:
                        process.terminate()
                        raise TimeoutError("Run exceeded the configured timeout")
                    if not process.stdout:
                        break
                    ready, _, _ = select.select([process.stdout], [], [], 0.5)
                    if not ready:
                        continue
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if not line:
                        continue
                    line = line.rstrip("\n")
                    if line.startswith(EVENT_PREFIX):
                        payload = json.loads(line[len(EVENT_PREFIX) :])
                        on_event(payload)
                    elif line.startswith(ERROR_PREFIX):
                        payload = json.loads(line[len(ERROR_PREFIX) :])
                        on_event({"type": "error", "payload": payload})
                    else:
                        stdout_logs.append(line)
                stderr_output = process.stderr.read().strip() if process.stderr else ""
                if stderr_output:
                    stdout_logs.extend([line for line in stderr_output.splitlines() if line])
                return_code = process.wait(timeout=settings.run_timeout_seconds)
                return return_code, stdout_logs
            finally:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                with self._lock:
                    self._processes.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        """Terminate an in-flight local subprocess, if one exists."""
        with self._lock:
            process = self._processes.get(run_id)
        if not process:
            return False
        process.terminate()
        return True


@dataclass
class RunnerLaunchResult:
    """Result returned when the control plane hands execution to a backend."""

    backend: str
    completion_mode: str
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    task_arn: str | None = None
    callback_token: str | None = None
    input_object_key: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class ExecutionBootstrapError(RuntimeError):
    """Raised when execution cannot be handed off to the runner backend."""


class EcsRunner:
    """Launch Ask execution inside an isolated ECS task."""

    name = "ecs"

    def launch(self, run_id: str, tenant_id: str, code: str) -> RunnerLaunchResult:
        """Upload the manifest, persist execution state, and call ECS `RunTask`."""
        repository.upsert_run_execution(
            tenant_id,
            run_id,
            self.name,
            "preparing",
            metadata={"mode": "async"},
        )
        try:
            if not settings.ecs_cluster or not settings.ecs_task_definition:
                raise ExecutionBootstrapError(
                    "ASK_ECS_CLUSTER and ASK_ECS_TASK_DEFINITION must be configured for the ECS runner backend"
                )
            if not settings.control_plane_internal_url:
                raise ExecutionBootstrapError("ASK_CONTROL_PLANE_INTERNAL_URL must be configured for the ECS runner backend")
            if not settings.runner_input_bucket:
                raise ExecutionBootstrapError("ASK_RUNNER_INPUT_BUCKET must be configured for the ECS runner backend")
            subnets = [item.strip() for item in settings.ecs_subnets.split(",") if item.strip()]
            security_groups = [item.strip() for item in settings.ecs_security_groups.split(",") if item.strip()]
            if not subnets or not security_groups:
                raise ExecutionBootstrapError(
                    "ASK_ECS_SUBNETS and ASK_ECS_SECURITY_GROUPS must be configured for the ECS runner backend"
                )

            callback_token = generate_callback_token()
            callback_url = (
                settings.control_plane_internal_url.rstrip("/") + f"/api/v1/ask/internal/runs/{run_id}/callbacks"
            )
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
                status="launching",
                input_object_key=input_object_key,
                metadata={"callbackUrl": callback_url},
            )
            repository.upsert_run_execution(
                tenant_id,
                run_id,
                self.name,
                "launching",
                callback_token_hash=hash_callback_token(callback_token),
                input_object_key=input_object_key,
                metadata={"callbackUrl": callback_url},
            )

            overrides = {
                "containerOverrides": [
                    {
                        "name": settings.ecs_container_name,
                        "environment": [
                            {"name": "RUN_ID", "value": run_id},
                            {"name": "CALLBACK_URL", "value": callback_url},
                            {"name": "CALLBACK_TOKEN", "value": callback_token},
                            {"name": "INPUT_S3_URI", "value": f"s3://{settings.runner_input_bucket}/{input_object_key}"},
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
            # Analytics DB credentials are intentionally not placed in the manifest.
            # ECS should inject them from the task definition / secret store instead.
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
            repository.update_run_execution(run_id, task_arn=task_arn, metadata={"launchResponse": {"taskArn": task_arn}})
            return RunnerLaunchResult(
                backend=self.name,
                completion_mode="async",
                task_arn=task_arn,
                callback_token=callback_token,
                input_object_key=input_object_key,
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


def execute_local_run(run_id: str, tenant_id: str, code: str, on_event: Callable[[dict], None]) -> RunnerLaunchResult:
    """Small adapter that gives the local path the same return shape as ECS."""
    return_code, logs = local_runner.run(run_id, tenant_id, code, on_event)
    return RunnerLaunchResult(
        backend=local_runner.name,
        completion_mode="sync",
        return_code=return_code,
        logs=logs,
    )


def _build_runner_backend():
    """Pick the active execution backend from configuration."""
    if settings.runner_backend == "ecs":
        return EcsRunner()
    return local_runner


local_runner = LocalRunner()
active_runner = _build_runner_backend()

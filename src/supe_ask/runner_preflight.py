"""Connectivity smoke checks for the isolated Ask runner image.

Run this inside the ECS runner task definition to validate that the task can
reach RDS, the internal Ask control plane URL, and the configured S3 buckets
before enabling Ask execution against the ECS runner path in staging.
"""

from __future__ import annotations

import json
import os
import socket
import uuid

import httpx

from .aws_clients import s3_storage, secrets_service
from .config import settings
from .db import Database
from .services.runner_network import build_internal_health_url


def _emit(check: str, status: str, **payload) -> None:
    print(json.dumps({"check": check, "status": status, **payload}, ensure_ascii=True), flush=True)


def _probe_database() -> dict[str, str]:
    database = Database()
    with database.connection() as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("select current_database(), current_user")
            database_name, username = cursor.fetchone()
    return {"database": str(database_name), "user": str(username)}


def _probe_control_plane() -> dict[str, str]:
    health_url = build_internal_health_url(settings.control_plane_internal_url)
    with httpx.Client(timeout=10.0) as client:
        response = client.get(health_url)
        response.raise_for_status()
    body = response.json()
    return {
        "url": health_url,
        "service": str(body.get("service") or ""),
        "runnerBackend": str(body.get("runnerBackend") or ""),
    }


def _probe_bucket(bucket: str) -> dict[str, str]:
    key = f"ask-runner-smoke/{uuid.uuid4().hex}.json"
    payload = {"probe": "ask-runner-smoke", "hostname": socket.gethostname()}
    stored = s3_storage.put_json(bucket, key, payload)
    fetched = s3_storage.get_json(bucket, key)
    if fetched != payload:
        raise RuntimeError(f"S3 round-trip verification failed for bucket {bucket}")
    s3_storage.delete_object(bucket, key)
    return {"bucket": bucket, "objectKey": stored.key}


def _probe_secret(secret_id: str) -> dict[str, str]:
    described = secrets_service.describe_secret(secret_id)
    return {
        "secretId": secret_id,
        "arn": str(described.get("ARN") or ""),
        "name": str(described.get("Name") or ""),
    }


def main() -> int:
    checks: list[tuple[str, dict[str, str]]] = []

    database = _probe_database()
    _emit("database", "ok", **database)
    checks.append(("database", database))

    control_plane = _probe_control_plane()
    _emit("control-plane", "ok", **control_plane)
    checks.append(("control-plane", control_plane))

    if not settings.runner_input_bucket:
        raise RuntimeError("ASK_RUNNER_INPUT_BUCKET must be configured for runner preflight")
    input_bucket = _probe_bucket(settings.runner_input_bucket)
    _emit("s3-input-bucket", "ok", **input_bucket)
    checks.append(("s3-input-bucket", input_bucket))

    if not settings.runner_artifact_bucket:
        raise RuntimeError("ASK_RUNNER_ARTIFACT_BUCKET must be configured for runner preflight")
    if settings.runner_artifact_bucket != settings.runner_input_bucket:
        artifact_bucket = _probe_bucket(settings.runner_artifact_bucket)
        _emit("s3-artifact-bucket", "ok", **artifact_bucket)
        checks.append(("s3-artifact-bucket", artifact_bucket))

    secret_probe_id = os.getenv("ASK_RUNNER_SECRET_PROBE_ID", "").strip()
    if secret_probe_id:
        secret = _probe_secret(secret_probe_id)
        _emit("secrets-manager", "ok", **secret)
        checks.append(("secrets-manager", secret))
    else:
        _emit("secrets-manager", "skipped", reason="ASK_RUNNER_SECRET_PROBE_ID is not set")

    _emit("summary", "ok", checks=[name for name, _ in checks])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Entrypoint for the isolated Ask runner container.

This process runs inside the ephemeral ECS task. It downloads the execution
manifest from S3, executes the generated Python, and reports progress back to
the long-lived control plane through signed callbacks.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import httpx

from .artifact_utils import build_preview_payload, should_offload_artifact
from .aws_clients import s3_storage
from .config import settings
from .services.runner import LocalRunner

logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    """Read a required environment variable or fail fast."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse the S3 URI that points at the execution manifest."""
    if not uri.startswith("s3://"):
        raise RuntimeError("INPUT_S3_URI must be an s3:// URI")
    remainder = uri[5:]
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise RuntimeError("INPUT_S3_URI must include bucket and key")
    return bucket, key


class CallbackClient:
    """Serialize runner events into signed HTTP callbacks."""

    def __init__(self, callback_url: str, callback_token: str, run_id: str, tenant_id: str = "") -> None:
        self._callback_url = callback_url
        self._callback_token = callback_token
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._sequence = 0
        self._lock = threading.Lock()

    def set_tenant_context(self, tenant_id: str) -> None:
        """Attach tenant context once the execution manifest has been loaded."""
        self._tenant_id = tenant_id

    def _post(self, callback_type: str, payload: dict[str, Any]) -> None:
        """Send one callback with a monotonically increasing sequence number."""
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        body = {"type": callback_type, "sequence": sequence, "payload": payload}
        headers = {"Authorization": f"Bearer {self._callback_token}"}
        with httpx.Client(timeout=15.0) as client:
            client.post(self._callback_url, json=body, headers=headers).raise_for_status()

    def start_heartbeat(self, stop_event: threading.Event) -> threading.Thread:
        """Start a background heartbeat loop for reconciliation safety."""
        def _loop() -> None:
            while not stop_event.wait(settings.runner_callback_heartbeat_seconds):
                try:
                    self._post("heartbeat", {})
                except Exception:
                    continue

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        return thread

    def post_failed(self, message: str, *, stage: str, traceback: str | None = None) -> None:
        """Best-effort failure callback used for bootstrap and runtime errors."""
        payload: dict[str, Any] = {"message": message, "stage": stage}
        if traceback:
            payload["traceback"] = traceback
        try:
            self._post("failed", payload)
        except Exception:
            logger.exception(
                "Ask runner failed to deliver error callback",
                extra={"run_id": self._run_id, "stage": stage},
            )

    def handle_runner_event(self, event: dict[str, Any]) -> None:
        """Translate runtime events into progress/artifact callbacks."""
        event_type = str(event.get("type") or "")
        payload = dict(event.get("payload") or {})
        if event_type == "progress":
            self._post("progress", {"message": str(payload.get("message") or "")})
            return
        if event_type == "stdout":
            self._post(
                "progress",
                {
                    "message": str(payload.get("line") or ""),
                    "kind": "stdout",
                    "stdoutLine": str(payload.get("line") or ""),
                },
            )
            return
        if event_type == "artifact":
            artifact_type = str(payload.get("artifact_type") or "unknown")
            title = str(payload.get("title") or artifact_type.title())
            full_payload = dict(payload.get("payload") or {})
            preview_payload = build_preview_payload(artifact_type, full_payload)
            storage = None
            if settings.runner_artifact_bucket and should_offload_artifact(artifact_type, full_payload):
                # Large artifacts are uploaded before the callback is sent so the
                # control plane only has to persist preview data plus storage refs.
                object_key = f"ask-runs/{self._tenant_id}/{self._run_id}/artifacts/{self._sequence + 1:04d}-{artifact_type}.json"
                stored = s3_storage.put_json(settings.runner_artifact_bucket, object_key, full_payload)
                storage = {
                    "storageBackend": "s3",
                    "objectKey": stored.key,
                    "contentType": stored.content_type,
                    "byteSize": stored.byte_size,
                }
            self._post(
                "artifact",
                {
                    "artifactType": artifact_type,
                    "title": title,
                    "previewPayload": preview_payload,
                    "storage": storage,
                },
            )


def main() -> None:
    """Boot the isolated runner process and execute one Ask run."""
    run_id = _require_env("RUN_ID")
    callback_url = _require_env("CALLBACK_URL")
    callback_token = _require_env("CALLBACK_TOKEN")
    callback_client = CallbackClient(callback_url, callback_token, run_id)
    try:
        input_s3_uri = _require_env("INPUT_S3_URI")
        bucket, key = _parse_s3_uri(input_s3_uri)
        logger.info("Downloading Ask execution manifest", extra={"run_id": run_id})
        manifest = s3_storage.get_json(bucket, key)
        tenant_id = str(manifest.get("tenantId") or "")
        python_code = str(manifest.get("pythonCode") or "")
        if not tenant_id or not python_code:
            raise RuntimeError("Execution manifest is missing tenantId or pythonCode")
        callback_client.set_tenant_context(tenant_id)
    except Exception as error:
        logger.exception("Ask runner bootstrap failed", extra={"run_id": run_id})
        callback_client.post_failed(str(error), stage="execution_bootstrap")
        raise

    heartbeat_stop = threading.Event()
    heartbeat_thread = callback_client.start_heartbeat(heartbeat_stop)
    runner = LocalRunner()
    try:
        # The isolated ECS runner reuses the same local execution wrapper so the
        # runtime contract stays identical across local and production paths.
        logger.info("Starting Ask runner execution", extra={"run_id": run_id, "tenant_id": tenant_id})
        return_code, logs = runner.run(run_id, tenant_id, python_code, callback_client.handle_runner_event)
        if logs:
            callback_client.handle_runner_event(
                {
                    "type": "artifact",
                    "payload": {
                        "artifact_type": "log",
                        "title": "Execution log",
                        "payload": {"lines": logs},
                    },
                }
            )
        if return_code != 0:
            callback_client.post_failed("Runner exited with a non-zero status", stage="execution")
            raise SystemExit(return_code)
        callback_client._post("completed", {"summary": {"returnCode": return_code}})
    except Exception as error:
        logger.exception("Ask runner execution failed", extra={"run_id": run_id, "tenant_id": tenant_id})
        callback_client.post_failed(str(error), stage="execution")
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()

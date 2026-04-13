from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

from ..artifact_utils import build_preview_payload, should_offload_artifact
from ..aws_clients import s3_storage
from ..config import settings

logger = logging.getLogger(__name__)


class RunnerExecutor(Protocol):
    def run(
        self,
        run_id: str,
        tenant_id: str,
        code: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> tuple[int, list[str]]: ...


@dataclass(frozen=True)
class RunnerJob:
    run_id: str
    callback_url: str
    callback_token: str
    input_s3_uri: str
    tenant_id: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RunnerJob":
        run_id = str(payload.get("runId") or "").strip()
        callback_url = str(payload.get("callbackUrl") or "").strip()
        callback_token = str(payload.get("callbackToken") or "").strip()
        input_s3_uri = str(payload.get("inputS3Uri") or "").strip()
        tenant_id = str(payload.get("tenantId") or "").strip()
        if not run_id or not callback_url or not callback_token or not input_s3_uri:
            raise RuntimeError("Codebox message is missing runId, callbackUrl, callbackToken, or inputS3Uri")
        return cls(
            run_id=run_id,
            callback_url=callback_url,
            callback_token=callback_token,
            input_s3_uri=input_s3_uri,
            tenant_id=tenant_id,
        )


def parse_s3_uri(uri: str) -> tuple[str, str]:
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
        self._tenant_id = tenant_id

    def _post(self, callback_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        body = {"type": callback_type, "sequence": sequence, "payload": payload}
        headers = {"Authorization": f"Bearer {self._callback_token}"}
        with httpx.Client(timeout=15.0) as client:
            client.post(self._callback_url, json=body, headers=headers).raise_for_status()

    def start_heartbeat(self, stop_event: threading.Event) -> threading.Thread:
        def _loop() -> None:
            try:
                self._post("heartbeat", {})
            except Exception:
                pass
            while not stop_event.wait(settings.runner_callback_heartbeat_seconds):
                try:
                    self._post("heartbeat", {})
                except Exception:
                    continue

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        return thread

    def post_failed(self, message: str, *, stage: str, traceback: str | None = None) -> None:
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


def run_job(job: RunnerJob, executor: RunnerExecutor) -> int:
    callback_client = CallbackClient(job.callback_url, job.callback_token, job.run_id, job.tenant_id)
    heartbeat_stop = threading.Event()
    heartbeat_thread = callback_client.start_heartbeat(heartbeat_stop)
    try:
        bucket, key = parse_s3_uri(job.input_s3_uri)
        logger.info("Downloading Ask execution manifest", extra={"run_id": job.run_id})
        manifest = s3_storage.get_json(bucket, key)
        tenant_id = str(manifest.get("tenantId") or job.tenant_id or "")
        python_code = str(manifest.get("pythonCode") or "")
        if not tenant_id or not python_code:
            raise RuntimeError("Execution manifest is missing tenantId or pythonCode")
        callback_client.set_tenant_context(tenant_id)
    except Exception as error:
        logger.exception("Ask runner bootstrap failed", extra={"run_id": job.run_id})
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        callback_client.post_failed(str(error), stage="execution_bootstrap")
        raise

    try:
        logger.info("Starting Ask runner execution", extra={"run_id": job.run_id, "tenant_id": tenant_id})
        return_code, logs = executor.run(job.run_id, tenant_id, python_code, callback_client.handle_runner_event)
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
        return return_code
    except Exception as error:
        logger.exception("Ask runner execution failed", extra={"run_id": job.run_id, "tenant_id": tenant_id})
        callback_client.post_failed(str(error), stage="execution")
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

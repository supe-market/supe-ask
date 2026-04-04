"""Artifact persistence helpers for Ask execution results.

The control plane stores a UI-friendly preview in Postgres and optionally
offloads the full payload to S3 when the artifact is large.
"""

from __future__ import annotations

from typing import Any

from ..artifact_utils import build_preview_payload, should_offload_artifact
from ..aws_clients import s3_storage
from ..config import settings
from ..repository import repository


class ArtifactService:
    """Persist artifacts while hiding the database-vs-S3 storage split."""

    def persist_artifact(
        self,
        tenant_id: str,
        run_id: str,
        artifact_type: str,
        title: str,
        *,
        full_payload: dict[str, Any] | None = None,
        preview_payload: dict[str, Any] | None = None,
        storage: dict[str, Any] | None = None,
        ordinal: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an artifact preview and offload the full body when needed."""
        if ordinal is None:
            ordinal = repository.next_artifact_ordinal(tenant_id, run_id)

        effective_preview = preview_payload or build_preview_payload(artifact_type, full_payload or {})
        storage_backend = "database"
        object_key = None
        content_type = None
        byte_size = None

        if storage:
            storage_backend = str(storage.get("storageBackend") or "s3")
            object_key = storage.get("objectKey")
            content_type = storage.get("contentType")
            byte_size = storage.get("byteSize")
        elif full_payload is not None and settings.runner_artifact_bucket and should_offload_artifact(artifact_type, full_payload):
            # Large artifacts stay renderable in the UI because the DB row keeps a
            # compact preview while the full payload moves to object storage.
            object_key = f"ask-runs/{tenant_id}/{run_id}/artifacts/{ordinal:04d}-{artifact_type}.json"
            stored = s3_storage.put_json(settings.runner_artifact_bucket, object_key, full_payload)
            storage_backend = "s3"
            content_type = stored.content_type
            byte_size = stored.byte_size

        payload_for_db = effective_preview if storage_backend != "database" else (full_payload or effective_preview)

        return repository.create_artifact(
            tenant_id,
            run_id,
            artifact_type,
            title,
            payload_for_db,
            ordinal,
            storage_backend=storage_backend,
            object_key=object_key,
            content_type=content_type,
            byte_size=byte_size,
            metadata=metadata or {},
        )


artifact_service = ArtifactService()

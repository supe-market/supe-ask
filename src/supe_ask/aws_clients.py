from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import settings


def _build_boto3_client(service_name: str):
    try:
        import boto3
        from botocore.config import Config
    except Exception as error:
        raise RuntimeError("boto3 is required for AWS-backed Ask execution") from error

    client_kwargs: dict[str, Any] = {}
    if settings.aws_region:
        client_kwargs["region_name"] = settings.aws_region
    if settings.s3_endpoint and service_name == "s3":
        client_kwargs["endpoint_url"] = settings.s3_endpoint
    if settings.s3_access_key_id and settings.s3_secret_access_key:
        client_kwargs["aws_access_key_id"] = settings.s3_access_key_id
        client_kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
    if service_name == "s3" and settings.s3_force_path_style:
        client_kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client(service_name, **client_kwargs)


@dataclass
class StorageObject:
    bucket: str
    key: str
    content_type: str
    byte_size: int

    def s3_uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class S3StorageService:
    def __init__(self) -> None:
        self._client = None

    def _s3(self):
        if self._client is None:
            self._client = _build_boto3_client("s3")
        return self._client

    def put_json(self, bucket: str, key: str, payload: dict[str, Any]) -> StorageObject:
        body = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
        self._s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        return StorageObject(bucket=bucket, key=key, content_type="application/json", byte_size=len(body))

    def put_text(self, bucket: str, key: str, content: str, content_type: str = "text/plain") -> StorageObject:
        body = content.encode("utf-8")
        self._s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        return StorageObject(bucket=bucket, key=key, content_type=content_type, byte_size=len(body))

    def get_json(self, bucket: str, key: str) -> dict[str, Any]:
        response = self._s3().get_object(Bucket=bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)


class EcsService:
    def __init__(self) -> None:
        self._client = None

    def _ecs(self):
        if self._client is None:
            self._client = _build_boto3_client("ecs")
        return self._client

    def run_task(self, **kwargs):
        return self._ecs().run_task(**kwargs)

    def describe_tasks(self, **kwargs):
        return self._ecs().describe_tasks(**kwargs)

    def stop_task(self, **kwargs):
        return self._ecs().stop_task(**kwargs)


s3_storage = S3StorageService()
ecs_service = EcsService()

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

    def delete_object(self, bucket: str, key: str) -> None:
        self._s3().delete_object(Bucket=bucket, Key=key)


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


class SqsService:
    def __init__(self) -> None:
        self._client = None

    def _sqs(self):
        if self._client is None:
            self._client = _build_boto3_client("sqs")
        return self._client

    def send_message(self, queue_url: str, payload: dict[str, Any] | str):
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=True, default=str)
        return self._sqs().send_message(QueueUrl=queue_url, MessageBody=body)

    def receive_messages(
        self,
        queue_url: str,
        *,
        max_number: int = 1,
        wait_time_seconds: int = 20,
        visibility_timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "QueueUrl": queue_url,
            "MaxNumberOfMessages": max_number,
            "WaitTimeSeconds": wait_time_seconds,
            "AttributeNames": ["All"],
            "MessageAttributeNames": ["All"],
        }
        if visibility_timeout is not None:
            kwargs["VisibilityTimeout"] = visibility_timeout
        response = self._sqs().receive_message(**kwargs)
        return list(response.get("Messages") or [])

    def delete_message(self, queue_url: str, receipt_handle: str) -> None:
        self._sqs().delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


class SecretsManagerService:
    def __init__(self) -> None:
        self._client = None

    def _secretsmanager(self):
        if self._client is None:
            self._client = _build_boto3_client("secretsmanager")
        return self._client

    def describe_secret(self, secret_id: str):
        return self._secretsmanager().describe_secret(SecretId=secret_id)


s3_storage = S3StorageService()
ecs_service = EcsService()
sqs_service = SqsService()
secrets_service = SecretsManagerService()

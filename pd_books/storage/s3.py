"""Amazon S3 storage backend (production).

boto3 is imported lazily inside the constructor so that local development and the
test suite never require the AWS SDK to be installed.
"""

from __future__ import annotations

from .base import BaseStorage


class S3Storage(BaseStorage):
    def __init__(self, bucket: str, prefix: str = "", client: object | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise ImportError(
                    "the s3 backend requires boto3; install with: pip install 'pd-books[s3]'"
                ) from exc
            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key.lstrip('/')}" if self.prefix else key.lstrip("/")

    def write_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def read_bytes(self, key: str) -> bytes | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # noqa: BLE001 - botocore raises a dynamic class
            if type(exc).__name__ in {"NoSuchKey", "ClientError"} and self._is_missing(exc):
                return None
            raise
        return resp["Body"].read()

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        return code in {"NoSuchKey", "404", "NotFound"}

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # noqa: BLE001 - botocore raises a dynamic class
            if self._is_missing(exc):
                return False
            raise
        return True

    def list_keys(self, prefix: str) -> list[str]:
        full = self._key(prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][strip:])
        return sorted(keys)

"""Storage abstraction: backends move bytes, nothing more.

Serialization lives in ``parquet.py`` so every backend writes byte-identical data
and the layout is the same locally and on S3.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class BaseStorage(ABC):
    """A flat key/value byte store. Keys are '/'-separated relative paths."""

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes | None:
        """Return the object's bytes, or None if it does not exist."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...

    # --- JSON convenience, shared by all backends ---

    def write_json(self, key: str, payload: Any) -> None:
        self.write_bytes(key, json.dumps(payload, indent=2, default=str).encode("utf-8"))

    def read_json(self, key: str) -> Any | None:
        raw = self.read_bytes(key)
        return json.loads(raw.decode("utf-8")) if raw is not None else None


def build_storage(config: Any) -> BaseStorage:
    """Resolve a storage backend from config. Backends are imported lazily so
    local development never needs boto3 installed."""
    backend = config.backend
    if backend == "local":
        from .local import LocalStorage

        return LocalStorage(config.local_path)
    if backend == "s3":
        from .s3 import S3Storage

        return S3Storage(bucket=config.s3_bucket, prefix=config.s3_prefix)
    raise ValueError(f"unknown storage backend: {backend!r}")

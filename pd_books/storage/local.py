"""Local filesystem storage backend (development and testing)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .base import BaseStorage


class LocalStorage(BaseStorage):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Guard against a key escaping the configured root via '..'
        if not path.is_relative_to(self.root):
            raise ValueError(f"key escapes storage root: {key!r}")
        return path

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crashed run never leaves a half-written shard
        # that a reader would choke on.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        search_root = base if base.is_dir() else base.parent
        if not search_root.is_dir():
            return []
        keys = [
            p.relative_to(self.root).as_posix()
            for p in search_root.rglob("*")
            if p.is_file()
        ]
        return sorted(k for k in keys if k.startswith(prefix.strip("/")))

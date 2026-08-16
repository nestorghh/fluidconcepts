"""Configuration loading: config.yaml overlaid with environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ENV_PREFIX = "PDBOOKS__"


class RunConfig(BaseModel):
    mode: Literal["full", "incremental"] = "incremental"
    max_books: int | None = 1000
    max_requests_per_run: int | None = 1000
    log_level: str = "INFO"


class StorageConfig(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local_path: str = "./data/books"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    compression: str = "zstd"
    rows_per_shard: int = 5000

    @model_validator(mode="after")
    def _check_backend(self) -> "StorageConfig":
        if self.backend == "s3" and not self.s3_bucket:
            raise ValueError("storage.s3_bucket is required when backend is 's3'")
        return self


class SourceConfig(BaseModel):
    name: str
    enabled: bool = True
    base_url: str
    api_key_env: str | None = None
    page_size: int = 100
    request_timeout: float = 30.0
    max_retries: int = 4
    #: Minimum seconds between requests to this source (rate-limit politeness).
    min_request_interval: float = 0.0
    #: Extra provider-specific query params merged into listing requests
    #: (e.g. {"sort": "ascending"}), so provider quirks stay out of shared config.
    params: dict[str, str] = Field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        """Resolve the API key from the environment at call time, never from config."""
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class PreviewConfig(BaseModel):
    enabled: bool = True
    chars: int = Field(default=4000, gt=0)
    cleaning_mode: str = "simple"
    text_requests_per_run: int | None = 500
    order_by: str = "download_count"


class Settings(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    sources: list[SourceConfig] = Field(default_factory=list)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def _coerce(value: str) -> Any:
    """Turn an env-var string into the YAML-equivalent scalar (int, bool, null, str)."""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    """Overlay PDBOOKS__SECTION__KEY env vars onto the parsed config.

    Only nested mappings are addressable; the `sources` list is configured in YAML
    (its secrets already come from the environment via `api_key_env`).
    """
    for env_key, raw in environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in env_key[len(ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        cursor: dict[str, Any] = data
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce(raw)
    return data


def load_settings(
    path: str | Path = "config.yaml",
    environ: dict[str, str] | None = None,
) -> Settings:
    """Load settings from a YAML file, then apply environment overrides."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    merged = _apply_env_overrides(raw, environ if environ is not None else dict(os.environ))
    return Settings.model_validate(merged)

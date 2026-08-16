"""Shared fixtures. No test touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pd_books.config import PreviewConfig, RunConfig, Settings, SourceConfig, StorageConfig
from pd_books.storage.local import LocalStorage

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


@pytest.fixture
def source_config() -> SourceConfig:
    return SourceConfig(
        name="gutenberg",
        base_url="https://project-gutenberg-books-api.p.rapidapi.com",
        api_key_env="TEST_RAPIDAPI_KEY",
        page_size=2,
    )


@pytest.fixture
def settings(tmp_path, source_config) -> Settings:
    return Settings(
        run=RunConfig(mode="incremental", max_books=100, max_requests_per_run=50),
        storage=StorageConfig(backend="local", local_path=str(tmp_path / "data"), rows_per_shard=2),
        sources=[source_config],
        preview=PreviewConfig(enabled=True, chars=100, text_requests_per_run=10),
    )

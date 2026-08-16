import pytest
from pydantic import ValidationError

from pd_books.config import Settings, load_settings

CONFIG = """
run:
  mode: incremental
  max_books: 1000
storage:
  backend: local
  local_path: ./data/books
sources:
  - name: gutenberg
    base_url: https://example.invalid
    api_key_env: TEST_KEY
preview:
  chars: 4000
"""


def _write(tmp_path, text=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_loads_yaml(tmp_path):
    settings = load_settings(_write(tmp_path), environ={})
    assert settings.run.max_books == 1000
    assert settings.storage.backend == "local"
    assert [s.name for s in settings.enabled_sources()] == ["gutenberg"]


def test_env_overrides_are_typed(tmp_path):
    settings = load_settings(
        _write(tmp_path),
        environ={
            "PDBOOKS__RUN__MAX_BOOKS": "25",
            "PDBOOKS__STORAGE__BACKEND": "s3",
            "PDBOOKS__STORAGE__S3_BUCKET": "my-bucket",
            "PDBOOKS__PREVIEW__ENABLED": "false",
        },
    )
    assert settings.run.max_books == 25 and isinstance(settings.run.max_books, int)
    assert settings.storage.backend == "s3"
    assert settings.preview.enabled is False


def test_unrelated_env_vars_are_ignored(tmp_path):
    settings = load_settings(_write(tmp_path), environ={"PATH": "/usr/bin", "HOME": "/root"})
    assert settings.storage.backend == "local"


def test_s3_backend_requires_bucket():
    with pytest.raises(ValidationError, match="s3_bucket"):
        Settings.model_validate({"storage": {"backend": "s3"}})


def test_api_key_comes_from_environment(tmp_path, monkeypatch):
    settings = load_settings(_write(tmp_path), environ={})
    source = settings.sources[0]
    assert source.api_key is None
    monkeypatch.setenv("TEST_KEY", "secret-value")
    assert source.api_key == "secret-value"


def test_disabled_sources_are_excluded(tmp_path):
    text = CONFIG.replace("  - name: gutenberg", "  - name: gutenberg\n    enabled: false")
    settings = load_settings(_write(tmp_path, text), environ={})
    assert settings.enabled_sources() == []

from datetime import datetime, timedelta, timezone

import pyarrow.dataset as ds
import pytest

from pd_books.config import StorageConfig
from pd_books.models import BOOK_SCHEMA, Book
from pd_books.storage.base import build_storage
from pd_books.storage.local import LocalStorage
from pd_books.storage.parquet import (
    books_to_parquet_bytes,
    parquet_bytes_to_table,
    read_catalog,
)


def make_book(i: int, **overrides) -> Book:
    data = dict(
        book_uid=f"gutenberg:{i}",
        source="gutenberg",
        source_id=str(i),
        title=f"Book {i}",
        download_count=i,
    )
    data.update(overrides)
    return Book(**data).finalize()


def test_write_read_roundtrip(storage):
    storage.write_bytes("a/b.parquet", b"payload")
    assert storage.exists("a/b.parquet")
    assert storage.read_bytes("a/b.parquet") == b"payload"


def test_missing_key_returns_none(storage):
    assert storage.read_bytes("nope") is None
    assert storage.exists("nope") is False


def test_json_helpers(storage):
    storage.write_json("_state/x.json", {"last_page": 3})
    assert storage.read_json("_state/x.json") == {"last_page": 3}


def test_list_keys_filters_by_prefix(storage):
    storage.write_bytes("books/a.parquet", b"1")
    storage.write_bytes("books/nested/b.parquet", b"2")
    storage.write_bytes("_state/c.json", b"3")
    assert storage.list_keys("books/") == ["books/a.parquet", "books/nested/b.parquet"]


def test_key_cannot_escape_root(storage):
    with pytest.raises(ValueError, match="escapes storage root"):
        storage.write_bytes("../evil.txt", b"x")


def test_build_storage_resolves_local(tmp_path):
    storage = build_storage(StorageConfig(backend="local", local_path=str(tmp_path)))
    assert isinstance(storage, LocalStorage)


def test_build_storage_rejects_unknown_backend():
    class Cfg:
        backend = "gcs"

    with pytest.raises(ValueError, match="unknown storage backend"):
        build_storage(Cfg())


def test_parquet_uses_explicit_schema():
    data = books_to_parquet_bytes([make_book(1)])
    table = parquet_bytes_to_table(data)
    assert table.num_rows == 1
    assert table.schema.field("publication_year").type == BOOK_SCHEMA.field("publication_year").type


def test_hive_partitioned_dataset_read(storage, tmp_path):
    key = "books/provider=gutenberg/ingest_date=2026-08-15/part-0000.parquet"
    storage.write_bytes(key, books_to_parquet_bytes([make_book(1), make_book(2)]))
    table = ds.dataset(storage.root / "books", partitioning="hive").to_table()
    assert table.num_rows == 2
    assert set(table.column("provider").to_pylist()) == {"gutenberg"}


def test_read_catalog_dedupes_latest_wins(storage):
    """Append-only writes mean a record can appear twice; newest ingest wins."""
    old = make_book(1, title="Old Title")
    old.ingested_at = datetime.now(timezone.utc) - timedelta(days=1)
    new = make_book(1, title="New Title")

    storage.write_bytes(
        "books/provider=gutenberg/ingest_date=2026-08-14/part-0000.parquet",
        books_to_parquet_bytes([old]),
    )
    storage.write_bytes(
        "books/provider=gutenberg/ingest_date=2026-08-15/part-0000.parquet",
        books_to_parquet_bytes([new]),
    )

    catalog = read_catalog(storage, "gutenberg")
    assert len(catalog) == 1
    assert catalog[0].title == "New Title"


def test_read_catalog_ignores_non_parquet(storage):
    storage.write_bytes(
        "books/provider=gutenberg/ingest_date=2026-08-15/part-0000.parquet",
        books_to_parquet_bytes([make_book(1)]),
    )
    storage.write_json("books/provider=gutenberg/notes.json", {"ignore": True})
    assert len(read_catalog(storage, "gutenberg")) == 1

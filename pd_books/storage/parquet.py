"""Serialize Book records to Parquet bytes using the canonical schema."""

from __future__ import annotations

import io
from collections.abc import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from ..models import BOOK_SCHEMA, Book


def books_to_table(books: Sequence[Book]) -> pa.Table:
    """Build an Arrow table under the explicit BOOK_SCHEMA.

    The schema is always passed in: inferring it would type an all-null optional
    column as ``null`` in one shard and as its real type in another, which breaks
    reads across the dataset.
    """
    return pa.Table.from_pylist([b.to_row() for b in books], schema=BOOK_SCHEMA)


def books_to_parquet_bytes(books: Sequence[Book], compression: str = "zstd") -> bytes:
    """Serialize records to an in-memory Parquet file."""
    table = books_to_table(books)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression=compression)
    return buf.getvalue()


def parquet_bytes_to_table(data: bytes) -> pa.Table:
    """Read Parquet bytes back into a table (used by tests and readers)."""
    return pq.read_table(io.BytesIO(data))


def read_catalog(storage, provider: str | None = None) -> list[Book]:
    """Read the deduplicated latest-wins view of the catalog.

    Writes are append-only, so a record updated across runs appears more than once.
    The newest ``ingested_at`` per ``book_uid`` wins. This is the view downstream ML
    consumers should build embeddings from.
    """
    prefix = f"books/provider={provider}/" if provider else "books/"
    latest: dict[str, Book] = {}
    for key in storage.list_keys(prefix):
        if not key.endswith(".parquet"):
            continue
        raw = storage.read_bytes(key)
        if raw is None:
            continue
        for row in parquet_bytes_to_table(raw).to_pylist():
            book = Book.from_row(row)
            current = latest.get(book.book_uid)
            if current is None or book.ingested_at >= current.ingested_at:
                latest[book.book_uid] = book
    return list(latest.values())

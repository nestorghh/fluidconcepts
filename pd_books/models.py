"""Canonical book schema shared by every provider and storage backend.

The Pydantic models are the ingestion contract; the pyarrow schema below is the
storage contract. Both are versioned via ``SCHEMA_VERSION``.

Embeddings deliberately live outside this schema. ``book_uid`` is a stable primary
key, so a vector index can be built later and joined on it without touching the
ingestion pipeline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# The content hash answers one question: "has the upstream metadata changed?"
#
# Excluded are (a) values that churn on every poll, (b) per-run bookkeeping, and
# (c) the enriched text. Text is excluded because stage 1 hashes a record before
# stage 2 fetches its preview -- including it would make every record compare as
# "changed" on the next run and trigger endless rewrites.
_HASH_EXCLUDED = {
    "download_count",
    "ingested_at",
    "content_hash",
    "provider_version",
    "schema_version",
    "searchable_text",
    "text_source",
    "text_char_count",
}


class TextSource(str, Enum):
    """Where ``searchable_text`` came from, in the spec's order of preference."""

    SUMMARY = "summary"
    DESCRIPTION = "description"
    INTRO = "intro"
    FIRST_CHARS = "first_chars"
    NONE = "none"


class Author(BaseModel):
    name: str
    source_author_id: str | None = None
    birth_year: int | None = None
    death_year: int | None = None


class Book(BaseModel):
    """One book's metadata, normalized across providers."""

    # --- identity ---
    book_uid: str
    source: str
    source_id: str
    identifiers: dict[str, str] = Field(default_factory=dict)

    # --- core metadata ---
    title: str
    alternative_title: str | None = None
    authors: list[Author] = Field(default_factory=list)
    publication_year: int | None = None
    language: str | None = None
    subjects: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    publisher: str | None = None

    # --- access ---
    download_url: str | None = None
    source_url: str | None = None
    cover_image_url: str | None = None
    media_type: str | None = None

    # --- rights ---
    license: str | None = None
    is_public_domain: bool | None = None
    rights_statement: str | None = None

    # --- searchable text ---
    searchable_text: str | None = None
    text_source: TextSource = TextSource.NONE
    text_char_count: int = 0

    # --- ranking signals ---
    download_count: int | None = None
    reading_ease_score: float | None = None
    issued_at: datetime | None = None
    #: Set when the source has withdrawn the work; downstream should usually filter these out.
    withdrawn_reason: str | None = None

    # --- provenance ---
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provider_version: str | None = None
    content_hash: str = ""
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def make_uid(source: str, source_id: str | int) -> str:
        return f"{source}:{source_id}"

    def compute_content_hash(self) -> str:
        """Stable sha256 over the meaningful fields only.

        Used for incremental change detection: if this is unchanged, the record does
        not need to be rewritten.
        """
        payload = self.model_dump(mode="json", exclude=_HASH_EXCLUDED)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def finalize(self) -> "Book":
        """Fill derived fields. Call once the record is fully populated."""
        if self.searchable_text:
            self.text_char_count = len(self.searchable_text)
        elif self.text_source is TextSource.NONE:
            self.text_char_count = 0
        self.content_hash = self.compute_content_hash()
        return self

    def to_row(self) -> dict[str, Any]:
        """Flatten to a pyarrow-compatible row matching BOOK_SCHEMA."""
        row = self.model_dump(mode="python")
        row["text_source"] = self.text_source.value
        row["authors"] = [a.model_dump(mode="python") for a in self.authors]
        # pyarrow maps map<string,string> from a list of key/value pairs
        row["identifiers"] = list(self.identifiers.items())
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Book":
        """Inverse of :meth:`to_row`, for reading records back out of Parquet."""
        data = dict(row)
        identifiers = data.get("identifiers")
        if isinstance(identifiers, list):
            data["identifiers"] = dict(identifiers)
        elif identifiers is None:
            data["identifiers"] = {}
        for key in ("authors", "subjects", "categories"):
            if data.get(key) is None:
                data[key] = []
        # Hive partition columns are not part of the model.
        for key in ("provider", "ingest_date"):
            data.pop(key, None)
        return cls.model_validate(data)


_AUTHOR_STRUCT = pa.struct(
    [
        pa.field("name", pa.string()),
        pa.field("source_author_id", pa.string()),
        pa.field("birth_year", pa.int32()),
        pa.field("death_year", pa.int32()),
    ]
)

# Explicit schema, never inferred: a shard where an optional column happens to be
# entirely null would otherwise be typed as null and break dataset reads.
# Evolution rule: append new fields at the end, never retype or remove existing ones.
BOOK_SCHEMA = pa.schema(
    [
        pa.field("book_uid", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("identifiers", pa.map_(pa.string(), pa.string())),
        pa.field("title", pa.string(), nullable=False),
        pa.field("alternative_title", pa.string()),
        pa.field("authors", pa.list_(_AUTHOR_STRUCT)),
        pa.field("publication_year", pa.int32()),
        pa.field("language", pa.string()),
        pa.field("subjects", pa.list_(pa.string())),
        pa.field("categories", pa.list_(pa.string())),
        pa.field("publisher", pa.string()),
        pa.field("download_url", pa.string()),
        pa.field("source_url", pa.string()),
        pa.field("cover_image_url", pa.string()),
        pa.field("media_type", pa.string()),
        pa.field("license", pa.string()),
        pa.field("is_public_domain", pa.bool_()),
        pa.field("rights_statement", pa.string()),
        pa.field("searchable_text", pa.string()),
        pa.field("text_source", pa.string()),
        pa.field("text_char_count", pa.int32()),
        pa.field("download_count", pa.int64()),
        pa.field("reading_ease_score", pa.float64()),
        pa.field("issued_at", pa.timestamp("us", tz="UTC")),
        pa.field("withdrawn_reason", pa.string()),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("provider_version", pa.string()),
        pa.field("content_hash", pa.string(), nullable=False),
        pa.field("schema_version", pa.int32(), nullable=False),
    ],
    metadata={"schema_version": str(SCHEMA_VERSION)},
)

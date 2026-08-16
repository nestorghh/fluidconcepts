from datetime import datetime, timezone

import pyarrow as pa

from pd_books.models import BOOK_SCHEMA, Author, Book, TextSource


def make_book(**overrides) -> Book:
    data = dict(
        book_uid="gutenberg:1342",
        source="gutenberg",
        source_id="1342",
        identifiers={"gutenberg_id": "1342"},
        title="Pride and Prejudice",
        authors=[Author(name="Austen, Jane", source_author_id="68", birth_year=1775)],
        subjects=["Love stories"],
        categories=["Harvard Classics"],
        download_count=100,
        issued_at=datetime(1998, 6, 1, tzinfo=timezone.utc),
        is_public_domain=True,
    )
    data.update(overrides)
    return Book(**data).finalize()


def test_uid_is_stable_join_key():
    assert Book.make_uid("gutenberg", 1342) == "gutenberg:1342"


def test_hash_ignores_volatile_download_count():
    a = make_book()
    b = make_book(download_count=999999)
    assert a.content_hash == b.content_hash


def test_hash_ignores_ingest_timestamp():
    a = make_book()
    b = make_book(ingested_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert a.content_hash == b.content_hash


def test_hash_ignores_enriched_text():
    """Stage 1 hashes a record before stage 2 adds preview text. If text counted
    toward the hash, every record would compare as changed on the next run."""
    before = make_book()
    after = make_book(searchable_text="the opening lines", text_source=TextSource.FIRST_CHARS)
    assert before.content_hash == after.content_hash


def test_hash_changes_on_meaningful_edit():
    assert make_book().content_hash != make_book(title="Something Else").content_hash
    assert make_book().content_hash != make_book(subjects=["Other"]).content_hash


def test_finalize_sets_text_char_count():
    book = make_book(searchable_text="abcde", text_source=TextSource.FIRST_CHARS)
    assert book.text_char_count == 5


def test_row_roundtrip_through_arrow():
    book = make_book(searchable_text="hello", text_source=TextSource.FIRST_CHARS)
    table = pa.Table.from_pylist([book.to_row()], schema=BOOK_SCHEMA)
    restored = Book.from_row(table.to_pylist()[0])
    assert restored.book_uid == book.book_uid
    assert restored.identifiers == {"gutenberg_id": "1342"}
    assert restored.authors[0].name == "Austen, Jane"
    assert restored.authors[0].birth_year == 1775
    assert restored.text_source is TextSource.FIRST_CHARS
    assert restored.content_hash == book.content_hash


def test_from_row_drops_hive_partition_columns():
    row = make_book().to_row()
    row.update({"provider": "gutenberg", "ingest_date": "2026-08-15"})
    assert Book.from_row(row).source == "gutenberg"


def test_schema_types_survive_all_null_optional_columns():
    """A shard where every optional column is null must still carry real types."""
    book = Book(
        book_uid="gutenberg:1", source="gutenberg", source_id="1", title="T"
    ).finalize()
    table = pa.Table.from_pylist([book.to_row()], schema=BOOK_SCHEMA)
    assert table.schema.field("publication_year").type == pa.int32()
    assert table.schema.field("language").type == pa.string()

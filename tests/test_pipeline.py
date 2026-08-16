"""End-to-end pipeline behaviour, driven by a fake provider (no network)."""

from __future__ import annotations

import pytest

from pd_books.http import QuotaExhausted, RequestBudget
from pd_books.models import Book, TextSource
from pd_books.pipeline import run_provider
from pd_books.providers.base import _REGISTRY, BaseProvider, PreviewText
from pd_books.state import load_state
from pd_books.storage.parquet import read_catalog


class FakeProvider(BaseProvider):
    name = "gutenberg"  # overrides the real provider in the registry for these tests
    version = "fake/1"

    #: class-level knobs so tests can steer behaviour through build_provider()
    catalog: list[dict] = []
    page_size: int = 2
    text_by_id: dict[str, str] = {}
    fail_text_for: set[str] = set()
    quota_out_after: int | None = None
    calls: dict[str, int] = {}

    def __init__(self, config, preview):
        super().__init__(config, preview)
        type(self).calls = {"pages": 0, "text": 0}

    def iter_records(self, state, budget):
        # Mirrors the real provider: resume on the last page, not after it.
        page = max(1, state.last_page)
        while True:
            start = (page - 1) * self.page_size
            chunk = self.catalog[start : start + self.page_size]
            if not chunk:
                return
            if not budget.can_spend(1):
                return
            budget.spend(1)
            type(self).calls["pages"] += 1
            yield from chunk
            state.last_page = page
            if start + self.page_size >= len(self.catalog):
                return
            page += 1

    def to_book(self, raw):
        return Book(
            book_uid=Book.make_uid(self.name, raw["id"]),
            source=self.name,
            source_id=str(raw["id"]),
            title=raw["title"],
            download_count=raw.get("download_count", 0),
            provider_version=self.version,
        )

    def fetch_preview_text(self, source_id, budget):
        if self.quota_out_after is not None and type(self).calls["text"] >= self.quota_out_after:
            raise QuotaExhausted("plan exhausted")
        budget.spend(1)
        type(self).calls["text"] += 1
        if source_id in self.fail_text_for:
            raise RuntimeError("boom")
        text = self.text_by_id.get(source_id)
        return PreviewText(text=text, source=TextSource.FIRST_CHARS) if text else None


@pytest.fixture(autouse=True)
def use_fake_provider(monkeypatch):
    monkeypatch.setitem(_REGISTRY, "gutenberg", FakeProvider)
    FakeProvider.catalog = [
        {"id": i, "title": f"Book {i}", "download_count": i * 10} for i in range(1, 6)
    ]
    FakeProvider.page_size = 2
    FakeProvider.text_by_id = {str(i): f"text for {i}" for i in range(1, 6)}
    FakeProvider.fail_text_for = set()
    FakeProvider.quota_out_after = None
    yield


def test_full_run_writes_shards_and_state(settings, storage):
    report = run_provider(settings, settings.sources[0], storage)

    assert report.new_records == 5
    assert report.rows_written == 5
    assert report.shards_written == 3  # rows_per_shard=2
    assert report.text_fetched == 5

    catalog = read_catalog(storage, "gutenberg")
    assert len(catalog) == 5
    assert all(b.searchable_text for b in catalog)

    state = load_state(storage, "gutenberg")
    assert state.total_records == 5
    assert state.max_id_seen == 5
    assert state.pending_text == []


def test_second_run_is_idempotent(settings, storage):
    run_provider(settings, settings.sources[0], storage)
    second = run_provider(settings, settings.sources[0], storage)

    assert second.new_records == 0
    assert second.changed_records == 0
    assert second.rows_written == 0
    assert len(read_catalog(storage, "gutenberg")) == 5


def test_incremental_picks_up_new_books(settings, storage):
    run_provider(settings, settings.sources[0], storage)

    FakeProvider.catalog.append({"id": 6, "title": "Book 6", "download_count": 60})
    FakeProvider.text_by_id["6"] = "text for 6"
    # Incremental resumes from the page checkpoint rather than rescanning.
    report = run_provider(settings, settings.sources[0], storage)

    assert report.new_records == 1
    assert {b.source_id for b in read_catalog(storage, "gutenberg")} == {
        "1", "2", "3", "4", "5", "6"
    }


def test_changed_record_is_rewritten_and_latest_wins(settings, storage):
    run_provider(settings, settings.sources[0], storage)

    FakeProvider.catalog[0]["title"] = "Book 1 (revised)"
    settings.run.mode = "full"  # full mode rescans from page 1
    report = run_provider(settings, settings.sources[0], storage)

    assert report.changed_records == 1
    assert report.new_records == 0
    catalog = {b.source_id: b for b in read_catalog(storage, "gutenberg")}
    assert catalog["1"].title == "Book 1 (revised)"
    assert len(catalog) == 5


def test_budget_exhaustion_stops_cleanly_and_resumes(settings, storage):
    settings.run.max_requests_per_run = 1  # one page only, no text
    first = run_provider(settings, settings.sources[0], storage)
    assert first.new_records == 2
    assert first.text_pending == 2

    settings.run.max_requests_per_run = 50
    second = run_provider(settings, settings.sources[0], storage)

    catalog = read_catalog(storage, "gutenberg")
    assert len(catalog) == 5, "no duplicates across resumed runs"
    assert all(b.searchable_text for b in catalog), "pending text filled in on resume"
    assert second.text_fetched >= 2


def test_max_books_caps_a_run(settings, storage):
    """The cap is honoured to the end of the page it lands on, so it can overshoot
    by up to page_size-1 records. That is the price of a checkpoint that advances."""
    settings.run.max_books = 3
    report = run_provider(settings, settings.sources[0], storage)
    assert report.new_records == 4  # cap of 3 lands mid-page-2; page 2 is finished
    assert "max_books" in report.stopped_reason


def test_text_sub_budget_prioritizes_most_downloaded(settings, storage):
    settings.preview.text_requests_per_run = 2
    report = run_provider(settings, settings.sources[0], storage)

    assert report.text_fetched == 2
    assert report.text_pending == 3
    with_text = {b.source_id for b in read_catalog(storage, "gutenberg") if b.searchable_text}
    assert with_text == {"5", "4"}, "highest download_count served first"


def test_quota_exhaustion_during_text_is_not_an_error(settings, storage):
    FakeProvider.quota_out_after = 1
    report = run_provider(settings, settings.sources[0], storage)

    assert report.text_fetched == 1
    assert "exhausted" in report.stopped_reason.lower()
    # Metadata collected before the quota ran out is still persisted.
    assert len(read_catalog(storage, "gutenberg")) == 5


def test_one_bad_text_fetch_does_not_abort_the_stage(settings, storage):
    FakeProvider.fail_text_for = {"3"}
    report = run_provider(settings, settings.sources[0], storage)

    assert report.text_fetched == 4
    catalog = {b.source_id: b for b in read_catalog(storage, "gutenberg")}
    assert catalog["3"].searchable_text is None
    assert catalog["5"].searchable_text is not None


def test_dry_run_writes_nothing(settings, storage):
    report = run_provider(settings, settings.sources[0], storage, dry_run=True)

    assert report.new_records == 5
    assert "dry run" in report.stopped_reason
    assert storage.list_keys("books/") == []
    assert storage.read_json("_state/gutenberg.json") is None


def test_metadata_only_stage_skips_text(settings, storage):
    report = run_provider(settings, settings.sources[0], storage, stages=("metadata",))
    assert report.new_records == 5
    assert report.text_fetched == 0
    assert all(b.searchable_text is None for b in read_catalog(storage, "gutenberg"))


def test_report_summary_is_readable(settings, storage):
    report = run_provider(settings, settings.sources[0], storage)
    assert "gutenberg" in report.summary()
    assert "5 new" in report.summary()


def test_max_books_stops_on_a_page_boundary(settings, storage):
    """Stopping mid-page would freeze the checkpoint and make each later run
    re-read every earlier page -- quadratic request growth over a backfill."""
    settings.run.max_books = 3          # cap falls inside page 2 (page_size=2)
    report = run_provider(settings, settings.sources[0], storage)

    state = load_state(storage, "gutenberg")
    # The cap is honoured only to the end of the page it lands on.
    assert report.new_records >= 3
    assert state.last_page == 2, "checkpoint must advance past fully-read pages"
    assert "max_books" in report.stopped_reason


def test_capped_runs_advance_and_never_stall(settings, storage):
    """Successive capped runs must keep making progress, not re-read forever."""
    settings.run.max_books = 1
    seen_pages = []
    for _ in range(3):
        run_provider(settings, settings.sources[0], storage)
        seen_pages.append(load_state(storage, "gutenberg").last_page)

    assert seen_pages == sorted(seen_pages), "checkpoint must be monotonic"
    assert seen_pages[-1] > seen_pages[0], "runs must make forward progress"
    ids = [b.source_id for b in read_catalog(storage, "gutenberg")]
    assert len(ids) == len(set(ids)), "no duplicates across capped runs"

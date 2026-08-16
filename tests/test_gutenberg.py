import pytest

from pd_books.config import PreviewConfig
from pd_books.http import RequestBudget
from pd_books.models import TextSource
from pd_books.providers.gutenberg import GutenbergProvider
from pd_books.state import ProviderState

from .conftest import load_fixture


class StubClient:
    """Stands in for HttpClient, replaying fixtures and recording calls."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, path, params=None, budget=None):
        self.calls.append((path, params))
        if budget is not None:
            budget.spend(1)
        result = self.responses[len(self.calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def provider(source_config, monkeypatch):
    monkeypatch.setenv("TEST_RAPIDAPI_KEY", "test-key")
    return GutenbergProvider(source_config, PreviewConfig(chars=50))


def test_missing_api_key_is_a_clear_error(source_config, monkeypatch):
    monkeypatch.delenv("TEST_RAPIDAPI_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_RAPIDAPI_KEY"):
        GutenbergProvider(source_config, PreviewConfig())


def test_sends_rapidapi_auth_headers(provider):
    assert provider.client.headers["X-RapidAPI-Key"] == "test-key"
    assert provider.client.headers["X-RapidAPI-Host"] == (
        "project-gutenberg-books-api.p.rapidapi.com"
    )


def test_normalizes_a_record(provider):
    raw = load_fixture("books_page1.json")["results"][0]
    book = provider.to_book(raw).finalize()

    assert book.book_uid == "gutenberg:1342"
    assert book.source_id == "1342"
    assert book.title == "Pride and Prejudice"
    assert book.authors[0].name == "Austen, Jane"
    assert book.authors[0].source_author_id == "68"
    assert book.subjects == ["Courtship -- Fiction", "England -- Fiction", "Love stories"]
    # bookshelves become categories, keeping the two spec fields distinct
    assert book.categories == ["Best Books Ever Listings", "Harvard Classics"]
    assert book.identifiers == {"gutenberg_id": "1342"}
    assert book.is_public_domain is True
    assert book.download_count == 183505


def test_urls_come_from_formats_or_are_derived(provider):
    page = load_fixture("books_page1.json")["results"]
    with_plain = provider.to_book(page[0])
    assert with_plain.source_url == "https://www.gutenberg.org/ebooks/1342"
    # a real text/plain URL from `formats` wins
    assert with_plain.download_url == "https://www.gutenberg.org/files/1342/1342.txt"
    # no text/plain in formats -> fall back to the derived URL, still no extra request
    assert provider.to_book(page[1]).download_url == (
        "https://www.gutenberg.org/ebooks/84.txt.utf-8"
    )


def test_summary_becomes_searchable_text_for_free(provider):
    """The listing carries an editorial summary, so most books need no text request."""
    book = provider.to_book(load_fixture("books_page1.json")["results"][0]).finalize()
    assert book.text_source is TextSource.SUMMARY
    assert book.searchable_text.startswith('"Pride and Prejudice" by Jane Austen')
    assert book.text_char_count > 0


def test_missing_summary_leaves_text_unset_for_the_fallback_stage(provider):
    book = provider.to_book(load_fixture("books_page1.json")["results"][1]).finalize()
    assert book.searchable_text is None
    assert book.text_source is TextSource.NONE


def test_summary_truncated_to_configured_length(source_config, monkeypatch):
    monkeypatch.setenv("TEST_RAPIDAPI_KEY", "k")
    provider = GutenbergProvider(source_config, PreviewConfig(chars=20))
    book = provider.to_book(load_fixture("books_page1.json")["results"][0]).finalize()
    assert book.text_char_count == 20


def test_withdrawn_records_are_flagged_not_silently_dropped(provider):
    page = load_fixture("books_page1.json")["results"]
    assert provider.to_book(page[0]).withdrawn_reason is None
    assert provider.to_book(page[1]).withdrawn_reason == "Copyright claim received 2019-04-02"


def test_provider_params_are_sent_with_listing_requests(source_config, monkeypatch):
    monkeypatch.setenv("TEST_RAPIDAPI_KEY", "k")
    source_config.params = {"sort": "ascending"}
    provider = GutenbergProvider(source_config, PreviewConfig())
    provider.client = StubClient([load_fixture("books_page2.json")])
    list(provider.iter_records(ProviderState(provider="gutenberg"), RequestBudget(max_requests=5)))
    assert provider.client.calls[0][1]["sort"] == "ascending"


def test_reading_ease_parsed_from_prose_or_number(provider):
    page = load_fixture("books_page1.json")["results"]
    assert provider.to_book(page[0]).reading_ease_score == 69.2  # from prose string
    assert provider.to_book(page[1]).reading_ease_score == 72.5  # already numeric


def test_issued_date_parsed_in_both_formats(provider):
    page = load_fixture("books_page1.json")["results"]
    assert provider.to_book(page[0]).issued_at.year == 1998  # ISO timestamp
    assert provider.to_book(page[1]).issued_at.year == 1993  # plain date


def test_fields_absent_from_the_api_stay_null(provider):
    """Documented gap: this API exposes no language/publisher/year/ISBN."""
    book = provider.to_book(load_fixture("books_page1.json")["results"][0])
    assert book.publication_year is None
    assert book.language is None
    assert book.publisher is None
    assert "isbn" not in book.identifiers


def test_malformed_record_does_not_crash_normalization(provider):
    book = provider.to_book({"id": 7}).finalize()
    assert book.book_uid == "gutenberg:7"
    assert book.title == ""
    assert book.authors == []


def test_iter_records_follows_pagination(provider):
    provider.client = StubClient(
        [load_fixture("books_page1.json"), load_fixture("books_page2.json")]
    )
    state = ProviderState(provider="gutenberg")
    records = list(provider.iter_records(state, RequestBudget(max_requests=10)))

    assert [r["id"] for r in records] == [1342, 84, 2701]
    assert state.last_page == 2
    assert provider.client.calls[0][1]["page"] == 1
    assert provider.client.calls[1][1]["page"] == 2


def test_iter_records_resumes_by_rereading_the_last_page(provider):
    """The last page of a previous run is usually partial, and new books land
    there first, so resuming must re-read it rather than skip past it."""
    provider.client = StubClient([load_fixture("books_page2.json")])
    state = ProviderState(provider="gutenberg", last_page=2)
    list(provider.iter_records(state, RequestBudget(max_requests=10)))
    assert provider.client.calls[0][1]["page"] == 2


def test_iter_records_stops_cleanly_when_budget_runs_out(provider):
    provider.client = StubClient(
        [load_fixture("books_page1.json"), load_fixture("books_page2.json")]
    )
    state = ProviderState(provider="gutenberg")
    # Budget allows exactly one page; the second must stop without raising.
    records = list(provider.iter_records(state, RequestBudget(max_requests=1)))
    assert [r["id"] for r in records] == [1342, 84]
    assert state.last_page == 1


def test_preview_text_truncated_to_configured_length(provider):
    provider.client = StubClient([{"text": "x" * 500}])
    preview = provider.fetch_preview_text("1342", RequestBudget())
    assert preview is not None
    assert len(preview.text) == 50
    assert preview.source is TextSource.FIRST_CHARS


def test_preview_strips_surviving_gutenberg_boilerplate(provider):
    body = (
        "The Project Gutenberg eBook of Whatever\nLicense blurb here.\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK WHATEVER ***\n"
        "Real opening line of the book."
    )
    provider.client = StubClient([{"text": body}])
    preview = provider.fetch_preview_text("1", RequestBudget())
    assert preview.text.startswith("Real opening line")


def test_preview_returns_none_for_empty_text(provider):
    provider.client = StubClient([{"text": "   "}])
    assert provider.fetch_preview_text("1", RequestBudget()) is None


def test_preview_disabled_makes_no_request(source_config, monkeypatch):
    monkeypatch.setenv("TEST_RAPIDAPI_KEY", "k")
    provider = GutenbergProvider(source_config, PreviewConfig(enabled=False))
    provider.client = StubClient([])
    assert provider.fetch_preview_text("1", RequestBudget()) is None
    assert provider.client.calls == []

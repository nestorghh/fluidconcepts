"""Pipeline orchestration.

Two stages per run:

  1. **metadata** - page through the provider's catalog and normalize records.
  2. **text**     - spend a separate request budget on preview text, highest
                    ``download_count`` first, so the most-searched books get text
                    even when the budget only covers part of the catalog.

Both stages are budget-aware and checkpointed: exhausting the run budget or the
account quota is a clean stop, not a failure, and the next scheduled run resumes
from the saved state.

Writes are append-only. A record that changes across runs is written again with a
newer ``ingested_at``; use :func:`read_catalog` for the deduplicated latest-wins view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings, SourceConfig
from .http import BudgetExhausted, QuotaExhausted, RequestBudget
from .models import Book
from .providers.base import BaseProvider, build_provider
from .state import ProviderState, load_state, save_state
from .storage.base import BaseStorage, build_storage
from .storage.parquet import books_to_parquet_bytes, read_catalog

logger = logging.getLogger(__name__)

SHARD_KEY = "books/provider={provider}/ingest_date={date}/part-{index:04d}.parquet"
MANIFEST_KEY = "_manifest/{date}-{provider}.json"


@dataclass
class RunReport:
    provider: str
    new_records: int = 0
    changed_records: int = 0
    unchanged_records: int = 0
    text_fetched: int = 0
    text_pending: int = 0
    shards_written: int = 0
    rows_written: int = 0
    requests_used: int = 0
    quota_remaining: int | None = None
    stopped_reason: str = "completed"
    shard_keys: list[str] = field(default_factory=list)

    def summary(self) -> str:
        quota = f" | quota left {self.quota_remaining}" if self.quota_remaining is not None else ""
        return (
            f"[{self.provider}] {self.stopped_reason}: "
            f"{self.new_records} new, {self.changed_records} changed, "
            f"{self.unchanged_records} unchanged | text {self.text_fetched} fetched, "
            f"{self.text_pending} pending | {self.rows_written} rows in "
            f"{self.shards_written} shard(s) | {self.requests_used} requests{quota}"
        )


def _collect_metadata(
    provider: BaseProvider,
    state: ProviderState,
    budget: RequestBudget,
    settings: Settings,
    report: RunReport,
) -> list[Book]:
    """Stage 1: page the catalog and keep records that are new or changed."""
    collected: list[Book] = []
    max_books = settings.run.max_books
    #: Page the cap was reached on. The provider only advances ``state.last_page``
    #: once a page is fully consumed, so stopping mid-page would leave the checkpoint
    #: behind and force the next run to re-read every earlier page -- request cost
    #: growing quadratically across a long backfill. Finish the page, then stop.
    page_at_cap: int | None = None

    for raw in provider.iter_records(state, budget):
        # Checked before consuming the record, so we stop exactly at the boundary
        # rather than spilling one record into the following page.
        if page_at_cap is not None and state.last_page > page_at_cap:
            report.stopped_reason = f"reached max_books={max_books}"
            logger.info("stopping stage 'metadata': %s", report.stopped_reason)
            break

        try:
            book = provider.to_book(raw).finalize()
        except Exception:  # noqa: BLE001 - one malformed record must not kill the run
            logger.exception("failed to normalize record: %r", raw)
            continue

        if state.is_new_or_changed(book.source_id, book.content_hash):
            if book.source_id in state.seen:
                report.changed_records += 1
            else:
                report.new_records += 1
            collected.append(book)
        else:
            report.unchanged_records += 1

        if max_books is not None and len(collected) >= max_books and page_at_cap is None:
            page_at_cap = state.last_page

    return collected


def _enrich_text(
    provider: BaseProvider,
    books: list[Book],
    state: ProviderState,
    budget: RequestBudget,
    settings: Settings,
    report: RunReport,
) -> None:
    """Stage 2: fetch preview text, most-downloaded first, within its own sub-budget."""
    if not settings.preview.enabled:
        return

    targets = [b for b in books if not b.searchable_text]
    targets.sort(key=lambda b: b.download_count or 0, reverse=True)

    text_cap = settings.preview.text_requests_per_run
    spent = 0

    for book in targets:
        if text_cap is not None and spent >= text_cap:
            report.stopped_reason = f"text budget spent ({text_cap} requests)"
            break
        if not budget.can_spend(1):
            report.stopped_reason = "run budget spent during text stage"
            break
        try:
            preview = provider.fetch_preview_text(book.source_id, budget)
        except (BudgetExhausted, QuotaExhausted) as exc:
            report.stopped_reason = str(exc)
            logger.info("stopping stage 'text': %s", exc)
            break
        except Exception:  # noqa: BLE001 - a bad book must not abort the stage
            logger.exception("preview fetch failed for %s", book.book_uid)
            spent += 1
            continue

        spent += 1
        if preview:
            book.searchable_text = preview.text
            book.text_source = preview.source
            book.finalize()  # refresh text_char_count and content_hash
            report.text_fetched += 1

    still_missing = [b.source_id for b in books if not b.searchable_text]
    state.pending_text = still_missing
    report.text_pending = len(still_missing)


def _write_shards(
    books: list[Book],
    storage: BaseStorage,
    settings: Settings,
    provider_name: str,
    report: RunReport,
) -> None:
    """Write records as date-partitioned Parquet shards."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows_per_shard = max(1, settings.storage.rows_per_shard)
    existing = len(storage.list_keys(f"books/provider={provider_name}/ingest_date={date}/"))

    for offset in range(0, len(books), rows_per_shard):
        batch = books[offset : offset + rows_per_shard]
        key = SHARD_KEY.format(
            provider=provider_name, date=date, index=existing + report.shards_written
        )
        storage.write_bytes(key, books_to_parquet_bytes(batch, settings.storage.compression))
        report.shards_written += 1
        report.rows_written += len(batch)
        report.shard_keys.append(key)
        logger.info("wrote %d rows -> %s", len(batch), key)

    storage.write_json(
        MANIFEST_KEY.format(date=date, provider=provider_name),
        {
            "provider": provider_name,
            "ingest_date": date,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "shards": report.shard_keys,
            "rows": report.rows_written,
            "schema_version": books[0].schema_version if books else None,
        },
    )


def _load_pending_books(storage: BaseStorage, state: ProviderState) -> list[Book]:
    """Re-read records that earlier runs wrote without preview text.

    Costs no API quota: the metadata is already in storage, only the text stage
    still owes them a request.
    """
    if not state.pending_text:
        return []
    wanted = set(state.pending_text)
    pending = [
        b
        for b in read_catalog(storage, state.provider)
        if b.source_id in wanted and not b.searchable_text
    ]
    if pending:
        logger.info("resuming preview text for %d record(s) from previous runs", len(pending))
    return pending


def run_provider(
    settings: Settings,
    source: SourceConfig,
    storage: BaseStorage,
    dry_run: bool = False,
    stages: tuple[str, ...] = ("metadata", "text"),
) -> RunReport:
    """Run one provider end to end."""
    report = RunReport(provider=source.name)
    provider = build_provider(source, settings.preview)
    state = load_state(storage, source.name)

    if settings.run.mode == "full":
        state.reset_for_full_run()

    budget = RequestBudget(max_requests=settings.run.max_requests_per_run)

    try:
        books: list[Book] = []
        if "metadata" in stages:
            books = _collect_metadata(provider, state, budget, settings, report)
        if "text" in stages:
            # Records still owed text from earlier runs are enriched alongside new ones.
            books = _load_pending_books(storage, state) + books
            if books:
                _enrich_text(provider, books, state, budget, settings, report)
    except QuotaExhausted as exc:
        report.stopped_reason = f"quota exhausted: {exc}"
        logger.warning(report.stopped_reason)
    except BudgetExhausted as exc:
        report.stopped_reason = f"budget exhausted: {exc}"
        logger.info(report.stopped_reason)

    report.requests_used = budget.used
    report.quota_remaining = budget.quota_remaining

    if dry_run:
        report.stopped_reason += " (dry run: nothing written)"
        return report

    if books:
        _write_shards(books, storage, settings, source.name, report)
        for book in books:
            state.record(book.source_id, book.content_hash)

    state.quota_remaining = budget.quota_remaining
    state.quota_limit = budget.quota_limit
    save_state(storage, state)
    return report


def run(
    settings: Settings,
    dry_run: bool = False,
    stages: tuple[str, ...] = ("metadata", "text"),
) -> list[RunReport]:
    """Run every enabled provider."""
    storage = build_storage(settings.storage)
    reports = []
    for source in settings.enabled_sources():
        logger.info("starting provider %s (mode=%s)", source.name, settings.run.mode)
        reports.append(run_provider(settings, source, storage, dry_run=dry_run, stages=stages))
    return reports

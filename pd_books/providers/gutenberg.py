"""Project Gutenberg provider, backed by gutenbergapi.com (a RapidAPI gateway).

Endpoints used:
    GET /books                 paginated catalog listing
    GET /books/{id}/text       cleaned full text, truncated locally to a preview

Note: gutenbergapi.com's published examples document these as ``/api/books`` on host
``project-gutenberg-books-api.p.rapidapi.com``. Both are wrong -- verified against the
live API, the working host is ``project-gutenberg-free-books-api1.p.rapidapi.com`` and
the paths carry no ``/api`` prefix. The two failure modes are distinguishable:
``{"message":"API doesn't exists"}`` is the RapidAPI proxy rejecting the Host header,
``{"message":"Endpoint ... does not exist"}`` is the backend rejecting the path.

Fields the API does not expose (language, publisher, publication year, ISBN) stay
null by design; see README "Known data gaps". Download and landing URLs are derived
from the Gutenberg ID rather than fetched, which costs no quota.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from ..config import PreviewConfig, SourceConfig
from ..http import BudgetExhausted, HttpClient, QuotaExhausted, RequestBudget
from ..models import Author, Book, TextSource
from .base import BaseProvider, PreviewText, register_provider

logger = logging.getLogger(__name__)

BOOKS_PATH = "/books"
BOOK_TEXT_PATH = "/books/{id}/text"

GUTENBERG_EBOOK_URL = "https://www.gutenberg.org/ebooks/{id}"
GUTENBERG_TEXT_URL = "https://www.gutenberg.org/ebooks/{id}.txt.utf-8"

# Project Gutenberg's catalog is overwhelmingly public domain in the USA, but the
# API exposes no per-record copyright flag, so this is a provider-level default and
# is recorded as such in rights_statement rather than presented as verified fact.
DEFAULT_LICENSE = "Public domain in the USA"
RIGHTS_NOTE = (
    "Project Gutenberg collection-level default; not verified per record "
    "(source API exposes no copyright field)"
)

# Some Gutenberg metadata renders the score as prose, e.g.
# "Reading ease score: 69.2 (8th & 9th grade)."
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Safety net in case the API's "cleaned" text still carries the PG header.
_START_MARKER_RE = re.compile(
    r"\*\*\*\s*START OF TH(?:E|IS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.IGNORECASE | re.DOTALL
)


def _first_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _FLOAT_RE.search(str(value))
    return float(match.group()) if match else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        for parse in (datetime.fromisoformat, lambda s: datetime.strptime(s, "%Y-%m-%d")):
            try:
                dt = parse(text)
                break
            except ValueError:
                continue
        else:
            logger.debug("unparseable date: %r", value)
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


@register_provider
class GutenbergProvider(BaseProvider):
    name = "gutenberg"
    version = "gutenbergapi.com/v1"

    def __init__(self, config: SourceConfig, preview: PreviewConfig) -> None:
        super().__init__(config, preview)
        api_key = config.api_key
        if not api_key:
            raise ValueError(
                f"missing API key: set the {config.api_key_env} environment variable"
            )
        host = config.base_url.split("://", 1)[-1].split("/", 1)[0]
        self.client = HttpClient(
            base_url=config.base_url,
            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": host},
            timeout=config.request_timeout,
            max_retries=config.max_retries,
            min_request_interval=config.min_request_interval,
        )

    # --- stage 1: catalog listing ---

    def iter_records(self, state: Any, budget: RequestBudget) -> Iterator[dict[str, Any]]:
        """Page through the catalog, resuming from the checkpoint in ``state``.

        Stops cleanly when the run budget or the account quota is spent; the caller
        checkpoints and the next scheduled run picks up from ``state.last_page``.
        """
        # Resume *on* the last completed page, not after it: the final page of a
        # previous run is usually partially filled, and newly published books land
        # there first. Re-reading one page costs a single request, and the content
        # hash discards the records we already have.
        page = max(1, getattr(state, "last_page", 0))
        while True:
            try:
                payload = self.client.get_json(
                    BOOKS_PATH,
                    {
                        "page": page,
                        "page_size": self.config.page_size,
                        **self.config.params,
                    },
                    budget=budget,
                )
            except (BudgetExhausted, QuotaExhausted) as exc:
                logger.info("stopping stage 'metadata' at page %d: %s", page, exc)
                return

            results = payload.get("results") or []
            if not results:
                logger.info("catalog exhausted at page %d", page)
                return

            for raw in results:
                yield raw

            state.last_page = page
            if not payload.get("next"):
                logger.info("reached last page (%d)", page)
                return
            page += 1

    # --- normalization ---

    @staticmethod
    def _download_url(raw: dict[str, Any], source_id: str) -> str:
        """Prefer a real plain-text URL from ``formats``; fall back to the derived one."""
        formats = raw.get("formats") or {}
        if isinstance(formats, dict):
            for mime, url in formats.items():
                if str(mime).startswith("text/plain") and url:
                    return str(url)
        return GUTENBERG_TEXT_URL.format(id=source_id)

    def to_book(self, raw: dict[str, Any]) -> Book:
        source_id = str(raw.get("id"))
        authors = [
            Author(
                name=a.get("name", "") if isinstance(a, dict) else str(a),
                source_author_id=str(a["id"]) if isinstance(a, dict) and a.get("id") is not None else None,
                birth_year=a.get("birth_year") if isinstance(a, dict) else None,
                death_year=a.get("death_year") if isinstance(a, dict) else None,
            )
            for a in raw.get("authors") or []
        ]

        # The listing already carries an editorial summary for most books, which is
        # the spec's first-choice searchable text *and* costs no extra request. The
        # per-book /text endpoint is only a fallback for the records missing one.
        summary = (raw.get("summary") or "").strip()

        return Book(
            book_uid=Book.make_uid(self.name, source_id),
            source=self.name,
            source_id=source_id,
            identifiers={"gutenberg_id": source_id},
            title=raw.get("title") or "",
            alternative_title=raw.get("alternative_title"),
            authors=authors,
            # publication_year / language / publisher are not exposed by this API.
            subjects=_as_str_list(raw.get("subjects")),
            categories=_as_str_list(raw.get("bookshelves")),
            download_url=self._download_url(raw, source_id),
            source_url=GUTENBERG_EBOOK_URL.format(id=source_id),
            cover_image_url=raw.get("cover_image"),
            media_type=raw.get("media_type"),
            license=DEFAULT_LICENSE,
            is_public_domain=True,
            rights_statement=RIGHTS_NOTE,
            searchable_text=summary[: self.preview.chars] or None,
            text_source=TextSource.SUMMARY if summary else TextSource.NONE,
            download_count=raw.get("download_count"),
            reading_ease_score=_first_float(raw.get("reading_ease_score")),
            issued_at=_parse_datetime(raw.get("issued")),
            withdrawn_reason=(
                str(raw["removed_from_catalog"]) if raw.get("removed_from_catalog") else None
            ),
            provider_version=self.version,
        )

    # --- stage 2: preview text ---

    def fetch_preview_text(self, source_id: str, budget: RequestBudget) -> PreviewText | None:
        """Fetch cleaned text and truncate to ``preview.chars``.

        The API has no summary or description field, so the opening of the book is
        the only available searchable text.
        """
        if not self.preview.enabled:
            return None
        payload = self.client.get_json(
            BOOK_TEXT_PATH.format(id=source_id),
            {"cleaning_mode": self.preview.cleaning_mode},
            budget=budget,
        )
        text = (payload or {}).get("text") or ""
        if not text.strip():
            return None

        # If the boilerplate survived cleaning, drop everything before the marker.
        match = _START_MARKER_RE.search(text[:20000])
        if match:
            text = text[match.end() :]
        return PreviewText(text=text.strip()[: self.preview.chars], source=TextSource.FIRST_CHARS)

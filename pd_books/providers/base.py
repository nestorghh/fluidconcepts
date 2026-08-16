"""Provider interface.

``pipeline.py`` must never import a concrete provider: sources are resolved by name
from config, so adding HathiTrust / Internet Archive / Open Library later is one new
module plus a config entry, with no pipeline changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..config import PreviewConfig, SourceConfig
from ..http import RequestBudget
from ..models import Book, TextSource


@dataclass
class PreviewText:
    """Searchable text plus where it came from."""

    text: str
    source: TextSource


class BaseProvider(ABC):
    """A metadata source."""

    name: str = "base"
    version: str = "0"

    def __init__(self, config: SourceConfig, preview: PreviewConfig) -> None:
        self.config = config
        self.preview = preview

    @abstractmethod
    def iter_records(self, state: Any, budget: RequestBudget) -> Iterator[dict[str, Any]]:
        """Yield raw provider payloads. Must stop cleanly when the budget runs out."""

    @abstractmethod
    def to_book(self, raw: dict[str, Any]) -> Book:
        """Normalize one raw payload into the canonical schema."""

    def fetch_preview_text(self, source_id: str, budget: RequestBudget) -> PreviewText | None:
        """Optional stage-2 enrichment. Providers without a text endpoint skip this."""
        return None


_REGISTRY: dict[str, type[BaseProvider]] = {}


def register_provider(cls: type[BaseProvider]) -> type[BaseProvider]:
    _REGISTRY[cls.name] = cls
    return cls


def build_provider(config: SourceConfig, preview: PreviewConfig) -> BaseProvider:
    """Resolve a provider by config name."""
    from . import gutenberg  # noqa: F401 - import registers the built-in providers

    try:
        cls = _REGISTRY[config.name]
    except KeyError:
        raise ValueError(
            f"unknown provider {config.name!r}; available: {sorted(_REGISTRY)}"
        ) from None
    return cls(config, preview)

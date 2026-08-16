"""Per-provider checkpoint state.

Persisted as JSON next to the data so a run can be interrupted (budget spent, quota
gone, process killed) and resumed by the next scheduled invocation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .storage.base import BaseStorage

STATE_KEY = "_state/{provider}.json"


class ProviderState(BaseModel):
    """Everything needed to resume ingestion for one provider."""

    provider: str
    last_page: int = 0
    max_id_seen: int = 0
    total_records: int = 0
    #: source_id -> content_hash, for incremental change detection.
    seen: dict[str, str] = Field(default_factory=dict)
    #: source_ids that still need stage-2 preview text.
    pending_text: list[str] = Field(default_factory=list)
    last_run_at: datetime | None = None
    quota_remaining: int | None = None
    quota_limit: int | None = None

    def is_new_or_changed(self, source_id: str, content_hash: str) -> bool:
        return self.seen.get(source_id) != content_hash

    def record(self, source_id: str, content_hash: str) -> None:
        if source_id not in self.seen:
            self.total_records += 1
        self.seen[source_id] = content_hash
        if source_id.isdigit():
            self.max_id_seen = max(self.max_id_seen, int(source_id))

    def reset_for_full_run(self) -> None:
        """Full mode re-reads the catalog from the start but keeps hashes, so
        unchanged records are still recognized and not rewritten."""
        self.last_page = 0
        self.pending_text = []


def load_state(storage: BaseStorage, provider: str) -> ProviderState:
    payload = storage.read_json(STATE_KEY.format(provider=provider))
    if not payload:
        return ProviderState(provider=provider)
    return ProviderState.model_validate(payload)


def save_state(storage: BaseStorage, state: ProviderState) -> None:
    state.last_run_at = datetime.now(timezone.utc)
    storage.write_json(
        STATE_KEY.format(provider=state.provider),
        state.model_dump(mode="json"),
    )

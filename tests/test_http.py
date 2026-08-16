import io
import json
import urllib.error

import pytest

from pd_books.http import (
    BudgetExhausted,
    HttpClient,
    HttpError,
    QuotaExhausted,
    RequestBudget,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, headers=None, body=b"{}"):
    return urllib.error.HTTPError(
        "http://x", code, "err", headers or {}, io.BytesIO(body)
    )


@pytest.fixture
def client(monkeypatch):
    c = HttpClient(base_url="https://api.invalid", max_retries=2, backoff_cap=0)
    monkeypatch.setattr(c, "sleep", lambda _: None)
    return c


def test_budget_counts_and_blocks():
    budget = RequestBudget(max_requests=2)
    budget.spend()
    budget.spend()
    assert budget.used == 2 and budget.remaining == 0
    assert not budget.can_spend()
    with pytest.raises(BudgetExhausted):
        budget.spend()


def test_unlimited_budget():
    budget = RequestBudget(max_requests=None)
    for _ in range(100):
        budget.spend()
    assert budget.remaining is None and budget.can_spend()


def test_get_json_spends_budget_and_reads_quota_headers(client, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: FakeResponse(
            {"ok": True},
            {"x-ratelimit-requests-remaining": "97", "x-ratelimit-requests-limit": "100"},
        ),
    )
    budget = RequestBudget(max_requests=5)
    assert client.get_json("/api/books", budget=budget) == {"ok": True}
    assert budget.used == 1
    assert budget.quota_remaining == 97 and budget.quota_limit == 100


def test_retries_then_succeeds(client, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_error(503)
        return FakeResponse({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    budget = RequestBudget(max_requests=5)
    assert client.get_json("/api/books", budget=budget) == {"ok": True}
    assert calls["n"] == 3
    # Retries must not each charge the run budget.
    assert budget.used == 1


def test_quota_exhausted_is_distinct_from_rate_limit(client, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            http_error(429, {"x-ratelimit-requests-remaining": "0"})
        ),
    )
    with pytest.raises(QuotaExhausted):
        client.get_json("/api/books", budget=RequestBudget())


def test_rate_limit_with_quota_left_is_retried(client, monkeypatch):
    calls = {"n": 0}

    def throttled(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http_error(429, {"x-ratelimit-requests-remaining": "50", "Retry-After": "0"})
        return FakeResponse({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", throttled)
    assert client.get_json("/api/books", budget=RequestBudget()) == {"ok": True}
    assert calls["n"] == 2


def test_non_retryable_status_raises_immediately(client, monkeypatch):
    calls = {"n": 0}

    def forbidden(*a, **k):
        calls["n"] += 1
        raise http_error(403, body=b'{"message":"invalid key"}')

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(HttpError) as exc:
        client.get_json("/api/books", budget=RequestBudget())
    assert exc.value.status == 403
    assert calls["n"] == 1


def test_retries_exhausted_raises(client, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(500)),
    )
    with pytest.raises(HttpError):
        client.get_json("/api/books", budget=RequestBudget())


def test_url_building_skips_none_params(client):
    url = client._build_url("/api/books", {"page": 2, "page_size": None})
    assert url == "https://api.invalid/api/books?page=2"


def test_min_request_interval_paces_requests(monkeypatch):
    """Being throttled is costlier than pacing, so requests are spaced out."""
    slept: list[float] = []
    client = HttpClient(base_url="https://api.invalid", min_request_interval=5.0)
    monkeypatch.setattr(client, "sleep", slept.append)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse({"ok": True}))

    client.get_json("/books")   # first call: nothing to wait for
    client.get_json("/books")   # second call: must be paced
    assert slept and slept[0] > 0


def test_no_pacing_when_interval_is_zero(monkeypatch):
    slept: list[float] = []
    client = HttpClient(base_url="https://api.invalid", min_request_interval=0)
    monkeypatch.setattr(client, "sleep", slept.append)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse({"ok": True}))

    client.get_json("/books")
    client.get_json("/books")
    assert slept == []

#!/usr/bin/env python
"""Probe gutenbergapi.com to resolve the undocumented behaviour the pipeline depends on.

The public docs are a marketing page and the parameter reference sits behind RapidAPI's
login, so these answers have to come from the live API. Run this once with a real key
before the first large ingestion, then tune `config.yaml` from the findings.

    export RAPIDAPI_KEY=...
    .venv/bin/python scripts/probe_api.py

Spends roughly a dozen requests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pd_books.http import HttpClient, HttpError, QuotaExhausted, RequestBudget  # noqa: E402

BASE_URL = "https://project-gutenberg-free-books-api1.p.rapidapi.com"


#: Seconds to wait between probes. The gateway throttles aggressively on the free
#: plan, and back-to-back probes otherwise return 429 and tell us nothing.
DELAY = 12.0


def probe(client: HttpClient, budget: RequestBudget, label: str, path: str, params: dict):
    """Run one probe and summarize the outcome without dumping the whole payload."""
    time.sleep(DELAY)
    try:
        payload = client.get_json(path, params, budget=budget)
    except QuotaExhausted:
        print(f"  {label:<34} QUOTA EXHAUSTED - stopping")
        raise
    except HttpError as exc:
        print(f"  {label:<34} FAILED: {exc}")
        return None

    results = payload.get("results", []) if isinstance(payload, dict) else []
    count = payload.get("count") if isinstance(payload, dict) else None
    print(
        f"  {label:<34} ok  returned={len(results):<4} "
        f"count={count} next={'yes' if payload.get('next') else 'no'}"
    )
    return payload


def main() -> int:
    global DELAY

    parser = argparse.ArgumentParser()
    parser.add_argument("--key-env", default="RAPIDAPI_KEY")
    parser.add_argument("--delay", type=float, default=DELAY, help="seconds between probes")
    args = parser.parse_args()
    DELAY = args.delay

    api_key = os.environ.get(args.key_env)
    if not api_key:
        print(f"error: set {args.key_env} first", file=sys.stderr)
        return 2

    client = HttpClient(
        base_url=BASE_URL,
        headers={
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": BASE_URL.split("://", 1)[1],
        },
        max_retries=2,
    )
    budget = RequestBudget(max_requests=25)
    findings: dict[str, object] = {}

    try:
        print("\n1. Can the catalog be listed without a search query?")
        baseline = probe(client, budget, "no q", "/books", {"page_size": 5})
        findings["lists_without_query"] = bool(baseline and baseline.get("results"))
        if baseline:
            findings["total_count"] = baseline.get("count")
            sample = (baseline.get("results") or [{}])[0]
            findings["observed_fields"] = sorted(sample.keys())
            print(f"     fields present: {sorted(sample.keys())}")

        print("\n2. What is the maximum page_size?")
        for size in (25, 50, 100, 200, 500, 1000):
            payload = probe(client, budget, f"page_size={size}", "/books", {"page_size": size})
            if not payload:
                break
            got = len(payload.get("results", []))
            findings[f"page_size_{size}"] = got
            if got < size:
                print(f"     -> capped at {got}")
                findings["max_page_size"] = got
                break

        print("\n3. Is there a usable sort/ordering parameter?")
        for param, value in (
            ("sort", "ascending"), ("ordering", "id"), ("order_by", "id"), ("sort", "id"),
        ):
            payload = probe(
                client, budget, f"{param}={value}", "/books", {param: value, "page_size": 5}
            )
            if payload and payload.get("results"):
                ids = [r.get("id") for r in payload["results"]]
                ordered = ids == sorted(i for i in ids if i is not None)
                print(f"     ids={ids} ascending={ordered}")
                findings[f"sort_{param}_{value}"] = {"ids": ids, "ascending": ordered}

        print("\n4. Preview text endpoint shape")
        text_payload = probe(
            client, budget, "books/1342/text", "/books/1342/text", {"cleaning_mode": "simple"}
        )
        if isinstance(text_payload, dict):
            text = text_payload.get("text") or ""
            findings["text_endpoint"] = {
                "keys": sorted(text_payload.keys()),
                "length": len(text),
                "metadata": text_payload.get("metadata"),
            }
            print(f"     text length={len(text)} keys={sorted(text_payload.keys())}")
            print(f"     first 160 chars: {text[:160]!r}")

    except QuotaExhausted:
        findings["quota_exhausted_during_probe"] = True

    findings["quota_limit"] = budget.quota_limit
    findings["quota_remaining"] = budget.quota_remaining
    findings["requests_spent"] = budget.used

    print("\n" + "=" * 60)
    print(f"quota: limit={budget.quota_limit} remaining={budget.quota_remaining}")
    print(f"requests spent by this probe: {budget.used}")

    out = "docs/api-capabilities.json"
    os.makedirs("docs", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2, default=str)
    print(f"findings written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

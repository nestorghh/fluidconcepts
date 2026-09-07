"""Download the entire Gutenberg catalog to JSONL. Resumable.

Each page is flushed to disk as soon as it arrives, and the run records the last
completed page, so an interruption never wastes the API quota already spent.

    python scripts/download_all_gutenberg.py
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

HOST = "project-gutenberg-free-books-api1.p.rapidapi.com"
OUT = "data/experiment/gutenberg_all.jsonl"
STATE = "data/experiment/gutenberg_all.state.json"
PAGE_SIZE = 100
SLEEP = 1.5          # verified safe for an authenticated key
QUOTA_FLOOR = 15     # stop while a little quota is still left


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"last_page": 0, "records": 0}


def main():
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        print("error: set RAPIDAPI_KEY")
        return 1

    state = load_state()
    page = state["last_page"] + 1
    total = state["records"]
    if page > 1:
        print(f"resuming at page {page} ({total:,} records already saved)")

    out = open(OUT, "a", encoding="utf-8")
    t0 = time.time()
    quota = None

    while True:
        params = urllib.parse.urlencode(
            {"page": page, "page_size": PAGE_SIZE, "sort": "ascending"})
        req = urllib.request.Request(
            f"https://{HOST}/books?{params}",
            headers={"x-rapidapi-key": key, "x-rapidapi-host": HOST})

        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    quota = r.headers.get("x-ratelimit-requests-remaining")
                    payload = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    wait = 5 * (attempt + 1)
                    print(f"  429 on page {page}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                print(f"FAILED page {page}: HTTP {e.code}")
                out.close()
                return 1
            except Exception as e:
                if attempt < 5:
                    time.sleep(5)
                    continue
                print(f"FAILED page {page}: {e}")
                out.close()
                return 1

        results = payload.get("results") or []
        for rec in results:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        total += len(results)

        with open(STATE, "w") as f:
            json.dump({"last_page": page, "records": total}, f)

        if page % 25 == 0 or not payload.get("next"):
            rate = total / max(1, time.time() - t0)
            print(f"  page {page:>4} | {total:>7,} records | quota left {quota} "
                  f"| {rate:.0f} rec/s", flush=True)

        if not payload.get("next") or not results:
            print(f"\nDONE: {total:,} records in {page} pages, {time.time()-t0:.0f}s")
            break
        if quota is not None and int(quota) <= QUOTA_FLOOR:
            print(f"\nSTOPPING: quota floor reached ({quota} left). Re-run to resume.")
            break

        page += 1
        time.sleep(SLEEP)

    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

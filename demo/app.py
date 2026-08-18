"""Demo dashboard: browse downloaded books from every source in one table.

Usage:
    python demo/fetch_books.py 50                     # Gutenberg
    python demo/fetch_books.py 50 --source hathitrust # HathiTrust
    export RAPIDAPI_KEY=your-key                      # only for "load full text"
    python demo/app.py

Then open http://localhost:8000

Reads every demo/output/books_*.parquet it finds and merges them. Full text is only
available for Gutenberg (HathiTrust publishes metadata files, not a text API), and it
is proxied through this server so the API key never reaches the browser.
"""

import glob
import html
import http.server
import json
import os
import re
import socketserver
import urllib.parse
import urllib.request

import pandas as pd

HOST = "project-gutenberg-free-books-api1.p.rapidapi.com"
PORT = 8000
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
MAX_TEXT_CHARS = 40000

# Gutenberg texts open with a licence header; skip past it when it is there.
START_MARKER = re.compile(r"\*\*\* START OF TH.{0,60}?\*\*\*", re.IGNORECASE | re.DOTALL)

# Placeholders are substituted rather than .format()-ed, so the CSS and JS braces
# below can be written normally instead of being doubled everywhere.
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Public Domain Books</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #f6f7f9; color: #1a1a1a; }
  header { background: #fff; border-bottom: 1px solid #e3e5e8; padding: 20px 28px; }
  h1 { margin: 0 0 4px; font-size: 20px; }
  .stats { color: #6b7280; font-size: 13px; }
  .wrap { display: flex; gap: 20px; padding: 20px 28px; align-items: flex-start; }
  .list { flex: 1 1 55%; min-width: 0; }
  .detail { flex: 1 1 45%; position: sticky; top: 20px; max-height: 85vh; overflow-y: auto;
            background: #fff; border: 1px solid #e3e5e8; border-radius: 8px; padding: 20px; }
  .controls { display: flex; gap: 8px; margin-bottom: 12px; }
  input, select { padding: 10px 12px; font-size: 14px; border: 1px solid #d6d9de;
                  border-radius: 6px; background: #fff; }
  input { flex: 1; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #e3e5e8; border-radius: 8px; overflow: hidden; }
  th { text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
       color: #6b7280; padding: 10px 12px; border-bottom: 1px solid #e3e5e8; }
  td { padding: 10px 12px; border-bottom: 1px solid #f0f1f3; font-size: 14px;
       vertical-align: top; }
  tr.book { cursor: pointer; }
  tr.book:hover td { background: #eef4ff; }
  tr.book.active td { background: #dfeaff; }
  .muted { color: #6b7280; font-size: 13px; }
  .src { display: inline-block; padding: 2px 7px; border-radius: 4px;
         font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .src-gutenberg { background: #dcfce7; color: #166534; }
  .src-hathitrust { background: #e0e7ff; color: #3730a3; }
  .tag { display: inline-block; background: #eef0f3; border-radius: 4px;
         padding: 2px 7px; margin: 2px 3px 2px 0; font-size: 12px; color: #444; }
  button { background: #2563eb; color: #fff; border: 0; padding: 9px 14px;
           border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:disabled { background: #9aa4b2; cursor: default; }
  pre { white-space: pre-wrap; word-wrap: break-word; background: #fafbfc;
        border: 1px solid #eceef1; border-radius: 6px; padding: 14px;
        font-size: 13px; line-height: 1.55; max-height: 45vh; overflow-y: auto; }
  a { color: #2563eb; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 13px; margin: 12px 0; }
  dt { color: #6b7280; }
  dd { margin: 0; }
</style></head><body>
<header>
  <h1>Public Domain Books</h1>
  <div class="stats">__STATS__</div>
</header>
<div class="wrap">
  <div class="list">
    <div class="controls">
      <input id="q" placeholder="Filter by title, author or subject...">
      <select id="src">__SOURCE_OPTIONS__</select>
    </div>
    <table>
      <thead><tr><th>Source</th><th>ID</th><th>Title</th><th>Author</th><th>Info</th></tr></thead>
      <tbody id="rows">__ROWS__</tbody>
    </table>
    <p class="muted" id="shown"></p>
  </div>
  <div class="detail" id="detail"><p class="muted">Select a book to see its details.</p></div>
</div>

<script>
const BOOKS = __BOOKS__;

function applyFilter() {
  const q = document.getElementById('q').value.toLowerCase();
  const src = document.getElementById('src').value;
  let visible = 0;
  document.querySelectorAll('tr.book').forEach(tr => {
    const ok = tr.dataset.search.includes(q) && (src === 'all' || tr.dataset.source === src);
    tr.style.display = ok ? '' : 'none';
    if (ok) visible++;
  });
  document.getElementById('shown').textContent = visible + ' shown';
}
document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('src').addEventListener('change', applyFilter);

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const tags = (s) => (s || '').split('; ').filter(Boolean)
  .map(x => '<span class="tag">' + esc(x) + '</span>').join('');
const row = (label, value) => value ? '<dt>' + label + '</dt><dd>' + esc(value) + '</dd>' : '';

function show(uid) {
  document.querySelectorAll('tr.book').forEach(t => t.classList.toggle('active', t.dataset.uid === uid));
  const b = BOOKS[uid];
  let body = '<h2 style="margin:0 0 6px;font-size:17px">' + esc(b.title) + '</h2>' +
    '<div class="muted"><span class="src src-' + b.source + '">' + b.source + '</span> &middot; ' +
    esc(b.authors || 'Unknown author') + ' &middot; ID ' + esc(b.id) + '</div>';

  if (b.source === 'gutenberg') {
    body += '<p>' + (b.summary ? esc(b.summary) : '<span class="muted">No summary available.</span>') + '</p>' +
      '<div>' + tags(b.subjects) + '</div>' +
      '<div style="margin:10px 0">' + tags(b.bookshelves) + '</div>' +
      '<dl>' + row('Downloads', b.download_count) + row('Reading ease', b.reading_ease_score) +
      row('Released', (b.issued || '').slice(0, 10)) + '</dl>' +
      '<p><a href="' + b.source_url + '" target="_blank">Gutenberg page</a></p>' +
      '<button onclick="loadText(' + JSON.stringify(b.id) + ')" id="btn">Load full text from API</button>' +
      '<div id="text"></div>';
  } else {
    body += '<dl>' +
      row('Published', b.publication_year) + row('Publisher', b.publisher) +
      row('Place', b.pub_place) + row('Language', b.language) +
      row('Rights', b.rights + ' (' + b.access + ')') + row('Format', b.bib_fmt) +
      row('OCLC', b.oclc_num) + row('ISBN', b.isbn) + row('LCCN', b.lccn) +
      row('Held by', b.holding_library) + '</dl>' +
      '<p><a href="' + b.source_url + '" target="_blank">Read at HathiTrust</a> &middot; ' +
      '<a href="' + b.catalog_url + '" target="_blank">Catalog record</a></p>' +
      '<p class="muted">HathiTrust publishes bulk metadata files, not a text API, ' +
      'so full text is not fetchable here.</p>';
  }
  document.getElementById('detail').innerHTML = body;
}

async function loadText(id) {
  const btn = document.getElementById('btn');
  btn.disabled = true; btn.textContent = 'Loading from API...';
  try {
    const res = await fetch('/api/text/' + id);
    const data = await res.json();
    if (data.error) {
      document.getElementById('text').innerHTML = '<p class="muted">' + esc(data.error) + '</p>';
      btn.disabled = false; btn.textContent = 'Retry';
      return;
    }
    document.getElementById('text').innerHTML =
      '<p class="muted">Showing ' + data.shown.toLocaleString() + ' of ' +
      data.total.toLocaleString() + ' characters</p><pre>' + esc(data.text) + '</pre>';
    btn.textContent = 'Loaded';
  } catch (err) {
    document.getElementById('text').innerHTML = '<p class="muted">' + esc(err) + '</p>';
    btn.disabled = false; btn.textContent = 'Retry';
  }
}
applyFilter();
</script>
</body></html>"""


def load_books():
    """Read every books_*.parquet in the output dir and merge them into one list."""
    frames = []
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "books_*.parquet"))):
        df = pd.read_parquet(path)
        if "source" not in df.columns:
            name = os.path.basename(path)[len("books_"):-len(".parquet")]
            df["source"] = name
        frames.append(df)

    if not frames:
        return []

    # Sources have different columns; concat fills the gaps with NaN, which we
    # turn into None so the JSON sent to the browser has real nulls.
    df = pd.concat(frames, ignore_index=True)
    df["uid"] = df["source"].astype(str) + ":" + df["id"].astype(str)
    df = df.astype(object).where(pd.notna(df), None)
    return df.to_dict("records")


def build_page(books):
    sources = sorted({b["source"] for b in books})

    rows = []
    for b in books:
        search = " ".join(
            str(b.get(k) or "") for k in ("title", "authors", "subjects", "publisher")
        ).lower()
        # Gutenberg ranks by downloads; HathiTrust has no such signal, so show the year.
        info = b.get("download_count") if b["source"] == "gutenberg" else b.get("publication_year")
        uid = html.escape(str(b["uid"]), quote=True)
        rows.append(
            f'<tr class="book" data-uid="{uid}" data-source="{html.escape(b["source"], quote=True)}" '
            f'data-search="{html.escape(search, quote=True)}" onclick="show(&quot;{uid}&quot;)">'
            f'<td><span class="src src-{html.escape(b["source"])}">{html.escape(b["source"])}</span></td>'
            f'<td class="muted">{html.escape(str(b.get("id") or ""))}</td>'
            f'<td>{html.escape(str(b.get("title") or ""))}</td>'
            f'<td class="muted">{html.escape(str(b.get("authors") or ""))}</td>'
            f'<td class="muted">{html.escape(str(info or ""))}</td></tr>'
        )

    per_source = " &middot; ".join(
        f"{sum(1 for b in books if b['source'] == s)} from {s}" for s in sources
    )
    options = '<option value="all">All sources</option>' + "".join(
        f'<option value="{html.escape(s, quote=True)}">{html.escape(s)}</option>' for s in sources
    )

    return (
        PAGE.replace("__STATS__", f"{len(books)} records &middot; {per_source}")
        .replace("__SOURCE_OPTIONS__", options)
        .replace("__ROWS__", "".join(rows))
        .replace("__BOOKS__", json.dumps({b["uid"]: b for b in books}, default=str))
    )


def fetch_text(book_id):
    """Proxy the Gutenberg text endpoint so the key stays server-side."""
    api_key = os.environ.get("RAPIDAPI_KEY")
    if not api_key:
        return {"error": "RAPIDAPI_KEY is not set, so full text cannot be fetched."}

    params = urllib.parse.urlencode({"cleaning_mode": "simple"})
    request = urllib.request.Request(
        f"https://{HOST}/books/{book_id}/text?{params}",
        headers={"x-rapidapi-key": api_key, "x-rapidapi-host": HOST},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except Exception as exc:
        return {"error": f"API request failed: {exc}"}

    text = payload.get("text") or ""
    match = START_MARKER.search(text[:20000])
    if match:
        text = text[match.end():].lstrip()
    return {"text": text[:MAX_TEXT_CHARS], "total": len(text),
            "shown": min(len(text), MAX_TEXT_CHARS)}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, content_type):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/":
            self._send(build_page(load_books()), "text/html; charset=utf-8")
        elif self.path.startswith("/api/text/"):
            book_id = self.path.rsplit("/", 1)[-1]
            print(f"  fetching text for book {book_id} from API...")
            self._send(json.dumps(fetch_text(book_id)), "application/json")
        else:
            self.send_error(404)


def main():
    books = load_books()
    if not books:
        print(f"error: no books_*.parquet files in {OUTPUT_DIR}")
        print("Run this first:  python demo/fetch_books.py 50")
        return 1

    for source in sorted({b["source"] for b in books}):
        print(f"  {sum(1 for b in books if b['source'] == source):>5} records from {source}")
    if not os.environ.get("RAPIDAPI_KEY"):
        print("note: RAPIDAPI_KEY not set - browsing works, Gutenberg full text will not")
    print(f"\n  http://localhost:{PORT}\n\nCtrl-C to stop.")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

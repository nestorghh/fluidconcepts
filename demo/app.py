"""Demo dashboard: browse the downloaded books, and load full text from the API.

Usage:
    python demo/fetch_books.py 100     # download some books first
    export RAPIDAPI_KEY=your-key       # only needed for the "load full text" button
    python demo/app.py

Then open http://localhost:8000

Reads demo/output/books.parquet. The full-text button proxies through this server so
the API key never reaches the browser.
"""

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
DATA_FILE = os.path.join(OUTPUT_DIR, "books.parquet")
MAX_TEXT_CHARS = 40000

# Gutenberg texts open with a licence header; skip past it when it is there.
START_MARKER = re.compile(r"\*\*\* START OF TH.{0,60}?\*\*\*", re.IGNORECASE | re.DOTALL)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Public Domain Books</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #f6f7f9; color: #1a1a1a; }}
  header {{ background: #fff; border-bottom: 1px solid #e3e5e8; padding: 20px 28px; }}
  h1 {{ margin: 0 0 4px; font-size: 20px; }}
  .stats {{ color: #6b7280; font-size: 13px; }}
  .wrap {{ display: flex; gap: 20px; padding: 20px 28px; align-items: flex-start; }}
  .list {{ flex: 1 1 55%; min-width: 0; }}
  .detail {{ flex: 1 1 45%; position: sticky; top: 20px; max-height: 85vh; overflow-y: auto;
             background: #fff; border: 1px solid #e3e5e8; border-radius: 8px; padding: 20px; }}
  input {{ width: 100%; padding: 10px 12px; font-size: 14px; border: 1px solid #d6d9de;
           border-radius: 6px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #e3e5e8; border-radius: 8px; overflow: hidden; }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
        color: #6b7280; padding: 10px 12px; border-bottom: 1px solid #e3e5e8; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f0f1f3; font-size: 14px;
        vertical-align: top; }}
  tr.book {{ cursor: pointer; }}
  tr.book:hover td {{ background: #eef4ff; }}
  tr.book.active td {{ background: #dfeaff; }}
  .muted {{ color: #6b7280; font-size: 13px; }}
  .tag {{ display: inline-block; background: #eef0f3; border-radius: 4px;
          padding: 2px 7px; margin: 2px 3px 2px 0; font-size: 12px; color: #444; }}
  button {{ background: #2563eb; color: #fff; border: 0; padding: 9px 14px;
            border-radius: 6px; cursor: pointer; font-size: 14px; }}
  button:disabled {{ background: #9aa4b2; cursor: default; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; background: #fafbfc;
         border: 1px solid #eceef1; border-radius: 6px; padding: 14px;
         font-size: 13px; line-height: 1.55; max-height: 45vh; overflow-y: auto; }}
  a {{ color: #2563eb; }}
</style></head><body>
<header>
  <h1>Public Domain Books</h1>
  <div class="stats">{count} books &middot; {with_summary} with summaries &middot;
       source: Project Gutenberg</div>
</header>
<div class="wrap">
  <div class="list">
    <input id="q" placeholder="Filter by title, author or subject...">
    <table>
      <thead><tr><th>ID</th><th>Title</th><th>Author</th><th>Downloads</th></tr></thead>
      <tbody id="rows">{rows}</tbody>
    </table>
  </div>
  <div class="detail" id="detail"><p class="muted">Select a book to see its details.</p></div>
</div>

<script>
const BOOKS = {books_json};

document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('tr.book').forEach(tr => {{
    tr.style.display = tr.dataset.search.includes(q) ? '' : 'none';
  }});
}});

function show(id) {{
  document.querySelectorAll('tr.book').forEach(t => t.classList.toggle('active', t.dataset.id == id));
  const b = BOOKS[id];
  const tags = (s) => (s || '').split('; ').filter(Boolean)
                      .map(x => `<span class="tag">${{x}}</span>`).join('');
  document.getElementById('detail').innerHTML = `
    <h2 style="margin:0 0 6px;font-size:17px">${{b.title}}</h2>
    <div class="muted">${{b.authors || 'Unknown author'}} &middot;
         ${{b.download_count}} downloads &middot; ID ${{b.id}}</div>
    <p>${{b.summary || '<span class="muted">No summary available.</span>'}}</p>
    <div>${{tags(b.subjects)}}</div>
    <div style="margin:10px 0">${{tags(b.bookshelves)}}</div>
    <p class="muted">Reading ease: ${{b.reading_ease_score || 'n/a'}} &middot;
       Released: ${{(b.issued || '').slice(0,10)}} &middot;
       <a href="${{b.source_url}}" target="_blank">Gutenberg page</a></p>
    <button onclick="loadText(${{b.id}})" id="btn">Load full text from API</button>
    <div id="text"></div>`;
}}

async function loadText(id) {{
  const btn = document.getElementById('btn');
  btn.disabled = true; btn.textContent = 'Loading from API...';
  try {{
    const res = await fetch('/api/text/' + id);
    const data = await res.json();
    if (data.error) {{
      document.getElementById('text').innerHTML =
        `<p class="muted">${{data.error}}</p>`;
      btn.disabled = false; btn.textContent = 'Retry';
      return;
    }}
    document.getElementById('text').innerHTML =
      `<p class="muted">Showing ${{data.shown.toLocaleString()}} of
       ${{data.total.toLocaleString()}} characters</p><pre>${{data.text}}</pre>`;
    btn.textContent = 'Loaded';
  }} catch (err) {{
    document.getElementById('text').innerHTML = `<p class="muted">${{err}}</p>`;
    btn.disabled = false; btn.textContent = 'Retry';
  }}
}}
</script>
</body></html>"""


def load_books():
    df = pd.read_parquet(DATA_FILE)
    return df.where(pd.notna(df), None).to_dict("records")


def build_page(books):
    rows = []
    for b in books:
        search = " ".join(
            str(b.get(k) or "") for k in ("title", "authors", "subjects")
        ).lower()
        rows.append(
            f'<tr class="book" data-id="{b["id"]}" data-search="{html.escape(search, quote=True)}" '
            f'onclick="show({b["id"]})">'
            f'<td class="muted">{b["id"]}</td>'
            f'<td>{html.escape(str(b.get("title") or ""))}</td>'
            f'<td class="muted">{html.escape(str(b.get("authors") or ""))}</td>'
            f'<td class="muted">{b.get("download_count") or 0}</td></tr>'
        )
    return PAGE.format(
        count=len(books),
        with_summary=sum(1 for b in books if b.get("summary")),
        rows="".join(rows),
        books_json=json.dumps({b["id"]: b for b in books}, default=str),
    )


def fetch_text(book_id):
    """Proxy the API's text endpoint so the key stays server-side."""
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
    if not os.path.exists(DATA_FILE):
        print(f"error: {DATA_FILE} not found.")
        print("Run this first:  python demo/fetch_books.py 100")
        return 1

    books = load_books()
    print(f"Loaded {len(books)} books from {DATA_FILE}")
    if not os.environ.get("RAPIDAPI_KEY"):
        print("note: RAPIDAPI_KEY not set - browsing works, full text will not")
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

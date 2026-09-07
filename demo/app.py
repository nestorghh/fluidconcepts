"""Demo dashboard.

Two tabs:
  Catalog          - every books_*.parquet found, merged across sources
  Authorless (PD)  - public-domain books that carry no author at all

Usage:
    python demo/fetch_books.py 50                     # Gutenberg
    python demo/fetch_books.py 50 --source hathitrust # HathiTrust
    python scripts/extract_authorless.py              # builds the second tab's data
    export RAPIDAPI_KEY=your-key                      # only for "load full text"
    python demo/app.py                                # -> http://localhost:8000

Full text is Gutenberg-only (HathiTrust publishes metadata files, not a text API)
and is proxied through this server so the API key never reaches the browser.
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
AUTHORLESS = os.path.join(OUTPUT_DIR, "authorless_pd_books.parquet")
MAX_TEXT_CHARS = 40000
# The authorless set is ~386k rows; only a slice is sent to the browser. Charts are
# computed over the whole frame, so the summary stays accurate.
AUTHORLESS_SAMPLE = 500

FMT_NAMES = {
    "BK": "Book", "SE": "Serial", "MU": "Music/score", "MP": "Map",
    "MX": "Mixed materials", "VM": "Visual material", "CF": "Computer file",
    "XX": "Unknown",
}

TIMELINES = os.path.join(OUTPUT_DIR, "rights_timelines.json")
TIMELINE_SUMMARY = os.path.join(OUTPUT_DIR, "rights_timelines_summary.parquet")

# Rights codes grouped by what they mean for reuse: green = public domain,
# red/orange = restricted, blue = openly licensed, grey = undetermined.
RIGHTS_COLORS = {
    "pd": "#16a34a", "pdus": "#0d9488", "pd-pvt": "#86efac", "cc-zero": "#22c55e",
    "ic": "#dc2626", "icus": "#ea580c", "op": "#9333ea", "nobody": "#7f1d1d",
    "und": "#9ca3af", "und-world": "#6b7280", "ic-world": "#f87171",
}
DEFAULT_RIGHTS_COLOR = "#3b82f6"   # the cc-* family and anything new

START_MARKER = re.compile(r"\*\*\* START OF TH.{0,60}?\*\*\*", re.IGNORECASE | re.DOTALL)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Public Domain Books</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #f6f7f9; color: #1a1a1a; }
  header { background: #fff; border-bottom: 1px solid #e3e5e8; padding: 18px 28px 0; }
  h1 { margin: 0 0 4px; font-size: 20px; }
  .stats { color: #6b7280; font-size: 13px; margin-bottom: 12px; }
  .tabs { display: flex; gap: 4px; }
  .tab { padding: 9px 16px; border: 1px solid #e3e5e8; border-bottom: none;
         border-radius: 7px 7px 0 0; background: #f6f7f9; cursor: pointer;
         font-size: 14px; color: #4b5563; }
  .tab.active { background: #fff; color: #111; font-weight: 600;
                box-shadow: 0 1px 0 #fff; }
  .wrap { display: flex; gap: 20px; padding: 20px 28px; align-items: flex-start; }
  .list { flex: 1 1 55%; min-width: 0; }
  .detail { flex: 1 1 45%; position: sticky; top: 20px; max-height: 85vh; overflow-y: auto;
            background: #fff; border: 1px solid #e3e5e8; border-radius: 8px; padding: 20px; }
  .controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  input, select { padding: 10px 12px; font-size: 14px; border: 1px solid #d6d9de;
                  border-radius: 6px; background: #fff; }
  input { flex: 1; min-width: 200px; }
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
  .fmt { display: inline-block; padding: 2px 7px; border-radius: 4px;
         font-size: 11px; background: #f1f5f9; color: #334155; white-space: nowrap; }
  .fmt-BK { background: #fef3c7; color: #92400e; }
  .fmt-SE { background: #f3e8ff; color: #6b21a8; }
  .tag { display: inline-block; background: #eef0f3; border-radius: 4px;
         padding: 2px 7px; margin: 2px 3px 2px 0; font-size: 12px; color: #444; }
  button { background: #2563eb; color: #fff; border: 0; padding: 9px 14px;
           border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:disabled { background: #9aa4b2; cursor: default; }
  pre { white-space: pre-wrap; word-wrap: break-word; background: #fafbfc;
        border: 1px solid #eceef1; border-radius: 6px; padding: 14px;
        font-size: 13px; line-height: 1.55; max-height: 45vh; overflow-y: auto; }
  a { color: #2563eb; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px;
       font-size: 13px; margin: 12px 0; }
  dt { color: #6b7280; }
  dd { margin: 0; word-break: break-word; }
  .cards { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .card { background: #fff; border: 1px solid #e3e5e8; border-radius: 8px;
          padding: 14px 18px; min-width: 150px; }
  .card .n { font-size: 22px; font-weight: 600; }
  .card .l { font-size: 12px; color: #6b7280; text-transform: uppercase;
             letter-spacing: .04em; }
  .charts { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 18px; }
  .chart { background: #fff; border: 1px solid #e3e5e8; border-radius: 8px;
           padding: 16px 18px; flex: 1 1 340px; }
  .chart h3 { margin: 0 0 12px; font-size: 13px; text-transform: uppercase;
              letter-spacing: .04em; color: #6b7280; }
  .bar { display: grid; grid-template-columns: 92px 1fr 74px; align-items: center;
         gap: 8px; margin-bottom: 5px; font-size: 12px; }
  .bar .fill { background: #93b4f7; height: 15px; border-radius: 3px; min-width: 2px; }
  .bar .val { text-align: right; color: #6b7280; }
  .tl { display: grid; grid-template-columns: 260px 1fr 120px; gap: 12px;
        align-items: center; padding: 7px 10px; border-bottom: 1px solid #f0f1f3;
        cursor: pointer; }
  .tl:hover { background: #eef4ff; }
  .tl.active { background: #dfeaff; }
  .tl-title { font-size: 13px; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
  .tl-meta { font-size: 11px; color: #6b7280; text-align: right; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0 16px;
            font-size: 12px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
  .note { background: #fffbeb; border: 1px solid #fde68a; border-radius: 7px;
          padding: 11px 14px; font-size: 13px; color: #78350f; margin-bottom: 16px; }
</style></head><body>
<header>
  <h1>Public Domain Books</h1>
  <div class="stats">__STATS__</div>
  <div class="tabs">
    <div class="tab active" data-panel="catalog" onclick="showTab('catalog')">Catalog</div>
    <div class="tab" data-panel="authorless" onclick="showTab('authorless')">__AUTHORLESS_TAB__</div>
    <div class="tab" data-panel="timelines" onclick="showTab('timelines')">__TIMELINE_TAB__</div>
  </div>
</header>

<div id="panel-catalog">
  <div class="wrap">
    <div class="list">
      <div class="controls">
        <input id="q" placeholder="Filter by title, author or subject...">
        <select id="src">__SOURCE_OPTIONS__</select>
        <select id="fmt">__FORMAT_OPTIONS__</select>
      </div>
      <table>
        <thead><tr><th>Source</th><th>Format</th><th>ID</th><th>Title</th>
                   <th>Author</th><th>Info</th></tr></thead>
        <tbody id="rows">__ROWS__</tbody>
      </table>
      <p class="muted" id="shown"></p>
    </div>
    <div class="detail" id="detail"><p class="muted">Select a book to see its details.</p></div>
  </div>
</div>

<div id="panel-authorless" style="display:none">
  <div style="padding: 20px 28px;">
    __AUTHORLESS_BODY__
  </div>
</div>

<div id="panel-timelines" style="display:none">
  <div style="padding: 20px 28px;">
    __TIMELINE_BODY__
  </div>
</div>

<script>
const BOOKS = __BOOKS__;
const FMT_NAMES = __FMT_NAMES__;
const AUTHORLESS = __AUTHORLESS_ROWS__;

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.panel === name));
  ['catalog', 'authorless', 'timelines'].forEach(p => {
    const el = document.getElementById('panel-' + p);
    if (el) el.style.display = (p === name) ? '' : 'none';
  });
}

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const tags = (s) => (s || '').split('; ').filter(Boolean)
  .map(x => '<span class="tag">' + esc(x) + '</span>').join('');
const row = (label, value) =>
  (value !== null && value !== undefined && value !== '')
    ? '<dt>' + label + '</dt><dd>' + esc(value) + '</dd>' : '';

function applyFilter() {
  const q = document.getElementById('q').value.toLowerCase();
  const src = document.getElementById('src').value;
  const fmt = document.getElementById('fmt').value;
  let visible = 0;
  document.querySelectorAll('#rows tr.book').forEach(tr => {
    const ok = tr.dataset.search.includes(q)
      && (src === 'all' || tr.dataset.source === src)
      && (fmt === 'all' || tr.dataset.fmt === fmt);
    tr.style.display = ok ? '' : 'none';
    if (ok) visible++;
  });
  document.getElementById('shown').textContent = visible + ' shown';
}
document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('src').addEventListener('change', applyFilter);
document.getElementById('fmt').addEventListener('change', applyFilter);

function show(uid) {
  document.querySelectorAll('#rows tr.book').forEach(t =>
    t.classList.toggle('active', t.dataset.uid === uid));
  const b = BOOKS[uid];
  let body = '<h2 style="margin:0 0 6px;font-size:17px">' + esc(b.title) + '</h2>' +
    '<div class="muted"><span class="src src-' + b.source + '">' + b.source + '</span> &middot; ' +
    esc(b.authors || 'Unknown author') + ' &middot; ID ' + esc(b.id) + '</div>';

  if (b.source === 'gutenberg') {
    body += '<p>' + (b.summary ? esc(b.summary)
              : '<span class="muted">No summary available.</span>') + '</p>' +
      '<div>' + tags(b.subjects) + '</div>' +
      '<div style="margin:10px 0">' + tags(b.bookshelves) + '</div>' +
      '<dl>' + row('Downloads', b.download_count) +
      row('Reading ease', b.reading_ease_score) +
      row('Released', (b.issued || '').slice(0, 10)) + '</dl>' +
      '<p><a href="' + b.source_url + '" target="_blank">Gutenberg page</a></p>' +
      '<button onclick="loadText(' + JSON.stringify(b.id) + ')" id="btn">' +
      'Load full text from API</button><div id="text"></div>';
  } else {
    body += '<dl>' +
      row('Published', b.publication_year) + row('Publisher', b.publisher) +
      row('Place', b.pub_place) + row('Language', b.language) +
      row('Rights', b.rights + ' (' + b.access + ')') +
      row('Format', b.bib_fmt ? (FMT_NAMES[b.bib_fmt] || b.bib_fmt) + ' (' + b.bib_fmt + ')' : null) +
      row('OCLC', b.oclc_num) + row('ISBN', b.isbn) + row('LCCN', b.lccn) +
      row('Held by', b.holding_library) + '</dl>' +
      '<p><a href="' + b.source_url + '" target="_blank">Read at HathiTrust</a> &middot; ' +
      '<a href="' + b.catalog_url + '" target="_blank">Catalog record</a></p>' +
      '<p class="muted">HathiTrust publishes bulk metadata files, not a text API, ' +
      'so full text is not fetchable here.</p>';
  }
  document.getElementById('detail').innerHTML = body;
}

function showAuthorless(i) {
  const b = AUTHORLESS[i];
  document.querySelectorAll('#arows tr.book').forEach(t =>
    t.classList.toggle('active', t.dataset.i == i));
  let dl = '';
  for (const [k, v] of Object.entries(b)) {
    if (k === 'title') continue;
    dl += row(k, v);
  }
  document.getElementById('adetail').innerHTML =
    '<h2 style="margin:0 0 10px;font-size:17px">' + esc(b.title) + '</h2>' +
    '<p class="muted">Every column from the hathifile row:</p>' +
    '<dl>' + dl + '</dl>' +
    '<p><a href="' + b.reader_url + '" target="_blank">Read at HathiTrust</a> &middot; ' +
    '<a href="' + b.catalog_url + '" target="_blank">Catalog record</a></p>';
}

function afilter() {
  const q = document.getElementById('aq').value.toLowerCase();
  const lang = document.getElementById('alang').value;
  let visible = 0;
  document.querySelectorAll('#arows tr.book').forEach(tr => {
    const ok = tr.dataset.search.includes(q) && (lang === 'all' || tr.dataset.lang === lang);
    tr.style.display = ok ? '' : 'none';
    if (ok) visible++;
  });
  document.getElementById('ashown').textContent = visible + ' shown';
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
// ---- rights timelines ----
const TL = __TIMELINES__;
const RC = __RIGHTS_COLORS__;
const rcolor = (r) => RC[r] || "#3b82f6";

function tlSvg(w, minY, maxY) {
  const W = 100, H = 30, pad = 2;          // viewBox units; scales to container
  const span = Math.max(1, maxY - minY);
  const x = (ts) => pad + ((parseInt(ts.slice(0,4)) - minY) / span) * (W - 2*pad);
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:30px">`;
  svg += `<line x1="${pad}" y1="${H/2}" x2="${W-pad}" y2="${H/2}" stroke="#e3e5e8" stroke-width="0.7"/>`;
  // connect consecutive determinations so a change reads as a step
  for (let i = 1; i < w.events.length; i++) {
    const a = x(w.events[i-1].ts), b = x(w.events[i].ts);
    if (w.events[i-1].rights !== w.events[i].rights) {
      svg += `<line x1="${a}" y1="${H/2}" x2="${b}" y2="${H/2}" stroke="#94a3b8" stroke-width="1.4"/>`;
    }
  }
  for (const e of w.events) {
    svg += `<circle cx="${x(e.ts)}" cy="${H/2}" r="2.6" fill="${rcolor(e.rights)}"`
         + ` fill-opacity="0.85"><title>${e.rights} - ${e.ts} (${e.reason})</title></circle>`;
  }
  return svg + `</svg>`;
}

function renderTimelines() {
  const q = document.getElementById('tq').value.toLowerCase();
  const onlyPd = document.getElementById('tpd').checked;
  const minR = parseInt(document.getElementById('tmin').value);
  const shown = TL.filter(w =>
    (w.title + ' ' + (w.author||'')).toLowerCase().includes(q) &&
    (!onlyPd || w.flips_to_pd > 0) &&
    w.n_distinct_rights >= minR);

  let minY = 9999, maxY = 0;
  shown.forEach(w => w.events.forEach(e => {
    const y = parseInt(e.ts.slice(0,4));
    if (y) { if (y < minY) minY = y; if (y > maxY) maxY = y; }
  }));
  if (!shown.length) { minY = 2000; maxY = 2026; }

  document.getElementById('tcount').textContent =
    `${shown.length} works shown  |  axis ${minY}-${maxY}`;
  document.getElementById('tlist').innerHTML = shown.map((w, i) => `
    <div class="tl" data-i="${TL.indexOf(w)}" onclick="showTimeline(${TL.indexOf(w)})">
      <div class="tl-title" title="${esc(w.title)}">${esc(w.title)}
        <div class="muted" style="font-size:11px">${esc(w.author || '(no author)')}</div>
      </div>
      <div>${tlSvg(w, minY, maxY)}</div>
      <div class="tl-meta">${w.first_rights} &rarr; ${w.last_rights}<br>
        ${w.n_copies} copies &middot; ${w.span_years}y</div>
    </div>`).join('');
}

function showTimeline(i) {
  const w = TL[i];
  document.querySelectorAll('.tl').forEach(t =>
    t.classList.toggle('active', t.dataset.i == i));
  const rowsHtml = w.events.map(e => `<tr>
      <td class="muted">${esc(e.ts)}</td>
      <td><span class="dot" style="background:${rcolor(e.rights)}"></span> ${esc(e.rights)}</td>
      <td class="muted">${esc(e.reason)}</td>
      <td class="muted">${esc(e.access)}</td>
      <td class="muted">${esc(e.year)}</td>
      <td class="muted">${esc((e.imprint||'').slice(0,40))}</td>
      <td class="muted" style="font-size:11px">${esc(e.htid)}</td></tr>`).join('');
  document.getElementById('tdetail').innerHTML =
    `<h2 style="margin:0 0 4px;font-size:16px">${esc(w.title)}</h2>
     <div class="muted">${esc(w.author || '(no author)')}</div>
     <dl>${row('Copies', w.n_copies)}${row('Distinct rights', w.n_distinct_rights)}
       ${row('Transitions', w.n_transitions)}${row('Span (years)', w.span_years)}
       ${row('First', w.first_rights + '  ' + w.first_ts)}
       ${row('Latest', w.last_rights + '  ' + w.last_ts)}</dl>
     <p class="muted">Every determination, oldest first:</p>
     <table><thead><tr><th>Decided</th><th>Rights</th><th>Reason</th><th>Access</th>
       <th>Yr</th><th>Imprint</th><th>htid</th></tr></thead>
       <tbody>${rowsHtml}</tbody></table>`;
}

if (document.getElementById('tq')) {
  ['tq','tpd','tmin'].forEach(id =>
    document.getElementById(id).addEventListener('input', renderTimelines));
  renderTimelines();
}

applyFilter();
if (document.getElementById('aq')) {
  document.getElementById('aq').addEventListener('input', afilter);
  document.getElementById('alang').addEventListener('change', afilter);
  afilter();
}
</script>
</body></html>"""


def load_books():
    """Read every books_*.parquet in the output dir and merge them into one list."""
    frames = []
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "books_*.parquet"))):
        df = pd.read_parquet(path)
        if "source" not in df.columns:
            df["source"] = os.path.basename(path)[len("books_"):-len(".parquet")]
        frames.append(df)
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    df["uid"] = df["source"].astype(str) + ":" + df["id"].astype(str)
    df = df.astype(object).where(pd.notna(df), None)
    return df.to_dict("records")


def bar_chart(title, pairs, total):
    """Horizontal bars, sized as a share of the largest value."""
    if not pairs:
        return ""
    top = max(v for _, v in pairs) or 1
    rows = "".join(
        f'<div class="bar"><div>{html.escape(str(k))}</div>'
        f'<div><div class="fill" style="width:{100*v/top:.1f}%"></div></div>'
        f'<div class="val">{v:,} ({100*v/total:.1f}%)</div></div>'
        for k, v in pairs
    )
    return f'<div class="chart"><h3>{html.escape(title)}</h3>{rows}</div>'


def build_authorless():
    """Tab 2: summary + charts over the whole frame, table over a sample."""
    if not os.path.exists(AUTHORLESS):
        return ("Authorless (PD)",
                '<div class="note">No data yet. Build it with:<br>'
                '<code>python scripts/extract_authorless.py</code></div>', "[]")

    df = pd.read_parquet(AUTHORLESS)
    n = len(df)

    langs = df["lang"].value_counts().head(8)
    rights = df["rights"].value_counts()
    years = df["publication_year"].dropna()
    decades = ((years // 10) * 10).astype(int).value_counts().sort_index()
    decades = decades[decades.index >= 1700]

    cards = f"""
    <div class="cards">
      <div class="card"><div class="n">{n:,}</div><div class="l">distinct books</div></div>
      <div class="card"><div class="n">{df.n_copies.sum():,}</div><div class="l">library copies</div></div>
      <div class="card"><div class="n">{df.publication_year.notna().sum():,}</div>
           <div class="l">with a usable year</div></div>
      <div class="card"><div class="n">{df.lang.nunique():,}</div><div class="l">languages</div></div>
      <div class="card"><div class="n">{df.oclc_num.astype(bool).sum():,}</div>
           <div class="l">with an OCLC</div></div>
    </div>"""

    note = ('<div class="note"><b>What this is:</b> public-domain books '
            '(<code>bib_fmt=BK</code>, rights in pd/pdus/pd-pvt) whose <code>author</code> '
            'field is empty on every copy &mdash; anthologies, edited volumes and corporate or '
            'government publications. Charts cover all '
            f'{n:,} rows; the table below shows the {AUTHORLESS_SAMPLE} most-duplicated.</div>')

    charts = ('<div class="charts">'
              + bar_chart("Publication decade", [(str(k), int(v)) for k, v in decades.items()][-12:], n)
              + bar_chart("Language", [(k, int(v)) for k, v in langs.items()], n)
              + bar_chart("Rights code", [(k, int(v)) for k, v in rights.items()], n)
              + "</div>")

    sample = df.head(AUTHORLESS_SAMPLE).astype(object).where(pd.notna(df.head(AUTHORLESS_SAMPLE)), None)
    records = sample.to_dict("records")

    trs = []
    for i, r in enumerate(records):
        search = f"{r.get('title') or ''} {r.get('imprint') or ''} {r.get('htid') or ''}".lower()
        yr = r.get("publication_year")
        trs.append(
            f'<tr class="book" data-i="{i}" data-lang="{html.escape(str(r.get("lang") or ""), quote=True)}" '
            f'data-search="{html.escape(search, quote=True)}" onclick="showAuthorless({i})">'
            f'<td>{html.escape(str(r.get("title") or ""))[:110]}</td>'
            f'<td class="muted">{html.escape(str(r.get("imprint") or ""))[:52]}</td>'
            f'<td class="muted">{"" if yr is None else int(yr)}</td>'
            f'<td class="muted">{html.escape(str(r.get("lang") or ""))}</td>'
            f'<td class="muted">{r.get("n_copies")}</td></tr>'
        )

    lang_opts = '<option value="all">All languages</option>' + "".join(
        f'<option value="{html.escape(k, quote=True)}">{html.escape(k)} ({v:,})</option>'
        for k, v in langs.items()
    )

    body = f"""{note}{cards}{charts}
      <div class="wrap" style="padding:0">
        <div class="list">
          <div class="controls">
            <input id="aq" placeholder="Filter by title, imprint or htid...">
            <select id="alang">{lang_opts}</select>
          </div>
          <table>
            <thead><tr><th>Title</th><th>Imprint</th><th>Year</th><th>Lang</th>
                       <th>Copies</th></tr></thead>
            <tbody id="arows">{''.join(trs)}</tbody>
          </table>
          <p class="muted" id="ashown"></p>
        </div>
        <div class="detail" id="adetail">
          <p class="muted">Select a book to see all 31 columns.</p>
        </div>
      </div>"""

    return f"Authorless PD ({n:,})", body, json.dumps(records, default=str)


def build_timelines():
    """Tab 3: rights determination timelines for works whose copies disagree."""
    if not os.path.exists(TIMELINES):
        return ("Rights timelines",
                '<div class="note">No data yet. Build it with:<br>'
                '<code>python scripts/rights_timelines.py</code></div>', "[]")

    with open(TIMELINES) as fh:
        works = json.load(fh)

    n_total = len(works)
    summary_note = ""
    if os.path.exists(TIMELINE_SUMMARY):
        sdf = pd.read_parquet(TIMELINE_SUMMARY)
        cards = f"""
        <div class="cards">
          <div class="card"><div class="n">{len(sdf):,}</div>
               <div class="l">works with divergent rights</div></div>
          <div class="card"><div class="n">{(sdf.flips_to_pd > 0).sum():,}</div>
               <div class="l">ever flip into public domain</div></div>
          <div class="card"><div class="n">{(sdf.n_distinct_rights >= 3).sum():,}</div>
               <div class="l">3+ distinct rights values</div></div>
          <div class="card"><div class="n">{int(sdf.n_transitions.max()):,}</div>
               <div class="l">most transitions on one work</div></div>
          <div class="card"><div class="n">{n_total}</div>
               <div class="l">shown here</div></div>
        </div>"""
        summary_note = cards

    legend = '<div class="legend">' + "".join(
        f'<span><i class="dot" style="background:{c}"></i>{r}</span>'
        for r, c in RIGHTS_COLORS.items()
    ) + '<span><i class="dot" style="background:#3b82f6"></i>cc-* / other</span></div>'

    note = ('<div class="note"><b>Read this carefully.</b> A hathifile row stores one '
            'rights value and one <code>rights_timestamp</code> &mdash; the <i>current</i> '
            'determination for that volume. There is no history column. What you see below '
            'is the sequence of determinations HathiTrust made across the different '
            '<i>copies</i> of one work (same title + author), ordered by decision date. '
            'A line joining two dots marks a change in outcome between consecutive '
            'determinations &mdash; not a revision of a single volume\'s status.</div>')

    body = f"""{note}{summary_note}{legend}
      <div class="wrap" style="padding:0">
        <div class="list">
          <div class="controls">
            <input id="tq" placeholder="Filter by title or author...">
            <label class="muted" style="display:flex;align-items:center;gap:6px">
              <input type="checkbox" id="tpd" style="flex:none"> only works that reach public domain
            </label>
            <select id="tmin">
              <option value="2">2+ distinct rights</option>
              <option value="3" selected>3+ distinct rights</option>
              <option value="4">4+ distinct rights</option>
              <option value="5">5 distinct rights</option>
            </select>
          </div>
          <p class="muted" id="tcount"></p>
          <div style="background:#fff;border:1px solid #e3e5e8;border-radius:8px">
            <div id="tlist"></div>
          </div>
        </div>
        <div class="detail" id="tdetail">
          <p class="muted">Select a work to see every determination.</p>
        </div>
      </div>"""

    return f"Rights timelines ({n_total})", body, json.dumps(works, default=str)


def build_page(books):
    sources = sorted({b["source"] for b in books})
    rows = []
    for b in books:
        search = " ".join(
            str(b.get(k) or "") for k in ("title", "authors", "subjects", "publisher")
        ).lower()
        info = b.get("download_count") if b["source"] == "gutenberg" else b.get("publication_year")
        uid = html.escape(str(b["uid"]), quote=True)
        fmt = b.get("bib_fmt") or ""
        fmt_cell = (
            f'<span class="fmt fmt-{html.escape(fmt)}">{html.escape(FMT_NAMES.get(fmt, fmt))}</span>'
            if fmt else '<span class="muted">-</span>'
        )
        rows.append(
            f'<tr class="book" data-uid="{uid}" data-source="{html.escape(b["source"], quote=True)}" '
            f'data-fmt="{html.escape(fmt, quote=True)}" '
            f'data-search="{html.escape(search, quote=True)}" onclick="show(&quot;{uid}&quot;)">'
            f'<td><span class="src src-{html.escape(b["source"])}">{html.escape(b["source"])}</span></td>'
            f'<td>{fmt_cell}</td>'
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
    fmts = sorted({(b.get("bib_fmt") or "") for b in books})
    fmt_counts = {f: sum(1 for b in books if (b.get("bib_fmt") or "") == f) for f in fmts}
    fmt_options = '<option value="all">All formats</option>' + "".join(
        f'<option value="{html.escape(f, quote=True)}">'
        f'{html.escape(FMT_NAMES.get(f, f) if f else "(no format)")} ({fmt_counts[f]})</option>'
        for f in fmts
    )

    tab_label, authorless_body, authorless_json = build_authorless()
    tl_label, tl_body, tl_json = build_timelines()

    return (
        PAGE.replace("__STATS__", f"{len(books)} records &middot; {per_source}")
        .replace("__SOURCE_OPTIONS__", options)
        .replace("__FORMAT_OPTIONS__", fmt_options)
        .replace("__FMT_NAMES__", json.dumps(FMT_NAMES))
        .replace("__AUTHORLESS_TAB__", html.escape(tab_label))
        .replace("__AUTHORLESS_BODY__", authorless_body)
        .replace("__AUTHORLESS_ROWS__", authorless_json)
        .replace("__TIMELINE_TAB__", html.escape(tl_label))
        .replace("__TIMELINE_BODY__", tl_body)
        .replace("__TIMELINES__", tl_json)
        .replace("__RIGHTS_COLORS__", json.dumps(RIGHTS_COLORS))
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
    print(f"  authorless tab: {'yes' if os.path.exists(AUTHORLESS) else 'no (run scripts/extract_authorless.py)'}")
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

#!/usr/bin/env python3
"""Render TradingAgents run reports into a single self-contained HTML view.

Turns the markdown reports under ``results_dir/reports/<TICKER>_<stamp>/`` into
one browsable page: a summary strip (rating distribution), a sortable/filterable
table, and per-ticker drill-down into the full rendered analysis. Output is a
body-content HTML fragment (inline <style>/<script>, no <html>/<head>/<body>)
suitable for publishing directly as a claude.ai Artifact; it also renders fine
opened straight in a browser.

Rating parsing reuses the app's canonical parser so it handles every decision
format the engine has emitted. Report markdown is rendered with html disabled,
so model-generated text cannot inject markup into the page.

Usage:
    python render_reports.py [--reports-dir DIR] [--out FILE]
Defaults: reports dir from DEFAULT_CONFIG results_dir, out = <repo>/reports_view.html
"""

from __future__ import annotations

import argparse
import html
import re
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.default_config import DEFAULT_CONFIG

# 5-tier scale, most bullish to most bearish. Order matters: it drives the
# ordinal color ramp and the default sort.
RATING_ORDER = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
RATING_RANK = {r: i for i, r in enumerate(RATING_ORDER)}

_DIR_RE = re.compile(r"^(?P<ticker>.+)_(?P<stamp>\d{8}_\d{6})$")

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
_md.enable("table")


def _stamp_to_dt(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S")


def _field(text: str, label: str) -> str | None:
    """Best-effort extract '**Label**: value' (tolerant of markdown/colon)."""
    m = re.search(rf"\*\*{re.escape(label)}\*\*\s*[:\-]?\s*(.+)", text)
    return m.group(1).strip().strip("*") if m else None


def _price_target(text: str) -> str | None:
    raw = _field(text, "Price Target")
    if not raw:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", raw)
    return f"${m.group(1)}" if m else raw[:24]


def collect(reports_dir: Path) -> list[dict]:
    """Latest report per ticker, newest first, with parsed decision fields."""
    latest: dict[str, dict] = {}
    for d in sorted(reports_dir.glob("*_*"), reverse=True):
        if not d.is_dir():
            continue
        m = _DIR_RE.match(d.name)
        if not m:
            continue
        ticker = m.group("ticker").upper()
        dt = _stamp_to_dt(m.group("stamp"))
        if ticker in latest and latest[ticker]["dt"] >= dt:
            continue

        decision_f = d / "5_portfolio" / "decision.md"
        report_f = d / "complete_report.md"
        decision_txt = decision_f.read_text(encoding="utf-8") if decision_f.exists() else ""
        report_txt = report_f.read_text(encoding="utf-8") if report_f.exists() else ""

        rating = parse_rating(decision_txt) if decision_txt else "Hold"
        latest[ticker] = {
            "ticker": ticker,
            "dt": dt,
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "rating": rating,
            "rank": RATING_RANK.get(rating, 99),
            "price_target": _price_target(decision_txt) or "—",
            "horizon": _field(decision_txt, "Time Horizon") or "—",
            "summary": _field(decision_txt, "Executive Summary") or "",
            "report_html": _md.render(report_txt) if report_txt else
                           "<p class='empty'>No report body on disk.</p>",
        }
    return sorted(latest.values(), key=lambda r: (r["rank"], r["ticker"]))


def build_html(rows: list[dict]) -> str:
    n = len(rows)
    dist = dict.fromkeys(RATING_ORDER, 0)
    for row in rows:
        dist[row["rating"]] = dist.get(row["rating"], 0) + 1
    dates = [r["dt"] for r in rows]
    span = (f"{min(dates).strftime('%b %d')} – {max(dates).strftime('%b %d, %Y')}"
            if dates else "—")

    # rating distribution bar segments (only non-zero tiers)
    seg = "".join(
        f'<div class="seg r-{r.lower()}" style="flex:{dist[r]}" '
        f'title="{r}: {dist[r]}"></div>'
        for r in RATING_ORDER if dist[r]
    )
    legend = "".join(
        f'<button class="chip r-{r.lower()}" data-rating="{r}" aria-pressed="true">'
        f'<span class="dot"></span>{r}<span class="cnt">{dist[r]}</span></button>'
        for r in RATING_ORDER if dist[r]
    )

    trows = []
    for i, row in enumerate(rows):
        t = html.escape(row["ticker"])
        trows.append(f"""
      <tr class="sum" data-rating="{row['rating']}" data-idx="{i}" tabindex="0"
          aria-expanded="false" aria-controls="d{i}">
        <td class="tk"><span class="stripe r-{row['rating'].lower()}"></span>{t}</td>
        <td><span class="pill r-{row['rating'].lower()}">{row['rating']}</span></td>
        <td class="num">{html.escape(row['price_target'])}</td>
        <td class="hz">{html.escape(row['horizon'])}</td>
        <td class="dt">{html.escape(row['date'])}</td>
        <td class="chev" aria-hidden="true">▸</td>
      </tr>
      <tr class="detail" id="d{i}" hidden>
        <td colspan="6">
          <div class="report-md">
            {f'<p class="exec"><strong>Executive summary.</strong> {html.escape(row["summary"])}</p>' if row["summary"] else ''}
            {row['report_html']}
          </div>
        </td>
      </tr>""")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<style>
  :root {{
    --bg:#f7f8fa; --surface:#ffffff; --surface-2:#eef1f5; --border:#d9dee6;
    --text:#1a2230; --muted:#5b6675; --accent:#0e7c86;
    --buy:#127a4e; --overweight:#1a8f5c; --hold:#b7791f; --underweight:#c2410c; --sell:#c0392b;
    --shadow:0 1px 2px rgba(20,34,48,.06),0 4px 16px rgba(20,34,48,.05);
    --mono:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }}
  @media (prefers-color-scheme:dark) {{
    :root {{
      --bg:#0e1218; --surface:#161c26; --surface-2:#1e2632; --border:#2a3340;
      --text:#e6ebf2; --muted:#93a0b2; --accent:#3bb3bf;
      --buy:#2bb877; --overweight:#35c98a; --hold:#e0b341; --underweight:#f08a4b; --sell:#f0655a;
      --shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.35);
    }}
  }}
  :root[data-theme="light"] {{
    --bg:#f7f8fa; --surface:#ffffff; --surface-2:#eef1f5; --border:#d9dee6;
    --text:#1a2230; --muted:#5b6675; --accent:#0e7c86;
    --buy:#127a4e; --overweight:#1a8f5c; --hold:#b7791f; --underweight:#c2410c; --sell:#c0392b;
    --shadow:0 1px 2px rgba(20,34,48,.06),0 4px 16px rgba(20,34,48,.05);
  }}
  :root[data-theme="dark"] {{
    --bg:#0e1218; --surface:#161c26; --surface-2:#1e2632; --border:#2a3340;
    --text:#e6ebf2; --muted:#93a0b2; --accent:#3bb3bf;
    --buy:#2bb877; --overweight:#35c98a; --hold:#e0b341; --underweight:#f08a4b; --sell:#f0655a;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.35);
  }}
  * {{ box-sizing:border-box; }}
  .wrap {{ font-family:var(--sans); color:var(--text); background:var(--bg);
    max-width:1080px; margin:0 auto; padding:32px 24px 64px; line-height:1.5; }}
  .r-buy{{--c:var(--buy)}} .r-overweight{{--c:var(--overweight)}} .r-hold{{--c:var(--hold)}}
  .r-underweight{{--c:var(--underweight)}} .r-sell{{--c:var(--sell)}}

  header.top {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px; margin-bottom:4px; }}
  header.top h1 {{ font-size:1.5rem; font-weight:650; margin:0; letter-spacing:-.01em; text-wrap:balance; }}
  .sub {{ color:var(--muted); font-size:.85rem; }}
  .sub b {{ color:var(--text); font-weight:600; font-variant-numeric:tabular-nums; }}

  .distbar {{ display:flex; height:8px; border-radius:99px; overflow:hidden; margin:18px 0 14px;
    background:var(--surface-2); gap:2px; }}
  .seg {{ background:var(--c); min-width:3px; }}

  .legend {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:22px; }}
  .chip {{ display:inline-flex; align-items:center; gap:7px; font:inherit; font-size:.82rem;
    padding:5px 11px; border:1px solid var(--border); border-radius:99px; background:var(--surface);
    color:var(--text); cursor:pointer; transition:opacity .15s,border-color .15s; }}
  .chip .dot {{ width:9px; height:9px; border-radius:50%; background:var(--c); }}
  .chip .cnt {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
  .chip[aria-pressed="false"] {{ opacity:.38; }}
  .chip:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}

  .tools {{ display:flex; gap:12px; margin-bottom:14px; }}
  .tools input {{ flex:1; font:inherit; font-size:.9rem; padding:9px 13px; border-radius:9px;
    border:1px solid var(--border); background:var(--surface); color:var(--text); }}
  .tools input::placeholder {{ color:var(--muted); }}
  .tools input:focus-visible {{ outline:2px solid var(--accent); outline-offset:1px; border-color:var(--accent); }}

  table {{ width:100%; border-collapse:collapse; background:var(--surface);
    border:1px solid var(--border); border-radius:12px; box-shadow:var(--shadow); overflow:hidden; }}
  thead th {{ text-align:left; font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
    color:var(--muted); font-weight:600; padding:12px 14px; border-bottom:1px solid var(--border);
    cursor:pointer; user-select:none; white-space:nowrap; }}
  thead th.nosort {{ cursor:default; }}
  thead th .ind {{ opacity:.4; font-size:.7rem; }}
  tr.sum {{ border-top:1px solid var(--border); cursor:pointer; }}
  tr.sum:first-of-type {{ border-top:none; }}
  tr.sum:hover {{ background:var(--surface-2); }}
  tr.sum:focus-visible {{ outline:2px solid var(--accent); outline-offset:-2px; }}
  tr.sum td {{ padding:11px 14px; vertical-align:middle; }}
  td.tk {{ font-family:var(--mono); font-weight:600; font-size:.92rem; position:relative; padding-left:20px; }}
  .stripe {{ position:absolute; left:0; top:6px; bottom:6px; width:4px; border-radius:2px; background:var(--c); }}
  .num, .hz, .dt {{ font-variant-numeric:tabular-nums; color:var(--muted); font-size:.88rem; }}
  .num {{ font-family:var(--mono); color:var(--text); }}
  .pill {{ display:inline-block; font-size:.76rem; font-weight:600; padding:3px 10px; border-radius:99px;
    color:var(--c); background:color-mix(in srgb, var(--c) 14%, transparent);
    border:1px solid color-mix(in srgb, var(--c) 30%, transparent); white-space:nowrap; }}
  td.chev {{ text-align:right; color:var(--muted); transition:transform .15s; }}
  tr.sum[aria-expanded="true"] td.chev {{ transform:rotate(90deg); color:var(--accent); }}
  tr.detail td {{ padding:0; background:var(--bg); }}
  tr.detail .report-md {{ padding:20px 26px 28px; max-width:74ch; }}
  .exec {{ margin:.2rem 0 1.4rem; padding:12px 16px; border-left:3px solid var(--accent);
    background:var(--surface-2); border-radius:0 8px 8px 0; }}

  .report-md {{ font-size:.92rem; color:var(--text); }}
  .report-md h1,.report-md h2,.report-md h3,.report-md h4 {{ line-height:1.25; text-wrap:balance;
    margin:1.6em 0 .5em; }}
  .report-md h1 {{ font-size:1.25rem; }} .report-md h2 {{ font-size:1.1rem; }}
  .report-md h3 {{ font-size:1rem; }} .report-md h4 {{ font-size:.92rem; color:var(--muted); }}
  .report-md p {{ margin:.6em 0; }}
  .report-md strong {{ font-weight:650; }}
  .report-md ul,.report-md ol {{ padding-left:1.3em; margin:.5em 0; }}
  .report-md li {{ margin:.25em 0; }}
  .report-md code {{ font-family:var(--mono); font-size:.85em; background:var(--surface-2);
    padding:.1em .35em; border-radius:4px; }}
  .report-md table {{ display:block; overflow-x:auto; border-collapse:collapse; box-shadow:none;
    border:none; border-radius:0; margin:1em 0; font-size:.85rem; width:auto; max-width:100%; }}
  .report-md th,.report-md td {{ border:1px solid var(--border); padding:6px 10px; text-align:left;
    white-space:nowrap; }}
  .report-md th {{ background:var(--surface-2); font-weight:600; text-transform:none; letter-spacing:0;
    color:var(--text); cursor:default; }}
  .report-md hr {{ border:none; border-top:1px solid var(--border); margin:1.5em 0; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .nomatch {{ text-align:center; color:var(--muted); padding:28px; font-size:.9rem; }}
  footer {{ margin-top:22px; color:var(--muted); font-size:.78rem; text-align:center; }}
  @media (max-width:640px) {{ .hz,.dt,thead th.hz,thead th.dt {{ display:none; }}
    .wrap {{ padding:20px 12px 48px; }} }}
  @media (prefers-reduced-motion:reduce) {{ * {{ transition:none!important; }} }}
</style>

<div class="wrap">
  <header class="top">
    <h1>TradingAgents — Screen Results</h1>
    <span class="sub"><b>{n}</b> tickers · analyzed <b>{span}</b></span>
  </header>

  <div class="distbar" role="img" aria-label="Rating distribution">{seg}</div>
  <div class="legend">{legend}</div>

  <div class="tools">
    <input id="q" type="search" placeholder="Filter by ticker…" aria-label="Filter by ticker" autocomplete="off">
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th data-sort="ticker">Ticker <span class="ind"></span></th>
        <th data-sort="rank">Rating <span class="ind">▲</span></th>
        <th data-sort="target" class="num">Price target <span class="ind"></span></th>
        <th data-sort="horizon" class="hz">Horizon <span class="ind"></span></th>
        <th data-sort="date" class="dt">Analyzed <span class="ind"></span></th>
        <th class="nosort" aria-label="expand"></th>
      </tr>
    </thead>
    <tbody>{''.join(trows)}</tbody>
  </table>
  <div class="nomatch" id="nomatch" hidden>No tickers match.</div>

  <footer>Generated {generated} · click a row to read the full analysis · not investment advice</footer>
</div>

<script>
(function() {{
  const tbl = document.getElementById('tbl');
  const tbody = tbl.querySelector('tbody');
  const sums = () => [...tbody.querySelectorAll('tr.sum')];
  const active = new Set({RATING_ORDER!r}.filter(r => document.querySelector('.chip[data-rating="'+r+'"]')));
  let q = '';

  function apply() {{
    let shown = 0;
    sums().forEach(tr => {{
      const tk = tr.querySelector('.tk').textContent.trim().toLowerCase();
      const rating = tr.dataset.rating;
      const ok = active.has(rating) && tk.includes(q);
      tr.hidden = !ok;
      const det = document.getElementById('d' + tr.dataset.idx);
      if (!ok) {{ det.hidden = true; tr.setAttribute('aria-expanded','false'); }}
      if (ok) shown++;
    }});
    document.getElementById('nomatch').hidden = shown > 0;
  }}

  // expand / collapse
  tbody.addEventListener('click', e => {{
    const tr = e.target.closest('tr.sum'); if (!tr) return; toggle(tr);
  }});
  tbody.addEventListener('keydown', e => {{
    const tr = e.target.closest('tr.sum'); if (!tr) return;
    if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); toggle(tr); }}
  }});
  function toggle(tr) {{
    const det = document.getElementById('d' + tr.dataset.idx);
    const open = det.hidden;
    det.hidden = !open;
    tr.setAttribute('aria-expanded', open ? 'true' : 'false');
  }}

  // filter chips
  document.querySelectorAll('.chip').forEach(chip => {{
    chip.addEventListener('click', () => {{
      const r = chip.dataset.rating;
      if (active.has(r)) {{ active.delete(r); chip.setAttribute('aria-pressed','false'); }}
      else {{ active.add(r); chip.setAttribute('aria-pressed','true'); }}
      apply();
    }});
  }});

  // search
  document.getElementById('q').addEventListener('input', e => {{ q = e.target.value.trim().toLowerCase(); apply(); }});

  // sort
  let sortKey = 'rank', asc = true;
  tbl.querySelectorAll('thead th[data-sort]').forEach(th => {{
    th.addEventListener('click', () => {{
      const k = th.dataset.sort;
      asc = (k === sortKey) ? !asc : true;
      sortKey = k;
      tbl.querySelectorAll('thead .ind').forEach(s => s.textContent = '');
      th.querySelector('.ind').textContent = asc ? '▲' : '▼';
      const pairs = sums().map(tr => ({{ tr, det: document.getElementById('d'+tr.dataset.idx) }}));
      pairs.sort((a,b) => {{
        const A = key(a.tr, k), B = key(b.tr, k);
        return (A < B ? -1 : A > B ? 1 : 0) * (asc ? 1 : -1);
      }});
      pairs.forEach(p => {{ tbody.appendChild(p.tr); tbody.appendChild(p.det); }});
    }});
  }});
  function key(tr, k) {{
    if (k === 'ticker') return tr.querySelector('.tk').textContent.trim();
    if (k === 'rank') return +tr.dataset.idx; // rows are pre-sorted by rank; idx preserves it
    if (k === 'target') {{ const m = tr.querySelector('.num').textContent.replace(/[^0-9.]/g,''); return m ? parseFloat(m) : -1; }}
    if (k === 'horizon') return tr.querySelector('.hz').textContent.trim();
    if (k === 'date') return tr.querySelector('.dt').textContent.trim();
    return 0;
  }}
}})();
</script>"""


def main() -> None:
    default_reports = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
    repo = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Render TradingAgents reports to one HTML page.")
    ap.add_argument("--reports-dir", default=str(default_reports))
    ap.add_argument("--out", default=str(repo / "reports_view.html"))
    args = ap.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.exists():
        raise SystemExit(f"reports dir not found: {reports_dir}")

    rows = collect(reports_dir)
    if not rows:
        raise SystemExit(f"no reports found under {reports_dir}")

    Path(args.out).write_text(build_html(rows), encoding="utf-8")
    print(f"rendered {len(rows)} tickers -> {args.out}")
    for r in rows:
        print(f"  {r['ticker']:10} {r['rating']:12} target={r['price_target']:>10} {r['date']}")


if __name__ == "__main__":
    main()

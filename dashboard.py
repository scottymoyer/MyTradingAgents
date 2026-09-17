#!/usr/bin/env python3
"""Local Streamlit dashboard over the TradingAgents durable layer.

Reads the decisions ledger from **Postgres** (via ``results_store``) and reports +
the watchlist from **S3**. Designed to run on the EC2 box, which already has DB and
S3 access — no new infra or auth. Start it with:

    pip install ".[dashboard]"      # once: streamlit + boto3
    .venv/bin/streamlit run dashboard.py

Views:
  * **Runs** — every run, drill into its per-ticker decisions, render the S3 report.
  * **Performance** — mock_portfolio scorecard (per-tier hit-rate/return) + equity
    curve vs SPY.
  * **Watchlist** — edit ``config/watchlist.yaml`` in S3 in a grid; the next
    scheduled Fargate run pulls it (no file editing, no redeploy).

Read-only against the ledger; the only write is the watchlist save to S3.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml


# --- env: results_store picks Postgres from TRADINGAGENTS_DATABASE_URL, so load
# ~/.tradingagents.env before importing it (setdefault: never clobber a real env).
def _load_env() -> None:
    envf = Path.home() / ".tradingagents.env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

import mock_portfolio  # noqa: E402  (imported after env is loaded)
import results_store  # noqa: E402

BUCKET = os.environ.get("TRADINGAGENTS_S3_BUCKET", "tradingagents-963910217112-results")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
WATCHLIST_KEY = "config/watchlist.yaml"
ALLOWED_ASSET_TYPES = ("stock", "etf", "crypto")
_LONG = {"Buy", "Overweight"}

st.set_page_config(page_title="TradingAgents", page_icon="📈", layout="wide")


@st.cache_resource
def _s3():
    import boto3

    return boto3.client("s3", region_name=REGION)


@st.cache_data(ttl=60)
def _recent(limit: int = 2000) -> pd.DataFrame:
    return pd.DataFrame(results_store.recent(limit))


@st.cache_data(ttl=60)
def _fetch_report(key: str) -> str:
    obj = _s3().get_object(Bucket=BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8", "replace")


@st.cache_data(ttl=60)
def _list_report_files(prefix: str) -> list[str]:
    resp = _s3().list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return sorted(o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".md"))


def _report_key(report_path: str | None) -> str | None:
    """Map a ledger report_path (an absolute container/VM path) to its S3 key.
    Reports live under reports/<TICKER>_<stamp>/… on both disk and S3."""
    if not report_path:
        return None
    marker = "/reports/"
    if marker in report_path:
        return "reports/" + report_path.split(marker, 1)[1]
    p = report_path.lstrip("/")
    return p if p.startswith("reports/") else "reports/" + p


# ---------------------------------------------------------------- Runs view
def view_runs() -> None:
    st.header("Runs")
    df = _recent()
    if df.empty:
        st.info("No decisions recorded yet. Run the watchlist analysis to populate the ledger.")
        return

    # Top metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Decisions", len(df))
    c2.metric("Runs", df["run_id"].nunique())
    c3.metric("Latest run", str(df["trade_date"].max()))
    c4.metric("Resolved", int(df["realized_return"].notna().sum()))

    # One row per run
    runs = (
        df.groupby("run_id")
        .agg(
            trade_date=("trade_date", "first"),
            decisions=("ticker", "count"),
            longs=("decision", lambda s: int(s.isin(_LONG).sum())),
            failed=("status", lambda s: int((s == "failed").sum())),
            recorded_at=("recorded_at", "max"),
        )
        .reset_index()
        .sort_values("recorded_at", ascending=False)
    )
    st.subheader("All runs")
    st.dataframe(runs, use_container_width=True, hide_index=True)

    run_id = st.selectbox("Inspect a run", runs["run_id"].tolist())
    if not run_id:
        return
    sub = df[df["run_id"] == run_id].copy()
    cols = ["ticker", "asset_type", "decision", "status", "duration_s",
            "realized_return", "error"]
    show = sub[[c for c in cols if c in sub.columns]].sort_values("ticker")
    st.subheader(f"Decisions — {run_id}")
    st.dataframe(show, use_container_width=True, hide_index=True)

    # Drill into one ticker's report
    tickers = sub["ticker"].tolist()
    ticker = st.selectbox("View report for", tickers)
    row = sub[sub["ticker"] == ticker].iloc[0]
    key = _report_key(row.get("report_path"))
    if not key:
        st.warning("No report path recorded for this decision.")
        return
    prefix = key.rsplit("/", 1)[0] + "/"
    try:
        files = _list_report_files(prefix)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not list reports in s3://{BUCKET}/{prefix} ({e})")
        return
    if not files:
        st.warning(f"No report files found in s3://{BUCKET}/{prefix}")
        return
    # Default to the complete report if present.
    default = next((f for f in files if f.endswith("complete_report.md")), files[0])
    chosen = st.selectbox("Report section", files, index=files.index(default),
                          format_func=lambda k: k.rsplit("/", 1)[-1])
    try:
        st.markdown(_fetch_report(chosen))
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not fetch s3://{BUCKET}/{chosen} ({e})")


# --------------------------------------------------------- Performance view
def view_performance() -> None:
    st.header("Performance")
    st.caption("Long-only (Buy/Overweight), equal-weight, fixed 5-trading-day hold. "
               "Only decisions matured ≥5 trading days past their trade date are graded.")
    sc = mock_portfolio.scorecard()
    if not sc["n_resolved"]:
        st.info("No resolved decisions yet. Decisions grade 5 trading days after their "
                "trade_date — check back once the earliest run matures.")
        return
    sim = mock_portfolio.simulate()

    c1, c2, c3 = st.columns(3)
    c1.metric("Resolved decisions", sc["n_resolved"])
    c2.metric("Portfolio return", f"{sim['total_return'] * 100:+.2f}%")
    c3.metric("SPY (same windows)", f"{sim['bench_return'] * 100:+.2f}%",
              delta=f"{(sim['total_return'] - sim['bench_return']) * 100:+.2f}% vs SPY")

    st.subheader("Signal by rating tier")
    tier = pd.DataFrame(
        [{"tier": t, "n": v["n"], "avg_return_%": round(v["avg_return"] * 100, 2),
          "hit_rate_%": round(v["hit_rate"] * 100, 1)}
         for t, v in sc["by_tier"].items()]
    )
    st.dataframe(tier, use_container_width=True, hide_index=True)

    if sim["curve"]:
        st.subheader("Equity curve vs SPY")
        curve = pd.DataFrame(sim["curve"])
        chart = curve.set_index("trade_date")[["equity", "bench_equity"]].rename(
            columns={"equity": "Portfolio", "bench_equity": "SPY"})
        st.line_chart(chart)

    w, ln = st.columns(2)
    w.subheader("Top winners")
    w.dataframe(pd.DataFrame(sc["top_winners"])[["ticker", "decision", "realized_return"]],
                use_container_width=True, hide_index=True)
    ln.subheader("Top losers")
    ln.dataframe(pd.DataFrame(sc["top_losers"])[["ticker", "decision", "realized_return"]],
                 use_container_width=True, hide_index=True)


# ----------------------------------------------------------- Watchlist view
@st.cache_data(ttl=30)
def _load_watchlist() -> tuple[list[dict], str | None]:
    try:
        obj = _s3().get_object(Bucket=BUCKET, Key=WATCHLIST_KEY)
        data = yaml.safe_load(obj["Body"].read()) or {}
        last = str(obj.get("LastModified", ""))
        return list(data.get("watchlist", [])), last
    except _s3().exceptions.NoSuchKey:
        return [], None


def view_watchlist() -> None:
    st.header("Watchlist")
    st.caption(f"Edits save to s3://{BUCKET}/{WATCHLIST_KEY} and are picked up by the "
               "next scheduled Fargate run — no file editing or redeploy.")
    items, last = _load_watchlist()
    if last:
        st.caption(f"S3 object last modified: {last}")
    base = pd.DataFrame(items) if items else pd.DataFrame(
        columns=["ticker", "asset_type", "thesis", "tag"])
    for col in ("ticker", "asset_type", "thesis", "tag"):
        if col not in base.columns:
            base[col] = ""
    base = base[["ticker", "asset_type", "thesis", "tag"]]

    edited = st.data_editor(
        base, num_rows="dynamic", use_container_width=True, hide_index=True,
        column_config={
            "ticker": st.column_config.TextColumn("ticker", required=True),
            "asset_type": st.column_config.SelectboxColumn(
                "asset_type", options=list(ALLOWED_ASSET_TYPES), required=True),
            "thesis": st.column_config.TextColumn("thesis"),
            "tag": st.column_config.TextColumn("tag"),
        },
    )

    if st.button("Save to S3", type="primary"):
        rows, errors = [], []
        seen = set()
        for i, r in edited.iterrows():
            tk = str(r.get("ticker") or "").strip().upper()
            at = str(r.get("asset_type") or "").strip().lower()
            if not tk:
                continue
            if at not in ALLOWED_ASSET_TYPES:
                errors.append(f"row {i + 1} ({tk}): asset_type must be one of {ALLOWED_ASSET_TYPES}")
                continue
            if tk in seen:
                errors.append(f"duplicate ticker {tk}")
                continue
            seen.add(tk)
            rows.append({"ticker": tk, "asset_type": at,
                         "thesis": str(r.get("thesis") or "").strip(),
                         "tag": str(r.get("tag") or "").strip()})
        if errors:
            st.error("Not saved:\n\n- " + "\n- ".join(errors))
            return
        if not rows:
            st.error("Not saved: the watchlist is empty.")
            return
        body = yaml.safe_dump({"watchlist": rows}, sort_keys=False, allow_unicode=True)
        _s3().put_object(Bucket=BUCKET, Key=WATCHLIST_KEY,
                         Body=body.encode("utf-8"), ContentType="text/yaml")
        _load_watchlist.clear()
        st.success(f"Saved {len(rows)} tickers to s3://{BUCKET}/{WATCHLIST_KEY}")


# ------------------------------------------------------------------- shell
def main() -> None:
    st.sidebar.title("📈 TradingAgents")
    backend = "Postgres" if os.environ.get("TRADINGAGENTS_DATABASE_URL") else "SQLite (local)"
    st.sidebar.caption(f"Ledger: {backend}")
    st.sidebar.caption(f"S3: {BUCKET}")
    page = st.sidebar.radio("View", ["Runs", "Performance", "Watchlist"])
    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()
    {"Runs": view_runs, "Performance": view_performance, "Watchlist": view_watchlist}[page]()


if __name__ == "__main__":
    main()

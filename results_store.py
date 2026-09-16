#!/usr/bin/env python3
"""Durable, queryable ledger of run decisions (SQLite).

Every run appends one row per analyzed ticker to a small SQLite table so that
downstream layers -- scheduled runs, the mock-portfolio feedback loop, a
frontend -- can query decisions after the process exits and later attach realized
returns. The full markdown reports still live on disk under
``results_dir/reports/...``; this ledger stores the decision plus a pointer
(``report_path``) to that report, not the report body.

Design notes
------------
- One denormalized table ``decisions``, primary key ``(run_id, ticker)``, so
  re-recording a run upserts rather than duplicating.
- The outcome columns (``entry_price`` / ``realized_return`` / ``outcome_date``)
  are created now but left NULL; the feedback loop fills them later, so no
  migration is needed. The upsert deliberately does NOT touch those columns on
  conflict, so re-recording a decision never clobbers an attached outcome.
- run_ddog records once, single-threaded, after the worker pool joins, so no
  locking is required (unlike the concurrently-written memory log). A short-lived
  connection is opened per call.

CLI (query the ledger):
    python results_store.py                 # most recent decisions
    python results_store.py --ticker INTC   # history for one ticker
    python results_store.py --latest        # just the most recent run
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path.home() / ".tradingagents" / "results.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    run_id          TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    trade_date      TEXT,
    ticker          TEXT NOT NULL,
    asset_type      TEXT,
    group_name      TEXT,
    status          TEXT NOT NULL,     -- ok | failed | not_run
    decision        TEXT,              -- Buy/Overweight/Hold/Underweight/Sell, else NULL
    error           TEXT,
    report_path     TEXT,
    deep_model      TEXT,
    quick_model     TEXT,
    depth           TEXT,
    mode            TEXT,
    git_sha         TEXT,
    duration_s      REAL,
    -- outcome columns: NULL until the feedback loop fills them
    entry_price     REAL,
    realized_return REAL,
    outcome_date    TEXT,
    PRIMARY KEY (run_id, ticker)
);
"""

# Columns written on every record_run (excludes the outcome columns, which stay
# NULL on insert and are preserved on conflict).
_WRITE_COLUMNS = (
    "run_id", "recorded_at", "trade_date", "ticker", "asset_type", "group_name",
    "status", "decision", "error", "report_path",
    "deep_model", "quick_model", "depth", "mode", "git_sha", "duration_s",
)
# On conflict, refresh everything except the primary key (and never the outcomes).
_UPDATE_COLUMNS = tuple(c for c in _WRITE_COLUMNS if c not in ("run_id", "ticker"))


def _db_path(db_path=None) -> Path:
    """Resolve the ledger path: explicit arg > TRADINGAGENTS_RESULTS_DB > default."""
    if db_path:
        p = Path(db_path)
    else:
        env = os.environ.get("TRADINGAGENTS_RESULTS_DB")
        p = Path(env) if env else _DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def record_run(
    *,
    run_id: str,
    trade_date: str,
    mode: str,
    depth: str,
    deep_model: str,
    quick_model: str,
    git_sha: str | None,
    rows: list[dict],
    db_path=None,
) -> int:
    """Upsert one ledger row per ticker for a run. Returns the number written.

    Each element of ``rows`` is a dict with keys: ticker, asset_type, group_name,
    status, decision, error, report_path, duration_s (missing keys default to None).
    """
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = ", ".join(_WRITE_COLUMNS)
    placeholders = ", ".join(":" + c for c in _WRITE_COLUMNS)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _UPDATE_COLUMNS)
    sql = (
        f"INSERT INTO decisions ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(run_id, ticker) DO UPDATE SET {set_clause}"
    )
    conn = _connect(db_path)
    try:
        n = 0
        for r in rows:
            conn.execute(sql, {
                "run_id": run_id,
                "recorded_at": recorded_at,
                "trade_date": trade_date,
                "ticker": r["ticker"],
                "asset_type": r.get("asset_type"),
                "group_name": r.get("group_name"),
                "status": r["status"],
                "decision": r.get("decision"),
                "error": r.get("error"),
                "report_path": r.get("report_path"),
                "deep_model": deep_model,
                "quick_model": quick_model,
                "depth": depth,
                "mode": mode,
                "git_sha": git_sha,
                "duration_s": r.get("duration_s"),
            })
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _query(sql: str, params: tuple, db_path=None) -> list[dict]:
    conn = _connect(db_path)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def recent(limit: int = 50, db_path=None) -> list[dict]:
    return _query(
        "SELECT * FROM decisions ORDER BY recorded_at DESC, ticker ASC LIMIT ?",
        (limit,), db_path)


def by_ticker(ticker: str, limit: int = 50, db_path=None) -> list[dict]:
    return _query(
        "SELECT * FROM decisions WHERE ticker = ? ORDER BY recorded_at DESC LIMIT ?",
        (ticker.strip().upper(), limit), db_path)


def latest_run(db_path=None) -> list[dict]:
    # Order by rowid (insertion order) so this is deterministic even when two runs
    # share a recorded_at second.
    rows = _query("SELECT run_id FROM decisions ORDER BY rowid DESC LIMIT 1", (), db_path)
    if not rows:
        return []
    return _query(
        "SELECT * FROM decisions WHERE run_id = ? ORDER BY ticker ASC",
        (rows[0]["run_id"],), db_path)


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no decisions recorded yet)")
        return
    print(f"{'run_id':<26} {'ticker':<9} {'status':<7} {'decision':<12} {'dur':>6}  report")
    print("-" * 100)
    for r in rows:
        dur = f"{r['duration_s']:.0f}s" if r.get("duration_s") is not None else "-"
        dec = r.get("decision") or ("FAILED" if r.get("status") == "failed" else "-")
        rp = r.get("report_path") or ""
        print(f"{r['run_id']:<26} {r['ticker']:<9} {r['status']:<7} {str(dec):<12} {dur:>6}  {rp}")


def _main() -> None:
    ap = argparse.ArgumentParser(description="Query the TradingAgents decisions ledger.")
    ap.add_argument("--ticker", help="show history for one ticker")
    ap.add_argument("--latest", action="store_true", help="show only the most recent run")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    if args.latest:
        _print_table(latest_run())
    elif args.ticker:
        _print_table(by_ticker(args.ticker, args.limit))
    else:
        _print_table(recent(args.limit))


if __name__ == "__main__":
    _main()

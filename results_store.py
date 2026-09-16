#!/usr/bin/env python3
"""Durable, queryable ledger of run decisions.

Backend is chosen at runtime:
  * **PostgreSQL** when ``TRADINGAGENTS_DATABASE_URL`` is set (durable and
    network-queryable — what the EC2 cron and a future frontend use in production);
  * **SQLite** otherwise (the default for local dev and the test suite).

Both share one denormalized ``decisions`` table (PK ``(run_id, ticker)``) and the
same SQL; only the driver, the float DDL type, and the placeholder style differ.
Every run upserts one row per analyzed ticker so downstream layers -- scheduled runs,
the mock-portfolio feedback loop, a frontend -- can query decisions after the process
exits and later attach realized returns. Reports live elsewhere (disk / S3); this
stores the decision plus a ``report_path`` pointer, not the report body.

The outcome columns (``entry_price`` / ``realized_return`` / ``outcome_date``) start
NULL; the upsert's ON CONFLICT deliberately does not touch them, so re-recording a
decision never clobbers an attached outcome.

CLI:
    python results_store.py                 # most recent decisions
    python results_store.py --ticker INTC   # history for one ticker
    python results_store.py --latest        # just the most recent run
    python results_store.py --migrate PATH  # copy rows from a SQLite file into the
                                            # active backend (e.g. SQLite -> Postgres)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path.home() / ".tradingagents" / "results.db"

# SQLite and Postgres DDL differ only in the float type (REAL vs DOUBLE PRECISION).
_COLUMNS_DDL = """
    run_id          TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    trade_date      TEXT,
    ticker          TEXT NOT NULL,
    asset_type      TEXT,
    group_name      TEXT,
    status          TEXT NOT NULL,
    decision        TEXT,
    error           TEXT,
    report_path     TEXT,
    deep_model      TEXT,
    quick_model     TEXT,
    depth           TEXT,
    mode            TEXT,
    git_sha         TEXT,
    duration_s      {float},
    entry_price     {float},
    realized_return {float},
    outcome_date    TEXT,
    PRIMARY KEY (run_id, ticker)
"""
_SCHEMA_SQLITE = f"CREATE TABLE IF NOT EXISTS decisions ({_COLUMNS_DDL.format(float='REAL')});"
_SCHEMA_PG = f"CREATE TABLE IF NOT EXISTS decisions ({_COLUMNS_DDL.format(float='DOUBLE PRECISION')});"

# Columns written on every record_run (the outcome columns stay NULL on insert and
# are preserved on conflict).
_WRITE_COLUMNS = (
    "run_id", "recorded_at", "trade_date", "ticker", "asset_type", "group_name",
    "status", "decision", "error", "report_path",
    "deep_model", "quick_model", "depth", "mode", "git_sha", "duration_s",
)
_UPDATE_COLUMNS = tuple(c for c in _WRITE_COLUMNS if c not in ("run_id", "ticker"))
_ALL_COLUMNS = _WRITE_COLUMNS + ("entry_price", "realized_return", "outcome_date")


def _use_pg() -> bool:
    return bool(os.environ.get("TRADINGAGENTS_DATABASE_URL"))


def _backend(db_path=None) -> str:
    """Postgres only when NO explicit db_path is given AND the URL is set. An explicit
    db_path always forces SQLite — so tests and local callers are immune to an ambient
    TRADINGAGENTS_DATABASE_URL and can never accidentally touch the production DB."""
    return "pg" if (db_path is None and _use_pg()) else "sqlite"


def _q(sql: str, db_path=None) -> str:
    """Queries use '?' as the positional placeholder; Postgres (psycopg) wants '%s'."""
    return sql.replace("?", "%s") if _backend(db_path) == "pg" else sql


def _db_path(db_path=None) -> Path:
    """SQLite path: explicit arg > TRADINGAGENTS_RESULTS_DB > default."""
    if db_path:
        p = Path(db_path)
    else:
        env = os.environ.get("TRADINGAGENTS_RESULTS_DB")
        p = Path(env) if env else _DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(db_path=None):
    """Open a connection to the active backend with the schema ensured. Rows come
    back as dicts from both backends."""
    if _backend(db_path) == "pg":
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(os.environ["TRADINGAGENTS_DATABASE_URL"], row_factory=dict_row)
        conn.execute(_SCHEMA_PG)
        conn.commit()
        return conn
    conn = sqlite3.connect(str(_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQLITE)
    return conn


def record_run(*, run_id, trade_date, mode, depth, deep_model, quick_model,
               git_sha, rows, db_path=None) -> int:
    """Upsert one ledger row per ticker for a run. Returns the number written.

    Each element of ``rows`` is a dict with keys: ticker, asset_type, group_name,
    status, decision, error, report_path, duration_s (missing keys default to None).
    """
    recorded_at = datetime.now(timezone.utc).isoformat()  # microsecond precision
    cols = ", ".join(_WRITE_COLUMNS)
    placeholders = ", ".join("?" for _ in _WRITE_COLUMNS)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _UPDATE_COLUMNS)
    sql = _q(f"INSERT INTO decisions ({cols}) VALUES ({placeholders}) "
             f"ON CONFLICT (run_id, ticker) DO UPDATE SET {set_clause}", db_path)
    conn = _connect(db_path)
    try:
        n = 0
        for r in rows:
            fields = {
                "run_id": run_id, "recorded_at": recorded_at, "trade_date": trade_date,
                "ticker": r["ticker"], "asset_type": r.get("asset_type"),
                "group_name": r.get("group_name"), "status": r["status"],
                "decision": r.get("decision"), "error": r.get("error"),
                "report_path": r.get("report_path"), "deep_model": deep_model,
                "quick_model": quick_model, "depth": depth, "mode": mode,
                "git_sha": git_sha, "duration_s": r.get("duration_s"),
            }
            conn.execute(sql, tuple(fields[c] for c in _WRITE_COLUMNS))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _query(sql: str, params: tuple = (), db_path=None) -> list[dict]:
    conn = _connect(db_path)
    try:
        return [dict(row) for row in conn.execute(_q(sql, db_path), params).fetchall()]
    finally:
        conn.close()


def recent(limit: int = 50, db_path=None) -> list[dict]:
    return _query("SELECT * FROM decisions ORDER BY recorded_at DESC, ticker ASC LIMIT ?",
                  (limit,), db_path)


def by_ticker(ticker: str, limit: int = 50, db_path=None) -> list[dict]:
    return _query("SELECT * FROM decisions WHERE ticker = ? ORDER BY recorded_at DESC LIMIT ?",
                  (ticker.strip().upper(), limit), db_path)


def latest_run(db_path=None) -> list[dict]:
    # recorded_at is microsecond-precision, so the max identifies the most recent run
    # (portable across SQLite/Postgres — no reliance on SQLite's rowid).
    rows = _query("SELECT run_id FROM decisions ORDER BY recorded_at DESC LIMIT 1", (), db_path)
    if not rows:
        return []
    return _query("SELECT * FROM decisions WHERE run_id = ? ORDER BY ticker ASC",
                  (rows[0]["run_id"],), db_path)


def unresolved_ok_decisions(db_path=None) -> list[dict]:
    """Decisions that succeeded but have no realized return yet (grading candidates)."""
    return _query("SELECT * FROM decisions WHERE status = 'ok' AND realized_return IS NULL "
                  "ORDER BY trade_date ASC, ticker ASC", (), db_path)


def resolved_decisions(db_path=None) -> list[dict]:
    """Decisions with a realized return attached (for the portfolio/scorecard)."""
    return _query("SELECT * FROM decisions WHERE realized_return IS NOT NULL "
                  "ORDER BY trade_date ASC, ticker ASC", (), db_path)


def attach_outcome(run_id: str, ticker: str, *, entry_price, realized_return,
                   outcome_date, db_path=None) -> None:
    """Fill the outcome columns for one decision (idempotent overwrite)."""
    conn = _connect(db_path)
    try:
        conn.execute(_q("UPDATE decisions SET entry_price = ?, realized_return = ?, "
                        "outcome_date = ? WHERE run_id = ? AND ticker = ?", db_path),
                     (entry_price, realized_return, outcome_date, run_id, ticker))
        conn.commit()
    finally:
        conn.close()


def migrate(from_db_path) -> int:
    """Copy all rows from a SQLite ledger file into the active backend (all columns,
    including outcomes), via idempotent upsert. Use to move SQLite -> Postgres."""
    src = sqlite3.connect(str(from_db_path))
    src.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in src.execute("SELECT * FROM decisions").fetchall()]
    finally:
        src.close()
    if not rows:
        return 0
    cols = ", ".join(_ALL_COLUMNS)
    placeholders = ", ".join("?" for _ in _ALL_COLUMNS)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _ALL_COLUMNS if c not in ("run_id", "ticker"))
    sql = _q(f"INSERT INTO decisions ({cols}) VALUES ({placeholders}) "
             f"ON CONFLICT (run_id, ticker) DO UPDATE SET {set_clause}", None)
    conn = _connect()
    try:
        for r in rows:
            conn.execute(sql, tuple(r.get(c) for c in _ALL_COLUMNS))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


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
    ap = argparse.ArgumentParser(description="Query/migrate the TradingAgents decisions ledger.")
    ap.add_argument("--ticker", help="show history for one ticker")
    ap.add_argument("--latest", action="store_true", help="show only the most recent run")
    ap.add_argument("--migrate", metavar="SQLITE_PATH",
                    help="copy rows from a SQLite ledger file into the active backend")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    if args.migrate:
        n = migrate(args.migrate)
        print(f"migrated {n} row(s) into {'postgres' if _use_pg() else 'sqlite'}")
    elif args.latest:
        _print_table(latest_run())
    elif args.ticker:
        _print_table(by_ticker(args.ticker, args.limit))
    else:
        _print_table(recent(args.limit))


if __name__ == "__main__":
    _main()

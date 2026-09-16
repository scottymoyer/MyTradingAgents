"""results_store SQLite ledger: round-trip, idempotent upsert, outcome-column
preservation on re-record, failed-row capture, and schema auto-create.

Each test uses a tmp DB via the db_path arg — no shared state, no real run.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import results_store  # noqa: E402

pytestmark = pytest.mark.unit


def _run(db, run_id="2026-09-16__120000", rows=None):
    return results_store.record_run(
        run_id=run_id, trade_date="2026-09-16", mode="watchlist", depth="shallow",
        deep_model="openai/gpt-oss-120b", quick_model="openai/gpt-oss-20b",
        git_sha="abc123", rows=rows or [], db_path=db,
    )


def test_round_trip_ok_and_failed(tmp_path):
    db = tmp_path / "results.db"
    n = _run(db, rows=[
        {"ticker": "INTC", "asset_type": "stock", "group_name": "watchlist",
         "status": "ok", "decision": "Overweight", "report_path": "/p/INTC.md", "duration_s": 12.3},
        {"ticker": "PURR", "asset_type": "stock", "group_name": "watchlist",
         "status": "failed", "error": "boom", "duration_s": 1.0},
    ])
    assert n == 2
    assert {r["ticker"] for r in results_store.recent(db_path=db)} == {"INTC", "PURR"}
    intc = results_store.by_ticker("intc", db_path=db)[0]
    assert intc["decision"] == "Overweight"
    assert intc["report_path"] == "/p/INTC.md"
    assert intc["git_sha"] == "abc123"
    assert intc["duration_s"] == 12.3
    assert intc["realized_return"] is None      # outcome cols start NULL
    purr = results_store.by_ticker("PURR", db_path=db)[0]
    assert purr["status"] == "failed" and purr["error"] == "boom" and purr["decision"] is None


def test_idempotent_upsert(tmp_path):
    db = tmp_path / "results.db"
    _run(db, rows=[{"ticker": "INTC", "status": "ok", "decision": "Hold"}])
    _run(db, rows=[{"ticker": "INTC", "status": "ok", "decision": "Overweight"}])
    rows = results_store.by_ticker("INTC", db_path=db)
    assert len(rows) == 1                         # replaced, not duplicated
    assert rows[0]["decision"] == "Overweight"


def test_rerecord_preserves_outcome_columns(tmp_path):
    db = tmp_path / "results.db"
    _run(db, rows=[{"ticker": "INTC", "status": "ok", "decision": "Hold"}])
    # feedback loop later attaches a realized outcome
    conn = sqlite3.connect(db)
    conn.execute("UPDATE decisions SET realized_return=0.12, outcome_date='2026-09-30' "
                 "WHERE run_id=? AND ticker=?", ("2026-09-16__120000", "INTC"))
    conn.commit()
    conn.close()
    # re-recording the same run/ticker refreshes the decision but must NOT wipe the outcome
    _run(db, rows=[{"ticker": "INTC", "status": "ok", "decision": "Overweight"}])
    row = results_store.by_ticker("INTC", db_path=db)[0]
    assert row["decision"] == "Overweight"
    assert row["realized_return"] == 0.12
    assert row["outcome_date"] == "2026-09-30"


def test_schema_autocreated_and_latest_run(tmp_path):
    db = tmp_path / "results.db"
    assert results_store.recent(db_path=db) == []     # empty DB: schema created, no rows
    _run(db, run_id="R1", rows=[{"ticker": "A", "status": "ok", "decision": "Buy"}])
    _run(db, run_id="R2", rows=[{"ticker": "B", "status": "ok", "decision": "Sell"}])
    assert [r["ticker"] for r in results_store.latest_run(db_path=db)] == ["B"]

"""mock_portfolio: outcome resolution (maturity gate + idempotency), long-only
equal-weight simulation + compounding, and per-tier scorecard math.

Prices are mocked (nday_return monkeypatched), so these are fully deterministic and
never hit the network. The ledger is a per-test tmp SQLite via db_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mock_portfolio  # noqa: E402
import results_store  # noqa: E402

pytestmark = pytest.mark.unit


def _seed(db, run_id, trade_date, items, resolved=True):
    """items: list of (ticker, decision, realized_return|None)."""
    results_store.record_run(
        run_id=run_id, trade_date=trade_date, mode="watchlist", depth="shallow",
        deep_model="m", quick_model="m", git_sha="x", db_path=db,
        rows=[{"ticker": t, "status": "ok", "decision": dec} for t, dec, _ in items])
    if resolved:
        for t, _, ret in items:
            results_store.attach_outcome(run_id, t, entry_price=100.0,
                                         realized_return=ret, outcome_date="2026-08-08", db_path=db)


def test_resolve_grades_matured_only_and_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01", [("INTC", "Buy", None), ("AMZN", "Hold", None)], resolved=False)

    def fake_nday(ticker, trade_date, horizon_days=5):
        if ticker == "INTC":
            return {"entry_price": 100.0, "exit_price": 110.0, "raw_return": 0.10,
                    "holding_days": 5, "exit_date": "2026-08-08"}
        return None  # AMZN not matured yet
    monkeypatch.setattr(mock_portfolio, "nday_return", fake_nday)

    assert mock_portfolio.resolve(db_path=db) == 1
    resolved = results_store.resolved_decisions(db_path=db)
    assert [r["ticker"] for r in resolved] == ["INTC"]
    assert resolved[0]["realized_return"] == 0.10
    assert resolved[0]["entry_price"] == 100.0
    assert resolved[0]["outcome_date"] == "2026-08-08"
    # idempotent: INTC already resolved, AMZN still not matured
    assert mock_portfolio.resolve(db_path=db) == 0


def test_simulate_is_long_only_and_compounds(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    # cohort R1 long book = Buy(+10%) only; Hold/Sell excluded -> +10%
    _seed(db, "R1", "2026-08-01", [("INTC", "Buy", 0.10), ("AMZN", "Hold", 0.50), ("BE", "Sell", -0.20)])
    # cohort R2 long book = Buy(0%) + Overweight(+20%) equal-weight -> +10%
    _seed(db, "R2", "2026-08-10", [("DE", "Buy", 0.00), ("HOOD", "Overweight", 0.20)])
    monkeypatch.setattr(mock_portfolio, "nday_return", lambda *a, **k: {"raw_return": 0.0})  # SPY flat

    sim = mock_portfolio.simulate(capital=100_000, db_path=db)
    assert sim["n_cohorts"] == 2
    assert round(sim["final_equity"], 2) == 121_000.00   # 100k * 1.10 * 1.10
    assert round(sim["total_return"], 4) == 0.21
    assert round(sim["final_bench_equity"], 2) == 100_000.00  # SPY flat
    assert sim["curve"][0]["n_long"] == 1 and sim["curve"][1]["n_long"] == 2


def test_scorecard_by_tier_is_direction_aware(tmp_path):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01",
          [("A", "Buy", 0.10), ("B", "Buy", -0.05), ("C", "Sell", -0.10),
           ("D", "Hold", 0.02), ("E", "Underweight", 0.20)])
    sc = mock_portfolio.scorecard(db_path=db)
    assert sc["n_resolved"] == 5
    buy = sc["by_tier"]["Buy"]
    assert buy["n"] == 2
    assert round(buy["avg_return"], 4) == 0.025   # (0.10 + -0.05) / 2
    assert buy["hit_rate"] == 0.5                 # A went up (hit), B went down (miss)
    assert sc["by_tier"]["Sell"]["hit_rate"] == 1.0          # C fell: the Sell was right
    assert sc["by_tier"]["Underweight"]["hit_rate"] == 0.0   # E rallied: the call was wrong
    assert sc["by_tier"]["Hold"]["hit_rate"] is None         # no directional call


def test_best_and_worst_calls_rank_by_call_direction(tmp_path):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01",
          [("A", "Buy", 0.10), ("B", "Buy", -0.05), ("C", "Sell", -0.10),
           ("D", "Hold", 0.02), ("E", "Underweight", 0.20)])
    sc = mock_portfolio.scorecard(db_path=db)
    best = [d["ticker"] for d in sc["best_calls"]]
    worst = [d["ticker"] for d in sc["worst_calls"]]
    assert best[0] in ("A", "C")       # Buy +10% and Sell -10% are both +10% right
    assert worst[0] == "E"             # Underweight +20% is the worst call, not a "winner"
    assert "E" not in best
    assert "D" not in best + worst     # Hold is never a best/worst call
    assert not set(best) & set(worst)  # disjoint

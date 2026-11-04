"""mock_portfolio: outcome resolution (maturity gate + idempotency), long-only
equal-weight simulation + compounding, and per-tier scorecard math.

Prices are mocked (nday_return monkeypatched), so these are fully deterministic and
never hit the network. The ledger is a per-test tmp SQLite via db_path.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mock_portfolio  # noqa: E402
import results_store  # noqa: E402

pytestmark = pytest.mark.unit


def _seed(db, run_id, trade_date, items, resolved=True, mode="watchlist"):
    """items: list of (ticker, decision, realized_return|None)."""
    results_store.record_run(
        run_id=run_id, trade_date=trade_date, mode=mode, depth="shallow",
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


def _fake_spy(returns: dict[str, float]):
    """SPY window stub: return per trade_date; window exits 7 calendar days later."""
    def fake(ticker, trade_date, horizon_days=5):
        if trade_date not in returns:
            return None
        exit_ = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
        return {"raw_return": returns[trade_date], "exit_date": exit_}
    return fake


def test_simulate_is_long_only_and_compounds(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    # cohort R1 long book = Buy(+10%) only; Hold/Sell excluded -> +10%
    _seed(db, "R1", "2026-08-01", [("INTC", "Buy", 0.10), ("AMZN", "Hold", 0.50), ("BE", "Sell", -0.20)])
    # cohort R2 (starts after R1's 08-08 exit) = Buy(0%) + Overweight(+20%) -> +10%
    _seed(db, "R2", "2026-08-10", [("DE", "Buy", 0.00), ("HOOD", "Overweight", 0.20)])
    monkeypatch.setattr(mock_portfolio, "nday_return",
                        _fake_spy({"2026-08-01": 0.0, "2026-08-10": 0.0}))  # SPY flat

    sim = mock_portfolio.simulate(capital=100_000, db_path=db)
    assert sim["n_held"] == 2
    assert round(sim["final_equity"], 2) == 121_000.00   # 100k * 1.10 * 1.10
    assert round(sim["total_return"], 4) == 0.21
    assert round(sim["final_bench_equity"], 2) == 100_000.00  # SPY flat
    assert sim["curve"][0]["n_long"] == 1 and sim["curve"][1]["n_long"] == 2


def test_simulate_curve_skips_overlapping_windows(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01", [("A", "Buy", 0.10)])   # held, exits 08-08
    _seed(db, "R2", "2026-08-04", [("B", "Buy", 0.50)])   # starts while R1's money is tied up
    _seed(db, "R3", "2026-08-08", [("C", "Buy", 0.10)])   # starts on R1's exit day -> held
    monkeypatch.setattr(mock_portfolio, "nday_return",
                        _fake_spy({"2026-08-01": 0.0, "2026-08-04": 0.0, "2026-08-08": 0.0}))

    sim = mock_portfolio.simulate(capital=100_000, db_path=db)
    assert [c["run_id"] for c in sim["curve"]] == ["R1", "R3"]
    assert round(sim["final_equity"], 2) == 121_000.00   # R2's +50% never compounds in
    assert sim["n_windows"] == 3 and sim["n_held"] == 2
    assert [w["in_curve"] for w in sim["windows"]] == [True, False, True]


def test_simulate_averages_excess_over_every_window(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01", [("A", "Buy", 0.10)])          # book +10%, SPY +2%
    _seed(db, "R2", "2026-08-04", [("B", "Underweight", 0.05)])  # no longs: cash 0%, SPY +1%
    monkeypatch.setattr(mock_portfolio, "nday_return",
                        _fake_spy({"2026-08-01": 0.02, "2026-08-04": 0.01}))

    sim = mock_portfolio.simulate(db_path=db)
    assert [round(w["excess_return"], 4) for w in sim["windows"]] == [0.08, -0.01]
    assert round(sim["avg_excess_return"], 4) == 0.035   # (0.08 - 0.01) / 2, uncompounded
    assert sim["n_beat"] == 1


def test_simulate_ignores_test_runs_and_dedupes_dates(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    _seed(db, "SMOKE", "2026-08-01", [("A", "Buy", 0.90)], mode="single")  # one-ticker test
    _seed(db, "FIRST", "2026-08-04", [("B", "Buy", 0.30)])
    _seed(db, "RERUN", "2026-08-04", [("B", "Buy", 0.10)])  # same date, recorded later -> wins
    monkeypatch.setattr(mock_portfolio, "nday_return",
                        _fake_spy({"2026-08-01": 0.0, "2026-08-04": 0.0}))

    sim = mock_portfolio.simulate(capital=100_000, db_path=db)
    assert [w["run_id"] for w in sim["windows"]] == ["RERUN"]
    assert round(sim["final_equity"], 2) == 110_000.00


def test_simulate_skips_windows_without_a_benchmark(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    _seed(db, "R1", "2026-08-01", [("A", "Buy", 0.10)])
    _seed(db, "R2", "2026-08-10", [("B", "Buy", 0.10)])
    monkeypatch.setattr(mock_portfolio, "nday_return", _fake_spy({"2026-08-10": 0.0}))

    sim = mock_portfolio.simulate(db_path=db)
    assert [w["run_id"] for w in sim["windows"]] == ["R2"]  # R1 not assumed flat vs SPY


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

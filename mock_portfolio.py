#!/usr/bin/env python3
"""Mock portfolio + decision scorecard over the results ledger.

This sits ALONGSIDE the app's existing agent-reflection loop (reflection.py +
memory.py), which already learns from realized returns on same-ticker re-runs. This
layer adds what that loop can't:

1. **resolve** — grade decisions on a fixed time horizon regardless of whether a
   ticker is ever re-run (the reflection loop's blind spot), filling results.db's
   dormant entry_price / realized_return / outcome_date columns.
2. **report** — simulate a long-only, equal-weight cash portfolio built from the
   decisions and print a scorecard: portfolio return vs SPY, and per-tier hit-rate /
   average return (does the rating actually carry signal?).

    python mock_portfolio.py resolve [--horizon-days N]
    python mock_portfolio.py report  [--capital C] [--horizon-days N]

Long-only, equal-weight: Buy/Overweight are held; Hold/Underweight/Sell are not.
Fixed N-trading-day hold (default 5, matching the reflection loop). Mock only — no
brokerage/real-money execution.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

import yfinance as yf

import results_store
from tradingagents.dataflows.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 5
DEFAULT_CAPITAL = 100_000.0
BENCHMARK = "SPY"
# Long-only, equal-weight book: only these ratings are held.
_LONG_DECISIONS = {"Buy", "Overweight"}
_TIER_ORDER = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
# Run modes that analyze the full book (and so form a portfolio). "single" runs are
# one-ticker tests/checks and "holdings" is a partial view, so neither is simulated.
_PORTFOLIO_MODES = {"watchlist", "all"}
# Direction each rating points: +1 bullish, -1 bearish, 0 = no directional call.
_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


def nday_return(ticker: str, trade_date: str,
                horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict | None:
    """Realized N-trading-day return for ``ticker`` starting at ``trade_date``.

    Mirrors TradingAgentsGraph._fetch_returns: entry = first close on/after
    trade_date; exit = close ``horizon_days`` trading days later; a +7 calendar-day
    buffer covers weekends/holidays; symbols are normalized so crypto/forex price the
    same way the analysis did. Returns None when the full horizon hasn't elapsed yet
    (too recent) or there's no price data — so callers only grade *matured* decisions.
    """
    try:
        start = datetime.strptime(trade_date, "%Y-%m-%d")
        end = (start + timedelta(days=horizon_days + 7)).strftime("%Y-%m-%d")
        hist = yf.Ticker(normalize_symbol(ticker)).history(start=trade_date, end=end)
        if len(hist) < horizon_days + 1:          # full horizon not yet available
            return None
        entry = float(hist["Close"].iloc[0])
        exit_ = float(hist["Close"].iloc[horizon_days])
        if entry <= 0:
            return None
        return {
            "entry_price": round(entry, 4),
            "exit_price": round(exit_, 4),
            "raw_return": (exit_ - entry) / entry,
            "holding_days": horizon_days,
            "exit_date": hist.index[horizon_days].strftime("%Y-%m-%d"),
        }
    except Exception as exc:
        logger.warning("nday_return failed for %s on %s: %s", ticker, trade_date, exc)
        return None


def resolve(horizon_days: int = DEFAULT_HORIZON_DAYS, db_path=None) -> int:
    """Grade matured, still-unresolved 'ok' decisions into the ledger.

    Idempotent: only touches rows whose realized_return is still NULL, and skips
    decisions that haven't matured yet (nday_return returns None) so they resolve on
    a later call. Returns the number newly resolved.
    """
    n = 0
    for d in results_store.unresolved_ok_decisions(db_path=db_path):
        r = nday_return(d["ticker"], d["trade_date"], horizon_days)
        if r is None:
            continue
        results_store.attach_outcome(
            d["run_id"], d["ticker"],
            entry_price=r["entry_price"], realized_return=r["raw_return"],
            outcome_date=r["exit_date"], db_path=db_path)
        n += 1
    return n


def _cohorts(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group resolved decisions into one cohort per trade_date, oldest first.

    Only full-book runs count (``_PORTFOLIO_MODES``): single-ticker / holdings-only
    runs are tests or partial views, not the portfolio the model would run. If two
    full-book runs share a trade_date, the most recently recorded one wins, so a
    re-run never double-counts a date."""
    by_run: dict[str, list[dict]] = {}
    for d in rows:
        if d.get("mode") in _PORTFOLIO_MODES:
            by_run.setdefault(d["run_id"], []).append(d)
    by_date: dict[str, tuple[str, list[dict]]] = {}
    for run_id, decisions in sorted(by_run.items(),
                                    key=lambda kv: max(d["recorded_at"] for d in kv[1])):
        by_date[decisions[0]["trade_date"]] = (run_id, decisions)
    return [by_date[date] for date in sorted(by_date)]


def simulate(capital: float = DEFAULT_CAPITAL,
             horizon_days: int = DEFAULT_HORIZON_DAYS, db_path=None) -> dict:
    """Two honest views of the equal-weight long book (Buy/Overweight) vs SPY.

    * **Per-window excess** — for every graded run, the long book's return minus
      SPY's over the same window, averaged with no compounding. This is the "does
      the long book beat the market" signal, and it uses every run.
    * **Equity curve** — a strategy you could actually run with one pot of money:
      buy a run's longs, hold ``horizon_days``, sell, then roll into the first run
      on/after the exit date. Runs that start while the money is still tied up are
      skipped. Runs are ~2-3 trading days apart but each book is held 5, so
      chaining *every* run would compound overlapping windows and count the same
      market days several times over.

    A run with no longs holds cash (0%) for its window — that is the model's call.
    A run whose SPY window can't be priced is left out rather than assumed flat.
    """
    windows = []
    for run_id, decisions in _cohorts(results_store.resolved_decisions(db_path=db_path)):
        trade_date = decisions[0]["trade_date"]
        spy = nday_return(BENCHMARK, trade_date, horizon_days)
        if spy is None:
            logger.warning("no %s benchmark for %s window; skipping run %s",
                           BENCHMARK, trade_date, run_id)
            continue
        longs = [d for d in decisions if d["decision"] in _LONG_DECISIONS]
        book = sum(d["realized_return"] for d in longs) / len(longs) if longs else 0.0
        windows.append({"run_id": run_id, "trade_date": trade_date,
                        "exit_date": spy["exit_date"], "n_long": len(longs),
                        "cohort_return": book, "spy_return": spy["raw_return"],
                        "excess_return": book - spy["raw_return"], "in_curve": False})

    equity = bench_equity = capital
    curve, free_from = [], None
    for w in windows:
        if free_from is not None and w["trade_date"] < free_from:
            continue                      # capital still in the previous book
        w["in_curve"] = True
        equity *= 1 + w["cohort_return"]
        bench_equity *= 1 + w["spy_return"]
        curve.append({**w, "equity": equity, "bench_equity": bench_equity})
        free_from = w["exit_date"]

    excess = [w["excess_return"] for w in windows]
    return {
        "capital": capital, "horizon_days": horizon_days,
        "n_windows": len(windows),
        "avg_excess_return": sum(excess) / len(excess) if excess else None,
        "n_beat": sum(1 for x in excess if x > 0),
        "windows": windows,
        "n_held": len(curve),
        "final_equity": equity, "final_bench_equity": bench_equity,
        "total_return": equity / capital - 1 if capital else 0.0,
        "bench_return": bench_equity / capital - 1 if capital else 0.0,
        "curve": curve,
    }


def scorecard(db_path=None) -> dict:
    """Per-tier count / average realized return / direction-aware hit-rate, plus the
    best and worst calls.

    A hit means the price moved the way the call pointed: up for Buy/Overweight,
    down for Underweight/Sell. Hold makes no directional call, so its hit_rate is
    None. Best/worst calls rank by call-signed return (return x direction), so an
    Underweight on a stock that rallied is a bad call, not a "winner"."""
    rows = results_store.resolved_decisions(db_path=db_path)
    tiers: dict[str, dict] = {}
    for d in rows:
        tier = d["decision"] or "?"
        s = tiers.setdefault(tier, {"n": 0, "sum": 0.0, "hits": 0})
        s["n"] += 1
        s["sum"] += d["realized_return"]
        s["hits"] += 1 if _DIRECTION.get(tier, 0) * d["realized_return"] > 0 else 0
    by_tier = {t: {"n": s["n"], "avg_return": s["sum"] / s["n"],
                   "hit_rate": s["hits"] / s["n"] if _DIRECTION.get(t, 0) else None}
               for t, s in tiers.items()}
    calls = [d for d in rows if _DIRECTION.get(d["decision"], 0)]  # directional only
    ranked = sorted(calls, key=lambda d: _DIRECTION[d["decision"]] * d["realized_return"],
                    reverse=True)
    best_calls = ranked[:3]
    # Disjoint from best, so a small decision set never lists the same call twice.
    worst_calls = [d for d in reversed(ranked) if d not in best_calls][:3]
    return {"n_resolved": len(rows), "by_tier": by_tier,
            "best_calls": best_calls, "worst_calls": worst_calls}


def _report(capital: float, horizon_days: int) -> None:
    sim = simulate(capital=capital, horizon_days=horizon_days)
    sc = scorecard()
    print(f"=== Mock portfolio — long-only equal-weight, {horizon_days}d hold ===")
    if sc["n_resolved"] == 0:
        print(f"No resolved decisions yet. Run `resolve` once decisions mature "
              f"({horizon_days} trading days after their trade_date).")
        return
    if sim["n_windows"]:
        print(f"\n  Long book vs SPY, per {horizon_days}d window (every graded run, not compounded):")
        print(f"    graded runs     : {sim['n_windows']}")
        print(f"    avg excess      : {sim['avg_excess_return'] * 100:+.2f} pts per window")
        print(f"    beat SPY        : {sim['n_beat']} of {sim['n_windows']} windows")
        tr, br = sim["total_return"] * 100, sim["bench_return"] * 100
        print(f"\n  Equity curve (one pot, non-overlapping {horizon_days}d holds):")
        print(f"    held windows    : {sim['n_held']} of {sim['n_windows']} runs")
        print(f"    start capital   : ${capital:,.0f}")
        print(f"    final equity    : ${sim['final_equity']:,.0f}  ({tr:+.2f}%)")
        print(f"    SPY, same days  : ${sim['final_bench_equity']:,.0f}  ({br:+.2f}%)")
        print(f"    vs SPY          : {tr - br:+.2f} pts")
    else:
        print("\n  No graded full-watchlist runs yet (portfolio view needs a matured run).")
    print(f"\n  Decision scorecard — realized {horizon_days}d returns:")
    print(f"  {'tier':<12} {'n':>3} {'avg_return':>11} {'hit_rate':>9}")
    for tier in _TIER_ORDER:
        s = sc["by_tier"].get(tier)
        if s:
            hr = f"{s['hit_rate'] * 100:>8.0f}%" if s["hit_rate"] is not None else f"{'—':>9}"
            print(f"  {tier:<12} {s['n']:>3} {s['avg_return'] * 100:>10.1f}% {hr}")
    print("  (hit = price moved the way the call pointed; Hold makes no directional call)")
    if sc["best_calls"]:
        print("\n  Best / worst calls (ranked by return x call direction):")
        for d in sc["best_calls"]:
            print(f"    + {d['ticker']:<8} {d['decision']:<12} {d['realized_return'] * 100:+.1f}%")
        for d in sc["worst_calls"]:
            print(f"    - {d['ticker']:<8} {d['decision']:<12} {d['realized_return'] * 100:+.1f}%")


def _main() -> None:
    ap = argparse.ArgumentParser(description="Mock portfolio + decision scorecard over the ledger.")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("resolve", help="grade matured decisions into the ledger")
    rp.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    rep = sub.add_parser("report", help="print the portfolio + decision scorecard")
    rep.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    rep.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    args = ap.parse_args()
    if args.cmd == "resolve":
        print(f"Resolved {resolve(horizon_days=args.horizon_days)} matured decision(s) into the ledger.")
    elif args.cmd == "report":
        _report(args.capital, args.horizon_days)
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()

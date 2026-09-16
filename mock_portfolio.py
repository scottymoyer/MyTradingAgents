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
    """Group resolved decisions by run_id, ordered chronologically by trade_date."""
    by_run: dict[str, list[dict]] = {}
    for d in rows:
        by_run.setdefault(d["run_id"], []).append(d)
    return sorted(by_run.items(), key=lambda kv: (kv[1][0]["trade_date"], kv[0]))


def simulate(capital: float = DEFAULT_CAPITAL,
             horizon_days: int = DEFAULT_HORIZON_DAYS, db_path=None) -> dict:
    """Compound each run's equal-weight long book (Buy/Overweight) into an equity
    curve, alongside a same-window SPY benchmark curve."""
    cohorts = _cohorts(results_store.resolved_decisions(db_path=db_path))
    equity = bench_equity = capital
    curve = []
    for run_id, decisions in cohorts:
        longs = [d for d in decisions if d["decision"] in _LONG_DECISIONS]
        cohort_ret = sum(d["realized_return"] for d in longs) / len(longs) if longs else 0.0
        equity *= (1 + cohort_ret)
        trade_date = decisions[0]["trade_date"]
        spy = nday_return(BENCHMARK, trade_date, horizon_days)
        spy_ret = spy["raw_return"] if spy else 0.0
        bench_equity *= (1 + spy_ret)
        curve.append({"run_id": run_id, "trade_date": trade_date, "n_long": len(longs),
                      "cohort_return": cohort_ret, "equity": equity,
                      "spy_return": spy_ret, "bench_equity": bench_equity})
    return {
        "capital": capital, "horizon_days": horizon_days, "n_cohorts": len(cohorts),
        "final_equity": equity, "final_bench_equity": bench_equity,
        "total_return": equity / capital - 1 if capital else 0.0,
        "bench_return": bench_equity / capital - 1 if capital else 0.0,
        "curve": curve,
    }


def scorecard(db_path=None) -> dict:
    """Per-tier count / average realized return / hit-rate, plus winners & losers."""
    rows = results_store.resolved_decisions(db_path=db_path)
    tiers: dict[str, dict] = {}
    for d in rows:
        s = tiers.setdefault(d["decision"] or "?", {"n": 0, "sum": 0.0, "wins": 0})
        s["n"] += 1
        s["sum"] += d["realized_return"]
        s["wins"] += 1 if d["realized_return"] > 0 else 0
    by_tier = {t: {"n": s["n"], "avg_return": s["sum"] / s["n"], "hit_rate": s["wins"] / s["n"]}
               for t, s in tiers.items()}
    ranked = sorted(rows, key=lambda d: d["realized_return"], reverse=True)
    top_winners = ranked[:3]
    # Disjoint from winners, so a small decision set never lists the same name as
    # both a winner and a loser.
    top_losers = [d for d in reversed(ranked) if d not in top_winners][:3]
    return {"n_resolved": len(rows), "by_tier": by_tier,
            "top_winners": top_winners, "top_losers": top_losers}


def _report(capital: float, horizon_days: int) -> None:
    sim = simulate(capital=capital, horizon_days=horizon_days)
    sc = scorecard()
    print(f"=== Mock portfolio — long-only equal-weight, {horizon_days}d hold ===")
    if sc["n_resolved"] == 0:
        print(f"No resolved decisions yet. Run `resolve` once decisions mature "
              f"({horizon_days} trading days after their trade_date).")
        return
    tr, br = sim["total_return"] * 100, sim["bench_return"] * 100
    print(f"  cohorts (runs)  : {sim['n_cohorts']}")
    print(f"  start capital   : ${capital:,.0f}")
    print(f"  final equity    : ${sim['final_equity']:,.0f}  ({tr:+.1f}%)")
    print(f"  SPY benchmark   : ${sim['final_bench_equity']:,.0f}  ({br:+.1f}%)")
    print(f"  vs SPY          : {tr - br:+.1f} pts")
    print(f"\n  Decision scorecard — realized {horizon_days}d returns:")
    print(f"  {'tier':<12} {'n':>3} {'avg_return':>11} {'hit_rate':>9}")
    for tier in _TIER_ORDER:
        s = sc["by_tier"].get(tier)
        if s:
            print(f"  {tier:<12} {s['n']:>3} {s['avg_return'] * 100:>10.1f}% {s['hit_rate'] * 100:>8.0f}%")
    if sc["top_winners"]:
        print("\n  Top winners / losers:")
        for d in sc["top_winners"][:3]:
            print(f"    + {d['ticker']:<8} {d['decision']:<12} {d['realized_return'] * 100:+.1f}%")
        for d in sc["top_losers"][:3]:
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

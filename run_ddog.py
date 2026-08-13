#!/usr/bin/env python3
"""Preset, Datadog-instrumented headless runner for TradingAgents.

Instrumentation is explicit (ddtrace-run + LLMObs.enable() in code) rather
than relying on host-mode SSI, which does not inject into hand-run
console-script CLIs (see diagnosis: the SSI injector denylists bare Python
interpreter invocations). Launch via run.sh, which wraps this script with
ddtrace-run.

Modes:
  single     one ticker (the original behavior; positional args still work)
  holdings   every ticker in holdings.yaml
  watchlist  every ticker in watchlist.yaml
  all        both lists, reported as separate groups

Multi-ticker modes loop the EXISTING single-ticker engine once per ticker, so
cost scales linearly with the number of tickers. Use --limit / --tickers to
keep a run small, and --dry-run to resolve everything without paid LLM calls.
"""

import ddtrace
from ddtrace.llmobs import LLMObs

# Explicit APM identity, independent of DD_SERVICE/DD_ENV env vars (belt and
# suspenders alongside run.sh's exports).
ddtrace.config.service = "tradingagents"
ddtrace.config.env = "prod"

# integrations_enabled=True patches langchain/openai for LLM Obs spans right
# here, before tradingagents (and its lazily-imported langchain_openai client)
# is imported below. agentless_enabled=False routes through the local Agent.
# Called exactly once per process — the per-ticker loop below must never
# re-enable or shut down the tracer mid-run.
LLMObs.enable(
    ml_app="tradingagents",
    service="tradingagents",
    env="prod",
    integrations_enabled=True,
    agentless_enabled=False,
)

import argparse
import copy
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import memlog_guard
import portfolio
import reddit_oauth
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

SELECTED_ANALYSTS = ("market", "social", "news", "fundamentals")

# "Research depth" is not a config key in this version of the app: the CLI maps
# a depth choice onto BOTH round counts (shallow=1, medium=3, deep=5). Multi-
# ticker runs default to shallow to control cost.
DEPTH_SHALLOW = 1
DEPTH_CHOICES = {"shallow": 1, "medium": 3, "deep": 5}

# Measured on this host (t4g.small): imports are shared across threads and cost
# ~124MB once; the first graph adds ~40MB and each extra graph only ~0.2MB. The
# real per-ticker cost is live run state (messages/reports/frames), inferred at
# ~115MB from an observed 281MB single-run peak. Threads are used rather than
# processes precisely so the import cost is paid once, not per worker.
MEM_BASE_MB = 165
MEM_PER_WORKER_MB = 115
DEFAULT_CONCURRENCY = 3

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    """Thread-safe progress line."""
    with _print_lock:
        print(msg, flush=True)


def available_mb() -> float | None:
    """MemAvailable in MB, or None if it cannot be read."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def check_memory(concurrency: int) -> None:
    """Warn when the requested concurrency will not fit in available RAM.

    Swapping would make the run slower than serial, so this is worth saying
    out loud rather than discovering via thrash.
    """
    avail = available_mb()
    need = MEM_BASE_MB + concurrency * MEM_PER_WORKER_MB
    if avail is None:
        print(f"memory                 = (unknown); estimated need ~{need:.0f} MB")
        return
    verdict = "OK" if need <= avail else "OVER BUDGET"
    print(f"memory                 = ~{need:.0f} MB needed / {avail:.0f} MB available  [{verdict}]")
    if need > avail:
        print(f"  WARNING: concurrency {concurrency} likely exceeds free RAM and may swap,\n"
              f"           which is slower than running serially. Consider --concurrency "
              f"{max(1, int((avail - MEM_BASE_MB) // MEM_PER_WORKER_MB))}.")


def most_recent_weekday() -> str:
    d = datetime.now()
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def build_config(depth: int = DEPTH_SHALLOW) -> dict:
    """DEFAULT_CONFIG with the OpenRouter preset applied.

    ``depth`` sets both round counts, mirroring how the interactive CLI's
    research-depth selection is applied.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "openrouter"
    config["backend_url"] = "https://openrouter.ai/api/v1"
    config["deep_think_llm"] = "openai/gpt-oss-120b"
    config["quick_think_llm"] = "openai/gpt-oss-20b"
    config["max_debate_rounds"] = depth
    config["max_risk_discuss_rounds"] = depth
    config["output_language"] = "English"
    return config


def print_resolved_config(config: dict) -> None:
    print("=== Resolved config ===")
    print(f"llm_provider           = {config['llm_provider']}")
    print(f"backend_url            = {config['backend_url']}")
    print(f"deep_think_llm         = {config['deep_think_llm']}")
    print(f"quick_think_llm        = {config['quick_think_llm']}")
    print(f"max_debate_rounds      = {config['max_debate_rounds']}")
    print(f"max_risk_discuss_rounds= {config['max_risk_discuss_rounds']}")
    print(f"output_language        = {config['output_language']}")
    print(f"selected_analysts      = {SELECTED_ANALYSTS}")


def _apply_filters(tickers: list[str], only: set[str] | None, limit: int | None) -> list[str]:
    """Apply --tickers then --limit, preserving file order."""
    if only is not None:
        tickers = [t for t in tickers if t in only]
    if limit is not None:
        tickers = tickers[:limit]
    return tickers


def resolve_targets(args) -> tuple[list[dict], list[dict]]:
    """Resolve the (holdings, watchlist) target rows for the selected mode.

    Each returned row carries the ticker plus the context needed for printing.
    Reads only the YAML files — no network, no LLM calls.
    """
    only = None
    if args.tickers:
        only = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}

    holdings_rows: list[dict] = []
    watchlist_rows: list[dict] = []

    if args.mode in ("holdings", "all"):
        holdings = portfolio.load_holdings()
        keep = set(_apply_filters([h.ticker for h in holdings], only, args.limit))
        seen: set[str] = set()
        for h in holdings:
            if h.ticker in keep and h.ticker not in seen:
                seen.add(h.ticker)
                holdings_rows.append({
                    "ticker": h.ticker,
                    "asset_type": "stock",
                    "account": h.account,
                    "shares": h.shares,
                    "cost_basis": h.cost_basis,
                    "acquire_date": h.acquire_date,
                })

    if args.mode in ("watchlist", "all"):
        watchlist = portfolio.load_watchlist()
        keep = set(_apply_filters([w.ticker for w in watchlist], only, args.limit))
        for w in watchlist:
            if w.ticker in keep:
                watchlist_rows.append({
                    "ticker": w.ticker,
                    "asset_type": w.engine_asset_type(),
                    "declared_asset_type": w.asset_type,
                    "thesis": w.thesis,
                    "tag": w.tag,
                })

    return holdings_rows, watchlist_rows


def analyze_one(graph: TradingAgentsGraph, ticker: str, trade_date: str, asset_type: str) -> dict:
    """Run the existing engine once for one ticker.

    Wrapped in an LLM Obs workflow span so each ticker's LLM calls group under
    their own root span in LLM Observability. The tracer itself is neither
    re-initialized nor shut down here — that happens once in main().
    """
    with LLMObs.workflow(name=f"analyze:{ticker}"):
        LLMObs.annotate(tags={"ticker": ticker, "trade_date": trade_date})
        final_state, decision = graph.propagate(ticker, trade_date, asset_type=asset_type)
        report_path = graph.save_reports(final_state, ticker)
    return {"decision": decision, "report_path": report_path}


def _print_group(title: str, rows: list[dict], results: dict[str, dict], is_watchlist: bool) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    if not rows:
        print("(none)")
        return
    for row in rows:
        ticker = row["ticker"]
        res = results.get(ticker)
        print(f"\n--- {ticker} ---")
        if is_watchlist:
            print(f"  thesis   : {row['thesis']}")
            print(f"  tag      : {row['tag']} (declared asset_type: {row['declared_asset_type']})")
        else:
            print(f"  account  : {row['account']}")
            print(f"  position : {row['shares']:g} sh @ cost basis {row['cost_basis']:g} "
                  f"(acquired {row['acquire_date']})")
        if res is None:
            print("  decision : (not run)")
        elif res.get("error"):
            print(f"  decision : FAILED — {res['error']}")
        else:
            print(f"  decision : {res['decision']}")
            print(f"  report   : {res['report_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preset, Datadog-instrumented TradingAgents run (OpenRouter).",
    )
    parser.add_argument(
        "ticker", nargs="?", default=None,
        help="Ticker for single mode (default: DDOG). Ignored in holdings/watchlist/all mode.",
    )
    parser.add_argument(
        "date", nargs="?", default=None,
        help="Trade date YYYY-MM-DD (default: most recent weekday)",
    )
    parser.add_argument(
        "--mode", choices=("single", "holdings", "watchlist", "all"), default="single",
        help="single (default) = one ticker; holdings/watchlist/all = loop the engine per ticker.",
    )
    parser.add_argument(
        "--date", dest="date_flag", default=None,
        help="Trade date YYYY-MM-DD (same as the positional date; this flag wins if both given).",
    )
    parser.add_argument(
        "--tickers", default=None,
        help="Comma-separated filter, e.g. --tickers DDOG,COPX (applied before --limit).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Analyze at most N tickers per group. Use this to keep cost down.",
    )
    parser.add_argument(
        "--depth", choices=tuple(DEPTH_CHOICES), default="shallow",
        help="Research depth; sets both round counts (shallow=1, medium=3, deep=5). Default: shallow.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
        help=f"Analyze N tickers at once (default: {DEFAULT_CONCURRENCY}). The work is "
             "I/O-bound on the LLM API, so threads help; RAM is the limiter.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve tickers/config and init the tracer, then exit WITHOUT any LLM calls.",
    )
    args = parser.parse_args()

    # Swap the sentiment analyst's Reddit source to the authenticated API when
    # credentials are present; no-op (keeps anonymous RSS) when they are not.
    reddit_oauth.install()

    # Serialise the shared memory-log file. Its read-modify-write cycles and
    # single fixed .tmp path corrupt/lose entries under concurrency.
    memlog_guard.install()

    trade_date = args.date_flag or args.date or most_recent_weekday()
    depth = DEPTH_CHOICES[args.depth]
    config = build_config(depth)

    print_resolved_config(config)
    print(f"mode                   = {args.mode}")
    print(f"trade_date             = {trade_date}")
    print(f"depth                  = {args.depth} ({depth})")
    if args.mode != "single":
        print(f"concurrency            = {args.concurrency}")
        check_memory(args.concurrency)
    if args.tickers:
        print(f"--tickers filter       = {args.tickers}")
    if args.limit is not None:
        print(f"--limit                = {args.limit}")

    try:
        if args.mode == "single":
            ticker = (args.ticker or "DDOG").upper()
            print(f"ticker                 = {ticker}")
            if args.dry_run:
                print("\n[dry-run] would analyze 1 ticker: " + ticker)
                print(f"[dry-run] LLMObs.enabled={LLMObs.enabled} "
                      f"service={ddtrace.config.service} env={ddtrace.config.env}")
                return

            graph = TradingAgentsGraph(
                selected_analysts=SELECTED_ANALYSTS, config=config, debug=False,
            )
            final_state, decision = graph.propagate(ticker, trade_date)
            print("\n=== FINAL DECISION ===")
            print(decision)
            report_path = graph.save_reports(final_state, ticker)
            print(f"\nReports written to: {report_path}")
            return

        # ---- multi-ticker modes -------------------------------------------
        holdings_rows, watchlist_rows = resolve_targets(args)
        total = len(holdings_rows) + len(watchlist_rows)

        print(f"\nResolved targets ({total} analysis run(s), one engine pass each):")
        print(f"  Current Holdings    : {[r['ticker'] for r in holdings_rows] or '(none)'}")
        print(f"  Watchlist/Candidates: {[r['ticker'] for r in watchlist_rows] or '(none)'}")

        if args.dry_run:
            print(f"\n[dry-run] LLMObs.enabled={LLMObs.enabled} "
                  f"service={ddtrace.config.service} env={ddtrace.config.env}")
            print("[dry-run] no LLM calls made; exiting before engine construction.")
            return

        if total == 0:
            print("\nNothing to analyze after filters. Exiting.")
            return

        rows = holdings_rows + watchlist_rows
        workers = max(1, min(args.concurrency, total))

        # Build the graphs SERIALLY, before any worker starts. TradingAgentsGraph
        # .__init__ calls dataflows.config.set_config(), which mutates a module-level
        # global; constructing concurrently would race on it. Extra graphs cost
        # ~0.2MB each (measured), so a pool of `workers` is effectively free.
        pool: queue.Queue = queue.Queue()
        for _ in range(workers):
            pool.put(TradingAgentsGraph(
                selected_analysts=SELECTED_ANALYSTS, config=config, debug=False,
            ))

        results: dict[str, dict] = {}
        results_lock = threading.Lock()
        done = 0

        def run_one(row: dict) -> None:
            """Analyze one ticker on a borrowed graph. Never raises."""
            nonlocal done
            ticker = row["ticker"]
            graph = pool.get()          # one graph per worker; never shared concurrently
            try:
                outcome = analyze_one(graph, ticker, trade_date, row["asset_type"])
            except Exception as exc:    # one bad ticker must not kill the batch
                outcome = {"error": f"{type(exc).__name__}: {exc}"}
                with _print_lock:
                    traceback.print_exc()
            finally:
                pool.put(graph)
            with results_lock:
                results[ticker] = outcome
                done += 1
                n = done
            _say(f"<<< [{n}/{total}] {ticker}: "
                 + (outcome["error"] if "error" in outcome else outcome["decision"]))

        print(f"\nAnalyzing {total} ticker(s) with concurrency {workers} ...")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ta") as ex:
            futures = [ex.submit(run_one, row) for row in rows]
            for f in as_completed(futures):
                f.result()   # run_one swallows analysis errors; this re-raises only bugs

        _print_group("Current Holdings", holdings_rows, results, is_watchlist=False)
        _print_group("Watchlist / Candidates", watchlist_rows, results, is_watchlist=True)

        # ------------------------------------------------------------------
        # TODO(portfolio-fit): FUTURE PORTFOLIO-FIT LAYER GOES HERE.
        #
        # Out of scope for this task, deliberately not implemented:
        #   - load objectives.yaml (risk tolerance, allocation targets,
        #     concentration limits, tax sensitivity)
        #   - portfolio-level weights, correlations, concentration checks
        #   - scoring each per-ticker decision above against those objectives
        #     (e.g. "Buy, but this would breach your 10% single-name cap")
        #
        # Everything above is intentionally per-ticker analysis grouped by
        # list, with no cross-position reasoning.
        # ------------------------------------------------------------------

    finally:
        # Flush/shutdown exactly once for the whole process, after every ticker.
        LLMObs.flush()
        ddtrace.tracer.shutdown()


if __name__ == "__main__":
    main()

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
import os
import queue
import threading
import time
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

# Default OpenRouter model IDs for the two engine roles. Overridable per run via
# --deep-model / --quick-model (or --model to set both).
#
# The gpt-oss preset is the default for speed (~8min vs ~28min per shallow
# ticker), which keeps a full-watchlist run tractable. In a head-to-head on INTC,
# GLM 5.3 Flash ("z-ai/glm-5.3-flash") produced clearly stronger financial
# reasoning (caught an FCF-annualization error gpt-oss took at face value, and
# used the reflection loop on the prior decision's realized return) with zero
# structured-output failures -- but at ~3.5x the latency. Flip either constant to
# "z-ai/glm-5.3-flash" when reasoning quality matters more than turnaround.
DEFAULT_DEEP_MODEL = "openai/gpt-oss-120b"
DEFAULT_QUICK_MODEL = "openai/gpt-oss-20b"

# Memory model, corrected against measurement.
#
# Component costs on this host (t4g.small), measured directly:
#   ~124MB  imports (langchain/langgraph/pandas) -- paid ONCE, shared by all
#           threads. This is why threads are used instead of processes.
#   ~40MB   first TradingAgentsGraph
#   ~0.2MB  each additional graph
#
# The first estimate here assumed ~115MB of *additional* live state per
# concurrent ticker. A real two-ticker run disproved that: peak RSS was 281MB
# for two tickers, identical to a single-ticker run, with MemAvailable never
# below 649MB and no swap growth. Most per-run state is short-lived and the
# allocator reuses it across threads, so the marginal cost of a second worker
# was effectively zero.
#
# Observed peak RSS (VmHWM) by concurrency, all on this host:
#     n=1  281MB      n=2  281MB      n=5  364MB
# Fitting the n=2 -> n=5 slope gives ~28MB per additional worker on a ~225MB
# base, which reproduces both points within ~1MB. The constants below round
# that up slightly so the estimate errs high (warning early is cheap; swapping
# is not). Each run prints its own VmHWM against this prediction, so the model
# stays checkable rather than becoming folklore.
MEM_BASE_MB = 250
MEM_PER_WORKER_MB = 32

# Default 10. Memory is not the constraint: at n=5 peak RSS was 360MB with
# ~600MB still free, and the model puts n=10 at ~570MB -- comfortable on this
# box, and the startup clamp reduces it anyway if free RAM is low. Two things
# remain UNTESTED above 5 and are the real risks to watch on the first large
# run: OpenRouter rate limits (n=10 bursts ~10x the requests; none seen at 5)
# and Alpha Vantage fallback exhaustion if yfinance stumbles mid-batch.
# Concurrency past the ticker count is harmless but does nothing -- wall clock
# floors at the slowest single ticker. Lower with --concurrency if 429s appear.
DEFAULT_CONCURRENCY = 10


def peak_rss_mb() -> float | None:
    """Peak RSS for this process (VmHWM), the kernel's own high-water mark."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None

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


def build_config(depth: int = DEPTH_SHALLOW,
                 deep_model: str = DEFAULT_DEEP_MODEL,
                 quick_model: str = DEFAULT_QUICK_MODEL) -> dict:
    """DEFAULT_CONFIG with the OpenRouter preset applied.

    ``depth`` sets both round counts, mirroring how the interactive CLI's
    research-depth selection is applied. ``deep_model``/``quick_model`` are the
    OpenRouter model IDs for the deep-thinking and quick-thinking roles; both
    default to the gpt-oss preset but can be overridden per run to A/B models.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "openrouter"
    config["backend_url"] = "https://openrouter.ai/api/v1"
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = quick_model
    config["max_debate_rounds"] = depth
    config["max_risk_discuss_rounds"] = depth
    config["output_language"] = "English"

    # Ordered vendor fallback. yfinance stays primary (unmetered), with
    # alpha_vantage behind it so a yfinance outage degrades instead of failing:
    # it is an unofficial scraper and does break. route_to_vendor() walks this
    # chain on rate-limit / not-configured / no-data / error, logging each hop.
    #
    # Verified against the live free-tier key, endpoint by endpoint:
    #     OVERVIEW        (fundamental_data)     -> works
    #     NEWS_SENTIMENT  (news_data)            -> works
    #     SMA             (technical_indicators) -> works
    #     TIME_SERIES_DAILY_ADJUSTED (core_stock_apis) -> PREMIUM ONLY
    #
    # core_stock_apis is therefore left on yfinance alone. The app's Alpha
    # Vantage stock path calls the adjusted series, which a free key cannot
    # reach, so chaining it there would spend a request to fail every time --
    # and it surfaces as a confusing "rate limited" warning, because the app
    # classifies any notice containing "premium" as a rate limit.
    #
    # macro_data is fred-only and prediction_markets polymarket-only, so
    # neither has a fallback to configure.
    #
    # Alpha Vantage is second, never first: the free tier is tightly rate
    # limited, so under concurrency several tickers failing over at once can
    # still exhaust it. This is best-effort insurance against a yfinance
    # outage, not a robust second source.
    config["data_vendors"] = {
        **config.get("data_vendors", {}),
        "technical_indicators": "yfinance,alpha_vantage",
        "fundamental_data": "yfinance,alpha_vantage",
        "news_data": "yfinance,alpha_vantage",
    }
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
    dv = config.get("data_vendors", {})
    print(f"data_vendors           = {dv}")
    if not os.environ.get("FRED_API_KEY"):
        print("  note: FRED_API_KEY unset -> macro indicators unavailable this run "
              "(free key: https://fred.stlouisfed.org/docs/api/api_key.html)")


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
        # Set input up front so even a failed/errored trace shows what was
        # attempted; the trace-explorer list surfaces the ROOT span's I/O, so
        # without this the list reads "No content" (the child agent/LLM spans
        # carry their own I/O via the langchain/openai integrations).
        LLMObs.annotate(
            input_data=f"Analyze {ticker} ({asset_type}) for trade date {trade_date}",
            tags={"ticker": ticker, "trade_date": trade_date},
        )
        final_state, decision = graph.propagate(ticker, trade_date, asset_type=asset_type)
        report_path = graph.save_reports(final_state, ticker)
        # Emit the decision (one of the 5-tier set Buy/Overweight/Hold/
        # Underweight/Sell) so it's chartable as a distribution. Two surfaces,
        # because they index differently:
        #   - set_tag on the APM span -> searchable as @decision in APM span
        #     search and groupable in dashboard widgets (aggregate_spans).
        #   - LLMObs.annotate tag -> facets in Agent Observability.
        # annotate tags do NOT reach APM span search (that's why @ticker was
        # empty in APM earlier), so the set_tag is what the dashboard needs.
        span = ddtrace.tracer.current_span()
        if span is not None:
            span.set_tag("decision", decision)
        LLMObs.annotate(output_data=str(decision), tags={"decision": decision})
    return {"decision": decision, "report_path": report_path}


def _print_timing(results: dict, wall: float, workers: int) -> None:
    """Show where the wall clock actually went.

    A concurrent batch can under-deliver for reasons that are invisible in the
    total (provider-side queuing, retries, one slow ticker holding the tail), so
    print each ticker's start/end offsets alongside the aggregate.
    """
    timed = {t: r for t, r in results.items() if "t_start" in r and "t_end" in r}
    if not timed:
        return
    serial = sum(r["t_end"] - r["t_start"] for r in timed.values())

    print()
    print("=" * 72)
    print("Timing")
    print("=" * 72)
    print(f"  {'ticker':<10} {'start':>8} {'end':>8} {'duration':>10}")
    for t, r in sorted(timed.items(), key=lambda kv: kv[1]["t_start"]):
        dur = r["t_end"] - r["t_start"]
        print(f"  {t:<10} {r['t_start']:>7.1f}s {r['t_end']:>7.1f}s {dur:>9.1f}s")

    # Peak overlap actually achieved, from the start/end intervals.
    events = []
    for r in timed.values():
        events.append((r["t_start"], 1))
        events.append((r["t_end"], -1))
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)

    print()
    print(f"  wall clock            : {wall:.1f}s")
    print(f"  sum of durations      : {serial:.1f}s  (what serial would cost)")
    print(f"  speedup               : {serial / wall:.2f}x  (theoretical max {workers}x)")
    print(f"  peak overlap observed : {peak} of {workers} workers")
    hwm = peak_rss_mb()
    if hwm is not None:
        avail = available_mb()
        print(f"  peak RSS (VmHWM)      : {hwm:.0f} MB"
              + (f"   MemAvailable now {avail:.0f} MB" if avail else ""))
        print(f"  model predicted       : ~{MEM_BASE_MB + workers * MEM_PER_WORKER_MB:.0f} MB")


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
        "--deep-model", default=DEFAULT_DEEP_MODEL, metavar="ID",
        help=f"OpenRouter model for the deep-thinking role (default: {DEFAULT_DEEP_MODEL}).",
    )
    parser.add_argument(
        "--quick-model", default=DEFAULT_QUICK_MODEL, metavar="ID",
        help=f"OpenRouter model for the quick-thinking role (default: {DEFAULT_QUICK_MODEL}).",
    )
    parser.add_argument(
        "--model", default=None, metavar="ID",
        help="Convenience: set BOTH deep and quick models to this one ID (overrides the two above).",
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
    deep_model = args.model or args.deep_model
    quick_model = args.model or args.quick_model
    config = build_config(depth, deep_model, quick_model)

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

        # Clamp DOWN to what free RAM supports, never up: available memory on
        # this box swings by hundreds of MB depending on what else is resident,
        # and swapping is slower than running serially. Scaling up automatically
        # would instead make the same command behave differently run to run.
        avail = available_mb()
        if avail is not None:
            fits = int((avail - MEM_BASE_MB) // MEM_PER_WORKER_MB)
            if 1 <= fits < workers:
                print(f"  note: reducing concurrency {workers} -> {fits} to fit "
                      f"{avail:.0f} MB available (pass --concurrency to override)")
                workers = fits

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
            t_start = time.monotonic()
            try:
                outcome = analyze_one(graph, ticker, trade_date, row["asset_type"])
            except Exception as exc:    # one bad ticker must not kill the batch
                outcome = {"error": f"{type(exc).__name__}: {exc}"}
                with _print_lock:
                    traceback.print_exc()
            finally:
                pool.put(graph)
            outcome["t_start"] = t_start - t_zero
            outcome["t_end"] = time.monotonic() - t_zero
            with results_lock:
                results[ticker] = outcome
                done += 1
                n = done
            _say(f"<<< [{n}/{total}] {ticker}: "
                 + (outcome["error"] if "error" in outcome else outcome["decision"]))

        print(f"\nAnalyzing {total} ticker(s) with concurrency {workers} ...")
        t_zero = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ta") as ex:
            futures = [ex.submit(run_one, row) for row in rows]
            for f in as_completed(futures):
                f.result()   # run_one swallows analysis errors; this re-raises only bugs
        wall = time.monotonic() - t_zero

        _print_timing(results, wall, workers)

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

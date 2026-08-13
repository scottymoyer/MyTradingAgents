#!/usr/bin/env python3
"""Preset, Datadog-instrumented headless runner for TradingAgents.

Instrumentation is explicit (ddtrace-run + LLMObs.enable() in code) rather
than relying on host-mode SSI, which does not inject into hand-run
console-script CLIs (see diagnosis: the SSI injector denylists bare Python
interpreter invocations). Launch via run.sh, which wraps this script with
ddtrace-run.
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
LLMObs.enable(
    ml_app="tradingagents",
    service="tradingagents",
    env="prod",
    integrations_enabled=True,
    agentless_enabled=False,
)

import argparse
import copy
from datetime import datetime, timedelta

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

SELECTED_ANALYSTS = ("market", "social", "news", "fundamentals")


def most_recent_weekday() -> str:
    d = datetime.now()
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def build_config() -> dict:
    """DEFAULT_CONFIG with the OpenRouter preset applied."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "openrouter"
    config["backend_url"] = "https://openrouter.ai/api/v1"
    config["deep_think_llm"] = "openai/gpt-oss-120b"
    config["quick_think_llm"] = "openai/gpt-oss-20b"
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["output_language"] = "English"
    return config


def print_resolved_config(config: dict) -> None:
    print("=== Resolved config ===")
    print(f"llm_provider          = {config['llm_provider']}")
    print(f"backend_url            = {config['backend_url']}")
    print(f"deep_think_llm         = {config['deep_think_llm']}")
    print(f"quick_think_llm        = {config['quick_think_llm']}")
    print(f"max_debate_rounds      = {config['max_debate_rounds']}")
    print(f"max_risk_discuss_rounds= {config['max_risk_discuss_rounds']}")
    print(f"output_language        = {config['output_language']}")
    print(f"selected_analysts      = {SELECTED_ANALYSTS}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preset, Datadog-instrumented TradingAgents run (OpenRouter, shallow depth)."
    )
    parser.add_argument("ticker", nargs="?", default="DDOG", help="Ticker symbol (default: DDOG)")
    parser.add_argument(
        "date", nargs="?", default=None,
        help="Trade date YYYY-MM-DD (default: most recent weekday)",
    )
    args = parser.parse_args()

    trade_date = args.date or most_recent_weekday()
    config = build_config()

    print_resolved_config(config)
    print(f"ticker                 = {args.ticker}")
    print(f"trade_date             = {trade_date}")

    graph = TradingAgentsGraph(
        selected_analysts=SELECTED_ANALYSTS,
        config=config,
        debug=False,
    )

    try:
        final_state, decision = graph.propagate(args.ticker, trade_date)

        print("\n=== FINAL DECISION ===")
        print(decision)

        report_path = graph.save_reports(final_state, args.ticker)
        print(f"\nReports written to: {report_path}")
    finally:
        LLMObs.flush()
        ddtrace.tracer.shutdown()


if __name__ == "__main__":
    main()

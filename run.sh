#!/usr/bin/env bash
# Non-interactive entry point for the Datadog-instrumented TradingAgents preset runner.
# Usage: run.sh [TICKER] [DATE]   e.g. run.sh            (defaults: DDOG, most recent weekday)
#                                       run.sh NVDA 2026-08-06
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

export DD_SERVICE=tradingagents
export DD_ENV=prod
export DD_LLMOBS_ENABLED=1
export DD_LLMOBS_ML_APP=tradingagents

exec "$REPO_DIR/.venv/bin/ddtrace-run" "$REPO_DIR/.venv/bin/python" "$REPO_DIR/run_ddog.py" "$@"

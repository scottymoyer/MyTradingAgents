#!/usr/bin/env bash
# Non-interactive entry point for the Datadog-instrumented TradingAgents preset runner.
#
# Single ticker (original behavior, unchanged):
#   run.sh                       # DDOG, most recent weekday
#   run.sh NVDA 2026-08-06
#
# Portfolio modes (loop the engine once per ticker — cost scales with count):
#   run.sh --mode holdings                  # every ticker in holdings.yaml
#   run.sh --mode watchlist                 # every ticker in watchlist.yaml
#   run.sh --mode all                       # both, reported as separate groups
#   run.sh --mode all --limit 1             # cheap: at most 1 ticker per group
#   run.sh --mode watchlist --tickers COPX   # cheap: just this symbol
#   run.sh --mode all --dry-run             # resolve everything, NO LLM calls
#
# All flags are passed straight through to run_ddog.py; see --help.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

export DD_SERVICE=tradingagents
export DD_ENV=prod
export DD_LLMOBS_ENABLED=1
export DD_LLMOBS_ML_APP=tradingagents

exec "$REPO_DIR/.venv/bin/ddtrace-run" "$REPO_DIR/.venv/bin/python" "$REPO_DIR/run_ddog.py" "$@"

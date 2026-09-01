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

# Datadog Runtime Code Security (via ddtrace, which already wraps this process):
#   IAST  - Interactive Application Security Testing: instruments code as it runs
#           to flag vulnerabilities at their source (weak hashing, path traversal,
#           command/SQL injection, SSRF, insecure deserialization, etc.).
#   SCA   - Software Composition Analysis (runtime): reports the actually-loaded
#           dependencies + versions so Datadog matches them against known CVEs.
# Enabling IAST also turns on its parent App & API Protection product, but no
# threat-detection rules fire here -- this is a batch CLI with no inbound HTTP.
# Findings surface in Datadog under Application Security for service:tradingagents.
export DD_IAST_ENABLED=true
export DD_APPSEC_SCA_ENABLED=true

# Datadog source code integration: link telemetry to the exact commit + repo so
# stack frames deep-link to GitHub. Resolved from the checkout at run time (not
# baked in at build time), so it always reflects the running code and needs no
# rebuild. The deep-link only resolves if this commit is pushed to the repo, so
# commit + push before a run you want linked. Guarded so a missing git binary or
# a non-repo checkout can't abort the run under `set -e`.
if _sha="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)"; then
    export DD_GIT_COMMIT_SHA="$_sha"
    export DD_GIT_REPOSITORY_URL="$(git -C "$REPO_DIR" config --get remote.origin.url 2>/dev/null || true)"
fi

# Optional data-source credentials (FRED macro, Reddit OAuth). Each is
# independently optional; the app degrades gracefully when one is unset.
if [ -f "$HOME/.tradingagents.env" ]; then set -a; . "$HOME/.tradingagents.env"; set +a; fi

exec "$REPO_DIR/.venv/bin/ddtrace-run" "$REPO_DIR/.venv/bin/python" "$REPO_DIR/run_ddog.py" "$@"

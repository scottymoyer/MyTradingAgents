#!/usr/bin/env bash
# Scheduled TradingAgents run (invoked by cron, Mon/Wed/Fri).
#
# Runs the watchlist analysis, then chains the feedback loop:
#   1. run.sh <args>             -> analysis (defaults to --mode watchlist)
#   2. mock_portfolio.py resolve -> grade prior decisions that have now matured
#   3. mock_portfolio.py report  -> scorecard
#
# Args pass straight through to run.sh, so the same wrapper serves cron and manual use:
#   ./scheduled_run.sh                        # cron default: full watchlist
#   ./scheduled_run.sh --tickers INTC         # cheap manual test (~1 ticker)
#   ./scheduled_run.sh --mode watchlist --dry-run   # free plumbing test (no LLM calls)
#
# --mode watchlist analyzes whatever is in watchlist.yaml at run time, so trimming
# the watchlist automatically shrinks each run's cost and duration.
#
# Install the schedule (Mon/Wed/Fri 08:00 UTC), idempotent:
#   ( crontab -l 2>/dev/null | grep -v scheduled_run.sh;
#     echo "0 8 * * 1,3,5 $HOME/TradingAgents/scheduled_run.sh >> $HOME/.tradingagents/logs/scheduled/cron.log 2>&1"
#   ) | crontab -
# Remove the schedule:
#   crontab -l | grep -v scheduled_run.sh | crontab -

# No `set -e`: a failed analysis must NOT skip grading the prior decisions.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

PY="$REPO_DIR/.venv/bin/python"

# Load data-source creds + durable-store config (TRADINGAGENTS_DATABASE_URL,
# TRADINGAGENTS_S3_BUCKET, API keys) so BOTH run.sh and the direct mock_portfolio
# resolve/report calls below hit the same (Postgres) backend.
[ -f "$HOME/.tradingagents.env" ] && { set -a; . "$HOME/.tradingagents.env"; set +a; }
S3_BUCKET="${TRADINGAGENTS_S3_BUCKET:-tradingagents-963910217112-results}"
LOG_DIR="$HOME/.tradingagents/logs/scheduled"
LOCK="$HOME/.tradingagents/scheduled.lock"
mkdir -p "$LOG_DIR" "$(dirname "$LOCK")"
LOG="$LOG_DIR/$(date -u +%Y%m%d_%H%M%S).log"

# Overlap guard: if a previous run still holds the lock, skip this fire rather than
# stack a second concurrent analysis on the box.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') another scheduled run holds the lock; skipping" >>"$LOG"
    exit 0
fi

# Cron passes no args -> default to the full watchlist.
if [ "$#" -eq 0 ]; then
    set -- --mode watchlist
fi

{
    echo "=== scheduled run start $(date -u '+%Y-%m-%dT%H:%M:%SZ') : run.sh $* ==="

    "$REPO_DIR/run.sh" "$@"
    rc=$?
    [ "$rc" -ne 0 ] && echo ">>> analysis exited $rc; continuing to grade prior decisions"

    echo "=== resolve matured decisions $(date -u '+%H:%M:%SZ') ==="
    "$PY" "$REPO_DIR/mock_portfolio.py" resolve || echo ">>> resolve failed; continuing"

    echo "=== scorecard ==="
    "$PY" "$REPO_DIR/mock_portfolio.py" report || true

    echo "=== sync reports to s3://$S3_BUCKET/reports/ ==="
    if command -v aws >/dev/null 2>&1; then
        aws s3 sync "$HOME/.tradingagents/logs/reports" "s3://$S3_BUCKET/reports/" \
            --only-show-errors || echo ">>> s3 sync failed; continuing"
    else
        echo ">>> aws CLI not found; skipping s3 sync"
    fi

    echo "=== scheduled run done $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
} >>"$LOG" 2>&1

# Keep only the ~30 most recent run logs.
ls -1t "$LOG_DIR"/*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

exit 0

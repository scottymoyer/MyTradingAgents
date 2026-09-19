#!/usr/bin/env bash
# Launch the local results dashboard (Streamlit) on the EC2 box.
#
#   ./run_dashboard.sh          # foreground (Ctrl-C to stop) — good for a quick look
#   ./run_dashboard.sh --bg     # detached: survives logout, logs to ~/.tradingagents/logs/dashboard.log
#   ./run_dashboard.sh --stop   # stop a running dashboard
#
# Then from your LAPTOP, tunnel and open it locally (the server binds to 127.0.0.1
# only, so it is never publicly exposed):
#   ssh -L 8501:127.0.0.1:8501 ubuntu@18.118.161.66
#   open http://localhost:8501
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
PY="$REPO_DIR/.venv/bin"
PORT="${DASHBOARD_PORT:-8501}"
EC2_HOST="${EC2_HOST:-18.118.161.66}"
TUNNEL="ssh -L ${PORT}:127.0.0.1:${PORT} ubuntu@${EC2_HOST}   ->   http://localhost:${PORT}"

if [ "${1:-}" = "--stop" ]; then
    pkill -f "streamlit run dashboard.py" && echo "dashboard stopped" || echo "no dashboard running"
    exit 0
fi

# Already up? Don't stack a second server.
if curl -sf --max-time 2 "http://127.0.0.1:${PORT}/_stcore/health" >/dev/null 2>&1; then
    echo "dashboard already serving on 127.0.0.1:${PORT}"
    echo "tunnel:  ${TUNNEL}"
    exit 0
fi

# Ensure deps (first run only).
if ! "$PY/python" -c "import streamlit, boto3" 2>/dev/null; then
    echo ">> installing dashboard deps (streamlit, boto3)..."
    "$PY/pip" install -q ".[dashboard]" || "$PY/pip" install -q streamlit boto3
fi

CMD=("$PY/streamlit" run dashboard.py
     --server.headless true --server.port "$PORT"
     --server.address 127.0.0.1 --browser.gatherUsageStats false)

if [ "${1:-}" = "--bg" ]; then
    LOG="$HOME/.tradingagents/logs/dashboard.log"
    mkdir -p "$(dirname "$LOG")"
    nohup "${CMD[@]}" > "$LOG" 2>&1 &
    echo "dashboard starting in background (pid $!)"
    echo "log:     $LOG"
    echo "tunnel:  ${TUNNEL}"
    echo "stop:    ./run_dashboard.sh --stop"
else
    echo "starting dashboard on 127.0.0.1:${PORT} (Ctrl-C to stop)"
    echo "tunnel:  ${TUNNEL}"
    exec "${CMD[@]}"
fi

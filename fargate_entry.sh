#!/usr/bin/env bash
# Container entrypoint for the scheduled Fargate batch run.
#
# The ECS task def overrides the image's default ENTRYPOINT with this script
# (the image still runs `ddtrace-run python run_ddog.py` directly for EKS/manual
# use). It's the container equivalent of the VM's scheduled_run.sh, minus the
# VM-only bits: no flock (one task per fire, no overlap) and no env-file sourcing
# (ECS injects env from Secrets Manager + the task def).
#
# Chain (args after the script name pass straight to run_ddog.py):
#   0. pull the live watchlist from S3 over the baked default (edit tickers w/o rebuild)
#   1. ddtrace-run python run_ddog.py <args>   -> analysis (defaults to --mode watchlist)
#   2. mock_portfolio.py resolve               -> grade prior decisions that matured
#   3. mock_portfolio.py report                -> scorecard
#   4. s3_sync.py push-reports                 -> reports -> S3 (durable)
#   5. brief sleep so the Datadog agent sidecar flushes final traces/metrics
#
# No `set -e`: a failed analysis must NOT skip grading prior decisions, and the
# task should still stop cleanly so the sidecar (essential:false) is torn down.
set -uo pipefail
cd /home/appuser/app

# Cron/scheduler passes no args -> default to the full watchlist.
if [ "$#" -eq 0 ]; then
    set -- --mode watchlist
fi

echo "=== fargate run start $(date -u '+%Y-%m-%dT%H:%M:%SZ') : run_ddog.py $* ==="

python s3_sync.py pull-watchlist || echo ">>> watchlist pull failed; using baked default"

ddtrace-run python run_ddog.py "$@"
rc=$?
[ "$rc" -ne 0 ] && echo ">>> analysis exited $rc; continuing to grade prior decisions"

echo "=== resolve matured decisions $(date -u '+%H:%M:%SZ') ==="
python mock_portfolio.py resolve || echo ">>> resolve failed; continuing"

echo "=== scorecard ==="
python mock_portfolio.py report || true

echo "=== push reports to S3 ==="
python s3_sync.py push-reports || echo ">>> report upload failed; continuing"

echo "=== fargate run done $(date -u '+%Y-%m-%dT%H:%M:%SZ') (analysis rc=$rc) ==="

# Give the Datadog agent sidecar a moment to flush the final trace/metric batch
# before the essential container exits and ECS tears the task down.
sleep 20

exit 0

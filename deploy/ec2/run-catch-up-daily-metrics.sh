#!/usr/bin/env bash
# Catch up daily vm_metric_points + standard metrics/analyst after missed daily job runs.
# Usage: run-catch-up-daily-metrics.sh [SINCE_DATE]   (default: 2026-06-19)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
SINCE_DATE="${1:-2026-06-19}"
LOG="${LOG_DIR}/catch-up-daily-metrics-${SINCE_DATE}.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"
export PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/backend"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) catch-up start (since=${SINCE_DATE}) ========"
  python - <<PY
from pathlib import Path
from interesting_stocks_service import extend_recent_daily_metrics

vm = Path("${VALUE_METRICS_DB_PATH}")
out = extend_recent_daily_metrics(vm, since_date="${SINCE_DATE}")
print(out)
PY
  python "$REPO_ROOT/backend/daily_market_refresh.py" --skip-prices --skip-daily-metrics
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) catch-up done ========"
} 2>&1 | tee -a "$LOG"

#!/usr/bin/env bash
# Daily pipeline: news ingest + gap backfills + price/momentum/analyst refresh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/daily-jobs.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

INGEST_ARGS=()
if [[ "${SKIP_TELEGRAM_INGEST:-}" == "1" ]]; then
  INGEST_ARGS=(--skip-ingest)
fi

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily jobs start ========"
  python "$REPO_ROOT/backend/interesting_stocks_daily_backfill.py" "${INGEST_ARGS[@]}"
  python "$REPO_ROOT/backend/daily_market_refresh.py"
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily jobs done ========"
} >>"$LOG" 2>&1

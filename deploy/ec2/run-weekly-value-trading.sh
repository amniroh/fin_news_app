#!/usr/bin/env bash
# Weekly value-trading agent: assess interesting stocks missing or expired valuations.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/weekly-value-trading.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export VALUE_TRADING_REASSESS_DAYS="${VALUE_TRADING_REASSESS_DAYS:-60}"
export VALUE_TRADING_BATCH_SIZE="${VALUE_TRADING_BATCH_SIZE:-10}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) value-trading weekly start ========"
  python "$REPO_ROOT/backend/value_trading_agent_run.py" --batch-size "${VALUE_TRADING_BATCH_SIZE}"
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) value-trading weekly done ========"
} >>"$LOG" 2>&1

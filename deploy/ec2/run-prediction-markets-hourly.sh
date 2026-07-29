#!/usr/bin/env bash
# Hourly prediction-market sync (Polymarket + Kalshi) into value_metrics.sqlite.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/prediction-markets-hourly.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
# Hourly defaults: large candidate scan, keep many open markets; settled less often via settled-keep.
export PM_POOL_SIZE="${PM_POOL_SIZE:-2000}"
export PM_KEEP="${PM_KEEP:-500}"
export PM_SETTLED_KEEP="${PM_SETTLED_KEEP:-150}"
# Optional: restrict fetches to a comma-separated allowlist (default = all categories)
# export PM_CATEGORY_ALLOWLIST=
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) prediction-markets hourly start ========"
  python "$REPO_ROOT/backend/prediction_markets_sync_cli.py" \
    --pool-size "${PM_POOL_SIZE}" \
    --keep "${PM_KEEP}" \
    --settled-keep "${PM_SETTLED_KEEP}" \
    --no-trades
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) prediction-markets hourly done ========"
} >>"$LOG" 2>&1

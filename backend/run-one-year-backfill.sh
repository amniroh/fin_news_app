#!/usr/bin/env bash
# Full 1-year backfill for all interesting stocks (prices, fundamentals, news, analyst, momentum).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/one-year-backfill-$(date -u +%Y%m%d).log"

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

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) one-year backfill start ========"
  echo "VM DB: $VALUE_METRICS_DB_PATH"
  echo "Agent DB: $AGENT_DB_PATH"

  python "$REPO_ROOT/backend/interesting_stocks_daily_backfill.py" --seed-only

  python - <<'PY'
from pathlib import Path
from interesting_stocks_service import run_daily_backfill_pipeline, seed_interesting_stocks_from_universe

vm = Path("backend/data/value_metrics.sqlite")
seed_interesting_stocks_from_universe(vm)
result = run_daily_backfill_pipeline(
    vm,
    only_gaps=False,
    years=1.0,
    run_ingest=True,
)
import json
print(json.dumps({
    "coverage_before": result.get("coverage_before"),
    "coverage_after": result.get("coverage_after"),
    "jobs": list((result.get("backfill") or {}).get("jobs", {}).keys()),
}, indent=2))
PY

  python "$REPO_ROOT/backend/daily_market_refresh.py" --price-days 365

  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) one-year backfill done ========"
} 2>&1 | tee -a "$LOG"

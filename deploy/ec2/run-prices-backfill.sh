#!/usr/bin/env bash
# Backfill daily prices for all interesting stocks (last 365 days).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/prices-backfill-$(date -u +%Y%m%d).log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"
export SYMBOL_UNIVERSE_PATH="${SYMBOL_UNIVERSE_PATH:-$REPO_ROOT/telegram_agent/top1000_investments.json}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi
export SYMBOL_UNIVERSE_PATH="${SYMBOL_UNIVERSE_PATH:-$REPO_ROOT/telegram_agent/top1000_investments.json}"

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) prices backfill start ========"
  PYTHONPATH="$REPO_ROOT:$REPO_ROOT/backend" python - <<'PY'
from pathlib import Path
from value_metrics_store import connect, init_db, list_interesting_stocks
from interesting_stocks_service import _backfill_prices

vm = Path("backend/data/value_metrics.sqlite")
con = connect(vm)
init_db(con)
syms = [str(r["symbol"]) for r in list_interesting_stocks(con)]
con.close()
days = 365
print(f"Backfilling {len(syms)} symbols, {days} days")
out = _backfill_prices(syms, days=days)
print(out)
PY
  python "$REPO_ROOT/backend/daily_market_refresh.py" --price-days 365
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) prices backfill done ========"
} 2>&1 | tee -a "$LOG"

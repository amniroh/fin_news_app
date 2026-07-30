#!/usr/bin/env bash
# DEPRECATED: value-trading runs from the orchestrator on Sundays (ORCHESTRATOR_VALUE_TRADING=auto).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "run-weekly-value-trading.sh is deprecated; forwarding to value_trading_agent_run.py once." >&2
cd "$REPO_ROOT"
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"
export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi
exec python "$REPO_ROOT/backend/value_trading_agent_run.py" --batch-size "${VALUE_TRADING_BATCH_SIZE:-10}"

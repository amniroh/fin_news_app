#!/bin/bash
set -euo pipefail
cd "$HOME/market_analysis"
source .venv/bin/activate
export VALUE_METRICS_DB_PATH="$HOME/market_analysis/backend/data/value_metrics.sqlite"
export PYTHONUNBUFFERED=1
exec python backend/prediction_markets_sync_cli.py --skip-sync --backfill-days 30

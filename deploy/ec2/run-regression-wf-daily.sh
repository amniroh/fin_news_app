#!/usr/bin/env bash
# Daily walk-forward retrain for regression_based_on_technicals + optional IB paper rebalance.
# Intended to run AFTER the orchestrator (fresh prices) and a technicals extend.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/regression-wf-daily.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"
export ML_PAPER_BACKEND_DIR="${ML_PAPER_BACKEND_DIR:-$REPO_ROOT/backend}"

# History window: technicals currently span ~2y on EC2; override with REGRESSION_WF_YEARS.
YEARS="${REGRESSION_WF_YEARS:-2.0}"
WF_MODE="${REGRESSION_WF_MODE:-auto}"
MAX_FOLDS="${REGRESSION_WF_MAX_FOLDS:-8}"
MIN_TRAIN_ROWS="${REGRESSION_WF_MIN_TRAIN_ROWS:-80}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) regression-wf-daily start years=${YEARS} mode=${WF_MODE} ========"

  if [[ "${REGRESSION_WF_SKIP_TECHNICALS:-0}" != "1" ]]; then
    echo "--- technicals extend-only ---"
    python backend/technical_indicators_backfill.py --extend-only || {
      echo "WARN: technicals extend failed; continuing with existing indicators"
    }
  fi

  if [[ "${REGRESSION_WF_DAILY:-1}" == "1" ]]; then
    echo "--- walk-forward train ---"
    python backend/regression_technicals_train.py \
      --years "$YEARS" \
      --wf-mode "$WF_MODE" \
      --max-folds "$MAX_FOLDS" \
      --min-train-rows "$MIN_TRAIN_ROWS" \
      --allow-partial-universe
  else
    echo "REGRESSION_WF_DAILY=0 — skip train"
  fi

  if [[ "${IB_PAPER_REGRESSION:-1}" == "1" ]]; then
    echo "--- IB paper rebalance (execute=${IB_PAPER_EXECUTE:-0}) ---"
    # Default dry-run unless IB_PAPER_EXECUTE=1 and Gateway/TWS is reachable.
    EXTRA=()
    if [[ "${IB_PAPER_EXECUTE:-0}" == "1" ]]; then
      EXTRA+=(--execute)
    else
      EXTRA+=(--dry-run)
    fi
    python packages/ml_ib_paper/regression_paper_rebalance.py "${EXTRA[@]}" \
      --deploy-fraction "${IB_PAPER_DEPLOY_FRACTION:-0.95}" \
      || {
        echo "WARN: paper rebalance failed (IB Gateway down or model missing?)"
        # Do not fail the whole unit if only paper trading fails after a successful train.
        true
      }
  else
    echo "IB_PAPER_REGRESSION=0 — skip paper rebalance"
  fi

  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) regression-wf-daily done ========"
} >>"$LOG" 2>&1

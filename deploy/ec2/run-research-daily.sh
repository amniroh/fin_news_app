#!/usr/bin/env bash
# Daily research agent run (bounded trial window via RESEARCH_TRIAL_END_DATE).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/research-daily.log"
STATE="${REPO_ROOT}/deploy/ec2/research-trial-end-date.txt"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_RESEARCH_PUBLISH="${AGENT_RESEARCH_PUBLISH:-false}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi
if [[ -f "$REPO_ROOT/telegram_agent/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/telegram_agent/.env"
  set +a
fi

# Trial default: quality/cost balance. Allow RESEARCH_DAILY_MODEL to override without changing global AGENT_RESEARCH_MODEL.
export AGENT_RESEARCH_MODEL="${RESEARCH_DAILY_MODEL:-google/gemini-2.5-flash}"

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

END_DATE="${RESEARCH_TRIAL_END_DATE:-}"
if [[ -z "$END_DATE" && -f "$STATE" ]]; then
  END_DATE="$(tr -d '[:space:]' < "$STATE" || true)"
fi
TODAY="$(date -u +%Y-%m-%d)"
if [[ -n "$END_DATE" ]]; then
  if [[ "$TODAY" > "$END_DATE" ]]; then
    {
      echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) research-daily skipped (past trial end ${END_DATE}) ========"
    } >>"$LOG"
    exit 0
  fi
fi

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) research-daily start model=${AGENT_RESEARCH_MODEL} end=${END_DATE:-none} ========"
  python -m telegram_agent.agent research \
    --model "${AGENT_RESEARCH_MODEL}" \
    --max-num-ofnews "${AGENT_RESEARCH_MAX_NEWS:-200}" \
    --research-max-priority "${AGENT_RESEARCH_MAX_PRIORITY:-1}"
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) research-daily done ========"
} >>"$LOG" 2>&1

#!/usr/bin/env bash
# Single daily pipeline: news/prices ingest → interesting-stocks enrich → preprocess →
# tester → research (+ memory) → optional Sunday value-trading.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/orchestrator-daily.log"

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"
export AGENT_RESEARCH_PUBLISH="${AGENT_RESEARCH_PUBLISH:-true}"
# Research model for this daily desk (override with RESEARCH_DAILY_MODEL).
export AGENT_RESEARCH_MODEL="${RESEARCH_DAILY_MODEL:-${AGENT_RESEARCH_MODEL:-google/gemini-2.5-flash}}"
# Value-trading: auto = Sundays UTC only.
export ORCHESTRATOR_VALUE_TRADING="${ORCHESTRATOR_VALUE_TRADING:-auto}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi
# Preserve root TARGET_CHANNEL if telegram_agent/.env still has a placeholder.
_SAVED_TARGET_CHANNEL="${TARGET_CHANNEL:-}"
if [[ -f "$REPO_ROOT/telegram_agent/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/telegram_agent/.env"
  set +a
fi
if [[ -n "$_SAVED_TARGET_CHANNEL" ]]; then
  case "${TARGET_CHANNEL:-}" in
    ""|"@my_digest_channel"|"my_digest_channel")
      export TARGET_CHANNEL="$_SAVED_TARGET_CHANNEL"
      ;;
  esac
fi
unset _SAVED_TARGET_CHANNEL

# Re-apply desk defaults after .env (publish on; Gemini unless RESEARCH_DAILY_MODEL set).
export AGENT_RESEARCH_MODEL="${RESEARCH_DAILY_MODEL:-google/gemini-2.5-flash}"
if [[ "${ORCHESTRATOR_DISABLE_PUBLISH:-}" == "1" ]]; then
  export AGENT_RESEARCH_PUBLISH=false
else
  export AGENT_RESEARCH_PUBLISH=true
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) orchestrator-daily start model=${AGENT_RESEARCH_MODEL} ========"
  bash "$REPO_ROOT/deploy/ec2/run-orchestrator.sh" orchestrate
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) orchestrator-daily done ========"
} >>"$LOG" 2>&1

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
export AGENT_RESEARCH_MODEL="${RESEARCH_DAILY_MODEL:-${AGENT_RESEARCH_MODEL:-google/gemini-4-argon}}"
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

# Optional end of a research trial (ISO8601 UTC, e.g. 2026-11-06T12:00:00Z): after it, research/memory
# and Telegram publishing are skipped while ingest/prices/market refresh keep running.
_research_window_over=0
if [[ -n "${AGENT_RESEARCH_UNTIL_UTC:-}" ]]; then
  if (( $(date -u +%s) > $(date -u -d "$AGENT_RESEARCH_UNTIL_UTC" +%s) )); then
    _research_window_over=1
    export ORCHESTRATOR_SKIP_RESEARCH=1
  fi
fi

# Re-apply desk defaults after .env (publish on). Model: RESEARCH_DAILY_MODEL > .env AGENT_RESEARCH_MODEL > default.
export AGENT_RESEARCH_MODEL="${RESEARCH_DAILY_MODEL:-${AGENT_RESEARCH_MODEL:-google/gemini-4-argon}}"
if [[ "${ORCHESTRATOR_DISABLE_PUBLISH:-}" == "1" || "$_research_window_over" == "1" ]]; then
  export AGENT_RESEARCH_PUBLISH=false
else
  export AGENT_RESEARCH_PUBLISH=true
fi

# Optional A/B arm B: same news/prices, own memory + recommendations DB (telegram_agent/research_variant.py).
_run_arm_b=0
if [[ -n "${AGENT_RESEARCH_B_MODEL:-}" && "$_research_window_over" == "0" && "${ORCHESTRATOR_SKIP_RESEARCH:-}" != "1" ]]; then
  _run_arm_b=1
  export AGENT_RESEARCH_VARIANT_LABEL="${AGENT_RESEARCH_VARIANT_LABEL:-A}"
fi

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

{
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) orchestrator-daily start model=${AGENT_RESEARCH_MODEL} arm_b=${AGENT_RESEARCH_B_MODEL:-none} research_until=${AGENT_RESEARCH_UNTIL_UTC:-none} research_skipped=${_research_window_over} ========"
  bash "$REPO_ROOT/deploy/ec2/run-orchestrator.sh" orchestrate
  if [[ "$_run_arm_b" == "1" ]]; then
    echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) research arm B start model=${AGENT_RESEARCH_B_MODEL} ========"
    _b_publish=()
    [[ "$AGENT_RESEARCH_PUBLISH" == "true" ]] || _b_publish=(--no-publish)
    python -m telegram_agent.research_variant run \
      --db "${AGENT_RESEARCH_B_DB:-$REPO_ROOT/telegram_agent/data/agent_b.sqlite}" \
      --model "$AGENT_RESEARCH_B_MODEL" --label "${AGENT_RESEARCH_B_LABEL:-B}" "${_b_publish[@]}" \
      || echo "research arm B failed (exit $?)"
  fi
  echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) orchestrator-daily done ========"
} >>"$LOG" 2>&1

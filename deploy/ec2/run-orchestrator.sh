#!/usr/bin/env bash
# Run orchestrator from repo root (ARM/x86). Use after bootstrap + .env.
# Example: bash deploy/ec2/run-orchestrator.sh orchestrate --backfill-from 2026-01-01 --backfill-to 2026-01-31
set -euo pipefail

REPO_ROOT="${MARKET_ANALYSIS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

export MPLBACKEND="${MPLBACKEND:-Agg}"

if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.venv/bin/activate"
fi

PY=python3
if command -v python &>/dev/null; then
  PY=python
elif command -v python3.11 &>/dev/null; then
  PY=python3.11
fi

if [[ $# -eq 0 ]]; then
  set -- orchestrate
fi

exec "$PY" -m telegram_agent.agent "$@"

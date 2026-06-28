#!/usr/bin/env bash
# Full server refresh: pip deps, build frontend, deploy nginx static, restart API, backfill data.
# Usage (on EC2, from repo root):
#   bash deploy/ec2/run-server-refresh.sh
#   bash deploy/ec2/run-server-refresh.sh --skip-technical-full   # skip long TA backfill
#   bash deploy/ec2/run-server-refresh.sh --skip-daily-jobs         # code-only deploy
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="${LOG_DIR}/server-refresh-${STAMP}.log"

SKIP_TECHNICAL_FULL=0
SKIP_DAILY=0
for arg in "$@"; do
  case "$arg" in
    --skip-technical-full) SKIP_TECHNICAL_FULL=1 ;;
    --skip-daily-jobs) SKIP_DAILY=1 ;;
  esac
done

export MPLBACKEND=Agg
export VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$REPO_ROOT/backend/data/value_metrics.sqlite}"
export AGENT_DB_PATH="${AGENT_DB_PATH:-$REPO_ROOT/telegram_agent/data/agent.sqlite}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/.env"
  set +a
fi

exec > >(tee -a "$LOG") 2>&1

echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) server refresh start ========"
echo "Log file: $LOG"
echo "Repo: $REPO_ROOT"
echo "Git: $(git rev-parse --short HEAD 2>/dev/null || echo unknown) $(git branch --show-current 2>/dev/null || true)"

# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"

echo "==> pip install"
pip install -q -r "$REPO_ROOT/deploy/ec2/requirements-server.txt"

echo "==> build value_web"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck source=/dev/null
[[ -s "$NVM_DIR/nvm.sh" ]] && source "$NVM_DIR/nvm.sh" && nvm use 20
cd "$REPO_ROOT/value_web"
npm ci
VITE_API_BASE="" npm run build
cd "$REPO_ROOT"

echo "==> deploy static files"
sudo rsync -a --delete "$REPO_ROOT/value_web/dist/" /var/www/value-web/
sudo chown -R nginx:nginx /var/www/value-web

echo "==> restart services"
sudo systemctl restart value-web-backend
sudo systemctl reload nginx

echo "==> health check"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf http://127.0.0.1:8000/health >/dev/null; then
    curl -s http://127.0.0.1:8000/health
    echo ""
    break
  fi
  sleep 2
done
curl -sf http://127.0.0.1:8000/health >/dev/null || { echo "health check failed"; exit 1; }

if [[ "$SKIP_TECHNICAL_FULL" -eq 0 ]]; then
  echo "==> technical indicators full backfill"
  python "$REPO_ROOT/backend/technical_indicators_backfill.py"
else
  echo "==> skip technical indicators full backfill"
fi

if [[ "$SKIP_DAILY" -eq 0 ]]; then
  echo "==> daily jobs pipeline (SKIP_TELEGRAM_INGEST=1 on headless servers)"
  SKIP_TELEGRAM_INGEST=1 bash "$REPO_ROOT/deploy/ec2/run-daily-jobs.sh"
else
  echo "==> skip daily jobs"
fi

echo "==> tracker sample"
curl -sf "http://127.0.0.1:8000/value/metrics/tracker?priority=0" | python3 -c "
import sys, json
rows = json.load(sys.stdin).get('rows') or []
if not rows:
    print('no rows')
else:
    r = rows[0]
    print('sample', r.get('symbol'), 'ema=', r.get('ema'), 'adx=', r.get('adx'), 'rvol=', r.get('rvol'))
"

echo "======== $(date -u +%Y-%m-%dT%H:%M:%SZ) server refresh done ========"

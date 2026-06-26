#!/usr/bin/env bash
# Copy local SQLite databases to the EC2 server (not in git — must be synced manually).
#
# Usage (from repo root on your Mac):
#   bash deploy/ec2/sync-local-data.sh
#   bash deploy/ec2/sync-local-data.sh --with-agent   # includes 11GB agent.sqlite (needs expanded EBS)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE="${REMOTE:-investor-bot}"
WITH_AGENT=false
for arg in "$@"; do
  [[ "$arg" == "--with-agent" ]] && WITH_AGENT=true
done

VM_DB="${REPO_ROOT}/backend/data/value_metrics.sqlite"
AGENT_DB="${REPO_ROOT}/telegram_agent/data/agent.sqlite"

if [[ ! -f "$VM_DB" ]]; then
  echo "Missing $VM_DB"
  exit 1
fi

echo "Stopping remote backend..."
ssh "$REMOTE" 'sudo systemctl stop value-web-backend'

echo "Syncing value_metrics.sqlite ($(du -h "$VM_DB" | cut -f1))..."
rsync -av --progress "$VM_DB" "$REMOTE:~/market_analysis/backend/data/value_metrics.sqlite"

if $WITH_AGENT; then
  if [[ ! -f "$AGENT_DB" ]]; then
    echo "Missing $AGENT_DB"
    exit 1
  fi
  echo "Syncing agent.sqlite ($(du -h "$AGENT_DB" | cut -f1)) — ensure EC2 disk is large enough..."
  rsync -av --progress "$AGENT_DB" "$REMOTE:~/market_analysis/telegram_agent/data/agent.sqlite"
else
  echo "Skipping agent.sqlite (use --with-agent after expanding EBS volume; ~11GB)."
fi

echo "Starting remote backend..."
ssh "$REMOTE" 'sudo systemctl start value-web-backend'
echo "Done."

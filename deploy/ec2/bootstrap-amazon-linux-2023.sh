#!/usr/bin/env bash
# One-time setup on Amazon Linux 2023 (aarch64 / t4g).
# Run: bash deploy/ec2/bootstrap-amazon-linux-2023.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Repo root: $REPO_ROOT"

sudo dnf update -y
sudo dnf install -y git gcc python3.11 python3.11-pip python3.11-devel

# Headless matplotlib / any GUI backends
grep -q 'MPLBACKEND' ~/.bashrc 2>/dev/null || echo 'export MPLBACKEND=Agg' >> ~/.bashrc
export MPLBACKEND=Agg

PY=python3.11
"$PY" -m venv "$REPO_ROOT/.venv"
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"
python -m pip install -U pip wheel
python -m pip install -r "$REPO_ROOT/telegram_agent/requirements.txt"

echo ""
echo "Done. Next:"
echo "  1. Copy .env to $REPO_ROOT/.env (see deploy/ec2/env.template)"
echo "  2. scp -r telegram_agent/data telegram_agent/sessions  (if migrating from laptop)"
echo "  3. bash deploy/ec2/run-orchestrator.sh orchestrate --backfill-from YYYY-MM-DD --backfill-to YYYY-MM-DD"
echo ""

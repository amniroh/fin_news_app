#!/usr/bin/env bash
# Full server bootstrap: Python venv, backend deps, Node.js, nginx.
# Run on Amazon Linux 2023 from repo root:
#   bash deploy/ec2/bootstrap-full.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "==> Repo root: $REPO_ROOT"

sudo dnf update -y
sudo dnf install -y git gcc python3.11 python3.11-pip python3.11-devel nginx

# Vite 7 requires Node 20+; install via nvm (Amazon Linux ships Node 18).
export NVM_DIR="$HOME/.nvm"
if [[ ! -s "$NVM_DIR/nvm.sh" ]]; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
fi
# shellcheck source=/dev/null
source "$NVM_DIR/nvm.sh"
nvm install 20
nvm use 20
node --version
npm --version

grep -q 'MPLBACKEND' ~/.bashrc 2>/dev/null || echo 'export MPLBACKEND=Agg' >> ~/.bashrc
export MPLBACKEND=Agg

PY=python3.11
"$PY" -m venv "$REPO_ROOT/.venv"
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"
python -m pip install -U pip wheel
python -m pip install -r "$REPO_ROOT/deploy/ec2/requirements-server.txt"

mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/backend/data" "$REPO_ROOT/telegram_agent/data"

echo "==> Building value_web (production)"
cd "$REPO_ROOT/value_web"
export NVM_DIR="$HOME/.nvm"
# shellcheck source=/dev/null
[[ -s "$NVM_DIR/nvm.sh" ]] && source "$NVM_DIR/nvm.sh" && nvm use 20
npm ci 2>/dev/null || npm install
VITE_API_BASE="" npm run build

echo ""
echo "Bootstrap complete."
echo "  1. Copy .env to $REPO_ROOT/.env (see deploy/ec2/env.template)"
echo "  2. bash deploy/ec2/install-services.sh"
echo ""

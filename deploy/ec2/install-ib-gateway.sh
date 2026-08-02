#!/usr/bin/env bash
# Install Docker (if needed) and the paper-only IB Gateway unit.
# Does NOT start Gateway until deploy/ec2/ib-gateway/.env has credentials.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GW_DIR="$REPO_ROOT/deploy/ec2/ib-gateway"

echo "==> Ensuring ib-gateway/.env exists (empty credentials until you fill them)"
if [[ ! -f "$GW_DIR/.env" ]]; then
  cp "$GW_DIR/.env.example" "$GW_DIR/.env"
  chmod 600 "$GW_DIR/.env"
  echo "Created $GW_DIR/.env — edit TWS_USERID / TWS_PASSWORD before starting Gateway."
else
  chmod 600 "$GW_DIR/.env" || true
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  sudo dnf install -y docker docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker ec2-user || true
  echo "Docker installed. You may need to re-login for group membership."
fi

sudo cp "$REPO_ROOT/deploy/ec2/ib-gateway-paper.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ib-gateway-paper.service

echo ""
echo "IB Gateway paper unit installed (not started until credentials are set)."
echo "  1. nano $GW_DIR/.env   # TWS_USERID=  TWS_PASSWORD="
echo "  2. sudo systemctl start ib-gateway-paper"
echo "  3. In repo .env: IB_HOST=127.0.0.1 IB_PORT=4002 IB_TRADING_MODE=paper"
echo "  4. Keep IB_PAPER_EXECUTE=0 until logs show: IB paper safety OK — using account DU…"
echo ""

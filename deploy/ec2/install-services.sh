#!/usr/bin/env bash
# Install systemd units for backend, nginx, and scheduled jobs.
# Run from repo root after bootstrap-full.sh and .env are in place.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

chmod +x "$REPO_ROOT/deploy/ec2/"*.sh

sudo cp "$REPO_ROOT/deploy/ec2/value-web-backend.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/orchestrator-daily.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/orchestrator-daily.timer" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/prediction-markets-hourly.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/prediction-markets-hourly.timer" /etc/systemd/system/

# Keep legacy unit files on disk for rollback, but do not enable them.
sudo cp "$REPO_ROOT/deploy/ec2/daily-jobs.service" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$REPO_ROOT/deploy/ec2/daily-jobs.timer" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$REPO_ROOT/deploy/ec2/weekly-value-trading.service" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$REPO_ROOT/deploy/ec2/weekly-value-trading.timer" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$REPO_ROOT/deploy/ec2/research-daily.service" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$REPO_ROOT/deploy/ec2/research-daily.timer" /etc/systemd/system/ 2>/dev/null || true

sudo cp "$REPO_ROOT/deploy/ec2/nginx-value-web.conf" /etc/nginx/conf.d/value-web.conf
# Drop default server block if present (Amazon Linux nginx package).
if [[ -f /etc/nginx/nginx.conf ]] && grep -q 'include /etc/nginx/conf.d/\*.conf' /etc/nginx/nginx.conf; then
  sudo rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
fi

sudo mkdir -p /var/www/value-web
sudo rsync -a --delete "$REPO_ROOT/value_web/dist/" /var/www/value-web/
sudo chown -R nginx:nginx /var/www/value-web

sudo systemctl daemon-reload
sudo systemctl enable --now value-web-backend.service
sudo systemctl enable --now nginx.service
sudo systemctl restart nginx

# Consolidated daily desk (replaces daily-jobs + research-daily + weekly-value-trading).
sudo systemctl enable --now orchestrator-daily.timer
sudo systemctl enable --now prediction-markets-hourly.timer

# Disable redundant timers if previously enabled.
sudo systemctl disable --now daily-jobs.timer 2>/dev/null || true
sudo systemctl disable --now research-daily.timer 2>/dev/null || true
sudo systemctl disable --now weekly-value-trading.timer 2>/dev/null || true

echo ""
echo "Services installed."
echo "  Website:  http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo 'YOUR_EC2_PUBLIC_IP')"
echo "  Backend:  systemd value-web-backend (port 8000, proxied via nginx :80)"
echo "  Daily:    orchestrator-daily.timer (21:00 UTC, after US close)"
echo "            ingest → prices → stocks enrich → preprocess → tester → research"
echo "            (+ value-trading on Sundays UTC; Telegram TARGET_CHANNEL publish on)"
echo "  Hourly:   prediction-markets-hourly.timer (Polymarket + Kalshi)"
echo ""
echo "Ensure EC2 security group allows inbound TCP 80 (and 22 for SSH)."
echo "Logs: $REPO_ROOT/logs/orchestrator-daily.log"
echo "      $REPO_ROOT/logs/prediction-markets-hourly.log"
echo "      journalctl -u value-web-backend -f"
echo ""

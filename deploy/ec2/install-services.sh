#!/usr/bin/env bash
# Install systemd units for backend, nginx, and scheduled jobs.
# Run from repo root after bootstrap-full.sh and .env are in place.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

chmod +x "$REPO_ROOT/deploy/ec2/"*.sh

sudo cp "$REPO_ROOT/deploy/ec2/value-web-backend.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/daily-jobs.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/daily-jobs.timer" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/weekly-value-trading.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/deploy/ec2/weekly-value-trading.timer" /etc/systemd/system/

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
sudo systemctl enable --now daily-jobs.timer
sudo systemctl enable --now weekly-value-trading.timer

echo ""
echo "Services installed."
echo "  Website:  http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo 'YOUR_EC2_PUBLIC_IP')"
echo "  Backend:  systemd value-web-backend (port 8000, proxied via nginx :80)"
echo "  Daily:    daily-jobs.timer (06:00 UTC)"
echo "  Weekly:   weekly-value-trading.timer (Sun 07:00 UTC)"
echo ""
echo "Ensure EC2 security group allows inbound TCP 80 (and 22 for SSH)."
echo "Logs: $REPO_ROOT/logs/daily-jobs.log  $REPO_ROOT/logs/weekly-value-trading.log"
echo "      journalctl -u value-web-backend -f"
echo ""

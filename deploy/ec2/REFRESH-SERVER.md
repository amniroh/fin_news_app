# Server refresh runbook

Use this after `git pull` on the EC2 host (`investor-bot` / `~/market_analysis`) to deploy new code and refresh market data.

## Prerequisites

- SSH access: `ssh investor-bot` (or `ssh ec2-user@<public-ip>`)
- Repo at `~/market_analysis`, branch `master` (or `main`)
- `.env` configured (see `deploy/ec2/env.template`)
- Node 20 via nvm (installed by `bootstrap-full.sh`)

## 1. Code deploy (frontend + backend)

```bash
cd ~/market_analysis
git pull origin master   # skip if already pulled

source .venv/bin/activate
pip install -r deploy/ec2/requirements-server.txt

export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh" && nvm use 20
cd value_web && npm ci && VITE_API_BASE="" npm run build && cd ..

sudo rsync -a --delete value_web/dist/ /var/www/value-web/
sudo chown -R nginx:nginx /var/www/value-web
sudo systemctl restart value-web-backend
sudo systemctl reload nginx
```

## 2. Data refresh

### Daily pipeline (recommended every deploy)

Runs gap backfills, prices, daily fundamentals metrics, technical indicators (incremental), standard metrics, analyst ratings.

```bash
cd ~/market_analysis
mkdir -p logs
bash deploy/ec2/run-daily-jobs.sh
tail -f logs/daily-jobs.log
```

Or trigger the systemd unit manually:

```bash
sudo systemctl start daily-jobs.service
journalctl -u daily-jobs.service -f
```

**Headless / no Telethon session:** set `SKIP_TELEGRAM_INGEST=1` so the daily script skips interactive Telegram login:

```bash
SKIP_TELEGRAM_INGEST=1 bash deploy/ec2/run-daily-jobs.sh
```

`run-server-refresh.sh` sets this automatically.

### Technical indicators — full backfill (first deploy or after schema change)

Populates `vm_technical_indicators` for all symbols with daily prices (~5–15 min for ~900 symbols).

```bash
cd ~/market_analysis
source .venv/bin/activate
python backend/technical_indicators_backfill.py 2>&1 | tee logs/technical-indicators-backfill.log
```

Incremental only (daily job also does this):

```bash
python backend/technical_indicators_backfill.py --extend-only
```

### Catch-up after missed daily jobs

```bash
bash deploy/ec2/run-catch-up-daily-metrics.sh 2026-06-19
```

## 3. Verify

```bash
curl -s http://127.0.0.1:8000/health
curl -s "http://127.0.0.1:8000/value/metrics/tracker?priority=0" | python3 -c "
import sys, json
r = json.load(sys.stdin)['rows'][0]
print('symbol', r.get('symbol'))
print('ema', r.get('ema'), 'adx', r.get('adx'), 'rvol', r.get('rvol'))
print('metrics_asof', r.get('metrics_asof_date'), 'tech_asof', r.get('tech_asof_date'))
"
```

Public site: `http://<EC2_PUBLIC_IP>/` — hard refresh (Cmd+Shift+R) after deploy.

## 4. Logs

| Log | Path |
|-----|------|
| Daily jobs | `~/market_analysis/logs/daily-jobs.log` |
| Technical backfill | `~/market_analysis/logs/technical-indicators-backfill.log` |
| Backend API | `journalctl -u value-web-backend -f` |
| Nginx | `/var/log/nginx/error.log` |

## 5. One-shot full refresh (copy-paste)

Long-running steps are best run in tmux (`tmux new -s refresh`).

```bash
cd ~/market_analysis && source .venv/bin/activate && \
pip install -q -r deploy/ec2/requirements-server.txt && \
export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh" && nvm use 20 && \
cd value_web && npm ci && VITE_API_BASE="" npm run build && cd .. && \
sudo rsync -a --delete value_web/dist/ /var/www/value-web/ && \
sudo chown -R nginx:nginx /var/www/value-web && \
sudo systemctl restart value-web-backend && \
sudo systemctl reload nginx && \
mkdir -p logs && \
python backend/technical_indicators_backfill.py 2>&1 | tee logs/technical-indicators-backfill.log && \
bash deploy/ec2/run-daily-jobs.sh
```

## Refresh history

| Date (UTC) | Operator | Notes |
|------------|----------|-------|
| 2026-06-28 | Cursor agent | Full refresh: code deploy OK; TA backfill 925 symbols / 492,772 rows; daily jobs restarted with `SKIP_TELEGRAM_INGEST=1` after Telethon phone prompt blocked first run. Log: `logs/server-refresh-20260628T181322Z.log` |

# Running the orchestrator on AWS EC2 (Amazon Linux 2023, ARM / t4g)

## 1. Launch

- AMI: **Amazon Linux 2023** (kernel 6.1, **aarch64** for t4g).
- Instance: **t4g.small** (or larger if you raise preprocess batch sizes).
- Storage: size **EBS** for your `agent.sqlite` and logs (gp3 is fine).
- Security group: **outbound HTTPS** (443) for OpenRouter, Finnhub, Yahoo, etc.

## 2. Clone and bootstrap

```bash
sudo dnf install -y git
git clone <your-repo-url> market_analysis
cd market_analysis
bash deploy/ec2/bootstrap-amazon-linux-2023.sh
```

This installs Python 3.11, creates `.venv`, and installs `telegram_agent/requirements.txt`.

## 3. Configuration

```bash
cp deploy/ec2/env.template .env
nano .env   # paste secrets; use paths like telegram_agent/top1000_investments.json
```

Relative paths in `.env` are resolved from the **repo root** (`market_analysis/`), not from your shell’s current directory.

Copy data from your laptop if needed:

```bash
# From laptop
scp -i your-key.pem -r telegram_agent/data telegram_agent/sessions ec2-user@INSTANCE:~/market_analysis/telegram_agent/
```

For **live** orchestration with Telegram ingest, copy `telegram_agent/sessions/*.session` (and matching `.session-journal` if present).

## 4. Run

```bash
cd ~/market_analysis
source .venv/bin/activate
bash deploy/ec2/run-orchestrator.sh orchestrate --backfill-from 2026-01-01 --backfill-to 2026-01-31
```

Or rely on `run-orchestrator.sh` activating `.venv` when present:

```bash
bash deploy/ec2/run-orchestrator.sh orchestrate --backfill-from 2026-01-01 --backfill-to 2026-01-31 --cadence 3
```

Logs go to stderr and to **`ORCHESTRATOR_LOG_PATH`** (see `.env`).

## 5. Optional: systemd

See `orchestrator-backfill.service.example`: set `User`, `WorkingDirectory`, and `ExecStart` dates, then enable the unit.

## 6. Notes

- **`MPLBACKEND=Agg`** is set by the scripts to avoid headless matplotlib issues.
- **`AGENT_RESEARCH_PUBLISH=false`** in `env.template` avoids Telethon channel posts from the server unless you want them.
- If `pip install` fails building wheels, the bootstrap script already installs `gcc` and `python3.11-devel`.

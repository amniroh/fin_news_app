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

## 4. Daily schedule (consolidated)

One timer owns the daily desk; a second refreshes the regression model for paper trading:

```text
orchestrator-daily.timer     →  21:00 UTC (after US equity close)
  ingest → prices → interesting-stocks enrich → preprocess → tester → research
  (+ value-trading on Sundays UTC; Telegram publish to TARGET_CHANNEL)

regression-wf-daily.timer    →  22:30 UTC (after orchestrator has refreshed data)
  technicals extend → walk-forward train (LightGBM + param search) → IB paper rebalance
  (paper orders only when IB_PAPER_EXECUTE=1 and Gateway/TWS is reachable)
```

Install / refresh units:

```bash
bash deploy/ec2/install-services.sh
```

Manual run:

```bash
cd ~/market_analysis
bash deploy/ec2/run-orchestrator-daily.sh
# or:
bash deploy/ec2/run-orchestrator.sh orchestrate

# Walk-forward + paper (dry-run by default):
bash deploy/ec2/run-regression-wf-daily.sh
```

Historical backfill (agent pipeline only — does not re-fetch live fundamentals):

```bash
bash deploy/ec2/run-orchestrator.sh orchestrate --backfill-from 2026-01-01 --backfill-to 2026-01-31
```

Logs: `logs/orchestrator-daily.log`, `logs/regression-wf-daily.log`, and `ORCHESTRATOR_LOG_PATH` (see `.env`).

### Env knobs

| Variable | Default | Meaning |
|---|---|---|
| `RESEARCH_DAILY_MODEL` | `google/gemini-2.5-flash` | Research model for the daily desk |
| `AGENT_RESEARCH_PUBLISH` | forced `true` by daily script | Post research thinking + memory + suggestions to `TARGET_CHANNEL` |
| `ORCHESTRATOR_DISABLE_PUBLISH` | off | Set `1` to suppress Telegram posts for a run |
| `ORCHESTRATOR_FORCE_RESEARCH` | off | Re-run research even if memory exists today |
| `ORCHESTRATOR_VALUE_TRADING` | `auto` | `auto`=Sundays, `always`, `never` |
| `ORCHESTRATOR_SKIP_MARKET_DATA` | off | Skip gap backfill + daily refresh |
| `ORCHESTRATOR_SKIP_RESEARCH` | off | Skip research/memory step |
| `SKIP_TELEGRAM_INGEST` | — | Used by gap-backfill helpers when invoked standalone |
| `REGRESSION_WF_DAILY` | `1` | Run walk-forward train in `regression-wf-daily` |
| `REGRESSION_WF_YEARS` | `2.0` | History window for WF train (match available technicals) |
| `IB_PAPER_REGRESSION` | `1` | Run paper rebalance after train |
| `IB_PAPER_EXECUTE` | `0` | Set `1` to place paper orders (needs Gateway/TWS) |
| `IB_HOST` / `IB_PORT` / `IB_CLIENT_ID` | `127.0.0.1` / `7497` / `61` | IB API endpoint |

### IB paper trading

See `packages/ml_ib_paper/README.md`. The regression bot is:

```bash
python packages/ml_ib_paper/regression_paper_rebalance.py --dry-run
# after Gateway is up on paper:
IB_PAPER_EXECUTE=1 python packages/ml_ib_paper/regression_paper_rebalance.py --execute
```

Use a **dedicated paper account** (the bot liquidates US stock names that leave the basket).

## 5. Optional: one-shot backfill systemd

See `orchestrator-backfill.service.example`: set `User`, `WorkingDirectory`, and `ExecStart` dates, then enable the unit.

## 6. Notes

- **`MPLBACKEND=Agg`** is set by the scripts to avoid headless matplotlib issues.
- **`AGENT_RESEARCH_PUBLISH=false`** in `env.template` avoids Telethon channel posts from the server unless you want them.
- If `pip install` fails building wheels, the bootstrap script already installs `gcc` and `python3.11-devel`.
- Prediction markets stay on **`prediction-markets-hourly.timer`** (separate from the equities desk).

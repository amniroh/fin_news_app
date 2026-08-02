# ML / Regression paper trading — Interactive Brokers

This folder contains small runtimes that target an IB **paper** account:

| Script | Strategy |
|--------|----------|
| `paper_rebalance.py` | Legacy ML top‑N (`sp500_return_model`, score-weighted) |
| `regression_paper_rebalance.py` | **Regression on technicals** (LightGBM + walk-forward `chosen_params`) |

**Nothing here is investment advice.** Paper trading can still diverge from backtests.

---

## Regression on technicals (recommended)

Daily EC2 schedule (`regression-wf-daily.timer`, 22:30 UTC):

1. Extend `vm_technical_indicators`
2. Re-run walk-forward train → writes `regression_technicals_model_daily.joblib` + metrics/WF JSON
3. Rebalance paper account toward equal-weight top‑N (dry-run unless `IB_PAPER_EXECUTE=1`)

Live inference uses the persisted model plus the **latest walk-forward fold’s** portfolio params (`top_n`, smoothing, trend filter, vol blend).

### Dry run

```bash
export ML_PAPER_BACKEND_DIR=/home/ec2-user/market_analysis/backend
python packages/ml_ib_paper/regression_paper_rebalance.py --dry-run
```

### Paper orders

Start **TWS or IB Gateway (paper)** with API enabled, then:

```bash
export IB_HOST=127.0.0.1
export IB_PORT=7497          # TWS paper; Gateway paper often 4002
export IB_CLIENT_ID=61
# export IB_ACCOUNT=DUxxxxxx
export IB_PAPER_EXECUTE=1

python packages/ml_ib_paper/regression_paper_rebalance.py --execute --deploy-fraction 0.95
```

By default the bot **sells US stock names that leave the basket** — use a dedicated paper account.

If IB runs on your laptop and the bot on EC2, tunnel:

```bash
ssh -N -L 7497:127.0.0.1:7497 investor-bot
# on EC2: IB_HOST=127.0.0.1 IB_PORT=7497
```

---

## Legacy ML top‑N

`paper_rebalance.py` still targets `sp500_return_model_daily.joblib` (score-weighted). See historical notes below for ports and layout.

### Information needed for your IB setup

1. **Where IB runs** — TWS on your laptop vs IB Gateway on a VPS; same machine vs SSH tunnel.
2. **Paper API port** (TWS paper often **7497**; Gateway paper often **4002**) and API socket clients enabled.
3. **Paper account id** (`DU…`) if multiple accounts are linked.
4. Confirm the account is **dedicated** to this strategy (regression liquidates non-target US stocks by default).
5. Optional: max notional per name, symbol-map JSON for Yahoo≠IB tickers.

| Item | Why |
|------|-----|
| TWS / Gateway logged into **paper** | API only talks to a live Gateway/TWS process |
| API enabled | Otherwise connections are refused |
| Host + port | Paper vs live ports differ — double-check paper |
| Client ID | Unique per connection |

### Env vars

| Env var | Meaning |
|---------|---------|
| `ML_PAPER_BACKEND_DIR` | Directory containing strategy modules (usually `.../backend`) |
| `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID` | IB API endpoint |
| `IB_ACCOUNT` | Optional paper account id |
| `IB_PAPER_EXECUTE` | `1` to place orders from the daily script |
| `IB_PAPER_DEPLOY_FRACTION` | Fraction of NetLiquidation (default `0.95`) |
| `IB_PAPER_REGRESSION_STATE` | Optional path for last-target state JSON |

### Install (remote)

```bash
cd ~/market_analysis
source .venv/bin/activate
pip install -r packages/ml_ib_paper/requirements.txt
bash deploy/ec2/install-services.sh
```

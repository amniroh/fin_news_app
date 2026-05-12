# ML top‑N (predicted‑return weighted) — Interactive Brokers paper trading

This folder is a **small runtime** around `backend/sp500_return_model.py`:

- Loads the **daily** `sp500_return_model_daily.joblib` bundle (same file for equal vs weighted; weighting only changes portfolio construction).
- Refreshes / reads cached **daily OHLCV** parquet files (same layout as training).
- Computes **score_weighted** top‑N targets via `predict_top_n(..., weighting="score_weighted")`.
- Optionally connects to **Interactive Brokers** (TWS or IB Gateway, **paper** account) and places **market** orders to move the account toward those target weights.

**Nothing here is investment advice.** Paper trading can still diverge from backtests (fills, halts, borrow, corporate actions, API outages).

---

## Information I need from you to finish *your* setup

To wire this to **your** Interactive Brokers paper account (beyond what the code already reads from env / flags), please confirm:

1. **Where IB runs** — TWS on your laptop vs IB Gateway on a VPS; whether the bot runs on the **same machine** or needs an **SSH tunnel / VPN**.
2. **Paper API port** you use (TWS paper is often **7497**; Gateway paper often **4002**) and that **“Enable ActiveX and Socket Clients”** is on.
3. **Paper account id** if you have multiple accounts linked (the `DU…` string), or confirm a single default account is fine.
4. **Whether this account holds only this strategy** — the script adjusts **top‑N names only**; it does **not** automatically sell unrelated legacy positions unless you add that policy.
5. **Order constraints** you want: max notional per name, trading window (e.g. after 15:45 ET only), block list, or “no first hour” rules.
6. **Symbol exceptions** — any tickers where Yahoo cache stem ≠ IB symbol (use `--symbol-map` JSON).

---

## What you must provide (IB + network)

| Item | Why |
|------|-----|
| **TWS or IB Gateway** running and logged into **paper** | IB API only talks to a live Gateway/TWS process. |
| **API enabled** in TWS/Gateway (Configure → API → Settings) | Otherwise connections are refused. |
| **Trusted IPs** | If the bot runs on a **remote host**, add that host’s IP (or `0.0.0.0` / “Allow only localhost” off + firewall rules—understand the risk). |
| **Host + port** | Typical **paper TWS**: `127.0.0.1:7497`. **Paper IB Gateway**: often `4002`. **Live** ports differ—double‑check you are on **paper**. |
| **Client ID** | Integer unique per connection (avoid clashes with other scripts or open TWS windows). |
| **Account id** (optional) | If multiple accounts are linked, set `IB_ACCOUNT` to the **paper** account number string. |
| **Market data** (optional) | For limit / mid pricing you may need market data subscriptions on paper; this script uses **brief snapshot** requests for sizing; with no data you may need to adjust logic or use delayed data settings in TWS. |

If the bot runs **remotely** and IB runs on your laptop, use **SSH tunnel**:

```bash
ssh -L 7497:127.0.0.1:7497 you@remote
# then on remote: IB_HOST=127.0.0.1 IB_PORT=7497
```

---

## Layout on the remote host

Example:

```text
/opt/ml_ib_paper/
  backend/
    sp500_return_model.py
    data/
      sp500_return_models/
        sp500_return_model_daily.joblib
        sp500_return_model_daily_score_weighted_metrics.json   # optional; not required for inference
      sp500_prices/
        *.parquet
  packages/ml_ib_paper/
    paper_rebalance.py
    requirements.txt
```

Paths are controlled with:

| Env var | Meaning |
|---------|---------|
| `ML_PAPER_BACKEND_DIR` | Directory containing `sp500_return_model.py` (usually `.../backend`). |
| `SP500_MODEL_DIR` | Directory with `sp500_return_model_daily.joblib`. |
| `SP500_PRICE_CACHE_DIR` | Directory with `SYMBOL.parquet` caches (Yahoo‑style symbols, e.g. `BRK-B.parquet`). |
| `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID` | IB API endpoint. |
| `IB_ACCOUNT` | Optional explicit paper account id. |

---

## Install (remote)

```bash
cd /opt/ml_ib_paper/packages/ml_ib_paper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Dry run (recommended first)

```bash
export ML_PAPER_BACKEND_DIR=/opt/ml_ib_paper/backend
export SP500_MODEL_DIR=/opt/ml_ib_paper/backend/data/sp500_return_models
export SP500_PRICE_CACHE_DIR=/opt/ml_ib_paper/backend/data/sp500_prices

python paper_rebalance.py --dry-run --top-n 50 --deploy-fraction 0.95
```

---

## Paper rebalance (real orders on **paper** account)

Start **TWS or Gateway (paper)**. Then:

```bash
export IB_HOST=127.0.0.1
export IB_PORT=7497
export IB_CLIENT_ID=7
# export IB_ACCOUNT=DUxxxxxx

python paper_rebalance.py --execute --top-n 50 --deploy-fraction 0.95
```

Use `--max-order-usd` to cap per‑name notional during early experiments.

---

## Bundle from your dev machine

From repo root:

```bash
bash packages/ml_ib_paper/bundle_ml_ib_paper.sh
```

Copy the resulting `dist/ml_ib_paper_bundle_*.tgz` to the server, extract, install `requirements.txt`, set env vars, and run.

If `sp500_prices` is huge, **rsync** it separately instead of stuffing the tarball:

```bash
rsync -av backend/data/sp500_prices/ remote:/opt/ml_ib_paper/backend/data/sp500_prices/
```

---

## Symbol mapping (IB vs Yahoo cache stems)

Most tickers match. A few hyphenated names differ on IB (e.g. `BRK-B` parquet → IB often wants `BRK B`). Use `--symbol-map` pointing to JSON:

```json
{"BRK-B": "BRK B", "BF-B": "BF B"}
```

---

## Limitations (v1)

- **US stocks, SMART/USD** only; no options/futures.
- **Market orders** for deltas; no smart limit logic.
- **Whole shares** (integer); no IB fractional‑share path in this script.
- **No shorting** — targets are long‑only; sells only reduce toward zero.
- **Single daily run** — schedule with `cron` / systemd timer after US close if you want “same bar” discipline as research (you should document your own clock).

---

## Re‑training

After you retrain on your workstation, copy the new `sp500_return_model_daily.joblib` (and refreshed `sp500_prices` if needed) to the server and restart your scheduler.

# IB Gateway paper trading (headless) on EC2

This runs **IB Gateway in PAPER mode only** via Docker, listening on
`127.0.0.1:4002`. The regression paper bot connects locally — no credentials in git
or in chat.

## Where to put username / password

On the server:

```bash
cd ~/market_analysis/deploy/ec2/ib-gateway
cp -n .env.example .env
nano .env   # set TWS_USERID= and TWS_PASSWORD=
chmod 600 .env
```

File path: **`deploy/ec2/ib-gateway/.env`**

Do **not** put them in the repo root `.env` for Gateway login (root `.env` still holds
`IB_HOST` / `IB_PORT` / `IB_PAPER_EXECUTE` for the Python bot).

## How we keep this on paper (not live)

| Layer | Protection |
|-------|------------|
| Docker compose | `TRADING_MODE: paper` hard-coded; only port **4002** published (live Gateway is **4001**) |
| Python bot | Refuses ports `4001` / `7496`; requires `IB_TRADING_MODE=paper` |
| Python bot | After connect, requires a managed account id starting with **`DU`** / **`DF`** (paper). Live accounts usually start with **`U`**. |
| Kill switch | `IB_PAPER_EXECUTE=0` by default — dry-run until you opt in |

Same IB username/password can access both paper and funded accounts; **mode + port + account prefix** are what select paper. Always confirm the bot log line:

`IB paper safety OK — using account DU…`

## Port mapping (important)

The Docker image listens for paper API on container port **4004** (socat → Gateway
`127.0.0.1:4002`). Compose maps that to host **`127.0.0.1:4002`**:

```yaml
ports:
  - "127.0.0.1:4002:4004"
```

Do **not** map `4002:4002` — Docker bridge clients are not `127.0.0.1`, so Gateway
TrustedIPs will ignore them and `ib_insync` will time out.

```bash
cd ~/market_analysis/deploy/ec2/ib-gateway
# after .env is filled:
docker compose up -d
docker compose logs -f --tail=100
```

Or via systemd (once installed):

```bash
sudo systemctl start ib-gateway-paper
sudo systemctl status ib-gateway-paper
```

Point the bot at Gateway paper:

```bash
# in ~/market_analysis/.env
IB_HOST=127.0.0.1
IB_PORT=4002
IB_TRADING_MODE=paper
IB_PAPER_EXECUTE=0          # set to 1 only after a successful dry connect
```

Dry-run (no orders):

```bash
cd ~/market_analysis && source .venv/bin/activate
python packages/ml_ib_paper/regression_paper_rebalance.py --dry-run
# with Gateway up, connection check without trading:
IB_PAPER_EXECUTE=0 python packages/ml_ib_paper/regression_paper_rebalance.py --execute --dry-run
```

(Use `--execute` only after safety checks pass; keep `IB_PAPER_EXECUTE=0` until then.)

## 2FA / first login

If IB Key / mobile 2FA prompts appear, SSH-tunnel VNC and approve once:

```bash
# on your laptop
ssh -L 5900:127.0.0.1:5900 investor-bot
# open VNC client → localhost:5900 (password = VNC_SERVER_PASSWORD in .env)
```

Confirm the Gateway title/session shows **Paper Trading**.

## Disk / architecture notes

Gateway images are large. Prefer ≥20 GB free disk and ≥2 GB RAM. On ARM hosts, if
`gnzsnz/ib-gateway:stable` fails to pull/run, switch the instance to **x86_64** or
pin an ARM-capable tag documented by the image maintainers.

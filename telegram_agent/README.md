# Telegram News Digest Agent

Aggregates news from selected **Telegram channels** and **RSS feeds**, summarizes them with an LLM, and posts a digest to **your Telegram channel** (or chat).

## Quick start

### 1. Install dependencies

From the **project root** (market_analysis):

```bash
pip install -r telegram_agent/requirements.txt
```

Or from `telegram_agent/`:

```bash
cd telegram_agent && pip install -r requirements.txt
```

### 2. Get Telegram API credentials

- Go to [my.telegram.org](https://my.telegram.org/apps), log in, create an app.
- Copy **API ID** (number) and **API Hash** (string).

### 3. Configure environment

Copy the example env and edit:

```bash
cp telegram_agent/.env.example telegram_agent/.env
# Edit telegram_agent/.env with your values
```

Required in `.env`:

- `TELEGRAM_API_ID` – your API ID (number).
- `TELEGRAM_API_HASH` – your API Hash.
- `TARGET_CHANNEL` – where to post the digest (e.g. `@my_digest_channel` or a channel you created).
- At least one of:
  - `OPENROUTER_API_KEY` – for summarization (recommended), or
  - `GEMINI_API_KEY` and `USE_GEMINI=true`.

Optional:

- `TELEGRAM_CHANNELS` – comma-separated channel usernames to read (e.g. `@reuters,@bbcworld`).
- `RSS_FEEDS` – comma-separated RSS URLs.
- `HOURS_BACK` – how many hours of news to include (default `6`).
- `LLM_MODEL` – OpenRouter model (default `openai/gpt-4o-mini`).

You can also use a `config.json` in `telegram_agent/` (see `config.json.example`) to set `telegram_channels`, `rss_feeds`, `target_channel`, etc. Env vars override the file.

### 4. First run (login + test)

From the **project root**:

```bash
python -m telegram_agent.run --once
```

- On first run, Telethon will ask for your **phone number** and a **login code** (sent to your Telegram). This creates a session so you don’t have to log in again.
- The script will:
  - Read from the configured Telegram channels (you must be subscribed) and RSS feeds.
  - Deduplicate, summarize with the LLM, and post to `TARGET_CHANNEL`.

If you don’t set `TARGET_CHANNEL`, the digest is only printed to the console (useful for testing).

### 5. Run on a schedule

To run the digest every `HOURS_BACK` hours (e.g. every 6 hours):

```bash
python -m telegram_agent.run --schedule
```

Stop with Ctrl+C.

## Project layout

```
telegram_agent/
├── config.py           # Loads env + config.json
├── models.py           # NewsItem dataclass
├── store.py            # Seen IDs (dedupe)
├── summarizer.py       # LLM digest (OpenRouter / Gemini)
├── publisher.py        # Send message to Telegram
├── run.py              # CLI: --once / --schedule
├── collectors/
│   ├── telegram_collector.py  # Telethon channel reader
│   └── rss_collector.py       # RSS/Atom feeds
├── data/               # seen_ids.json (created at runtime)
├── sessions/           # Telethon session (created on first login)
├── requirements.txt
├── .env.example
├── config.json.example
└── README.md
```

## Creating your digest channel

1. In Telegram, create a **channel** (e.g. “My News Digest”).
2. Set it to **private** if you want it only for you.
3. Add your **own user** as admin (so the bot can post).
4. The channel username is like `@my_digest_channel`, or use the channel ID.
5. Set `TARGET_CHANNEL=@my_digest_channel` (or the ID) in `.env`.

## Competitive systematic bots (short-term, universe P0/P1)

Three rule-based strategies (5d momentum, RSI mean-reversion tilt, 20d breakout) run on symbols with **priority ≤ `COMPETITIVE_BOTS_MAX_PRIORITY`** (default **1** = P0 and P1; set **`0`** for P0-only). Each run inserts `recommendations` tagged with `meta_json.competitive_bot_id`, evaluates them with the **same `test-suggestions` logic** as research (per-bot aggregate metrics in `kv_state`), stores rows in **`competitive_bot_runs`**, and optionally posts a summary to **`TARGET_CHANNEL`** (Telethon).

```bash
python -m telegram_agent.agent competitive-bots --cadence daily
```

Schedule with **cron** or **systemd** on the server; use a distinct `--cadence` label per schedule. In the Telegram **digest bot** (`bot_app.py`), use **`/competitive`** to run once from chat or channel.

**Price coverage / gaps (P0/P1):**

```bash
python -m telegram_agent.analyze_price_coverage --max-priority 1
python -m telegram_agent.analyze_price_coverage --json
```

**Historical walk-forward backtest** (same three scorers), on **every distinct `prices.interval`** in your database (often only `1d` unless you have ingested intraday bars):

```bash
python -m telegram_agent.agent competitive-bots --backtest
```

Results are stored in `kv_state` under `competitive_backtest:last_v1` and the summary is posted to **`TARGET_CHANNEL`** (unless `--no-publish`). Telegram: **`/competitive_backtest`**.

## RSI mean optimizer (`optimize_rsi_mean.py`) parameters

The optimizer writes per-parameter-set tables (e.g. `telegram_agent/data/optimize_rsi_mean_param_sets.csv`). All parameter columns are prefixed with **`p_`** and mean:

- **`p_top_k`**: number of symbols selected each evaluation step (equal-weight basket of top signals).
  - **What “top” means**: at each evaluation time, every symbol that has enough history is scored by the RSI-mean rule; symbols that fail the **RSI bounds** (`p_rsi_lo`/`p_rsi_hi`) or the **momentum filter** (`p_mom_max` over `p_mom_lookback`) are skipped. The remaining symbols are **ranked by score (highest first)**, and the first `p_top_k` symbols are selected and equal-weighted for the next step.
- **`p_min_bars`**: minimum history length (in bars on the reference hourly timeline) required before scoring a symbol.
- **`p_exposure`**: fraction of capital exposed to the basket each step (0 = cash, 1 = fully invested).
- **`p_dd_stop`**: optional risk-off trigger. If set, strategy goes to cash when portfolio drawdown ≥ this fraction (e.g. `0.08` = 8%).
- **`p_dd_resume`**: optional risk-on resume threshold. If set, strategy re-enters when drawdown falls back to ≤ this fraction.

RSI-mean scoring rule parameters (for cross-sectional ranking):

- **`p_rsi_period`**: RSI lookback period (bars) used by the signal.
- **`p_rsi_lo`**: lower RSI bound; signals outside \([p_rsi_lo, p_rsi_hi]\) are ignored.
- **`p_rsi_hi`**: upper RSI bound.
- **`p_rsi_target`**: the RSI “reference level” the score measures oversold-ness against. The score increases as RSI drops below this target.
  - Example: if `p_rsi_target=46`, then RSI=30 has a stronger base signal than RSI=40 because \((46-30)>(46-40)\).
- **`p_mom_lookback`**: momentum lookback (bars). Momentum is computed as percent change over this window:
  - \(mom = 100 \\times (close_{now}/close_{now-L} - 1)\) where \(L=p\\_mom\\_lookback\).
- **`p_mom_max`**: momentum filter threshold (in %). A symbol is **rejected** if its recent momentum is too positive:
  - Reject if \(mom > p\\_mom\\_max\).
  - Examples (with `p_mom_lookback=10`):
    - If price went 100→103 then \(mom=+3\\%\\). With `p_mom_max=2.0`, **reject** (too strong / not a dip).
    - If `p_mom_max=-0.2`, then you require a *dip*: only \(mom\\le -0.2\\%\\) passes.
- **`p_mom_scale`**: dip-magnitude boost scale (in %). After passing filters, the score gets a boost based on \(|mom|\), capped at `p_mom_scale`:
  - \(w = 1 + \\min(|mom|, p\\_mom\\_scale)/p\\_mom\\_scale\), and final score is multiplied by \(w\).
  - Example: `p_mom_scale=2.0`:
    - \(mom=-0.5\\%\\Rightarrow w=1.25\\) (small boost)
    - \(mom=-2.0\\%\\Rightarrow w=2.0\\) (max boost)
    - \(mom=-6.0\\%\\Rightarrow w=2.0\\) (still capped)

## Training pipeline: cross-sectional signal adapters

The generic training pipeline (`training_pipeline.py`) can run **`rsi_mean`**, **`rsi_mean_walk_forward`**, or any of the **`signal_*`** adapters below. They all rebalance **hourly** on the same reference timeline as the RSI optimizer: each bar, symbols are **ranked by score**, the top **`top_k`** names form an **equal-weight BASKET**, and the next bar’s return is applied (with optional drawdown overlay). Tuning uses `random_search`-style search with the same rolling-return objectives as `optimize_rsi_mean` (see `OPTIMIZE_*` env vars).

Run one signal:

```bash
python -m telegram_agent.training_pipeline --adapter signal_macd --start 2020-01-01 --end 2026-01-01
```

Run several on **one shared** DB context and **identical** train/validation/test splits:

```bash
python -m telegram_agent.training_pipeline \
  --adapters signal_macd,signal_bollinger,signal_sma_cross \
  --start 2020-01-01 --end 2026-01-01
```

### Signal summaries (what each one does)

| Adapter | Brief description |
|--------|-------------------|
| **`signal_macd`** | Trend-style spread: **fast SMA minus slow SMA** of adjusted close, normalized by the slow SMA. Ranks names by that spread (higher = stronger relative uptrend in the MACD spirit; not a full MACD signal line + 9-period EMA implementation). |
| **`signal_bollinger`** | **Bollinger %B** from closes: middle band = SMA(`bb_period`), width = `bb_std` × rolling stdev. Score uses **(0.5 − %B)** scaled by `bb_mode` (−1 or +1) so you can lean mean-reversion vs breakout in sampling. |
| **`signal_sma_cross`** | **Golden-cross style**: \((SMA_{short} - SMA_{long}) / SMA_{long}\) at each bar; higher score = short average further above the long average. |
| **`signal_stochastic`** | **Stochastic %K** using the **high and low of closes** over `stoch_k_period` (a close-only proxy for true high/low). Mean-reversion tilt: score favors **lower %K** (oversold side of the range). |
| **`signal_williams_r`** | **Williams %R** over the same close-window high/low proxy; higher score = less oversold in the classic %R sense. |
| **`signal_roc`** | **Rate of change**: percent return from `close[i-roc_period]` to `close[i]`; higher = stronger recent momentum. |
| **`signal_cci`** | **CCI** on closes only (typical price = close): deviation from window mean vs mean absolute deviation, standard CCI formula constant 0.015. |
| **`signal_atr_momentum`** | **Price change over ATR**: \((close_i - close_{i-n}) / ATR\), where ATR is a **simple average of one-bar true range** \(|Δclose|\) over `atr_period` (Wilder-style smoothing is not implemented here). |
| **`signal_adx`** | **Close-only trend-strength proxy**: over `adx_period`, sums up-moves vs down-moves of the close and divides by cumulative TR proxy; higher = stronger directional imbalance (not textbook ADX/DI with highs/lows). |
| **`signal_pe_ratio`** | **Value / earnings yield rank**: score \(\propto 1/\text{PE}\) (higher = cheaper on a PE basis). **Does not compute fundamentals from prices**; see inputs below. |

### Where inputs come from (data sources)

**Prices (all signals except the PE-only ranking step)**

- **Source:** the same **SQLite agent DB** path as the rest of the agent (`AGENT_DB_PATH` / default under `telegram_agent/data/`), via `build_context` → hourly adjusted close series merged onto a **single reference timeline** (densest symbol), then forward-filled per symbol (`optimize_rsi_mean` / `training_pipeline`).
- **Universe:** `COMPETITIVE_BACKTEST_SYMBOLS` (or whatever symbols you pass into the pipeline).
- **No live price API** is called inside these adapters during a training run; they **read whatever hourly rows are already in the DB** (e.g. from prior `agent prices` / ingest jobs).

**P/E and fundamentals (`signal_pe_ratio` only)**

- **Not from an earnings API** in this codebase. There is **no** automatic fetch of reported EPS or trailing PE from Yahoo, FMP, etc.
- **Configured inputs only:**
  1. **`PIPELINE_VALUE_METRICS_PATH`** — path to a **JSON file** you maintain. Supported shapes: `{"AAPL": {"pe": 28.5}, ...}` or `{"AAPL": 28.5, ...}` (see `signal_strategies._load_pe_map` and `telegram_agent/data/pipeline_pe_snapshot.json` as an example snapshot used for local experiments).
  2. Optionally, a **`pipeline_value_metrics_pe`** dict can be injected on the `cfg` object for tests or custom runners (same key semantics as the file).
- Symbols **missing** from the map are **skipped** for that signal (no synthetic PE). If the map is **empty**, the ranker returns no candidates and the strategy effectively produces **no basket trades** for PE-driven selection.

**Close-only approximations**

- **Stochastic / Williams %R:** use min/max of **closes** in the lookback window as a stand-in for true high/low (intraday highs/lows are not required in the DB schema used here).
- **ATR / ADX:** derived from **close-to-close** moves only, not full OHLC Wilder ATR/ADX.

For more detail, see `signal_strategies.py`, `cross_sectional_engine.py`, and `optimize_generic_signal.py`.

## Tips

- **Telegram channels**: You must be a **member** of each channel; the script uses your account (user client) to read them.
- **RSS**: If a feed fails, check the URL in a browser; some sites block non-browser clients.
- **No LLM**: If neither OpenRouter nor Gemini is set, the script will still collect and print “Raw item count” (no summary).
- **Testing without posting**: Omit `TARGET_CHANNEL` to only print the digest to the console.

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

## Tips

- **Telegram channels**: You must be a **member** of each channel; the script uses your account (user client) to read them.
- **RSS**: If a feed fails, check the URL in a browser; some sites block non-browser clients.
- **No LLM**: If neither OpenRouter nor Gemini is set, the script will still collect and print “Raw item count” (no summary).
- **Testing without posting**: Omit `TARGET_CHANNEL` to only print the digest to the console.

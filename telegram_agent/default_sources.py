"""
Default signal sources when TELEGRAM_CHANNELS / RSS_FEEDS / TWITTER_USERNAMES are empty.
Verify Telegram @handles in the app — names change; update as needed.
"""

# Telegram (read with Telethon user client — join/subscribe first)
DEFAULT_TELEGRAM_CHANNELS = [
    "unusual_whales",
    "whale_alert",
    "TheBlock__",
    "lookonchain",
    "WuBlockchain",
]

# Official & wire RSS (polling)
DEFAULT_RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://home.treasury.gov/news/press-releases/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptoslate.com/feed/",
]

# X/Twitter — user timelines (requires TWITTER_BEARER_TOKEN)
# Add FinTwit list IDs via TWITTER_LIST_IDS / config.json after creating lists in X.
DEFAULT_TWITTER_USERNAMES = [
    "reutersagency",
    "unusual_whales",
    "business",
]

export type PredictionColumnDoc = {
  key: string;
  label: string;
  description: string;
};

export const PREDICTION_MARKET_COLUMN_DOCS: PredictionColumnDoc[] = [
  {
    key: "source",
    label: "Source",
    description: "Exchange where the signal is listed: Polymarket (on-chain Polygon) or Kalshi (US regulated).",
  },
  {
    key: "title",
    label: "Signal",
    description: "The market question or contract title. Click to open the live market on the source site.",
  },
  {
    key: "status",
    label: "Status",
    description: "open = still tradeable; settled = resolved with a final outcome; closed = no longer trading but may be unresolved.",
  },
  {
    key: "close_time_utc",
    label: "Close",
    description: "When the market stops accepting trades or is scheduled to resolve.",
  },
  {
    key: "latest_yes_price",
    label: "Yes price",
    description: "Implied probability of the YES outcome (0–1). On Kalshi this is the last/mid YES price; on Polymarket, outcome token price.",
  },
  {
    key: "volume_total",
    label: "Total volume",
    description: "Lifetime traded notional on this market (USD). Higher volume usually means better liquidity and more reliable prices.",
  },
  {
    key: "volume_24h",
    label: "24h volume",
    description: "Trading volume in the last 24 hours. Useful for spotting markets with current activity.",
  },
  {
    key: "transaction_volume",
    label: "Recent txn volume",
    description: "Sum of recent on-chain/API trade sizes (USD) fetched at sync time. Reflects very recent transaction flow.",
  },
  {
    key: "trade_count",
    label: "Recent trades",
    description: "Number of recent trades included in the transaction volume sample.",
  },
  {
    key: "is_time_sensitive",
    label: "Time sensitive?",
    description:
      "Yes if the market resolves within 7 days or YES price moved ≥10% within 24h — the edge may decay quickly. No means the signal may stay actionable longer.",
  },
  {
    key: "signal_won",
    label: "Outcome",
    description: "For settled markets: whether following the higher-probability side at entry would have been profitable.",
  },
  {
    key: "profit_if_followed",
    label: "Profit",
    description: "Hypothetical return if you bought the favored side at entry and held to settlement (as a fraction, e.g. 0.12 = +12%).",
  },
  {
    key: "relevance_score",
    label: "Relevance",
    description: "Internal ranking score used during sync to prioritize high-volume, liquid, tradeable markets over noise/combo contracts.",
  },
  {
    key: "blockchain_ref",
    label: "Blockchain / ID",
    description: "Polymarket condition ID (Polygon) or Kalshi ticker — the on-chain or exchange identifier for this signal.",
  },
];

export function predictionColumnDoc(key: string): PredictionColumnDoc | undefined {
  return PREDICTION_MARKET_COLUMN_DOCS.find((d) => d.key === key);
}

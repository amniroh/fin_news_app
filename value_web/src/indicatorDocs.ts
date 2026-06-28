export type IndicatorDoc = {
  key: string;
  label: string;
  featureMeaning: string;
  llmInterpretation: string;
  formula?: string;
};

export const TECHNICAL_INDICATOR_DOCS: IndicatorDoc[] = [
  {
    key: "ema",
    label: "EMA (20)",
    formula: "EMA_t = (Price_t × α) + (EMA_{t−1} × (1 − α)), α = 2/(N+1), N=20",
    featureMeaning: "Represents the smoothed trend direction.",
    llmInterpretation:
      "If Price > EMA, the trend is bullish; if Price < EMA, it is bearish. The slope of the EMA indicates the acceleration of the trend.",
  },
  {
    key: "macd_line",
    label: "MACD",
    formula: "MACD Line = EMA₁₂ − EMA₂₆; Signal Line = 9-period EMA of MACD Line",
    featureMeaning: "Represents momentum magnitude and polarity.",
    llmInterpretation:
      'A "Bullish Crossover" occurs when the MACD Line crosses above the Signal Line, indicating that short-term price momentum is shifting upward relative to longer-term trends.',
  },
  {
    key: "adx",
    label: "ADX (14)",
    formula: "ADX from +DI and −DI over a 14-day Wilder window",
    featureMeaning: "Represents trend strength (intensity).",
    llmInterpretation:
      "Non-directional scalar. ADX < 20 suggests a range-bound, noise-heavy environment. ADX > 25 suggests a high-probability directional trend, regardless of whether it is bullish or bearish.",
  },
  {
    key: "rvol",
    label: "RVOL (20d)",
    formula: "RVOL = Current Volume / Average Volume over 20 trading days",
    featureMeaning: "Represents conviction or interest level.",
    llmInterpretation:
      "Acts as a boolean filter for validation. If a price move occurs with RVOL < 1.0, it is statistically insignificant (retail noise). If RVOL > 2.0, it signifies institutional participation and high conviction behind the price action.",
  },
];

export const INDICATOR_DOC_BY_KEY: Record<string, IndicatorDoc> = Object.fromEntries(
  TECHNICAL_INDICATOR_DOCS.map((d) => [d.key, d]),
);

export function indicatorDocForColumn(key: string): IndicatorDoc | undefined {
  return INDICATOR_DOC_BY_KEY[key];
}

import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_SERIES_COLORS } from "./chartSeriesStyles";

export type AnalystRatingRow = {
  asof_date: string;
  recommendation_key?: string | null;
  recommendation_mean?: number | null;
  num_analysts?: number | null;
  strong_buy?: number | null;
  buy?: number | null;
  hold?: number | null;
  sell?: number | null;
  strong_sell?: number | null;
  target_mean?: number | null;
  target_high?: number | null;
  target_low?: number | null;
  target_current?: number | null;
};

type ChartRow = AnalystRatingRow & {
  total_ratings: number;
};

function formatConsensus(mean: number | null | undefined): string {
  if (mean == null || Number.isNaN(mean)) return "—";
  if (mean <= 1.5) return "Strong buy";
  if (mean <= 2.5) return "Buy";
  if (mean <= 3.5) return "Hold";
  if (mean <= 4.5) return "Sell";
  return "Strong sell";
}

export function AnalystRatingsSection({ rows }: { rows: AnalystRatingRow[] }) {
  const chartRows = useMemo(() => {
    const sorted = [...rows].sort((a, b) => a.asof_date.localeCompare(b.asof_date));
    return sorted.map((r) => ({
      ...r,
      total_ratings:
        (Number(r.strong_buy) || 0) +
        (Number(r.buy) || 0) +
        (Number(r.hold) || 0) +
        (Number(r.sell) || 0) +
        (Number(r.strong_sell) || 0),
    })) as ChartRow[];
  }, [rows]);

  const latest = chartRows.length ? chartRows[chartRows.length - 1] : null;

  if (!chartRows.length) {
    return (
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16 }}>Analyst ratings (yfinance)</h2>
        <p style={{ color: "#718096", fontSize: 14 }}>
          No snapshots stored yet. The daily backfill script fetches analyst consensus for symbols with
          gaps; each run adds a dated snapshot and builds history over time.
        </p>
      </section>
    );
  }

  const hasHistory = chartRows.length >= 2;
  const hasCounts = chartRows.some((r) => r.total_ratings > 0);
  const hasTargets = chartRows.some((r) => r.target_mean != null);

  return (
    <section style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 16 }}>Analyst ratings (yfinance)</h2>
      <p style={{ fontSize: 13, color: "#4a5568", marginTop: 0 }}>
        {chartRows.length} snapshot{chartRows.length === 1 ? "" : "s"} in DB
        {hasHistory ? " — history grows with each daily backfill run" : ""}.
        Consensus score: 1 = strong buy, 5 = strong sell.
      </p>

      {latest && (
        <div style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 16 }}>
          <div>
            Latest ({latest.asof_date}):{" "}
            <strong>{String(latest.recommendation_key || formatConsensus(latest.recommendation_mean))}</strong>
            {latest.recommendation_mean != null ? (
              <> (mean {Number(latest.recommendation_mean).toFixed(2)})</>
            ) : null}
            {latest.num_analysts != null ? <> · {String(latest.num_analysts)} analysts</> : null}
          </div>
          {latest.target_mean != null && (
            <div>
              Price targets: mean {Number(latest.target_mean).toFixed(2)}
              {latest.target_high != null ? ` · high ${Number(latest.target_high).toFixed(2)}` : ""}
              {latest.target_low != null ? ` · low ${Number(latest.target_low).toFixed(2)}` : ""}
            </div>
          )}
        </div>
      )}

      {hasHistory && (
        <div style={{ width: "100%", height: 280, marginBottom: 20 }}>
          <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 8px" }}>Consensus score over time</p>
          <ResponsiveContainer>
            <ComposedChart data={chartRows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="asof_date" minTickGap={32} fontSize={11} />
              <YAxis domain={[1, 5]} ticks={[1, 2, 3, 4, 5]} fontSize={11} width={36} />
              <Tooltip
                formatter={(value: number) => [value.toFixed(2), "Mean score"]}
                labelFormatter={(lab) => String(lab)}
              />
              <Line
                type="monotone"
                dataKey="recommendation_mean"
                name="Mean score"
                stroke={CHART_SERIES_COLORS[2]}
                strokeWidth={2}
                dot={{ r: 3 }}
                connectNulls
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {hasHistory && hasCounts && (
        <div style={{ width: "100%", height: 300, marginBottom: 20 }}>
          <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 8px" }}>Rating counts (stacked)</p>
          <ResponsiveContainer>
            <ComposedChart data={chartRows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="asof_date" minTickGap={32} fontSize={11} />
              <YAxis fontSize={11} width={40} />
              <Tooltip />
              <Legend />
              <Bar dataKey="strong_buy" stackId="a" fill="#276749" name="Strong buy" />
              <Bar dataKey="buy" stackId="a" fill="#48bb78" name="Buy" />
              <Bar dataKey="hold" stackId="a" fill="#ecc94b" name="Hold" />
              <Bar dataKey="sell" stackId="a" fill="#ed8936" name="Sell" />
              <Bar dataKey="strong_sell" stackId="a" fill="#c53030" name="Strong sell" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {hasHistory && hasTargets && (
        <div style={{ width: "100%", height: 280 }}>
          <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 8px" }}>Price targets (mean / range)</p>
          <ResponsiveContainer>
            <ComposedChart data={chartRows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="asof_date" minTickGap={32} fontSize={11} />
              <YAxis domain={["auto", "auto"]} fontSize={11} width={56} />
              <Tooltip />
              <Legend />
              <Line
                type="monotone"
                dataKey="target_low"
                name="Target low"
                stroke={CHART_SERIES_COLORS[4]}
                strokeDasharray="4 4"
                dot={false}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="target_mean"
                name="Target mean"
                stroke={CHART_SERIES_COLORS[0]}
                strokeWidth={2}
                dot={{ r: 3 }}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="target_high"
                name="Target high"
                stroke={CHART_SERIES_COLORS[1]}
                strokeDasharray="4 4"
                dot={false}
                connectNulls
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {!hasHistory && (
        <p style={{ color: "#718096", fontSize: 13 }}>
          Only one snapshot on file. Run the daily backfill again on later dates to build a time series.
        </p>
      )}
    </section>
  );
}

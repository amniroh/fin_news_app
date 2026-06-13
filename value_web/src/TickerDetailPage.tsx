import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AnalystRatingsSection, type AnalystRatingRow } from "./AnalystRatingsSection";
import {
  ValueTradingSection,
  type ValueTradingAssessment,
} from "./ValueTradingSection";
import { CHART_SERIES_COLORS, CHART_STROKE_DASHARRAYS } from "./chartSeriesStyles";

type PriceRow = { ts: string; close: number };
type MetricRow = { asof_date: string; pe?: number | null; pb?: number | null };
type ChartPoint = { ts: string; close: number; pe?: number | null; pb?: number | null };

function mergeChart(metrics: MetricRow[], prices: PriceRow[]): ChartPoint[] {
  const sorted = [...metrics].sort((a, b) => a.asof_date.localeCompare(b.asof_date));
  let j = 0;
  let acc: Partial<ChartPoint> = {};
  return prices.map((p) => {
    const day = p.ts.slice(0, 10);
    while (j < sorted.length && sorted[j].asof_date <= day) {
      acc = { pe: sorted[j].pe ?? null, pb: sorted[j].pb ?? null };
      j += 1;
    }
    return { ts: p.ts, close: p.close, ...acc };
  });
}

export function TickerDetailPage({ apiBase }: { apiBase: string }) {
  const { symbol: symParam } = useParams();
  const symbol = (symParam || "").toUpperCase();
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`${apiBase}/value/interesting/stocks/${encodeURIComponent(symbol)}/detail`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [apiBase, symbol]);

  useEffect(() => {
    load();
  }, [load]);

  const chartData = useMemo(() => {
    if (!data) return [];
    const prices = (data.prices as PriceRow[]) || [];
    const metrics = (data.metrics as MetricRow[]) || [];
    return mergeChart(metrics, prices);
  }, [data]);

  if (!symbol) {
    return <p style={{ padding: 24 }}>Missing symbol.</p>;
  }

  const news = (data?.news as Record<string, unknown>[]) || [];
  const recs = (data?.recommendations as Record<string, unknown>[]) || [];
  const analyst = (data?.analyst_ratings as AnalystRatingRow[]) || [];
  const valueTrading = (data?.value_trading as ValueTradingAssessment[]) || [];
  const valueTradingLatest = (data?.value_trading_latest as ValueTradingAssessment | null) || null;
  const fundamentals = (data?.fundamentals as Record<string, unknown>[]) || [];

  return (
    <div style={{ padding: "16px 24px", maxWidth: 1100, margin: "0 auto" }}>
      <p style={{ margin: 0 }}>
        <Link to="/stocks">← Interesting stocks</Link>
      </p>
      <h1 style={{ margin: "8px 0 4px" }}>{symbol}</h1>
      {Boolean(data?.stock) && (
        <p style={{ color: "#4a5568", marginTop: 0 }}>
          Universe priority {(data!.stock as { universe_priority: number }).universe_priority}
        </p>
      )}

      {error && <p style={{ color: "#c53030" }}>{error}</p>}
      {loading && <p>Loading…</p>}

      {!loading && data && (
        <>
          <section style={{ marginTop: 24 }}>
            <h2 style={{ fontSize: 16 }}>Price & fundamentals (2y)</h2>
            {chartData.length < 2 ? (
              <p style={{ color: "#718096" }}>Not enough price history. Run backfill for this symbol.</p>
            ) : (
              <div style={{ width: "100%", height: 360 }}>
                <ResponsiveContainer>
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="ts" tickFormatter={(v) => String(v).slice(0, 10)} minTickGap={40} />
                    <YAxis yAxisId="price" domain={["auto", "auto"]} />
                    <YAxis yAxisId="pe" orientation="right" domain={["auto", "auto"]} />
                    <Tooltip labelFormatter={(v) => String(v).slice(0, 10)} />
                    <Legend />
                    <Line
                      yAxisId="price"
                      type="monotone"
                      dataKey="close"
                      name="Close"
                      stroke={CHART_SERIES_COLORS[0]}
                      dot={false}
                      strokeWidth={2}
                    />
                    <Line
                      yAxisId="pe"
                      type="monotone"
                      dataKey="pe"
                      name="P/E (daily)"
                      stroke={CHART_SERIES_COLORS[1]}
                      dot={false}
                      strokeWidth={1.5}
                      strokeDasharray={CHART_STROKE_DASHARRAYS[1]}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
            {fundamentals.length > 0 && (
              <p style={{ fontSize: 13, color: "#4a5568" }}>
                Quarterly EPS points in DB: {fundamentals.length} (latest{" "}
                {(fundamentals[fundamentals.length - 1] as { asof_date: string }).asof_date})
              </p>
            )}
          </section>

          <AnalystRatingsSection rows={analyst} />

          <ValueTradingSection latest={valueTradingLatest} history={valueTrading} />

          <section style={{ marginTop: 24 }}>
            <h2 style={{ fontSize: 16 }}>Recent news</h2>
            {news.length === 0 ? (
              <p style={{ color: "#718096" }}>No linked news in agent DB for this symbol.</p>
            ) : (
              <ul style={{ paddingLeft: 18, fontSize: 14 }}>
                {news.map((n) => (
                  <li key={String(n.id)} style={{ marginBottom: 10 }}>
                    <div style={{ fontWeight: 500 }}>{String(n.title || "(no title)")}</div>
                    <div style={{ color: "#718096", fontSize: 12 }}>
                      {String(n.ts_utc || "").slice(0, 19)} · {String(n.source_type || "")}
                    </div>
                    {n.url ? (
                      <a href={String(n.url)} target="_blank" rel="noreferrer">
                        Source
                      </a>
                    ) : null}
                    {n.snippet ? (
                      <p style={{ margin: "4px 0 0", color: "#4a5568" }}>{String(n.snippet)}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section style={{ marginTop: 24, marginBottom: 40 }}>
            <h2 style={{ fontSize: 16 }}>Research agent suggestions</h2>
            {recs.length === 0 ? (
              <p style={{ color: "#718096" }}>No recommendations stored for this symbol.</p>
            ) : (
              <ul style={{ paddingLeft: 18, fontSize: 14 }}>
                {recs.map((r) => (
                  <li key={String(r.id)} style={{ marginBottom: 12 }}>
                    <div>
                      <strong>{String(r.ts_utc || "").slice(0, 19)}</strong>
                      {r.confidence != null ? ` · confidence ${Number(r.confidence).toFixed(2)}` : ""}
                      {r.forecast_pct != null ? ` · forecast ${Number(r.forecast_pct).toFixed(1)}%` : ""}
                    </div>
                    <p style={{ margin: "4px 0 0" }}>{String(r.rationale || "")}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}

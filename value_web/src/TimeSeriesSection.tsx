import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  CHART_SERIES_COLORS,
  CHART_STROKE_DASHARRAYS,
  LegendSeriesGlyph,
  sparseSeriesDot,
} from "./chartSeriesStyles";

type Resolution = "daily" | "hourly" | "minute";

const METRICS: { key: string; label: string; pct?: boolean }[] = [
  { key: "pe", label: "P/E" },
  { key: "pb", label: "P/B" },
  { key: "peg", label: "PEG" },
  { key: "dividend_yield", label: "Dividend yield", pct: true },
  { key: "free_cash_flow_yield", label: "FCF yield", pct: true },
  { key: "debt_to_equity", label: "Debt / equity" },
  { key: "roe", label: "ROE", pct: true },
  { key: "current_ratio", label: "Current ratio" },
  { key: "operating_margin", label: "Operating margin", pct: true },
  { key: "ev_to_ebitda", label: "EV / EBITDA" },
];

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function defaultRange(res: Resolution): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end);
  if (res === "daily") start.setFullYear(start.getFullYear() - 2);
  else if (res === "hourly") start.setDate(start.getDate() - 120);
  else start.setDate(start.getDate() - 7);
  return { start: ymd(start), end: ymd(end) };
}

type MetricApiRow = {
  asof_date: string;
  pe?: number | null;
  pb?: number | null;
  peg?: number | null;
  dividend_yield?: number | null;
  free_cash_flow_yield?: number | null;
  debt_to_equity?: number | null;
  roe?: number | null;
  current_ratio?: number | null;
  operating_margin?: number | null;
  ev_to_ebitda?: number | null;
};

type PriceRow = { ts: string; close: number; volume?: number | null };

export type ChartPoint = PriceRow & {
  pe?: number | null;
  pb?: number | null;
  peg?: number | null;
  dividend_yield?: number | null;
  free_cash_flow_yield?: number | null;
  debt_to_equity?: number | null;
  roe?: number | null;
  current_ratio?: number | null;
  operating_margin?: number | null;
  ev_to_ebitda?: number | null;
};

function mergeForward(metrics: MetricApiRow[], prices: PriceRow[]): ChartPoint[] {
  const sorted = [...metrics].sort((a, b) => a.asof_date.localeCompare(b.asof_date));
  let j = 0;
  let acc: Partial<ChartPoint> = {};
  return prices.map((p) => {
    const day = p.ts.slice(0, 10);
    while (j < sorted.length && sorted[j].asof_date <= day) {
      const r = sorted[j];
      acc = {
        pe: r.pe ?? null,
        pb: r.pb ?? null,
        peg: r.peg ?? null,
        dividend_yield: r.dividend_yield ?? null,
        free_cash_flow_yield: r.free_cash_flow_yield ?? null,
        debt_to_equity: r.debt_to_equity ?? null,
        roe: r.roe ?? null,
        current_ratio: r.current_ratio ?? null,
        operating_margin: r.operating_margin ?? null,
        ev_to_ebitda: r.ev_to_ebitda ?? null,
      };
      j++;
    }
    return { ts: p.ts, close: p.close, volume: p.volume ?? null, ...acc };
  });
}

function formatTick(ts: string, res: Resolution): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  if (res === "minute")
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  if (res === "hourly")
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "2-digit" });
}

type AxisFmtProps = { tickFormatter: (v: string) => string };

const tsLegendBtn: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  font: "inherit",
  color: "var(--text, #1e293b)",
  background: "transparent",
  border: "none",
  borderRadius: 4,
  padding: "2px 6px",
  margin: 0,
  cursor: "pointer",
};

function PriceSeriesBlock({
  chartData,
  axisProps,
  strokePrice,
}: {
  chartData: ChartPoint[];
  axisProps: AxisFmtProps;
  strokePrice: string;
}) {
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    setHidden(false);
  }, [chartData]);
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <button
          type="button"
          onClick={() => setHidden((h) => !h)}
          aria-pressed={!hidden}
          title={hidden ? "Show series" : "Hide series"}
          style={{
            ...tsLegendBtn,
            fontSize: 13,
            fontWeight: 500,
            opacity: hidden ? 0.42 : 1,
            textDecoration: hidden ? "line-through" : undefined,
          }}
        >
          <LegendSeriesGlyph color={strokePrice} shapeIndex={0} />
          Close
        </button>
      </div>
      <div style={{ width: "100%", height: 280 }}>
        <ResponsiveContainer>
          <LineChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
            <XAxis dataKey="ts" tickFormatter={axisProps.tickFormatter} minTickGap={24} fontSize={11} />
            <YAxis domain={["auto", "auto"]} fontSize={11} width={56} />
            <Tooltip
              shared={false}
              labelFormatter={(lab) => String(lab)}
              formatter={(value: number | string) => [Number(value).toFixed(4), "Close"]}
            />
            <Line
              type="monotone"
              dataKey="close"
              name="Close"
              stroke={strokePrice}
              hide={hidden}
              dot={sparseSeriesDot({ fill: strokePrice, shapeIndex: 0, stride: 36 })}
              strokeWidth={2.25}
              activeDot={{ r: 5 }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function MetricSparkBlock({
  m,
  index,
  chartData,
  axisProps,
}: {
  m: (typeof METRICS)[number];
  index: number;
  chartData: ChartPoint[];
  axisProps: AxisFmtProps;
}) {
  const stroke = CHART_SERIES_COLORS[index % CHART_SERIES_COLORS.length];
  const dash = CHART_STROKE_DASHARRAYS[index % CHART_STROKE_DASHARRAYS.length];
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    setHidden(false);
  }, [chartData]);

  return (
    <div style={{ minHeight: 200, border: "1px solid var(--border, #e5e4e7)", borderRadius: 8, padding: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <button
          type="button"
          onClick={() => setHidden((h) => !h)}
          aria-pressed={!hidden}
          title={hidden ? "Show series" : "Hide series"}
          style={{
            ...tsLegendBtn,
            fontSize: 13,
            fontWeight: 600,
            opacity: hidden ? 0.42 : 1,
            textDecoration: hidden ? "line-through" : undefined,
          }}
        >
          <LegendSeriesGlyph color={stroke} shapeIndex={index} strokeDasharray={dash} />
          {m.label}
        </button>
      </div>
      <div style={{ width: "100%", height: 162 }}>
        <ResponsiveContainer>
          <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
            <XAxis dataKey="ts" tickFormatter={axisProps.tickFormatter} hide={false} minTickGap={20} fontSize={10} />
            <YAxis domain={["auto", "auto"]} fontSize={10} width={44} />
            <Tooltip
              shared={false}
              formatter={(val: number | string) => {
                const v = typeof val === "number" ? val : parseFloat(String(val));
                if (m.pct) return [`${(v * 100).toFixed(2)}%`, m.label];
                return [Number(v).toFixed(3), m.label];
              }}
            />
            <Line
              type="stepAfter"
              dataKey={m.key}
              name={m.label}
              stroke={stroke}
              strokeDasharray={dash}
              hide={hidden}
              dot={sparseSeriesDot({ fill: stroke, shapeIndex: index, stride: 24 })}
              strokeWidth={1.75}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

type Props = { apiBase: string };

export function TimeSeriesSection({ apiBase }: Props) {
  const [symbol, setSymbol] = useState("AAPL");
  const [resolution, setResolution] = useState<Resolution>("daily");
  const [start, setStart] = useState(() => defaultRange("daily").start);
  const [end, setEnd] = useState(() => defaultRange("daily").end);
  const [provider, setProvider] = useState<"yfinance" | "sec">("yfinance");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chartData, setChartData] = useState<ChartPoint[]>([]);
  const [meta, setMeta] = useState<{ priceN: number; metricN: number } | null>(null);

  const applyResolutionDefaults = useCallback(() => {
    const r = defaultRange(resolution);
    setStart(r.start);
    setEnd(r.end);
  }, [resolution]);

  const load = useCallback(async () => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) {
      setError("Enter a symbol");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const histUrl = `${apiBase}/value/metrics/history?symbols=${encodeURIComponent(sym)}&period=daily&provider=${encodeURIComponent(provider)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      const pxUrl = `${apiBase}/value/price/history?symbol=${encodeURIComponent(sym)}&interval=${encodeURIComponent(resolution)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;

      const [hr, pr] = await Promise.all([fetch(histUrl), fetch(pxUrl)]);
      if (!hr.ok) throw new Error(`Metrics history HTTP ${hr.status}`);
      if (!pr.ok) throw new Error(`Price history HTTP ${pr.status}`);

      const hj = await hr.json();
      const pj = await pr.json();
      const metricRows: MetricApiRow[] = hj.rows || [];
      const priceRows: PriceRow[] = (pj.rows || []).map((r: any) => ({
        ts: r.ts,
        close: r.close,
        volume: r.volume ?? null,
      }));

      setMeta({ priceN: priceRows.length, metricN: metricRows.length });
      setChartData(mergeForward(metricRows, priceRows));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load series");
      setChartData([]);
      setMeta(null);
    } finally {
      setLoading(false);
    }
  }, [apiBase, symbol, resolution, start, end, provider]);

  const axisProps = useMemo(
    () => ({
      tickFormatter: (v: string) => formatTick(v, resolution),
    }),
    [resolution],
  );

  const strokePrice = "var(--accent, #aa3bff)";

  return (
    <section className="time-series-section" style={{ marginTop: 36, textAlign: "left" }}>
      <h2 style={{ margin: "16px 0 8px", fontSize: "1.35rem" }}>Time series: metrics &amp; price</h2>
      <p style={{ margin: "0 0 16px", fontSize: 14, color: "var(--text, #666)", maxWidth: 720 }}>
        Daily value metrics come from precomputed storage (<code>period=daily</code>). Price uses the selected resolution
        (daily / hourly / minute). On intraday resolutions, metrics are forward-filled from the latest daily point (fundamentals
        update at most daily). Minute bars are limited to about the last week by the data provider. Click a legend label to
        hide or show that series.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end", marginBottom: 16 }}>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Symbol</label>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ width: 120, padding: 6 }} />
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Price interval</label>
          <select
            value={resolution}
            onChange={(e) => setResolution(e.target.value as Resolution)}
            style={{ padding: 6 }}
          >
            <option value="daily">Daily</option>
            <option value="hourly">Hourly</option>
            <option value="minute">Minute</option>
          </select>
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Fundamentals provider</label>
          <select value={provider} onChange={(e) => setProvider(e.target.value as "yfinance" | "sec")} style={{ padding: 6, minWidth: 140 }}>
            <option value="yfinance">yfinance (pipeline)</option>
            <option value="sec">SEC</option>
          </select>
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Start</label>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={{ padding: 6 }} />
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>End</label>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={{ padding: 6 }} />
        </div>
        <button type="button" onClick={applyResolutionDefaults} style={{ padding: "6px 10px" }}>
          Default range
        </button>
        <button type="button" onClick={() => void load()} disabled={loading} style={{ padding: "6px 14px" }}>
          {loading ? "Loading…" : "Load charts"}
        </button>
      </div>

      {error && <div style={{ color: "crimson", marginBottom: 12 }}>{error}</div>}
      {meta && (
        <div style={{ fontSize: 12, color: "var(--text, #666)", marginBottom: 12 }}>
          Loaded {meta.priceN} price bars · {meta.metricN} daily metric rows (merged forward onto price timeline)
        </div>
      )}

      {chartData.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <PriceSeriesBlock chartData={chartData} axisProps={axisProps} strokePrice={strokePrice} />

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
              gap: 16,
            }}
          >
            {METRICS.map((m, i) => (
              <MetricSparkBlock key={m.key} m={m} index={i} chartData={chartData} axisProps={axisProps} />
            ))}
          </div>
        </div>
      )}

      {!loading && chartData.length === 0 && !error && (
        <div style={{ color: "#888", fontSize: 14 }}>Click &quot;Load charts&quot; to fetch data.</div>
      )}
    </section>
  );
}

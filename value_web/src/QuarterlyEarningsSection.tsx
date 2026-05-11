import { useCallback, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type FundRow = {
  symbol?: string;
  asof_date: string;
  period?: string;
  provider?: string;
  eps?: number | null;
  net_income?: number | null;
  revenue?: number | null;
};

type SplitRow = {
  ex_date: string;
  split_ratio: number;
};

type Props = { apiBase: string };

function ymdShiftYears(yearsBack: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - yearsBack);
  return d.toISOString().slice(0, 10);
}

/** yfinance: ratio = new shares per 1 pre-split share (e.g. 4 = 4:1). */
function formatSplitLabel(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return "split";
  if (ratio >= 1) {
    const r = ratio % 1 === 0 ? String(ratio) : ratio.toFixed(2);
    return `${r}:1`;
  }
  const inv = Math.round(1 / ratio);
  return `1:${inv}`;
}

export function QuarterlyEarningsSection({ apiBase }: Props) {
  const [symbol, setSymbol] = useState("AAPL");
  const [provider, setProvider] = useState<"yfinance" | "sec">("yfinance");
  const [years, setYears] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<FundRow[]>([]);
  const [splits, setSplits] = useState<SplitRow[]>([]);

  const load = useCallback(async () => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) {
      setError("Enter a symbol");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const start = ymdShiftYears(years);
      const end = new Date().toISOString().slice(0, 10);
      const fundUrl = `${apiBase}/value/fundamentals/quarterly?symbol=${encodeURIComponent(sym)}&provider=${encodeURIComponent(provider)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      const splitUrl = `${apiBase}/value/stock/splits?symbol=${encodeURIComponent(sym)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      const [fr, sr] = await Promise.all([fetch(fundUrl), fetch(splitUrl)]);
      if (!fr.ok) throw new Error(`Fundamentals HTTP ${fr.status}`);
      const fj = await fr.json();
      setRows(fj.rows || []);

      if (sr.ok) {
        const sj = await sr.json();
        const raw = (sj.rows || []) as { ex_date?: string; split_ratio?: number }[];
        setSplits(
          raw
            .filter((r) => r.ex_date && r.split_ratio != null && Number.isFinite(Number(r.split_ratio)))
            .map((r) => ({ ex_date: String(r.ex_date).slice(0, 10), split_ratio: Number(r.split_ratio) })),
        );
      } else {
        setSplits([]);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setRows([]);
      setSplits([]);
    } finally {
      setLoading(false);
    }
  }, [apiBase, symbol, provider, years]);

  const chartData = useMemo(() => {
    return rows
      .filter((r) => r.asof_date && r.eps != null && !Number.isNaN(Number(r.eps)))
      .map((r) => {
        const d = r.asof_date.slice(0, 10);
        return {
          asof_date: d,
          label: d.slice(0, 7),
          eps: Number(r.eps),
          net_income: r.net_income != null ? Number(r.net_income) : null,
          revenue: r.revenue != null ? Number(r.revenue) : null,
        };
      })
      .sort((a, b) => a.asof_date.localeCompare(b.asof_date));
  }, [rows]);

  /** Map each split ex-date to the first fiscal quarter bar at or after the split (X = YYYY-MM). */
  const splitMarkers = useMemo(() => {
    if (!chartData.length || !splits.length) return [];
    const sorted = [...chartData].sort((a, b) => a.asof_date.localeCompare(b.asof_date));
    const byLabel = new Map<string, string[]>();
    for (const s of splits) {
      const ex = s.ex_date.slice(0, 10);
      const row = sorted.find((r) => r.asof_date >= ex);
      if (!row) continue;
      const t = formatSplitLabel(s.split_ratio);
      if (!byLabel.has(row.label)) byLabel.set(row.label, []);
      byLabel.get(row.label)!.push(t);
    }
    return Array.from(byLabel.entries()).map(([label, texts]) => {
      const uniq = [...new Set(texts)];
      const caption = uniq.length === 1 ? uniq[0]! : uniq.join(", ");
      return { label, caption };
    });
  }, [chartData, splits]);

  return (
    <section style={{ marginTop: 36, textAlign: "left" }}>
      <h2 style={{ margin: "16px 0 8px", fontSize: "1.35rem" }}>Quarterly earnings (EPS)</h2>
      <p style={{ margin: "0 0 12px", fontSize: 14, color: "var(--text, #666)", maxWidth: 720 }}>
        Diluted EPS per fiscal quarter from SQLite fundamentals (<code>vm_fundamental_points</code>). Populate via the yfinance / SEC
        fundamentals backfill for the chosen provider. Orange dashed lines mark stock splits stored in <code>vm_stock_splits</code>{" "}
        (sourced from yfinance when you load this chart or run the daily backfill).
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end", marginBottom: 12 }}>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Symbol</label>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ width: 120, padding: 6 }} />
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Fundamentals provider</label>
          <select value={provider} onChange={(e) => setProvider(e.target.value as "yfinance" | "sec")} style={{ padding: 6 }}>
            <option value="yfinance">yfinance</option>
            <option value="sec">SEC</option>
          </select>
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Lookback (years)</label>
          <input
            type="number"
            min={1}
            max={40}
            value={years}
            onChange={(e) => setYears(Number(e.target.value) || 20)}
            style={{ width: 72, padding: 6 }}
          />
        </div>
        <button type="button" onClick={() => void load()} disabled={loading} style={{ padding: "6px 14px" }}>
          {loading ? "Loading…" : "Load chart"}
        </button>
      </div>

      {error && <div style={{ color: "crimson", marginBottom: 10 }}>{error}</div>}

      {chartData.length > 0 && (
        <div style={{ width: "100%", height: 320, marginTop: 8 }}>
          <ResponsiveContainer>
            <BarChart data={chartData} margin={{ top: 28, right: 12, left: 8, bottom: 48 }}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
              <XAxis
                dataKey="label"
                angle={-40}
                textAnchor="end"
                height={52}
                minTickGap={12}
                fontSize={10}
                tick={{ fill: "var(--text, #555)" }}
              />
              <YAxis domain={["auto", "auto"]} fontSize={11} width={52} tickFormatter={(v) => Number(v).toFixed(2)} />
              <Tooltip
                formatter={(value: number) => [`${Number(value).toFixed(4)}`, "Diluted EPS"]}
                labelFormatter={(_, payload) => {
                  const p = payload?.[0]?.payload as { asof_date?: string } | undefined;
                  return p?.asof_date ? `Quarter end ${p.asof_date}` : "";
                }}
              />
              {splitMarkers.map((m) => (
                <ReferenceLine
                  key={`${m.label}-${m.caption}`}
                  x={m.label}
                  stroke="#ea580c"
                  strokeDasharray="5 4"
                  strokeWidth={1.5}
                  isFront
                  label={{
                    value: m.caption,
                    position: "top",
                    fill: "#c2410c",
                    fontSize: 10,
                  }}
                />
              ))}
              <Bar dataKey="eps" name="Diluted EPS" fill="#3b7ddd" radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {!loading && chartData.length === 0 && !error && (
        <div style={{ color: "#888", fontSize: 14 }}>Click &quot;Load chart&quot; after backfilling quarterly fundamentals.</div>
      )}
      {!loading && rows.length > 0 && chartData.length === 0 && !error && (
        <div style={{ color: "#a60", fontSize: 14 }}>No EPS values in range — check backfill or try the other provider.</div>
      )}
    </section>
  );
}

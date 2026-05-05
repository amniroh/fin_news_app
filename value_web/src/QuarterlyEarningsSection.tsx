import { useCallback, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
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

type Props = { apiBase: string };

function ymdShiftYears(yearsBack: number): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - yearsBack);
  return d.toISOString().slice(0, 10);
}

export function QuarterlyEarningsSection({ apiBase }: Props) {
  const [symbol, setSymbol] = useState("AAPL");
  const [provider, setProvider] = useState<"yfinance" | "sec">("yfinance");
  const [years, setYears] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<FundRow[]>([]);

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
      const url = `${apiBase}/value/fundamentals/quarterly?symbol=${encodeURIComponent(sym)}&provider=${encodeURIComponent(provider)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setRows(j.rows || []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setRows([]);
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

  return (
    <section style={{ marginTop: 36, textAlign: "left" }}>
      <h2 style={{ margin: "16px 0 8px", fontSize: "1.35rem" }}>Quarterly earnings (EPS)</h2>
      <p style={{ margin: "0 0 12px", fontSize: 14, color: "var(--text, #666)", maxWidth: 720 }}>
        Diluted EPS per fiscal quarter from SQLite fundamentals (<code>vm_fundamental_points</code>). Populate via the yfinance / SEC
        fundamentals backfill for the chosen provider.
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
            <BarChart data={chartData} margin={{ top: 8, right: 12, left: 8, bottom: 48 }}>
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

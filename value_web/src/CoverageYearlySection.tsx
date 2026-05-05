import { useCallback, useMemo, useState } from "react";

type YearRow = {
  year: number;
  n_days: number;
  n_pe?: number | null;
  fraction_pe?: number | null;
};

type Props = { apiBase: string };

export function CoverageYearlySection({ apiBase }: Props) {
  const [symbol, setSymbol] = useState("AAPL");
  const [provider, setProvider] = useState<"yfinance" | "sec">("yfinance");
  const [years, setYears] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<YearRow[]>([]);
  const [meta, setMeta] = useState<{ window?: { start: string; end: string } } | null>(null);

  const load = useCallback(async () => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) {
      setError("Enter a symbol");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const url = `${apiBase}/value/coverage/yearly?symbol=${encodeURIComponent(sym)}&provider=${encodeURIComponent(provider)}&years=${encodeURIComponent(String(years))}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setRows(j.years || []);
      setMeta({ window: j.window });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setRows([]);
      setMeta(null);
    } finally {
      setLoading(false);
    }
  }, [apiBase, symbol, provider, years]);

  const pct = (x: number | null | undefined) =>
    x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(1)}%`;

  const summary = useMemo(() => {
    if (!rows.length) return null;
    const last = rows[rows.length - 1];
    return `Latest year ${last.year}: ${pct(last.fraction_pe)} of trading days have P/E (daily pipeline)`;
  }, [rows]);

  return (
    <section style={{ marginTop: 36, textAlign: "left" }}>
      <h2 style={{ margin: "16px 0 8px", fontSize: "1.35rem" }}>Coverage by calendar year</h2>
      <p style={{ margin: "0 0 12px", fontSize: 14, color: "var(--text, #666)", maxWidth: 720 }}>
        Fraction of trading days in SQLite with a non-null P/E for the selected fundamentals provider.
        Run <code>value_metrics_daily_backfill.py</code> to populate data.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end", marginBottom: 12 }}>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Symbol</label>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ width: 120, padding: 6 }} />
        </div>
        <div>
          <label style={{ display: "block", fontSize: 12 }}>Fundamentals provider</label>
          <select value={provider} onChange={(e) => setProvider(e.target.value as "yfinance" | "sec")} style={{ padding: 6 }}>
            <option value="yfinance">yfinance (pipeline)</option>
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
          {loading ? "Loading…" : "Load coverage"}
        </button>
      </div>

      {error && <div style={{ color: "crimson", marginBottom: 10 }}>{error}</div>}
      {meta?.window && (
        <div style={{ fontSize: 12, color: "#666", marginBottom: 8 }}>
          Window {meta.window.start} … {meta.window.end}
        </div>
      )}
      {summary && <div style={{ fontSize: 13, marginBottom: 10 }}>{summary}</div>}

      {rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", fontSize: 14 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Year</th>
                <th style={{ textAlign: "right", padding: 8, borderBottom: "1px solid #ddd" }}>Trading days</th>
                <th style={{ textAlign: "right", padding: 8, borderBottom: "1px solid #ddd" }}>Days with P/E</th>
                <th style={{ textAlign: "right", padding: 8, borderBottom: "1px solid #ddd" }}>P/E coverage</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.year}>
                  <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0" }}>{r.year}</td>
                  <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", textAlign: "right" }}>{r.n_days}</td>
                  <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", textAlign: "right" }}>{r.n_pe ?? "—"}</td>
                  <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", textAlign: "right" }}>{pct(r.fraction_pe)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

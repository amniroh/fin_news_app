import { useEffect, useMemo, useState } from "react";
import "./App.css";
import { TimeSeriesSection } from "./TimeSeriesSection";

function App() {
  const [userId, setUserId] = useState("demo");
  const [symbolsText, setSymbolsText] = useState("AAPL,MSFT,NVDA,AMZN");
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [sortKey, setSortKey] = useState<string>("symbol");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  // Default: no filters (avoid empty-table surprise).
  const [filters, setFilters] = useState<Record<string, { min?: number; max?: number }>>({});

  const apiBase = (import.meta as any).env?.VITE_API_BASE || "http://localhost:8000";

  async function fetchMetrics() {
    setLoading(true);
    setError(null);
    try {
      const syms = symbolsText
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      const url = `${apiBase}/value/metrics?symbols=${encodeURIComponent(syms.join(","))}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setRows(j.rows || []);
    } catch (e: any) {
      setError(e?.message || "Failed to fetch");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchMetrics();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const columns: { key: string; label: string; fmt?: (v: any) => string }[] = [
    { key: "symbol", label: "Symbol" },
    { key: "pe", label: "P/E", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "pb", label: "P/B", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "peg", label: "PEG", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "dividend_yield", label: "Div Yield", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "free_cash_flow_yield", label: "FCF Yield", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "debt_to_equity", label: "D/E", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "roe", label: "ROE", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(1) + "%") },
    { key: "current_ratio", label: "Current", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "operating_margin", label: "Op Margin", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(1) + "%") },
    { key: "ev_to_ebitda", label: "EV/EBITDA", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "total_return_1y", label: "Total Return 1Y", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "total_return_3y", label: "Total Return 3Y", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "total_return_5y", label: "Total Return 5Y", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "total_return_10y", label: "Total Return 10Y", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "high_52w", label: "52W High", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "low_52w", label: "52W Low", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "range_position_52w", label: "52W Range Pos", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(1) + "%") },
    { key: "ytd_return", label: "YTD Return", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "sharpe_ratio", label: "Sharpe", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "beta", label: "Beta", fmt: (v) => (v == null ? "" : Number(v).toFixed(3)) },
    { key: "alpha", label: "Alpha", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "volatility", label: "Volatility", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "max_drawdown", label: "Max Drawdown", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "average_volume", label: "Avg Volume (30d)", fmt: (v) => (v == null ? "" : Number(v).toLocaleString()) },
    { key: "expense_ratio", label: "Expense Ratio", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "trailing_pe", label: "Trailing P/E", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_7d", label: "Mean RSI 7d", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_30d", label: "Mean RSI 30d", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_3m", label: "Mean RSI 3m", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_1y", label: "Mean RSI 1Y", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
  ];

  const filteredSorted = useMemo(() => {
    const applyFilters = (r: any) => {
      for (const [k, mm] of Object.entries(filters)) {
        const v = r[k];
        if (v == null) return false;
        if (mm.min != null && Number(v) < mm.min) return false;
        if (mm.max != null && Number(v) > mm.max) return false;
      }
      return true;
    };
    const out = rows.filter(applyFilters);
    out.sort((a, b) => {
      const va = a[sortKey];
      const vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string" || typeof vb === "string") {
        const cmp = String(va).localeCompare(String(vb));
        return sortDir === "asc" ? cmp : -cmp;
      }
      const cmp = Number(va) - Number(vb);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [rows, filters, sortKey, sortDir]);

  function toggleSort(k: string) {
    if (sortKey === k) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else {
      setSortKey(k);
      setSortDir("asc");
    }
  }

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 16 }}>
      <h2 style={{ margin: "12px 0" }}>Value Metrics Tracker</h2>

      <div style={{ display: "flex", gap: 12, alignItems: "end", flexWrap: "wrap" }}>
        <div>
          <label>User</label>
          <input value={userId} onChange={(e) => setUserId(e.target.value)} style={{ display: "block", width: 180 }} />
        </div>
        <div style={{ flex: 1, minWidth: 320 }}>
          <label>Symbols</label>
          <input
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            style={{ display: "block", width: "100%" }}
          />
        </div>
        <button onClick={fetchMetrics} disabled={loading}>
          {loading ? "Loading..." : "Refresh"}
        </button>
      </div>

      <div style={{ marginTop: 12 }}>
        <strong>Quick filter</strong>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6 }}>
          <div>
            <label>P/E max</label>
            <input
              type="number"
              value={filters.pe?.max ?? ""}
              onChange={(e) =>
                setFilters({
                  ...filters,
                  pe: { ...filters.pe, max: e.target.value === "" ? undefined : Number(e.target.value) },
                })
              }
              style={{ display: "block", width: 140 }}
            />
          </div>
          <button onClick={() => setFilters({})}>Clear filters</button>
        </div>
      </div>

      {error && <div style={{ color: "crimson", marginTop: 10 }}>{error}</div>}

      <div style={{ overflowX: "auto", marginTop: 14 }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  onClick={() => toggleSort(c.key)}
                  style={{
                    textAlign: "left",
                    padding: 8,
                    borderBottom: "1px solid #ddd",
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  {c.label} {sortKey === c.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filteredSorted.map((r) => (
              <tr key={r.symbol}>
                {columns.map((c) => {
                  const v = r[c.key];
                  const txt = c.fmt ? c.fmt(v) : v ?? "";
                  let color: string | undefined;
                  if (c.key === "pe" && v != null) color = Number(v) <= 15 ? "green" : Number(v) >= 30 ? "crimson" : undefined;
                  if (c.key === "debt_to_equity" && v != null) color = Number(v) <= 100 ? "green" : Number(v) >= 200 ? "crimson" : undefined;
                  if (c.key === "operating_margin" && v != null) color = Number(v) >= 0.2 ? "green" : Number(v) <= 0.05 ? "crimson" : undefined;
                  return (
                    <td key={c.key} style={{ padding: 8, borderBottom: "1px solid #f0f0f0", color }}>
                      {txt}
                    </td>
                  );
                })}
              </tr>
            ))}
            {filteredSorted.length === 0 && (
              <tr>
                <td colSpan={columns.length} style={{ padding: 12, color: "#666" }}>
                  No rows match the filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 14, fontSize: 12, color: "#666" }}>
        Alerts/watchlist API is available at <code>{apiBase}/value/...</code> (backend).
      </div>

      <TimeSeriesSection apiBase={apiBase} />
    </div>
  );
}

export default App

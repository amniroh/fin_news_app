import { useEffect, useMemo, useState } from "react";
import "./App.css";
import { AddInterestingStock } from "./AddInterestingStock";
import { SymbolPricePopover } from "./SymbolPricePopover";

type LoadMode = "custom" | "0" | "1" | "2" | "3" | "all";

type FilterKind = "numeric" | "enum";

type FilterableColumn = {
  key: string;
  label: string;
  kind: FilterKind;
  enumOptions?: { value: string; label: string }[];
};

type NumericFilter = { kind: "numeric"; column: string; min?: number; max?: number };
type EnumFilter = { kind: "enum"; column: string; values: string[] };
type ColumnFilter = NumericFilter | EnumFilter;

const PILLAR_COLUMNS: { key: string; label: string }[] = [
  { key: "value_pillar_competitive_edge", label: "Moat" },
  { key: "value_pillar_management_competence", label: "Mgmt" },
  { key: "value_pillar_financial_fortress", label: "Fortress" },
  { key: "value_pillar_pricing_power", label: "Pricing" },
  { key: "value_pillar_understandability", label: "Understand" },
  { key: "value_pillar_valuation", label: "Valuation" },
];

const ANALYST_FILTER_OPTIONS = [
  { value: "strong_buy", label: "Strong Buy" },
  { value: "buy", label: "Buy" },
  { value: "hold", label: "Hold" },
  { value: "sell", label: "Sell" },
  { value: "strong_sell", label: "Strong Sell" },
];

const FILTERABLE_COLUMNS: FilterableColumn[] = [
  { key: "pe", label: "P/E", kind: "numeric" },
  { key: "pb", label: "P/B", kind: "numeric" },
  { key: "peg", label: "PEG", kind: "numeric" },
  { key: "mean_rsi_30d", label: "RSI (30d)", kind: "numeric" },
  { key: "mean_rsi_7d", label: "RSI (7d)", kind: "numeric" },
  { key: "total_return_1y", label: "Momentum (1Y)", kind: "numeric" },
  { key: "value_trading_score", label: "LLM value score", kind: "numeric" },
  { key: "value_pillar_competitive_edge", label: "Moat score", kind: "numeric" },
  { key: "value_pillar_valuation", label: "Valuation score", kind: "numeric" },
  { key: "debt_to_equity", label: "D/E", kind: "numeric" },
  { key: "roe", label: "ROE", kind: "numeric" },
  { key: "operating_margin", label: "Op Margin", kind: "numeric" },
  { key: "universe_priority", label: "Priority", kind: "numeric" },
  {
    key: "analyst_recommendation_key",
    label: "Analyst rating",
    kind: "enum",
    enumOptions: ANALYST_FILTER_OPTIONS,
  },
];

function newFilterForColumn(colKey: string): ColumnFilter {
  const col = FILTERABLE_COLUMNS.find((c) => c.key === colKey);
  if (col?.kind === "enum") return { kind: "enum", column: colKey, values: [] };
  return { kind: "numeric", column: colKey };
}

function rowPassesFilter(row: Record<string, unknown>, f: ColumnFilter): boolean {
  const raw = row[f.column];
  if (f.kind === "numeric") {
    if (raw == null || raw === "") return false;
    const n = Number(raw);
    if (Number.isNaN(n)) return false;
    if (f.min != null && n < f.min) return false;
    if (f.max != null && n > f.max) return false;
    return true;
  }
  if (!f.values.length) return true;
  if (raw == null || raw === "") return false;
  return f.values.includes(String(raw).toLowerCase());
}

function filterIsActive(f: ColumnFilter): boolean {
  if (f.kind === "numeric") return f.min != null || f.max != null;
  return f.values.length > 0;
}

function formatAnalystKey(v: unknown): string {
  if (v == null || v === "") return "—";
  return String(v)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function cellTitle(row: Record<string, unknown>, colKey: string): string | undefined {
  if (colKey === "analyst_recommendation_key") {
    const parts: string[] = [];
    if (row.analyst_asof_date) parts.push(`As of ${row.analyst_asof_date}`);
    if (row.analyst_recommendation_mean != null) {
      parts.push(`Consensus mean: ${Number(row.analyst_recommendation_mean).toFixed(2)}`);
    }
    return parts.length ? parts.join(" · ") : undefined;
  }
  if (colKey === "value_trading_score" && row.value_trading_summary) {
    return String(row.value_trading_summary);
  }
  if (colKey.startsWith("value_pillar_")) {
    const rationale = row[`${colKey}_rationale`];
    if (rationale) return String(rationale);
  }
  return undefined;
}

function App() {
  const [symbolsText, setSymbolsText] = useState("AAPL,MSFT,NVDA,AMZN");
  const [loadMode, setLoadMode] = useState<LoadMode>("0");
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [sortKey, setSortKey] = useState<string>("symbol");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [columnFilters, setColumnFilters] = useState<ColumnFilter[]>([{ kind: "numeric", column: "pe" }]);

  const apiBase =
    (import.meta as any).env?.VITE_API_BASE ??
    (import.meta.env.DEV ? "http://localhost:8000" : "");

  async function fetchMetrics() {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (loadMode === "custom") {
        const syms = symbolsText
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean);
        if (!syms.length) throw new Error("Enter at least one symbol");
        params.set("symbols", syms.join(","));
      } else {
        params.set("priority", loadMode);
      }
      const r = await fetch(`${apiBase}/value/metrics/tracker?${params}`);
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
  }, [loadMode]);

  const columns: { key: string; label: string; fmt?: (v: any) => string }[] = [
    { key: "symbol", label: "Symbol" },
    { key: "universe_priority", label: "Priority", fmt: (v) => (v == null ? "" : `P${v}`) },
    { key: "pe", label: "Low P/E", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_30d", label: "Low RSI", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    {
      key: "analyst_recommendation_key",
      label: "Analyst ratings",
      fmt: (v) => formatAnalystKey(v),
    },
    {
      key: "total_return_1y",
      label: "Momentum",
      fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%"),
    },
    {
      key: "value_trading_score",
      label: "Value assessed by LLM",
      fmt: (v) => (v == null ? "—" : `${Number(v)}/30`),
    },
    ...PILLAR_COLUMNS.map((p) => ({
      key: p.key,
      label: p.label,
      fmt: (v: any) => (v == null ? "—" : `${Number(v)}/5`),
    })),
    {
      key: "metrics_asof_date",
      label: "Daily metrics as of",
      fmt: (v) => (v == null || v === "" ? "—" : String(v)),
    },
    { key: "pb", label: "P/B", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "peg", label: "PEG", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "dividend_yield", label: "Div Yield", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "free_cash_flow_yield", label: "FCF Yield", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(2) + "%") },
    { key: "debt_to_equity", label: "D/E", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "roe", label: "ROE", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(1) + "%") },
    { key: "current_ratio", label: "Current", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "operating_margin", label: "Op Margin", fmt: (v) => (v == null ? "" : (Number(v) * 100).toFixed(1) + "%") },
    { key: "ev_to_ebitda", label: "EV/EBITDA", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
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
    { key: "mean_rsi_3m", label: "Mean RSI 3m", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
    { key: "mean_rsi_1y", label: "Mean RSI 1Y", fmt: (v) => (v == null ? "" : Number(v).toFixed(2)) },
  ];

  function valueScoreColor(score: number): string {
    if (score >= 24) return "#276749";
    if (score >= 18) return "#48bb78";
    if (score >= 12) return "#d69e2e";
    return "#c53030";
  }

  function pillarColor(score: number): string | undefined {
    if (score >= 4) return "#276749";
    if (score >= 3) return "#48bb78";
    if (score <= 1) return "#c53030";
    return undefined;
  }

  const filteredSorted = useMemo(() => {
    const active = columnFilters.filter(filterIsActive);
    const out = rows.filter((r) => active.every((f) => rowPassesFilter(r, f)));
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
  }, [rows, columnFilters, sortKey, sortDir]);

  function updateFilter(idx: number, next: ColumnFilter) {
    setColumnFilters((prev) => prev.map((f, i) => (i === idx ? next : f)));
  }

  function addFilter() {
    const used = new Set(columnFilters.map((f) => f.column));
    const nextCol = FILTERABLE_COLUMNS.find((c) => !used.has(c.key))?.key ?? "pe";
    setColumnFilters((prev) => [...prev, newFilterForColumn(nextCol)]);
  }

  function removeFilter(idx: number) {
    setColumnFilters((prev) => prev.filter((_, i) => i !== idx));
  }

  function changeFilterColumn(idx: number, colKey: string) {
    setColumnFilters((prev) => prev.map((f, i) => (i === idx ? newFilterForColumn(colKey) : f)));
  }

  const activeFilterCount = columnFilters.filter(filterIsActive).length;

  function toggleSort(k: string) {
    if (sortKey === k) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else {
      setSortKey(k);
      setSortDir("asc");
    }
  }

  return (
    <div style={{ maxWidth: 1200, margin: "0 auto", padding: 16 }}>
      <AddInterestingStock apiBase={apiBase} />

      <h2 style={{ margin: "12px 0" }}>Value Metrics Tracker</h2>

      <div style={{ display: "flex", gap: 12, alignItems: "end", flexWrap: "wrap", marginBottom: 12 }}>
        <div>
          <label>Load stocks</label>
          <select
            value={loadMode}
            onChange={(e) => setLoadMode(e.target.value as LoadMode)}
            style={{ display: "block", padding: 8, minWidth: 160 }}
          >
            <option value="0">Priority P0</option>
            <option value="1">Priority P1</option>
            <option value="2">Priority P2</option>
            <option value="3">Priority P3</option>
            <option value="all">All interesting stocks</option>
            <option value="custom">Custom symbols</option>
          </select>
        </div>
        {loadMode === "custom" && (
          <div style={{ flex: 1, minWidth: 280 }}>
            <label>Symbols</label>
            <input
              value={symbolsText}
              onChange={(e) => setSymbolsText(e.target.value)}
              style={{ display: "block", width: "100%" }}
            />
          </div>
        )}
        <button onClick={fetchMetrics} disabled={loading}>
          {loading ? "Loading..." : "Refresh"}
        </button>
      </div>

      <div className="filter-panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <strong>Column filters</strong>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" onClick={addFilter}>
              + Add filter
            </button>
            <button type="button" onClick={() => setColumnFilters([])}>
              Clear all
            </button>
          </div>
        </div>
        {columnFilters.length === 0 && (
          <div style={{ color: "#64748b", fontSize: 14 }}>
            No filters — showing all loaded rows. Click &quot;+ Add filter&quot; to filter by P/E, analyst rating, etc.
          </div>
        )}
        {columnFilters.map((f, idx) => {
          const meta = FILTERABLE_COLUMNS.find((c) => c.key === f.column) ?? FILTERABLE_COLUMNS[0];
          return (
            <div key={idx} className="filter-row">
              <div>
                <label>Column</label>
                <select
                  value={f.column}
                  onChange={(e) => changeFilterColumn(idx, e.target.value)}
                  style={{ minWidth: 160 }}
                >
                  {FILTERABLE_COLUMNS.map((c) => (
                    <option key={c.key} value={c.key}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
              {f.kind === "numeric" ? (
                <>
                  <div>
                    <label>Min</label>
                    <input
                      type="number"
                      step="any"
                      value={f.min ?? ""}
                      onChange={(e) =>
                        updateFilter(idx, {
                          ...f,
                          min: e.target.value === "" ? undefined : Number(e.target.value),
                        })
                      }
                      style={{ width: 100 }}
                    />
                  </div>
                  <div>
                    <label>Max</label>
                    <input
                      type="number"
                      step="any"
                      value={f.max ?? ""}
                      onChange={(e) =>
                        updateFilter(idx, {
                          ...f,
                          max: e.target.value === "" ? undefined : Number(e.target.value),
                        })
                      }
                      style={{ width: 100 }}
                    />
                  </div>
                </>
              ) : (
                <div>
                  <label>Rating</label>
                  <select
                    multiple
                    value={f.values}
                    onChange={(e) => {
                      const values = Array.from(e.target.selectedOptions).map((o) => o.value);
                      updateFilter(idx, { ...f, values });
                    }}
                    style={{ minWidth: 180, minHeight: 72 }}
                  >
                    {(meta.enumOptions ?? []).map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <button type="button" onClick={() => removeFilter(idx)} style={{ marginBottom: 2 }}>
                Remove
              </button>
            </div>
          );
        })}
        <div className="filter-summary">
          Showing {filteredSorted.length} of {rows.length} rows
          {activeFilterCount > 0 ? ` (${activeFilterCount} active filter${activeFilterCount === 1 ? "" : "s"})` : ""}
        </div>
      </div>

      {error && <div style={{ color: "crimson", marginTop: 10 }}>{error}</div>}

      <div className="tracker-scroll">
        <table className="tracker-table">
          <thead>
            <tr>
              {columns.map((c, colIdx) => (
                <th
                  key={c.key}
                  onClick={() => toggleSort(c.key)}
                  className={colIdx === 0 ? "sticky-col" : undefined}
                >
                  {c.label} {sortKey === c.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filteredSorted.map((r) => (
              <tr key={r.symbol}>
                {columns.map((c, colIdx) => {
                  const v = r[c.key];
                  const txt = c.fmt ? c.fmt(v) : v ?? "";
                  let color: string | undefined;
                  if (c.key === "pe" && v != null) color = Number(v) <= 15 ? "green" : Number(v) >= 30 ? "crimson" : undefined;
                  if (c.key === "mean_rsi_30d" && v != null) {
                    color = Number(v) <= 30 ? "green" : Number(v) >= 70 ? "crimson" : undefined;
                  }
                  if (c.key === "analyst_recommendation_key" && v != null) {
                    const k = String(v).toLowerCase();
                    if (k.includes("buy") || k === "strong_buy") color = "green";
                    else if (k.includes("sell") || k === "underperform") color = "crimson";
                  }
                  if (c.key === "total_return_1y" && v != null) {
                    color = Number(v) > 0 ? "green" : Number(v) < 0 ? "crimson" : undefined;
                  }
                  if (c.key === "value_trading_score" && v != null) color = valueScoreColor(Number(v));
                  if (c.key.startsWith("value_pillar_") && v != null) color = pillarColor(Number(v));
                  if (c.key === "debt_to_equity" && v != null) color = Number(v) <= 100 ? "green" : Number(v) >= 200 ? "crimson" : undefined;
                  if (c.key === "operating_margin" && v != null) color = Number(v) >= 0.2 ? "green" : Number(v) <= 0.05 ? "crimson" : undefined;
                  const title = cellTitle(r, c.key);
                  return (
                    <td
                      key={c.key}
                      title={title}
                      className={colIdx === 0 ? "sticky-col" : undefined}
                      style={{
                        color,
                        fontWeight:
                          c.key === "value_trading_score" || c.key.startsWith("value_pillar_")
                            ? v != null
                              ? 600
                              : undefined
                            : undefined,
                      }}
                    >
                      {c.key === "symbol" ? (
                        <SymbolPricePopover symbol={String(r.symbol)} apiBase={apiBase} />
                      ) : (
                        txt
                      )}
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
    </div>
  );
}

export default App;

import { useCallback, useEffect, useMemo, useState } from "react";
import { ColumnHeaderTooltip } from "./ColumnHeaderTooltip";
import { predictionColumnDoc } from "./predictionMarketColumnDocs";

type SignalRow = {
  id: number;
  source: string;
  external_id: string;
  title: string;
  status: string;
  close_time_utc?: string | null;
  blockchain_ref?: string | null;
  event_url?: string | null;
  latest_yes_price?: number | null;
  volume_total?: number | null;
  volume_24h?: number | null;
  transaction_volume?: number | null;
  trade_count?: number | null;
  relevance_score?: number | null;
  is_time_sensitive?: boolean;
  time_sensitive_reason?: string | null;
  signal_won?: boolean | null;
  profit_if_followed?: number | null;
  pending?: number;
  settlement_result?: string | null;
};

type ApiResponse = {
  n: number;
  total: number;
  rows: SignalRow[];
  sync: { source: string; last_sync_ts_utc?: string; markets_fetched?: number; meta?: Record<string, unknown> }[];
  aggregate: { settled_n: number; wins: number; losses: number; win_rate?: number | null };
};

type FilterKind = "numeric" | "enum" | "bool";

type FilterableColumn = {
  key: string;
  label: string;
  kind: FilterKind;
  enumOptions?: { value: string; label: string }[];
};

type NumericFilter = { kind: "numeric"; column: string; min?: number; max?: number };
type EnumFilter = { kind: "enum"; column: string; values: string[] };
type BoolFilter = { kind: "bool"; column: string; value?: boolean };
type ColumnFilter = NumericFilter | EnumFilter | BoolFilter;

const STATUS_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "settled", label: "Settled" },
  { value: "closed", label: "Closed" },
];

const SOURCE_OPTIONS = [
  { value: "polymarket", label: "Polymarket" },
  { value: "kalshi", label: "Kalshi" },
];

const FILTERABLE_COLUMNS: FilterableColumn[] = [
  { key: "source", label: "Source", kind: "enum", enumOptions: SOURCE_OPTIONS },
  { key: "status", label: "Status", kind: "enum", enumOptions: STATUS_OPTIONS },
  { key: "volume_total", label: "Total volume", kind: "numeric" },
  { key: "volume_24h", label: "24h volume", kind: "numeric" },
  { key: "transaction_volume", label: "Recent txn volume", kind: "numeric" },
  { key: "latest_yes_price", label: "Yes price", kind: "numeric" },
  { key: "relevance_score", label: "Relevance", kind: "numeric" },
  { key: "profit_if_followed", label: "Profit", kind: "numeric" },
  { key: "is_time_sensitive", label: "Time sensitive", kind: "bool" },
];

const TABLE_COLUMNS: { key: string; label: string; fmt?: (v: unknown, row?: SignalRow) => string }[] = [
  { key: "source", label: "Source" },
  { key: "title", label: "Signal" },
  { key: "status", label: "Status" },
  { key: "close_time_utc", label: "Close", fmt: (v) => fmtDate(String(v ?? "")) },
  { key: "latest_yes_price", label: "Yes price", fmt: (v) => fmtPrice(v as number | null) },
  { key: "volume_total", label: "Total volume", fmt: (v) => fmtUsd(v as number | null) },
  { key: "volume_24h", label: "24h volume", fmt: (v) => fmtUsd(v as number | null) },
  { key: "transaction_volume", label: "Recent txn volume", fmt: (v) => fmtUsd(v as number | null) },
  { key: "trade_count", label: "Recent trades", fmt: (v) => (v == null ? "—" : String(v)) },
  { key: "is_time_sensitive", label: "Time sensitive?", fmt: (v) => (v ? "Yes" : "No") },
  { key: "signal_won", label: "Outcome", fmt: (_v, row) => (row ? outcomeLabel(row) : "—") },
  { key: "profit_if_followed", label: "Profit", fmt: (v) => (v == null ? "—" : fmtPct(v as number)) },
  { key: "relevance_score", label: "Relevance", fmt: (v) => (v == null ? "—" : Number(v).toFixed(0)) },
  { key: "blockchain_ref", label: "Blockchain / ID" },
];

function newFilterForColumn(colKey: string): ColumnFilter {
  const col = FILTERABLE_COLUMNS.find((c) => c.key === colKey);
  if (col?.kind === "enum") return { kind: "enum", column: colKey, values: [] };
  if (col?.kind === "bool") return { kind: "bool", column: colKey };
  return { kind: "numeric", column: colKey };
}

function rowPassesFilter(row: SignalRow, f: ColumnFilter): boolean {
  const raw = row[f.column as keyof SignalRow];
  if (f.kind === "numeric") {
    if (raw == null || raw === "") return false;
    const n = Number(raw);
    if (Number.isNaN(n)) return false;
    if (f.min != null && n < f.min) return false;
    if (f.max != null && n > f.max) return false;
    return true;
  }
  if (f.kind === "bool") {
    if (f.value == null) return true;
    return Boolean(raw) === f.value;
  }
  if (!f.values.length) return true;
  if (raw == null || raw === "") return false;
  return f.values.includes(String(raw).toLowerCase());
}

function filterIsActive(f: ColumnFilter): boolean {
  if (f.kind === "numeric") return f.min != null || f.max != null;
  if (f.kind === "bool") return f.value != null;
  return f.values.length > 0;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(3);
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s.slice(0, 10);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function outcomeLabel(row: SignalRow): string {
  if (row.pending) return "Pending";
  if (row.signal_won === true) return "Win";
  if (row.signal_won === false) return "Loss";
  return row.settlement_result || "—";
}

type Props = { apiBase?: string };

export function PredictionMarketsPage({ apiBase: apiBaseProp }: Props) {
  const apiBase = apiBaseProp ?? "";
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [aggregate, setAggregate] = useState<ApiResponse["aggregate"] | null>(null);
  const [sync, setSync] = useState<ApiResponse["sync"]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<string>("relevance_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [columnFilters, setColumnFilters] = useState<ColumnFilter[]>([
    { kind: "numeric", column: "volume_24h", min: 1000 },
  ]);

  const fetchRows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "1000" });
      const r = await fetch(`${apiBase}/prediction-markets/signals?${params}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: ApiResponse = await r.json();
      setRows(j.rows || []);
      setAggregate(j.aggregate || null);
      setSync(j.sync || []);
      setTotal(j.total || 0);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void fetchRows();
  }, [fetchRows]);

  const filteredSorted = useMemo(() => {
    const active = columnFilters.filter(filterIsActive);
    const out = rows.filter((r) => active.every((f) => rowPassesFilter(r, f)));
    out.sort((a, b) => {
      const va = a[sortKey as keyof SignalRow];
      const vb = b[sortKey as keyof SignalRow];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "boolean" || typeof vb === "boolean") {
        const cmp = Number(va) - Number(vb);
        return sortDir === "asc" ? cmp : -cmp;
      }
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
    const nextCol = FILTERABLE_COLUMNS.find((c) => !used.has(c.key))?.key ?? "volume_24h";
    setColumnFilters((prev) => [...prev, newFilterForColumn(nextCol)]);
  }

  function removeFilter(idx: number) {
    setColumnFilters((prev) => prev.filter((_, i) => i !== idx));
  }

  function changeFilterColumn(idx: number, colKey: string) {
    setColumnFilters((prev) => prev.map((f, i) => (i === idx ? newFilterForColumn(colKey) : f)));
  }

  function toggleSort(k: string) {
    if (sortKey === k) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else {
      setSortKey(k);
      setSortDir(k.includes("volume") || k === "relevance_score" ? "desc" : "asc");
    }
  }

  const activeFilterCount = columnFilters.filter(filterIsActive).length;

  const runSync = async () => {
    setSyncing(true);
    setError(null);
    try {
      const r = await fetch(`${apiBase}/prediction-markets/sync`, { method: "POST" });
      if (!r.ok) throw new Error(`Sync HTTP ${r.status}`);
      await fetchRows();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  };

  const syncSummary = useMemo(() => {
    return sync
      .map((s) => {
        const meta = s.meta as { pool_size?: number; keep?: number } | undefined;
        const pool = meta?.pool_size ? `pool ${meta.pool_size}` : "";
        const keep = meta?.keep ? `kept ${s.markets_fetched ?? 0}/${meta.keep}` : `${s.markets_fetched ?? 0} stored`;
        return `${s.source}: ${keep}${pool ? ` (${pool})` : ""}`;
      })
      .join(" · ");
  }, [sync]);

  return (
    <div className="tracker-page">
      <h2 style={{ margin: "12px 0" }}>Prediction Market Signals</h2>
      <p style={{ color: "#64748b", fontSize: 14, marginTop: 0, maxWidth: 900 }}>
        Sync scans up to <strong>800 candidates per source</strong>, ranks by 24h volume, liquidity, and trading
        relevance, then keeps the <strong>top ~200 open + ~80 settled</strong> markets per exchange. Combo/noise
        contracts are deprioritized.
      </p>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end", marginBottom: 12 }}>
        <button type="button" onClick={() => void fetchRows()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
        <button type="button" onClick={() => void runSync()} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync from APIs"}
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
            No filters — showing all loaded rows. Default sync already prioritizes high-volume markets.
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
                  style={{ minWidth: 180 }}
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
              ) : f.kind === "bool" ? (
                <div>
                  <label>Value</label>
                  <select
                    value={f.value == null ? "" : f.value ? "yes" : "no"}
                    onChange={(e) => {
                      const v = e.target.value;
                      updateFilter(idx, {
                        ...f,
                        value: v === "" ? undefined : v === "yes",
                      });
                    }}
                    style={{ minWidth: 120 }}
                  >
                    <option value="">Any</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                  </select>
                </div>
              ) : (
                <div>
                  <label>Values</label>
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
          Showing {filteredSorted.length} of {rows.length} loaded ({total} in DB)
          {activeFilterCount > 0 ? ` (${activeFilterCount} active filter${activeFilterCount === 1 ? "" : "s"})` : ""}
        </div>
      </div>

      {aggregate && (
        <div className="filter-summary" style={{ marginBottom: 12 }}>
          Settled win rate {aggregate.win_rate != null ? fmtPct(aggregate.win_rate) : "—"} ({aggregate.wins}W /{" "}
          {aggregate.losses}L of {aggregate.settled_n})
          {syncSummary ? ` · Last sync: ${syncSummary}` : ""}
        </div>
      )}

      {error && <div style={{ color: "crimson", marginBottom: 10 }}>{error}</div>}

      <div className="tracker-scroll">
        <table className="tracker-table">
          <thead>
            <tr>
              {TABLE_COLUMNS.map((c, colIdx) => {
                const doc = predictionColumnDoc(c.key);
                return (
                  <th key={c.key} className={colIdx === 0 ? "sticky-col" : undefined}>
                    <div className="th-inner">
                      <button type="button" className="th-sort" onClick={() => toggleSort(c.key)}>
                        {c.label} {sortKey === c.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                      </button>
                      {doc && <ColumnHeaderTooltip label={doc.label} description={doc.description} />}
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {filteredSorted.map((r) => (
              <tr key={`${r.source}-${r.external_id}`}>
                {TABLE_COLUMNS.map((c, colIdx) => {
                  const v = c.key === "title" ? r.title : c.key === "blockchain_ref" ? r.blockchain_ref || r.external_id : r[c.key as keyof SignalRow];
                  const txt = c.fmt ? c.fmt(v, r) : String(v ?? "");
                  let color: string | undefined;
                  if (c.key === "is_time_sensitive") color = r.is_time_sensitive ? "#c53030" : "#276749";
                  if (c.key === "signal_won") {
                    color = r.signal_won === true ? "green" : r.signal_won === false ? "crimson" : undefined;
                  }
                  if (c.key === "profit_if_followed" && r.profit_if_followed != null) {
                    color = r.profit_if_followed > 0 ? "green" : r.profit_if_followed < 0 ? "crimson" : undefined;
                  }
                  return (
                    <td
                      key={c.key}
                      className={colIdx === 0 ? "sticky-col" : undefined}
                      style={{
                        color,
                        fontWeight: c.key === "signal_won" && !r.pending ? 600 : undefined,
                        maxWidth: c.key === "title" ? 360 : c.key === "blockchain_ref" ? 140 : undefined,
                        whiteSpace: c.key === "title" || c.key === "is_time_sensitive" ? "normal" : undefined,
                        fontFamily: c.key === "blockchain_ref" ? "monospace" : undefined,
                        fontSize: c.key === "blockchain_ref" ? 12 : undefined,
                        overflow: c.key === "blockchain_ref" ? "hidden" : undefined,
                        textOverflow: c.key === "blockchain_ref" ? "ellipsis" : undefined,
                        textTransform: c.key === "source" ? "capitalize" : undefined,
                      }}
                    >
                      {c.key === "title" && r.event_url ? (
                        <a href={r.event_url} target="_blank" rel="noreferrer" style={{ color: "#2563eb" }}>
                          {txt}
                        </a>
                      ) : c.key === "is_time_sensitive" ? (
                        <>
                          <span style={{ fontWeight: 600 }}>{txt}</span>
                          {r.time_sensitive_reason ? (
                            <div style={{ fontSize: 12, color: "#64748b", fontWeight: 400 }}>{r.time_sensitive_reason}</div>
                          ) : null}
                        </>
                      ) : (
                        txt
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
            {filteredSorted.length === 0 && !loading && (
              <tr>
                <td colSpan={TABLE_COLUMNS.length} style={{ padding: 12, color: "#666" }}>
                  No rows match the filters. Try Sync from APIs or loosen filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

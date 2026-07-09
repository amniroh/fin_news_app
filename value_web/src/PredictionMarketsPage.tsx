import { useCallback, useEffect, useMemo, useState } from "react";

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
  is_time_sensitive?: boolean;
  time_sensitive_reason?: string | null;
  signal_won?: boolean | null;
  profit_if_followed?: number | null;
  win_rate?: number | null;
  pending?: number;
  settlement_result?: string | null;
};

type ApiResponse = {
  n: number;
  total: number;
  rows: SignalRow[];
  sync: { source: string; last_sync_ts_utc?: string; markets_fetched?: number }[];
  aggregate: { settled_n: number; wins: number; losses: number; win_rate?: number | null };
};

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(3);
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
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchRows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "500" });
      if (sourceFilter !== "all") params.set("source", sourceFilter);
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
  }, [apiBase, sourceFilter]);

  useEffect(() => {
    void fetchRows();
  }, [fetchRows]);

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

  const syncSummary = useMemo(
    () =>
      sync
        .map((s) => `${s.source}: ${s.markets_fetched ?? 0} markets (${s.last_sync_ts_utc?.slice(0, 19) ?? "never"})`)
        .join(" · "),
    [sync],
  );

  return (
    <div className="tracker-page">
      <h2 style={{ margin: "12px 0" }}>Prediction Market Signals</h2>
      <p style={{ color: "#64748b", fontSize: 14, marginTop: 0, maxWidth: 820 }}>
        Signals from Polymarket (Polygon on-chain condition IDs + CLOB/Gamma APIs) and Kalshi (CFTC-regulated
        exchange API). Time-sensitive signals resolve soon or move quickly; win/loss is computed for settled markets
        by comparing the entry implied probability to the final outcome.
      </p>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end", marginBottom: 12 }}>
        <div>
          <label>Source</label>
          <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} style={{ display: "block", padding: 8 }}>
            <option value="all">All</option>
            <option value="polymarket">Polymarket</option>
            <option value="kalshi">Kalshi</option>
          </select>
        </div>
        <button type="button" onClick={() => void fetchRows()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
        <button type="button" onClick={() => void runSync()} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync from APIs"}
        </button>
      </div>

      {aggregate && (
        <div className="filter-summary" style={{ marginBottom: 12 }}>
          {total} signals stored · {aggregate.settled_n} settled · win rate{" "}
          {aggregate.win_rate != null ? fmtPct(aggregate.win_rate) : "—"} ({aggregate.wins}W / {aggregate.losses}L)
          {syncSummary ? ` · Last sync: ${syncSummary}` : ""}
        </div>
      )}

      {error && <div style={{ color: "crimson", marginBottom: 10 }}>{error}</div>}

      <div className="tracker-scroll">
        <table className="tracker-table">
          <thead>
            <tr>
              <th className="sticky-col">Source</th>
              <th>Signal</th>
              <th>Status</th>
              <th>Close</th>
              <th>Yes price</th>
              <th>Time sensitive?</th>
              <th>Outcome</th>
              <th>Profit</th>
              <th>Blockchain / ID</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.source}-${r.external_id}`}>
                <td className="sticky-col" style={{ textTransform: "capitalize", fontWeight: 600 }}>
                  {r.source}
                </td>
                <td style={{ maxWidth: 360, whiteSpace: "normal" }}>
                  {r.event_url ? (
                    <a href={r.event_url} target="_blank" rel="noreferrer" style={{ color: "#2563eb" }}>
                      {r.title}
                    </a>
                  ) : (
                    r.title
                  )}
                </td>
                <td>{r.status}</td>
                <td>{fmtDate(r.close_time_utc)}</td>
                <td>{fmtPrice(r.latest_yes_price)}</td>
                <td style={{ maxWidth: 220, whiteSpace: "normal" }}>
                  <span style={{ color: r.is_time_sensitive ? "#c53030" : "#276749", fontWeight: 600 }}>
                    {r.is_time_sensitive ? "Yes" : "No"}
                  </span>
                  {r.time_sensitive_reason ? (
                    <div style={{ fontSize: 12, color: "#64748b" }}>{r.time_sensitive_reason}</div>
                  ) : null}
                </td>
                <td
                  style={{
                    color: r.signal_won === true ? "green" : r.signal_won === false ? "crimson" : undefined,
                    fontWeight: r.pending ? undefined : 600,
                  }}
                >
                  {outcomeLabel(r)}
                </td>
                <td>{r.profit_if_followed != null ? fmtPct(r.profit_if_followed) : "—"}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12, maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {r.blockchain_ref || r.external_id}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={9} style={{ padding: 12, color: "#666" }}>
                  No signals yet. Click &quot;Sync from APIs&quot; to fetch Polymarket and Kalshi markets.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

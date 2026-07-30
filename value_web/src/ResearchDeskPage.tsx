import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

type TrendItem = { text?: string; confidence?: number; symbol?: string };
type MemoryStructured = {
  strongest_trends?: TrendItem[];
  recent_trends?: TrendItem[];
  suggestions_log?: TrendItem[];
  version?: number;
};

type SignalInsightBlock = {
  observations?: TrendItem[];
  buying_opportunities?: TrendItem[];
};

type MemoryRow = {
  id: number;
  ts_utc: string;
  horizon_months?: number | null;
  structured?: MemoryStructured | null;
  text?: string;
  model?: string | null;
  signal_insights?: {
    value_analyst?: SignalInsightBlock;
    momentum_fundamentals?: SignalInsightBlock;
  } | null;
};

type RecommendationRow = {
  id: number;
  ts_utc: string;
  symbol: string;
  confidence?: number | null;
  forecast_pct?: number | null;
  rationale?: string | null;
  suggestion_ts_utc?: string | null;
  entry_window_start_utc?: string | null;
  entry_window_end_utc?: string | null;
  execute_review_utc?: string | null;
  plan?: unknown;
  model?: string | null;
};

type Overview = {
  counts?: { memories?: number; recommendations?: number };
  latest_memory?: MemoryRow | null;
  memories?: MemoryRow[];
  recommendations?: RecommendationRow[];
};

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  return String(ts).replace("T", " ").replace("+00:00", " UTC").slice(0, 19);
}

function confColor(c: number | null | undefined, scale: "10" | "1" = "10"): string {
  if (c == null || Number.isNaN(Number(c))) return "#718096";
  const n = scale === "10" ? Number(c) / 10 : Number(c);
  if (n >= 0.75) return "#276749";
  if (n >= 0.5) return "#b7791f";
  return "#c53030";
}

function TrendList({ title, items }: { title: string; items: TrendItem[] }) {
  if (!items.length) {
    return (
      <div style={{ marginBottom: 16 }}>
        <h3 style={{ margin: "0 0 8px", fontSize: 15 }}>{title}</h3>
        <p style={{ color: "#a0aec0", fontSize: 13, margin: 0 }}>None yet.</p>
      </div>
    );
  }
  return (
    <div style={{ marginBottom: 16 }}>
      <h3 style={{ margin: "0 0 8px", fontSize: 15 }}>{title}</h3>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        {items.map((t, i) => (
          <li
            key={`${title}-${i}`}
            style={{
              border: "1px solid #e2e8f0",
              borderRadius: 8,
              padding: "10px 12px",
              background: "#fff",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "baseline" }}>
              <div style={{ fontSize: 14, lineHeight: 1.4, color: "#2d3748" }}>
                {t.symbol ? <strong>{t.symbol}: </strong> : null}
                {t.text || "—"}
              </div>
              <span
                style={{
                  flexShrink: 0,
                  fontSize: 12,
                  fontWeight: 700,
                  color: confColor(t.confidence, "10"),
                }}
              >
                {t.confidence != null ? `${t.confidence}/10` : "—"}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function InsightSection({ title, block }: { title: string; block?: SignalInsightBlock | null }) {
  const obs = block?.observations || [];
  const buys = block?.buying_opportunities || [];
  return (
    <div style={{ marginBottom: 18 }}>
      <h3 style={{ margin: "0 0 8px", fontSize: 15 }}>{title}</h3>
      <TrendList title="Observations" items={obs} />
      <TrendList title="Buying opportunities" items={buys} />
    </div>
  );
}

export function ResearchDeskPage({ apiBase }: { apiBase: string }) {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedMemId, setSelectedMemId] = useState<number | null>(null);
  const [symFilter, setSymFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ memory_limit: "30", rec_limit: "80" });
      if (symFilter.trim()) params.set("symbol", symFilter.trim().toUpperCase());
      const r = await fetch(`${apiBase}/agent/research/overview?${params}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const raw = await r.text();
      let j: Overview;
      try {
        j = JSON.parse(raw) as Overview;
      } catch {
        throw new Error(
          "Research API returned non-JSON (is /agent/ proxied by nginx?). Hard-refresh after deploy."
        );
      }
      setData(j);
      if (j.latest_memory?.id != null) setSelectedMemId(j.latest_memory.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [apiBase, symFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const selectedMemory = useMemo(() => {
    const rows = data?.memories || [];
    if (selectedMemId == null) return data?.latest_memory || rows[0] || null;
    return rows.find((m) => m.id === selectedMemId) || data?.latest_memory || null;
  }, [data, selectedMemId]);

  const structured = selectedMemory?.structured || {};
  const strongest = structured.strongest_trends || [];
  const recent = structured.recent_trends || [];
  const sugLog = structured.suggestions_log || [];
  const insights = selectedMemory?.signal_insights || {};
  const recs = data?.recommendations || [];

  return (
    <div style={{ padding: "16px 20px", maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: "0 0 6px", fontSize: 22 }}>Research & memory</h1>
          <p style={{ margin: 0, color: "#4a5568", fontSize: 14, maxWidth: 720 }}>
            Consolidated memory and recommendations from news plus Interesting Stocks table signals
            (value/analyst, momentum/fundamentals). Data-quality notes live on{" "}
            <Link to="/internal-logs">Internal logs</Link>.
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading} style={{ padding: "8px 12px" }}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <div style={{ color: "#c53030", marginTop: 12, fontSize: 14 }}>Error: {error}</div>
      )}

      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, marginTop: 16, fontSize: 13, color: "#4a5568" }}>
        <span>
          Memories in DB: <strong>{data?.counts?.memories ?? "—"}</strong>
        </span>
        <span>
          Recommendations in DB: <strong>{data?.counts?.recommendations ?? "—"}</strong>
        </span>
        <span>
          Showing memory: <strong>{selectedMemory ? fmtTs(selectedMemory.ts_utc) : "—"}</strong>
        </span>
        <span>
          Model: <strong>{selectedMemory?.model || "—"}</strong>
        </span>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(200px, 260px) 1fr",
          gap: 16,
          marginTop: 18,
        }}
        className="research-desk-grid"
      >
        <aside>
          <h2 style={{ fontSize: 14, margin: "0 0 8px" }}>Memory history</h2>
          <div
            style={{
              border: "1px solid #e2e8f0",
              borderRadius: 8,
              maxHeight: 420,
              overflow: "auto",
              background: "#fff",
            }}
          >
            {(data?.memories || []).length === 0 && !loading ? (
              <p style={{ padding: 12, color: "#a0aec0", fontSize: 13 }}>No memory snapshots yet.</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {(data?.memories || []).map((m) => {
                  const active = m.id === selectedMemory?.id;
                  return (
                    <li key={m.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedMemId(m.id)}
                        style={{
                          width: "100%",
                          textAlign: "left",
                          border: "none",
                          borderBottom: "1px solid #edf2f7",
                          background: active ? "#ebf8ff" : "transparent",
                          padding: "10px 12px",
                          cursor: "pointer",
                          fontSize: 13,
                        }}
                      >
                        <div style={{ fontWeight: active ? 700 : 500 }}>{fmtTs(m.ts_utc)}</div>
                        <div style={{ color: "#718096", fontSize: 12 }}>
                          {(m.structured?.strongest_trends || []).length} strong ·{" "}
                          {(m.structured?.recent_trends || []).length} recent
                          {m.model ? ` · ${m.model}` : ""}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </aside>

        <section>
          <h2 style={{ fontSize: 16, margin: "0 0 12px" }}>Memory snapshot</h2>
          {!selectedMemory ? (
            <p style={{ color: "#a0aec0" }}>No memory selected.</p>
          ) : (
            <>
              <p style={{ margin: "0 0 12px", fontSize: 13, color: "#4a5568" }}>
                Model: <code>{selectedMemory.model || "—"}</code>
              </p>
              <InsightSection title="Value / analyst insights" block={insights.value_analyst} />
              <InsightSection
                title="Momentum / fundamentals insights"
                block={insights.momentum_fundamentals}
              />
              <TrendList title="Strongest trends" items={strongest} />
              <TrendList title="Recent trends" items={recent} />
              <TrendList title="Suggestions log" items={sugLog} />
            </>
          )}
        </section>
      </div>

      <section style={{ marginTop: 28 }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", marginBottom: 10 }}>
          <h2 style={{ fontSize: 16, margin: 0 }}>Research recommendations</h2>
          <input
            value={symFilter}
            onChange={(e) => setSymFilter(e.target.value)}
            placeholder="Filter symbol"
            style={{ padding: "6px 8px", borderRadius: 6, border: "1px solid #cbd5e0", width: 120 }}
            onKeyDown={(e) => e.key === "Enter" && void load()}
          />
          <button type="button" onClick={() => void load()}>
            Apply
          </button>
        </div>
        <div style={{ overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 800 }}>
            <thead>
              <tr style={{ background: "#edf2f7", textAlign: "left" }}>
                <th style={{ padding: 8 }}>When</th>
                <th style={{ padding: 8 }}>Model</th>
                <th style={{ padding: 8 }}>Symbol</th>
                <th style={{ padding: 8 }}>Conf</th>
                <th style={{ padding: 8 }}>Forecast</th>
                <th style={{ padding: 8 }}>Entry window</th>
                <th style={{ padding: 8 }}>Review</th>
                <th style={{ padding: 8 }}>Rationale</th>
                <th style={{ padding: 8 }}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {recs.length === 0 && !loading ? (
                <tr>
                  <td colSpan={9} style={{ padding: 12, color: "#a0aec0" }}>
                    No recommendations yet.
                  </td>
                </tr>
              ) : (
                recs.map((r) => (
                  <tr key={r.id} style={{ borderBottom: "1px solid #e2e8f0", verticalAlign: "top" }}>
                    <td style={{ padding: 8, whiteSpace: "nowrap" }}>{fmtTs(r.ts_utc)}</td>
                    <td style={{ padding: 8, fontSize: 11, maxWidth: 140, wordBreak: "break-all" }}>
                      {r.model || "—"}
                    </td>
                    <td style={{ padding: 8, fontWeight: 700 }}>{r.symbol}</td>
                    <td style={{ padding: 8, color: confColor(r.confidence, "1"), fontWeight: 600 }}>
                      {r.confidence != null ? Number(r.confidence).toFixed(2) : "—"}
                    </td>
                    <td style={{ padding: 8 }}>
                      {r.forecast_pct != null ? `${Number(r.forecast_pct).toFixed(1)}%` : "—"}
                    </td>
                    <td style={{ padding: 8, fontSize: 12 }}>
                      {fmtTs(r.entry_window_start_utc)}
                      <br />→ {fmtTs(r.entry_window_end_utc)}
                    </td>
                    <td style={{ padding: 8, fontSize: 12 }}>{fmtTs(r.execute_review_utc)}</td>
                    <td style={{ padding: 8, maxWidth: 280, color: "#4a5568" }}>
                      {r.rationale
                        ? String(r.rationale).length > 160
                          ? `${String(r.rationale).slice(0, 157)}…`
                          : r.rationale
                        : "—"}
                    </td>
                    <td style={{ padding: 8 }}>
                      <Link to={`/stocks/${encodeURIComponent(r.symbol)}`}>View</Link>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <style>{`
        @media (max-width: 800px) {
          .research-desk-grid {
            grid-template-columns: 1fr !important;
          }
        }
      `}</style>
    </div>
  );
}

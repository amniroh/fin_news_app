import { useCallback, useEffect, useState } from "react";

type DataQualityIssue = {
  symbol?: string;
  gap?: string;
  text?: string;
  severity?: string;
};

type InternalLogRow = {
  id: number;
  ts_utc: string;
  category: string;
  model?: string | null;
  source_run_ts_utc?: string | null;
  issues?: DataQualityIssue[];
};

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  return String(ts).replace("T", " ").replace("+00:00", " UTC").slice(0, 19);
}

function severityColor(s?: string): string {
  const v = String(s || "").toLowerCase();
  if (v === "high") return "#c53030";
  if (v === "medium") return "#b7791f";
  if (v === "low") return "#2b6cb0";
  return "#718096";
}

export function InternalLogsPage({ apiBase }: { apiBase: string }) {
  const [rows, setRows] = useState<InternalLogRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`${apiBase}/agent/research/internal-logs?limit=100&category=data_quality`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const raw = await r.text();
      let j: { rows?: InternalLogRow[]; total?: number };
      try {
        j = JSON.parse(raw) as { rows?: InternalLogRow[]; total?: number };
      } catch {
        throw new Error("Internal logs API returned non-JSON (check nginx /agent/ proxy).");
      }
      setRows(j.rows || []);
      setTotal(j.total ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void load();
  }, [load]);

  const flatIssues = rows.flatMap((row) =>
    (row.issues || []).map((issue, idx) => ({
      key: `${row.id}-${idx}`,
      logId: row.id,
      ts_utc: row.ts_utc,
      model: row.model,
      source_run_ts_utc: row.source_run_ts_utc,
      ...issue,
    }))
  );

  return (
    <div style={{ padding: "16px 20px", maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: "0 0 6px", fontSize: 22 }}>Internal logs</h1>
          <p style={{ margin: 0, color: "#4a5568", fontSize: 14, maxWidth: 720 }}>
            Data-quality and coverage-gap notes from research runs. Use these to prioritize backfills and
            pipeline fixes — they are not investment theses.
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading} style={{ padding: "8px 12px" }}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <div style={{ color: "#c53030", marginTop: 12, fontSize: 14 }}>Error: {error}</div>
      )}

      <p style={{ marginTop: 14, fontSize: 13, color: "#4a5568" }}>
        Log batches: <strong>{rows.length}</strong>
        {total != null ? (
          <>
            {" "}
            (total rows in DB: <strong>{total}</strong>)
          </>
        ) : null}
        {" · "}
        Flattened issues: <strong>{flatIssues.length}</strong>
      </p>

      <div style={{ overflowX: "auto", WebkitOverflowScrolling: "touch", marginTop: 12 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 860 }}>
          <thead>
            <tr style={{ background: "#edf2f7", textAlign: "left" }}>
              <th style={{ padding: 8 }}>Logged</th>
              <th style={{ padding: 8 }}>Model</th>
              <th style={{ padding: 8 }}>Symbol</th>
              <th style={{ padding: 8 }}>Gap</th>
              <th style={{ padding: 8 }}>Severity</th>
              <th style={{ padding: 8 }}>Issue</th>
            </tr>
          </thead>
          <tbody>
            {flatIssues.length === 0 && !loading ? (
              <tr>
                <td colSpan={6} style={{ padding: 12, color: "#a0aec0" }}>
                  No data-quality logs yet. They appear after the next research run that reports coverage gaps.
                </td>
              </tr>
            ) : (
              flatIssues.map((issue) => (
                <tr key={issue.key} style={{ borderBottom: "1px solid #e2e8f0", verticalAlign: "top" }}>
                  <td style={{ padding: 8, whiteSpace: "nowrap" }}>{fmtTs(issue.ts_utc)}</td>
                  <td style={{ padding: 8, fontSize: 11, maxWidth: 140, wordBreak: "break-all" }}>
                    {issue.model || "—"}
                  </td>
                  <td style={{ padding: 8, fontWeight: 600 }}>{issue.symbol || "—"}</td>
                  <td style={{ padding: 8 }}>
                    <code style={{ fontSize: 12 }}>{issue.gap || "—"}</code>
                  </td>
                  <td style={{ padding: 8, fontWeight: 700, color: severityColor(issue.severity) }}>
                    {issue.severity || "—"}
                  </td>
                  <td style={{ padding: 8, color: "#2d3748", maxWidth: 480 }}>{issue.text || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

type CoverageFlags = {
  prices: boolean;
  fundamentals: boolean;
  daily_metrics: boolean;
  news: boolean;
  analyst_ratings: boolean;
};

type LatestAnalyst = {
  asof_date?: string;
  recommendation_key?: string | null;
  recommendation_mean?: number | null;
  target_mean?: number | null;
};

type ValuePillarScores = {
  competitive_edge?: number | null;
  management_competence?: number | null;
  financial_fortress?: number | null;
  pricing_power?: number | null;
  understandability?: number | null;
  valuation?: number | null;
};

type LatestValueTrading = {
  produced_ts_utc?: string;
  total_score?: number | null;
  investment_name?: string | null;
  model?: string | null;
  overall_summary?: string | null;
  pillar_scores?: ValuePillarScores | null;
};

function valueScoreColor(score: number): string {
  if (score >= 24) return "#276749";
  if (score >= 18) return "#48bb78";
  if (score >= 12) return "#d69e2e";
  return "#c53030";
}

function formatValueTradingDate(ts?: string | null): string {
  if (!ts) return "";
  return String(ts).slice(0, 10);
}

function valueTradingTooltip(vt: LatestValueTrading): string {
  const lines = [
    vt.investment_name ? String(vt.investment_name) : "",
    vt.produced_ts_utc ? `Assessed ${String(vt.produced_ts_utc).slice(0, 19).replace("T", " ")} UTC` : "",
    vt.model ? `Model: ${vt.model}` : "",
  ];
  const ps = vt.pillar_scores;
  if (ps) {
    lines.push(
      `Moat ${ps.competitive_edge ?? "—"}/5 · Mgmt ${ps.management_competence ?? "—"}/5 · ` +
        `Fortress ${ps.financial_fortress ?? "—"}/5 · Pricing ${ps.pricing_power ?? "—"}/5 · ` +
        `Understand ${ps.understandability ?? "—"}/5 · Valuation ${ps.valuation ?? "—"}/5`
    );
  }
  if (vt.overall_summary) {
    const s = String(vt.overall_summary);
    lines.push(s.length > 220 ? `${s.slice(0, 217)}…` : s);
  }
  return lines.filter(Boolean).join("\n");
}

type InterestingRow = {
  symbol: string;
  universe_priority: number;
  name?: string | null;
  gaps: string[];
  needs_backfill: boolean;
  coverage: CoverageFlags;
  counts: Record<string, number>;
  latest_analyst?: LatestAnalyst | null;
  latest_value_trading?: LatestValueTrading | null;
};

function GapBadges({ gaps }: { gaps: string[] }) {
  if (!gaps.length) {
    return <span style={{ color: "#276749", fontSize: 12 }}>complete</span>;
  }
  return (
    <span style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
      {gaps.map((g) => (
        <span
          key={g}
          style={{
            fontSize: 11,
            padding: "2px 6px",
            borderRadius: 4,
            background: "#fef3c7",
            color: "#92400e",
          }}
        >
          {g}
        </span>
      ))}
    </span>
  );
}

export function InterestingStocksPage({ apiBase }: { apiBase: string }) {
  const [rows, setRows] = useState<InterestingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newSymbol, setNewSymbol] = useState("");
  const [busy, setBusy] = useState(false);
  const [filterP0, setFilterP0] = useState(false);
  const [filterGaps, setFilterGaps] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`${apiBase}/value/interesting/stocks?seed=true`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setRows(data.rows || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    load();
  }, [load]);

  const addSymbol = async () => {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym) return;
    setBusy(true);
    try {
      const r = await fetch(`${apiBase}/value/interesting/stocks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setNewSymbol("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const displayed = rows.filter((row) => {
    if (filterP0 && row.universe_priority !== 0) return false;
    if (filterGaps && !row.needs_backfill) return false;
    return true;
  });

  const gapCounts = rows.reduce<Record<string, number>>((acc, row) => {
    for (const g of row.gaps || []) {
      acc[g] = (acc[g] || 0) + 1;
    }
    return acc;
  }, {});

  return (
    <div style={{ padding: "16px 24px", maxWidth: 1200, margin: "0 auto" }}>
      <h1 style={{ margin: "0 0 8px", fontSize: 22 }}>Interesting stocks</h1>
      <p style={{ color: "#4a5568", marginTop: 0, fontSize: 14 }}>
        Universe tickers from <code>top1000_investments_prioritised.json</code>. Coverage shows what is
        missing over the last ~2 years (prices, fundamentals, news, analyst ratings). The{" "}
        <strong>Value (6-pillar)</strong> column shows the latest intrinsic-value assessment from the
        database (produced by <code>backend/value_trading_agent_run.py</code>). Gap backfills run via{" "}
        <code>backend/interesting_stocks_daily_backfill.py</code>, not from this UI.
      </p>

      {!loading && rows.length > 0 && (
        <p style={{ fontSize: 13, color: "#4a5568", marginBottom: 12 }}>
          {rows.filter((r) => r.needs_backfill).length} of {rows.length} tickers have gaps
          {Object.keys(gapCounts).length > 0 && (
            <>
              {" "}
              (
              {Object.entries(gapCounts)
                .map(([k, v]) => `${k}: ${v}`)
                .join(", ")}
              )
            </>
          )}
        </p>
      )}

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 12,
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <input
          value={newSymbol}
          onChange={(e) => setNewSymbol(e.target.value)}
          placeholder="Add ticker e.g. AAPL"
          style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid #cbd5e0", width: 140 }}
          onKeyDown={(e) => e.key === "Enter" && addSymbol()}
        />
        <button type="button" onClick={addSymbol} disabled={busy}>
          Add to list
        </button>
        <button type="button" onClick={load} disabled={loading}>
          Refresh coverage
        </button>
        <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={filterP0} onChange={(e) => setFilterP0(e.target.checked)} />
          Priority 0 only
        </label>
        <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={filterGaps}
            onChange={(e) => setFilterGaps(e.target.checked)}
          />
          Needs backfill only
        </label>
      </div>

      {error && (
        <div style={{ color: "#c53030", marginBottom: 12, fontSize: 14 }}>Error: {error}</div>
      )}

      {loading ? (
        <p>Loading…</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: "#edf2f7", textAlign: "left" }}>
                <th style={{ padding: 8 }}>Symbol</th>
                <th style={{ padding: 8 }}>Priority</th>
                <th style={{ padding: 8 }}>Coverage gaps</th>
                <th style={{ padding: 8 }}>Analyst</th>
                <th style={{ padding: 8 }}>Value (6-pillar)</th>
                <th style={{ padding: 8 }}>Assessed</th>
                <th style={{ padding: 8 }}>Snapshots</th>
                <th style={{ padding: 8 }}>News</th>
                <th style={{ padding: 8 }}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {displayed.map((row) => (
                <tr key={row.symbol} style={{ borderBottom: "1px solid #e2e8f0" }}>
                  <td style={{ padding: 8, fontWeight: 600 }}>{row.symbol}</td>
                  <td style={{ padding: 8 }}>{row.universe_priority}</td>
                  <td style={{ padding: 8 }}>
                    <GapBadges gaps={row.gaps || []} />
                  </td>
                  <td style={{ padding: 8, fontSize: 12 }}>
                    {row.latest_analyst?.recommendation_key ? (
                      <span title={row.latest_analyst.asof_date}>
                        {row.latest_analyst.recommendation_key}
                      </span>
                    ) : row.coverage?.analyst_ratings ? (
                      <span style={{ color: "#718096" }}>—</span>
                    ) : (
                      <span style={{ color: "#c05621" }}>missing</span>
                    )}
                  </td>
                  <td style={{ padding: 8, fontSize: 12 }}>
                    {row.latest_value_trading?.total_score != null ? (
                      <span
                        title={valueTradingTooltip(row.latest_value_trading)}
                        style={{
                          fontWeight: 600,
                          color: valueScoreColor(Number(row.latest_value_trading.total_score)),
                        }}
                      >
                        {row.latest_value_trading.total_score}/30
                      </span>
                    ) : (
                      <span style={{ color: "#718096" }}>—</span>
                    )}
                  </td>
                  <td style={{ padding: 8, fontSize: 12, color: "#4a5568" }}>
                    {row.latest_value_trading?.produced_ts_utc
                      ? formatValueTradingDate(row.latest_value_trading.produced_ts_utc)
                      : "—"}
                  </td>
                  <td style={{ padding: 8 }}>{row.counts?.analyst_snapshots ?? 0}</td>
                  <td style={{ padding: 8 }}>{row.counts?.linked_news ?? "—"}</td>
                  <td style={{ padding: 8 }}>
                    <Link to={`/stocks/${encodeURIComponent(row.symbol)}`}>View</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p style={{ color: "#718096", fontSize: 12, marginTop: 8 }}>
            Showing {displayed.length} of {rows.length} tickers
          </p>
        </div>
      )}
    </div>
  );
}

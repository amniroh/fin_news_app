import { useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";

type CoverageFlags = {
  prices: boolean;
  fundamentals: boolean;
  daily_metrics: boolean;
  news: boolean;
  analyst_ratings: boolean;
};

type LatestAnalyst = {
  asof_date?: string | null;
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
  produced_ts_utc?: string | null;
  total_score?: number | null;
  investment_name?: string | null;
  model?: string | null;
  overall_summary?: string | null;
  pillar_scores?: ValuePillarScores | null;
};

type NewsItem = {
  id?: number | string;
  ts_utc?: string | null;
  source_name?: string | null;
  title?: string | null;
  url?: string | null;
  snippet?: string | null;
};

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
  recent_news?: NewsItem[];
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

function fmtNewsDay(ts?: string | null): string {
  if (!ts) return "";
  const d = String(ts).slice(0, 10);
  return d;
}

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

function NewsCell({ symbol, items }: { symbol: string; items: NewsItem[] }) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const preview = items.slice(0, 5);
  const extra = Math.max(0, items.length - 5);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  if (!items.length) {
    return <span style={{ color: "#a0aec0", fontSize: 12 }}>—</span>;
  }

  return (
    <div style={{ minWidth: 180, maxWidth: 320 }}>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }}>
        {preview.map((n, idx) => {
          const title = String(n.title || "Untitled").trim();
          const day = fmtNewsDay(n.ts_utc);
          const inner = (
            <>
              {day ? <span style={{ color: "#718096", marginRight: 4 }}>{day}</span> : null}
              <span>{title.length > 72 ? `${title.slice(0, 69)}…` : title}</span>
            </>
          );
          return (
            <li key={String(n.id ?? idx)} style={{ fontSize: 12, lineHeight: 1.35, color: "#2d3748" }}>
              {n.url ? (
                <a href={n.url} target="_blank" rel="noreferrer" style={{ color: "#2b6cb0", textDecoration: "none" }}>
                  {inner}
                </a>
              ) : (
                inner
              )}
            </li>
          );
        })}
      </ul>
      {(extra > 0 || items.length > 0) && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          style={{
            marginTop: 6,
            fontSize: 12,
            padding: "4px 8px",
            borderRadius: 6,
            border: "1px solid #cbd5e0",
            background: "#f7fafc",
            cursor: "pointer",
          }}
        >
          {extra > 0 ? `More (+${extra})` : "More"}
        </button>
      )}

      {open && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          onClick={() => setOpen(false)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 1000,
            background: "rgba(15, 23, 42, 0.45)",
            display: "flex",
            alignItems: "flex-end",
            justifyContent: "center",
            padding: 12,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "min(560px, 100%)",
              maxHeight: "min(78vh, 640px)",
              overflow: "auto",
              background: "#fff",
              borderRadius: "16px 16px 12px 12px",
              boxShadow: "0 12px 40px rgba(0,0,0,0.2)",
              padding: "14px 16px 18px",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, marginBottom: 10 }}>
              <h3 id={titleId} style={{ margin: 0, fontSize: 16 }}>
                {symbol} news ({items.length})
              </h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                style={{
                  border: "1px solid #cbd5e0",
                  background: "#edf2f7",
                  borderRadius: 8,
                  padding: "6px 10px",
                  fontSize: 13,
                  cursor: "pointer",
                }}
              >
                Close
              </button>
            </div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
              {items.map((n, idx) => (
                <li
                  key={String(n.id ?? idx)}
                  style={{
                    borderBottom: "1px solid #edf2f7",
                    paddingBottom: 10,
                  }}
                >
                  <div style={{ fontSize: 11, color: "#718096", marginBottom: 2 }}>
                    {fmtNewsDay(n.ts_utc) || "—"}
                    {n.source_name ? ` · ${n.source_name}` : ""}
                  </div>
                  {n.url ? (
                    <a
                      href={n.url}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "#2b6cb0", fontWeight: 600, fontSize: 14, textDecoration: "none" }}
                    >
                      {n.title || "Untitled"}
                    </a>
                  ) : (
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{n.title || "Untitled"}</div>
                  )}
                  {n.snippet ? (
                    <p style={{ margin: "6px 0 0", fontSize: 13, color: "#4a5568", lineHeight: 1.4 }}>
                      {String(n.snippet).length > 280 ? `${String(n.snippet).slice(0, 277)}…` : n.snippet}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
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
    <div style={{ padding: "16px 24px", maxWidth: 1400, margin: "0 auto" }}>
      <h1 style={{ margin: "0 0 8px", fontSize: 22 }}>Interesting stocks</h1>
      <p style={{ color: "#4a5568", marginTop: 0, fontSize: 14 }}>
        Universe tickers from <code>top1000_investments_prioritised.json</code>. Coverage shows what is
        missing over the last ~2 years (prices, fundamentals, news, analyst ratings). The{" "}
        <strong>Value (6-pillar)</strong> column shows the latest intrinsic-value assessment from the
        database. <strong>News</strong> shows up to 5 recent linked headlines; use <strong>More</strong> for
        additional items. Gap backfills run via <code>backend/interesting_stocks_daily_backfill.py</code>.
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
        <div style={{ overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 900 }}>
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
                <tr key={row.symbol} style={{ borderBottom: "1px solid #e2e8f0", verticalAlign: "top" }}>
                  <td style={{ padding: 8, fontWeight: 600 }}>{row.symbol}</td>
                  <td style={{ padding: 8 }}>{row.universe_priority}</td>
                  <td style={{ padding: 8 }}>
                    <GapBadges gaps={row.gaps || []} />
                  </td>
                  <td style={{ padding: 8, fontSize: 12 }}>
                    {row.latest_analyst?.recommendation_key ? (
                      <span title={row.latest_analyst.asof_date || undefined}>
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
                  <td style={{ padding: 8 }}>
                    <NewsCell symbol={row.symbol} items={row.recent_news || []} />
                  </td>
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

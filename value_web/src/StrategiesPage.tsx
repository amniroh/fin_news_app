import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  CHART_BASELINE_DASHARRAYS,
  CHART_STROKE_DASHARRAYS,
  LegendSeriesGlyph,
  sparseSeriesDot,
} from "./chartSeriesStyles";

type StrategyMeta = { label: string; description: string; color: string };

type SnapshotListItem = {
  strategy: string;
  cadence: string | null;
  ts_utc: string | null;
  top_symbols: string[];
  n_top: number;
  snapshot_path: string;
  label?: string;
};

type MetricsFileItem = {
  strategy: string;
  cadence: string;
  trained_at: string | null;
  test_total_return: number | null;
  baseline_test_total_return: number | null;
  test_ic: number | null;
  val_ic: number | null;
  metrics_path: string;
  label?: string;
};

type ListResponse = {
  strategy_meta: Record<string, StrategyMeta>;
  snapshots: SnapshotListItem[];
  metrics_files: MetricsFileItem[];
};

type EquityPoint = { date: string; equity: number };

type MetricsBlock = {
  total_return?: number | null;
  cagr?: number | null;
  ann_vol?: number | null;
  sharpe?: number | null;
  max_drawdown?: number | null;
  rolling_1y_median_return?: number | null;
  rolling_1y_hit_rate?: number | null;
  ic?: number | null;
  turnover_avg?: number | null;
};

type CompareVariant = {
  strategy: string;
  label: string;
  color: string;
  trained_at: string | null;
  train_metrics?: MetricsBlock;
  val_metrics: MetricsBlock;
  test_metrics: MetricsBlock;
  train_curve?: EquityPoint[];
  val_curve: EquityPoint[];
  test_curve: EquityPoint[];
  current_top: Record<string, unknown>[];
};

type CompareResponse = {
  cadence: string;
  variants: CompareVariant[];
  baselines?: {
    label: string;
    color: string;
    train_curve?: EquityPoint[];
    val_curve: EquityPoint[];
    test_curve: EquityPoint[];
    train_metrics?: MetricsBlock | null;
    val_metrics: MetricsBlock | null;
    test_metrics: MetricsBlock | null;
  }[];
  baseline: {
    label: string;
    color: string;
    train_curve?: EquityPoint[];
    val_curve: EquityPoint[];
    test_curve: EquityPoint[];
    train_metrics?: MetricsBlock | null;
    val_metrics: MetricsBlock | null;
    test_metrics: MetricsBlock | null;
  };
};

type SnapshotResponse = {
  strategy: string;
  cadence: string | null;
  label: string;
  color: string | null;
  snapshot: Record<string, unknown>;
  ml_metrics: Record<string, unknown> | null;
};

type WalkforwardCiAgg = {
  mean: number;
  ci_low: number;
  ci_high: number;
  n: number;
};

type WalkforwardFold = {
  test_year: number;
  val_year?: number | null;
  strategies?: Record<
    string,
    {
      test_metrics?: MetricsBlock;
      baseline_test_metrics?: MetricsBlock;
    }
  >;
};

type WalkforwardPayload = {
  cadence: string;
  top_n: number;
  benchmark: string;
  years_history: number;
  n_folds_requested: number;
  n_folds_completed: number;
  min_train_rows: number;
  transaction_cost_model?: {
    slippage_one_way?: number;
    commission_one_way_rate?: number;
  };
  folds?: WalkforwardFold[];
  aggregate: Record<string, Record<string, WalkforwardCiAgg>>;
};

const FMT_PCT = (v: number | null | undefined, digits = 2): string =>
  v == null || Number.isNaN(v) ? "—" : `${(Number(v) * 100).toFixed(digits)}%`;
const FMT_NUM = (v: number | null | undefined, digits = 3): string =>
  v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(digits);
const FMT_DATE = (v: string | null | undefined): string => (v ? v.slice(0, 10) : "—");

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** One row of merged Recharts data: date + strategy/baseline equity multipliers. */
type ChartDatum = { date: string; [key: string]: number | string | undefined };

type TopPickRow = {
  symbol: string;
  predicted_return?: number;
  rsi_mean?: number;
  weight?: number;
  composite_rank?: number;
  rank?: number;
};

type RiskMatchSnapshot = {
  benchmark?: string;
  vol_portfolio_equal_weight_annual?: number;
  vol_benchmark_annual?: number;
  suggested_equity_fraction_equal_weight_vs_cash?: number;
  inverse_variance_weights?: Record<string, number>;
};

const CADENCES: { id: "daily" | "weekly" | "monthly"; label: string }[] = [
  { id: "daily", label: "Daily" },
  { id: "weekly", label: "Weekly" },
  { id: "monthly", label: "Monthly" },
];

const METRIC_COLS: { key: keyof MetricsBlock; label: string; fmt: (v: number | null | undefined) => string }[] = [
  { key: "total_return", label: "Total return", fmt: (v) => FMT_PCT(v) },
  { key: "cagr", label: "CAGR", fmt: (v) => FMT_PCT(v) },
  { key: "ann_vol", label: "Ann. vol", fmt: (v) => FMT_PCT(v) },
  { key: "sharpe", label: "Sharpe", fmt: (v) => FMT_NUM(v, 2) },
  { key: "max_drawdown", label: "Max DD", fmt: (v) => FMT_PCT(v) },
  { key: "rolling_1y_median_return", label: "Rolling 1y median", fmt: (v) => FMT_PCT(v) },
  { key: "rolling_1y_hit_rate", label: "Rolling 1y hit rate", fmt: (v) => FMT_PCT(v, 1) },
  { key: "ic", label: "Cross-section IC", fmt: (v) => FMT_NUM(v, 4) },
  { key: "turnover_avg", label: "Turnover", fmt: (v) => FMT_PCT(v, 1) },
];

const WF_SID_ORDER = ["ml_equal", "ml_pred_weighted"];

function walkforwardStrategyLabel(sid: string, strategyMeta: Record<string, StrategyMeta>): string {
  if (sid === "ml_equal") return strategyMeta.ml?.label || "ML top-N (equal-weight)";
  return strategyMeta[sid]?.label || sid;
}

function walkforwardStrategyColor(sid: string, strategyMeta: Record<string, StrategyMeta>): string {
  if (sid === "ml_equal") return strategyMeta.ml?.color || "#38a169";
  return strategyMeta[sid]?.color || "#4a5568";
}

function walkforwardSidSort(a: string, b: string): number {
  const ia = WF_SID_ORDER.indexOf(a);
  const ib = WF_SID_ORDER.indexOf(b);
  const ra = ia === -1 ? 100 + WF_SID_ORDER.length : ia;
  const rb = ib === -1 ? 100 + WF_SID_ORDER.length : ib;
  if (ra !== rb) return ra - rb;
  return a.localeCompare(b);
}

function formatWalkforwardCi(
  col: (typeof METRIC_COLS)[number],
  agg: WalkforwardCiAgg | undefined,
): string {
  if (!agg || !Number.isFinite(agg.mean)) return "—";
  const m = col.fmt(agg.mean);
  const lo = col.fmt(agg.ci_low);
  const hi = col.fmt(agg.ci_high);
  return `${m} [${lo}, ${hi}]`;
}

function ColorSwatch({ color }: { color: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 10,
        height: 10,
        background: color,
        borderRadius: 2,
        marginRight: 6,
        verticalAlign: "middle",
      }}
    />
  );
}

type TooltipPayloadEntry = {
  name?: string;
  value?: number | string;
  dataKey?: string | number;
  color?: string;
};

function CompareEquityTooltipView({
  active,
  label,
  payload,
}: {
  active?: boolean;
  label?: string | number;
  payload?: TooltipPayloadEntry[];
}) {
  if (!active || !payload?.length) return null;
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.97)",
        border: "1px solid #e2e8f0",
        borderRadius: 6,
        padding: "8px 10px",
        fontSize: 12,
        boxShadow: "0 2px 10px rgba(0,0,0,0.08)",
      }}
    >
      <div style={{ marginBottom: 6, color: "#64748b", fontSize: 11 }}>{label != null ? String(label) : ""}</div>
      {payload.map((p, idx) => {
        const k = p.dataKey != null ? String(p.dataKey) : `i-${idx}`;
        return (
          <div key={k} style={{ display: "flex", alignItems: "baseline", gap: 8, marginTop: idx ? 4 : 0 }}>
            <span style={{ fontWeight: 400, color: p.color || "#0f172a" }}>{p.name}</span>
            <span style={{ fontFamily: "ui-monospace, SFMono-Regular, monospace" }}>{Number(p.value).toFixed(3)}</span>
          </div>
        );
      })}
    </div>
  );
}

const legendBtnReset: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  font: "inherit",
  color: "#1e293b",
  background: "transparent",
  border: "none",
  borderRadius: 4,
  padding: "2px 6px",
  margin: 0,
  cursor: "pointer",
};

function CompareEquityLegend({
  variants,
  baseSet,
  hiddenKeys,
  onToggleSeries,
}: {
  variants: CompareVariant[];
  baseSet: { label: string; color: string }[];
  hiddenKeys: ReadonlySet<string>;
  onToggleSeries: (dataKey: string) => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "10px 18px",
        fontSize: 12,
        paddingTop: 10,
        justifyContent: "center",
        lineHeight: 1.35,
      }}
    >
      {variants.map((v, i) => {
        const dk = v.strategy;
        const dash = CHART_STROKE_DASHARRAYS[i % CHART_STROKE_DASHARRAYS.length];
        const hidden = hiddenKeys.has(dk);
        return (
          <button
            key={dk}
            type="button"
            onClick={() => onToggleSeries(dk)}
            aria-pressed={!hidden}
            title={hidden ? "Show series" : "Hide series"}
            style={{
              ...legendBtnReset,
              fontWeight: 400,
              opacity: hidden ? 0.42 : 1,
              textDecoration: hidden ? "line-through" : undefined,
            }}
          >
            <LegendSeriesGlyph color={v.color} shapeIndex={i} strokeDasharray={dash} />
            {v.label}
          </button>
        );
      })}
      {baseSet.map((b, i) => {
        const dk = `__baseline__${i}`;
        const dash = CHART_BASELINE_DASHARRAYS[i % CHART_BASELINE_DASHARRAYS.length];
        const hidden = hiddenKeys.has(dk);
        return (
          <button
            key={`${b.label}-${i}`}
            type="button"
            onClick={() => onToggleSeries(dk)}
            aria-pressed={!hidden}
            title={hidden ? "Show series" : "Hide series"}
            style={{
              ...legendBtnReset,
              fontWeight: 400,
              opacity: hidden ? 0.42 : 1,
              textDecoration: hidden ? "line-through" : undefined,
            }}
          >
            <LegendSeriesGlyph color={b.color} shapeIndex={i + 16} strokeDasharray={dash} />
            {b.label} (baseline)
          </button>
        );
      })}
    </div>
  );
}

function CompareMetricsTable({
  variants,
  baseline,
  baselines,
  split,
}: {
  variants: CompareVariant[];
  baseline: CompareResponse["baseline"];
  baselines?: CompareResponse["baselines"];
  split: "val" | "test";
}) {
  const blockKey: keyof CompareVariant = split === "val" ? "val_metrics" : "test_metrics";
  const baseMetrics: MetricsBlock | null = split === "val" ? baseline.val_metrics : baseline.test_metrics;
  const extra = (baselines || []).filter((b) => b.label !== baseline.label);
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Strategy</th>
            {METRIC_COLS.map((c) => (
              <th
                key={String(c.key)}
                style={{ textAlign: "right", padding: 8, borderBottom: "1px solid #ddd", whiteSpace: "nowrap" }}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {variants.map((v) => (
            <tr key={v.strategy}>
              <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontWeight: 600, color: v.color }}>
                <ColorSwatch color={v.color} /> {v.label}
              </td>
              {METRIC_COLS.map((c) => (
                <td
                  key={String(c.key)}
                  style={{
                    padding: 8,
                    borderBottom: "1px solid #f0f0f0",
                    textAlign: "right",
                    fontFamily: "ui-monospace, SFMono-Regular, monospace",
                  }}
                >
                  {c.fmt((v[blockKey] as MetricsBlock | undefined)?.[c.key])}
                </td>
              ))}
            </tr>
          ))}
          <tr>
            <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontWeight: 600, color: baseline.color }}>
              <ColorSwatch color={baseline.color} /> {baseline.label} (baseline)
            </td>
            {METRIC_COLS.map((c) => (
              <td
                key={String(c.key)}
                style={{
                  padding: 8,
                  borderBottom: "1px solid #f0f0f0",
                  textAlign: "right",
                  fontFamily: "ui-monospace, SFMono-Regular, monospace",
                }}
              >
                {c.fmt((baseMetrics ?? {})[c.key])}
              </td>
            ))}
          </tr>
          {extra.map((b) => {
            const m: MetricsBlock | null = split === "val" ? b.val_metrics : b.test_metrics;
            return (
              <tr key={`${b.label}-${split}`}>
                <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontWeight: 600, color: b.color }}>
                  <ColorSwatch color={b.color} /> {b.label} (baseline)
                </td>
                {METRIC_COLS.map((c) => (
                  <td
                    key={String(c.key)}
                    style={{
                      padding: 8,
                      borderBottom: "1px solid #f0f0f0",
                      textAlign: "right",
                      fontFamily: "ui-monospace, SFMono-Regular, monospace",
                    }}
                  >
                    {c.fmt((m ?? {})[c.key])}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CompareEquityChart({
  title,
  variants,
  baseline,
  baselines,
  split,
  cadence,
}: {
  title: string;
  variants: CompareVariant[];
  baseline: CompareResponse["baseline"];
  baselines?: CompareResponse["baselines"];
  split: "train" | "val" | "test";
  cadence: string;
}) {
  const baseSet = baselines && baselines.length > 0 ? baselines : [baseline];
  const legendFingerprint = useMemo(
    () => `${variants.map((v) => v.strategy).join("|")}:${baseSet.map((b) => b.label).join("|")}`,
    [variants, baseSet],
  );
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    setHiddenKeys(new Set());
  }, [legendFingerprint]);
  const toggleSeries = useCallback((dataKey: string) => {
    setHiddenKeys((prev) => {
      const next = new Set(prev);
      if (next.has(dataKey)) next.delete(dataKey);
      else next.add(dataKey);
      return next;
    });
  }, []);

  const data = useMemo(() => {
    const map = new Map<string, ChartDatum>();
    const addCurve = (key: string, curve: EquityPoint[]) => {
      for (const p of curve || []) {
        const e = map.get(p.date) || { date: p.date };
        e[key] = p.equity;
        map.set(p.date, e);
      }
    };
    for (const v of variants) {
      const raw = split === "train" ? v.train_curve : split === "val" ? v.val_curve : v.test_curve;
      const curve = Array.isArray(raw) ? raw : [];
      addCurve(v.strategy, curve);
    }
    baseSet.forEach((b, i) => {
      const key = `__baseline__${i}`;
      const raw = split === "train" ? b.train_curve : split === "val" ? b.val_curve : b.test_curve;
      addCurve(key, Array.isArray(raw) ? raw : []);
    });
    return Array.from(map.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }, [variants, baseSet, split]);

  if (data.length < 2) {
    if (split === "train") {
      return (
        <div style={{ color: "#64748b", fontSize: 13, lineHeight: 1.55 }}>
          <div style={{ fontWeight: 600, color: "#334155", marginBottom: 8 }}>No training-period equity curves yet</div>
          <div style={{ marginBottom: 8 }}>
            This cadence&apos;s metrics files must include <code>train_curve</code> / <code>baseline_train_curve</code>{" "}
            (from a recent trainer). If validation charts look fine but this is empty, re-run training for{" "}
            <code>{cadence}</code>.
          </div>
          <code style={{ fontSize: 12, display: "block", wordBreak: "break-word" }}>
            python backend/sp500_return_model_train.py --cadence {cadence}
          </code>
        </div>
      );
    }
    return <div style={{ color: "#888" }}>Not enough data for {title}.</div>;
  }
  return (
    <div>
      <div style={{ fontSize: 13, color: "#444", marginBottom: 4, fontWeight: 600 }}>{title}</div>
      <div style={{ width: "100%", height: 280 }}>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 10, right: 24, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={32} />
            <YAxis tick={{ fontSize: 11 }} domain={["auto", "auto"]} />
            <Tooltip
              shared={false}
              content={(props) => (
                <CompareEquityTooltipView
                  active={props.active}
                  label={props.label}
                  payload={props.payload as TooltipPayloadEntry[] | undefined}
                />
              )}
            />
            {variants.map((v, i) => (
              <Line
                key={v.strategy}
                type="monotone"
                dataKey={v.strategy}
                stroke={v.color}
                name={v.label}
                hide={hiddenKeys.has(v.strategy)}
                dot={sparseSeriesDot({ fill: v.color, shapeIndex: i, stride: 26 })}
                strokeWidth={2}
                strokeDasharray={CHART_STROKE_DASHARRAYS[i % CHART_STROKE_DASHARRAYS.length]}
                activeDot={{ r: 5 }}
                connectNulls
              />
            ))}
            {baseSet.map((b, i) => {
              const key = `__baseline__${i}`;
              return (
                <Line
                  key={`${b.label}-${i}`}
                  type="monotone"
                  dataKey={key}
                  stroke={b.color}
                  name={`${b.label} (baseline)`}
                  hide={hiddenKeys.has(key)}
                  dot={sparseSeriesDot({ fill: b.color, shapeIndex: i + 16, stride: 30 })}
                  strokeWidth={1.75}
                  strokeDasharray={CHART_BASELINE_DASHARRAYS[i % CHART_BASELINE_DASHARRAYS.length]}
                  activeDot={{ r: 4 }}
                  connectNulls
                />
              );
            })}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <CompareEquityLegend
        variants={variants}
        baseSet={baseSet}
        hiddenKeys={hiddenKeys}
        onToggleSeries={toggleSeries}
      />
    </div>
  );
}

function WalkForwardPanel({
  apiBase,
  cadence,
  strategyMeta,
}: {
  apiBase: string;
  cadence: "daily" | "weekly" | "monthly";
  strategyMeta: Record<string, StrategyMeta>;
}) {
  const [payload, setPayload] = useState<WalkforwardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setError(null);
      setPayload(null);
      try {
        const r = await fetch(`${apiBase}/strategy/walkforward/${cadence}`);
        if (r.status === 404) {
          let detail = "Walk-forward JSON not found.";
          try {
            const j = (await r.json()) as { detail?: string };
            if (typeof j.detail === "string") detail = j.detail;
          } catch {
            /* ignore */
          }
          if (!cancelled) setError(detail);
          return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as WalkforwardPayload;
        if (!cancelled) setPayload(j);
      } catch (e: unknown) {
        if (!cancelled) setError(errMessage(e) || "Failed to load walk-forward results");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [apiBase, cadence]);

  return (
    <div style={{ marginTop: 28, paddingTop: 20, borderTop: "1px solid #e2e8f0" }}>
      <h3 style={{ margin: "0 0 8px" }}>Yearly walk-forward (out-of-sample)</h3>
      <p style={{ margin: "0 0 14px", fontSize: 13, color: "#555", lineHeight: 1.45 }}>
        When generated, this section loads precomputed folds: each test window is one calendar year, the prior year is
        validation, and all earlier rows train the model. Up to 20 of the most recent eligible years are used (fewer if
        history is shorter or the minimum training length is not met). Table cells show the mean and 95% interval
        (Student&nbsp;t) of each metric <em>across those test years</em> (cross-fold uncertainty). Portfolio returns use
        one-way slippage and commission on rebalance turnover from{" "}
        <code style={{ fontSize: 12 }}>backend/data/transaction_costs_us.json</code> when present.
      </p>
      {loading && <div style={{ fontSize: 13 }}>Loading walk-forward…</div>}
      {error && (
        <div
          style={{
            color: "#744210",
            background: "#fffbeb",
            border: "1px solid #f6e05e",
            borderRadius: 8,
            padding: "10px 12px",
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}
      {payload && (() => {
        const sids = Object.keys(payload.aggregate || {}).sort(walkforwardSidSort);
        const sampleSid = sids[0];
        const agg0 = sampleSid ? payload.aggregate[sampleSid] : undefined;
        const wfCols = METRIC_COLS.filter((c) => {
          const k = `strategy_${String(c.key)}`;
          return agg0?.[k] != null && Number.isFinite(agg0[k]!.mean);
        });
        const slip = payload.transaction_cost_model?.slippage_one_way;
        const comm = payload.transaction_cost_model?.commission_one_way_rate;
        const firstSid = sids[0];
        const baselineBlock = firstSid ? payload.aggregate[firstSid] : undefined;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ fontSize: 13, color: "#444" }}>
              <strong>{payload.n_folds_completed}</strong> of {payload.n_folds_requested} requested folds completed ·
              top_n={payload.top_n} · benchmark={payload.benchmark} · min train rows={payload.min_train_rows} · loaded{" "}
              {payload.years_history}y history
              {slip != null && comm != null && (
                <>
                  {" "}
                  · TC one-way: slippage {(Number(slip) * 100).toFixed(2)}% + commission {(Number(comm) * 100).toFixed(3)}
                  %
                </>
              )}
            </div>
            {wfCols.length === 0 ? (
              <div style={{ color: "#666" }}>No aggregate metrics in walk-forward payload.</div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
                  <thead>
                    <tr>
                      <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Variant</th>
                      {wfCols.map((c) => (
                        <th
                          key={String(c.key)}
                          style={{
                            textAlign: "right",
                            padding: 8,
                            borderBottom: "1px solid #ddd",
                            whiteSpace: "nowrap",
                            maxWidth: 220,
                          }}
                        >
                          {c.label}
                          <div style={{ fontWeight: 400, fontSize: 10, color: "#718096" }}>mean [95% CI]</div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sids.map((sid) => {
                      const color = walkforwardStrategyColor(sid, strategyMeta);
                      const label = walkforwardStrategyLabel(sid, strategyMeta);
                      const row = payload.aggregate[sid] || {};
                      return (
                        <tr key={sid}>
                          <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontWeight: 600, color }}>
                            <ColorSwatch color={color} /> {label}
                          </td>
                          {wfCols.map((c) => (
                            <td
                              key={`${sid}-${String(c.key)}`}
                              style={{
                                padding: 8,
                                borderBottom: "1px solid #f0f0f0",
                                textAlign: "right",
                                fontFamily: "ui-monospace, SFMono-Regular, monospace",
                                fontSize: 12,
                                verticalAlign: "top",
                              }}
                            >
                              {formatWalkforwardCi(c, row[`strategy_${String(c.key)}`])}
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                    <tr>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontWeight: 600, color: "#718096" }}>
                        <ColorSwatch color="#718096" /> {payload.benchmark} (buy-and-hold baseline)
                      </td>
                      {wfCols.map((c) => (
                        <td
                          key={`baseline-${String(c.key)}`}
                          style={{
                            padding: 8,
                            borderBottom: "1px solid #f0f0f0",
                            textAlign: "right",
                            fontFamily: "ui-monospace, SFMono-Regular, monospace",
                            fontSize: 12,
                            verticalAlign: "top",
                          }}
                        >
                          {formatWalkforwardCi(c, baselineBlock?.[`baseline_${String(c.key)}`])}
                        </td>
                      ))}
                    </tr>
                  </tbody>
                </table>
              </div>
            )}
            {payload.folds && payload.folds.length > 0 && (
              <details style={{ fontSize: 13 }}>
                <summary style={{ cursor: "pointer", fontWeight: 600 }}>Per-fold test-year total return</summary>
                <div style={{ marginTop: 10, overflowX: "auto" }}>
                  <table style={{ borderCollapse: "collapse", width: "100%" }}>
                    <thead>
                      <tr>
                        <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>Test year</th>
                        <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>Val year</th>
                        {sids.map((sid) => (
                          <th
                            key={sid}
                            style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd", fontSize: 12 }}
                          >
                            {walkforwardStrategyLabel(sid, strategyMeta)}
                            <div style={{ fontWeight: 400, fontSize: 10, color: "#718096" }}>strategy / {payload.benchmark}</div>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {payload.folds.map((fold) => (
                        <tr key={fold.test_year}>
                          <td style={{ padding: 6, borderBottom: "1px solid #f5f5f5" }}>{fold.test_year}</td>
                          <td style={{ padding: 6, borderBottom: "1px solid #f5f5f5" }}>{fold.val_year ?? "—"}</td>
                          {sids.map((sid) => {
                            const b = fold.strategies?.[sid];
                            const tr = b?.test_metrics?.total_return;
                            const br = b?.baseline_test_metrics?.total_return;
                            return (
                              <td
                                key={`${fold.test_year}-${sid}`}
                                style={{
                                  padding: 6,
                                  borderBottom: "1px solid #f5f5f5",
                                  textAlign: "right",
                                  fontFamily: "ui-monospace, monospace",
                                  fontSize: 12,
                                }}
                              >
                                {FMT_PCT(tr)} / {FMT_PCT(br)}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </div>
        );
      })()}
    </div>
  );
}


function CompareView({
  apiBase,
  cadence,
  strategyMeta,
}: {
  apiBase: string;
  cadence: "daily" | "weekly" | "monthly";
  strategyMeta: Record<string, StrategyMeta>;
}) {
  const [data, setData] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(`${apiBase}/strategy/compare/${cadence}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: CompareResponse = await r.json();
        if (!cancelled) setData(j);
      } catch (e: unknown) {
        if (!cancelled) setError(errMessage(e) || "Failed to load comparison");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [apiBase, cadence]);

  if (loading) return <div>Loading comparison…</div>;
  if (error) return <div style={{ color: "crimson" }}>{error}</div>;
  if (!data) return null;

  if (data.variants.length === 0) {
    return (
      <div style={{ color: "#666", padding: 16, background: "#f7fafc", borderRadius: 8 }}>
        No backtest metrics for cadence <code>{cadence}</code> yet. Train the models with{" "}
        <code>python backend/sp500_return_model_train.py --cadence {cadence}</code>.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "flex-start" }}>
        {data.variants.map((v) => (
          <div
            key={v.strategy}
            style={{
              flex: "1 1 260px",
              minWidth: 240,
              border: `2px solid ${v.color}`,
              padding: "10px 12px",
              borderRadius: 8,
              background: "#fff",
            }}
          >
            <div style={{ fontWeight: 700, color: v.color, marginBottom: 4 }}>
              <ColorSwatch color={v.color} /> {v.label}
            </div>
            <div style={{ fontSize: 12, color: "#555", lineHeight: 1.4 }}>
              {strategyMeta[v.strategy]?.description || ""}
            </div>
            <div style={{ fontSize: 12, color: "#777", marginTop: 6 }}>
              trained {FMT_DATE(v.trained_at)} · test: {FMT_PCT(v.test_metrics?.total_return)} ·{" "}
              SPY {FMT_PCT(data.baseline.test_metrics?.total_return)}
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 16 }}>
        <CompareEquityChart
          title="Training equity curves"
          variants={data.variants}
          baseline={data.baseline}
          baselines={data.baselines}
          split="train"
          cadence={cadence}
        />
        <CompareEquityChart
          title="Validation equity curves"
          variants={data.variants}
          baseline={data.baseline}
          baselines={data.baselines}
          split="val"
          cadence={cadence}
        />
        <CompareEquityChart
          title="Test equity curves"
          variants={data.variants}
          baseline={data.baseline}
          baselines={data.baselines}
          split="test"
          cadence={cadence}
        />
      </div>

      <div>
        <h4 style={{ margin: "0 0 6px" }}>Validation metrics — {cadence}</h4>
        <CompareMetricsTable variants={data.variants} baseline={data.baseline} baselines={data.baselines} split="val" />
      </div>
      <div>
        <h4 style={{ margin: "0 0 6px" }}>Test metrics — {cadence}</h4>
        <CompareMetricsTable variants={data.variants} baseline={data.baseline} baselines={data.baselines} split="test" />
      </div>
    </div>
  );
}

function StrategyDetail({
  apiBase,
  strategy,
  cadence,
  strategyMeta,
}: {
  apiBase: string;
  strategy: string;
  cadence: string;
  strategyMeta: Record<string, StrategyMeta>;
}) {
  const [data, setData] = useState<SnapshotResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setError(null);
      try {
        const r = await fetch(`${apiBase}/strategy/snapshot/${strategy}/${cadence}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        if (!cancelled) setData(j);
      } catch (e: unknown) {
        if (!cancelled) setError(errMessage(e) || "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [apiBase, strategy, cadence]);

  if (loading) return <div>Loading…</div>;
  if (error) return <div style={{ color: "crimson" }}>{error}</div>;
  if (!data) return null;

  const snap = data.snapshot || {};
  const meta = strategyMeta[strategy];
  const color = data.color || meta?.color || "#2b6cb0";
  const topDetail = snap["top_detail"];
  const topSymbols = snap["top_symbols"];
  const top: TopPickRow[] = Array.isArray(topDetail)
    ? (topDetail as TopPickRow[])
    : Array.isArray(topSymbols)
      ? (topSymbols as string[]).map((s) => ({ symbol: s }))
      : [];
  const riskMatch = snap["risk_match"] as RiskMatchSnapshot | undefined;

  const isMl = strategy === "ml" || strategy === "ml_pred_weighted";
  const isRsi = strategy === "rsi_mean";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ background: "#f7f7f9", padding: 12, borderRadius: 8, borderLeft: `4px solid ${color}` }}>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "baseline" }}>
          <h3 style={{ margin: 0, color }}>
            <ColorSwatch color={color} /> {data.label} — {cadence}
          </h3>
          <span style={{ color: "#666", fontSize: 13 }}>
            snapshot @ {FMT_DATE(typeof snap.ts_utc === "string" ? snap.ts_utc : undefined)}
          </span>
        </div>
        {meta && <div style={{ marginTop: 6, fontSize: 13, color: "#555" }}>{meta.description}</div>}
        {Boolean(snap["synthesized"]) && (
          <div
            style={{
              marginTop: 8,
              padding: 8,
              background: "#fffbe6",
              border: "1px solid #f6e05e",
              borderRadius: 6,
              fontSize: 12,
              color: "#744210",
            }}
          >
            {(typeof snap["synthesized_note"] === "string" && snap["synthesized_note"]) ||
              "Showing the strategy's most recent top picks (no evaluator snapshot yet)."}
          </div>
        )}
      </div>

      <div>
        <h4 style={{ margin: "0 0 6px" }}>Current portfolio — top {top.length}</h4>
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>#</th>
                <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>Symbol</th>
                {isMl && (
                  <th style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd" }}>Predicted return</th>
                )}
                {isRsi && (
                  <th style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd" }}>Mean RSI(14, 30d)</th>
                )}
                {!isMl && !isRsi && (
                  <th style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd" }}>Composite rank</th>
                )}
                <th style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd" }}>Weight</th>
                <th style={{ textAlign: "right", padding: 6, borderBottom: "1px solid #ddd" }}>Inv-var weight</th>
              </tr>
            </thead>
            <tbody>
              {top.map((row, i) => {
                const ivw = riskMatch?.inverse_variance_weights?.[row.symbol];
                return (
                  <tr key={row.symbol + i}>
                    <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", color: "#888" }}>{i + 1}</td>
                    <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", fontWeight: 600 }}>{row.symbol}</td>
                    {isMl && (
                      <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", textAlign: "right", fontFamily: "ui-monospace, monospace" }}>
                        {row.predicted_return != null ? FMT_PCT(row.predicted_return, 2) : "—"}
                      </td>
                    )}
                    {isRsi && (
                      <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", textAlign: "right", fontFamily: "ui-monospace, monospace" }}>
                        {row.rsi_mean != null ? FMT_NUM(row.rsi_mean, 1) : "—"}
                      </td>
                    )}
                    {!isMl && !isRsi && (
                      <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", textAlign: "right", fontFamily: "ui-monospace, monospace" }}>
                        {row.composite_rank != null ? FMT_NUM(row.composite_rank, 1) : "—"}
                      </td>
                    )}
                    <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", textAlign: "right", fontFamily: "ui-monospace, monospace" }}>
                      {row.weight != null ? FMT_PCT(row.weight, 2) : "—"}
                    </td>
                    <td style={{ padding: 6, borderBottom: "1px solid #f0f0f0", textAlign: "right", fontFamily: "ui-monospace, monospace" }}>
                      {ivw != null ? FMT_PCT(ivw, 1) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {riskMatch && (
        <div style={{ fontSize: 13, color: "#444" }}>
          <strong>Risk match (vs {riskMatch.benchmark}):</strong>{" "}
          portfolio EW vol = {FMT_PCT(riskMatch.vol_portfolio_equal_weight_annual)} · benchmark vol ={" "}
          {FMT_PCT(riskMatch.vol_benchmark_annual)} · suggested equity fraction (EW vs cash) ={" "}
          {FMT_NUM(riskMatch.suggested_equity_fraction_equal_weight_vs_cash, 2)}
        </div>
      )}
    </div>
  );
}

export function StrategiesPage({ apiBase }: { apiBase: string }) {
  const [list, setList] = useState<ListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cadence, setCadence] = useState<"daily" | "weekly" | "monthly">("weekly");
  const [drillIn, setDrillIn] = useState<{ strategy: string; cadence: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      try {
        const r = await fetch(`${apiBase}/strategy/list`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: ListResponse = await r.json();
        if (cancelled) return;
        setList(j);
      } catch (e: unknown) {
        if (!cancelled) setError(errMessage(e) || "Failed to load strategies");
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [apiBase]);

  const drillRows: { strategy: string; cadence: string; ts_utc: string | null; n_top: number; key: string; label: string; color: string }[] = useMemo(() => {
    if (!list) return [];
    const meta = list.strategy_meta || {};
    const seen = new Map<string, { strategy: string; cadence: string; ts_utc: string | null; n_top: number; key: string; label: string; color: string }>();
    for (const s of list.snapshots) {
      const cad = s.cadence || "none";
      const key = `${s.strategy}/${cad}`;
      seen.set(key, {
        strategy: s.strategy,
        cadence: cad,
        ts_utc: s.ts_utc,
        n_top: s.n_top,
        key,
        label: meta[s.strategy]?.label || s.label || s.strategy,
        color: meta[s.strategy]?.color || "#4a5568",
      });
    }
    for (const m of list.metrics_files) {
      const key = `${m.strategy}/${m.cadence}`;
      if (seen.has(key)) continue;
      seen.set(key, {
        strategy: m.strategy,
        cadence: m.cadence,
        ts_utc: m.trained_at,
        n_top: 0,
        key,
        label: meta[m.strategy]?.label || m.label || m.strategy,
        color: meta[m.strategy]?.color || "#4a5568",
      });
    }
    return Array.from(seen.values()).sort((a, b) => a.key.localeCompare(b.key));
  }, [list]);

  return (
    <div className="strategies-page">
      <h2 style={{ margin: "12px 0" }}>Strategies — Quality, ML, Pred-weighted ML &amp; RSI mean</h2>
      <p style={{ margin: "0 0 12px", fontSize: 13, color: "#555" }}>
        Compare strategies side-by-side at a chosen cadence — each strategy uses its theme color plus a distinct dash
        pattern and marker shape along the line; baselines use their own dashed styles and markers. Click a legend label to
        hide or show that series. Models are trained on a{" "}
        <strong>50% / 25% / 25%</strong> chronological split (train / validation / test) of the loaded price history;
        regression uses SPY–risk–aligned sample weights so fitting emphasizes stocks whose trailing volatility and beta
        are close to the benchmark. Below the comparison there&apos;s a drill-in for the current portfolio of any
        individual variant.
      </p>

      {error && <div style={{ color: "crimson", marginBottom: 12 }}>{error}</div>}
      {!list ? (
        <div>Loading…</div>
      ) : (
        <>
          <div className="strategies-cadence-bar">
            <span className="strategies-cadence-label">Cadence:</span>
            {CADENCES.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => setCadence(c.id)}
                className={cadence === c.id ? "cadence-btn cadence-btn-active" : "cadence-btn"}
              >
                {c.label}
              </button>
            ))}
          </div>

          <CompareView apiBase={apiBase} cadence={cadence} strategyMeta={list.strategy_meta || {}} />
          <WalkForwardPanel apiBase={apiBase} cadence={cadence} strategyMeta={list.strategy_meta || {}} />

          <hr style={{ margin: "24px 0", border: 0, borderTop: "1px solid #e2e8f0" }} />
          <h3 style={{ margin: "0 0 8px" }}>Drill-in: current portfolio per variant</h3>
          <p style={{ fontSize: 13, color: "#555", marginTop: 0 }}>
            Click any row to see its current top-50 portfolio with weights, plus risk-match vs SPY (when an evaluator
            snapshot has been generated).
          </p>
          <div className="strategies-table-scroll">
            <table className="strategies-table">
              <thead>
                <tr>
                  <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Variant</th>
                  <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Strategy</th>
                  <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Cadence</th>
                  <th style={{ textAlign: "left", padding: 8, borderBottom: "1px solid #ddd" }}>Last update</th>
                  <th style={{ textAlign: "right", padding: 8, borderBottom: "1px solid #ddd" }}>N top</th>
                  <th style={{ padding: 8, borderBottom: "1px solid #ddd" }}></th>
                </tr>
              </thead>
              <tbody>
                {drillRows.map((v) => {
                  const active = drillIn && drillIn.strategy === v.strategy && drillIn.cadence === v.cadence;
                  return (
                    <tr
                      key={v.key}
                      onClick={() => setDrillIn({ strategy: v.strategy, cadence: v.cadence })}
                      style={{
                        cursor: "pointer",
                        background: active ? "#e6f0fb" : undefined,
                      }}
                    >
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", fontFamily: "ui-monospace, monospace" }}>
                        <ColorSwatch color={v.color} />
                        {v.key}
                      </td>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", color: v.color, fontWeight: 600 }}>
                        {v.label}
                      </td>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0" }}>{v.cadence}</td>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0" }}>{FMT_DATE(v.ts_utc)}</td>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0", textAlign: "right" }}>{v.n_top}</td>
                      <td style={{ padding: 8, borderBottom: "1px solid #f0f0f0" }}>
                        <button>{active ? "Selected" : "View"}</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {drillIn && (
            <StrategyDetail
              apiBase={apiBase}
              strategy={drillIn.strategy}
              cadence={drillIn.cadence}
              strategyMeta={list.strategy_meta || {}}
            />
          )}
        </>
      )}
    </div>
  );
}

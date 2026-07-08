import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { OverlayPanel } from "./OverlayPanel";
import { useCoarsePointer } from "./useCoarsePointer";
import { useDeferredOutsideDismiss } from "./useDeferredOutsideDismiss";

type SourceKind = "metric_points_daily" | "technicals_daily" | "analyst_daily";

export type HistoricalSeriesSpec = {
  key: string;
  label: string;
  source: SourceKind;
  /** Format values as percent (expects 0.12 = 12%). */
  pct?: boolean;
};

type ChartRow = {
  ts: string; // YYYY-MM-DD
  value?: number | null;
  aux1?: number | null;
  aux2?: number | null;
  aux1Label?: string;
  aux2Label?: string;
  note?: string | null;
};

type Props = {
  symbol: string;
  apiBase: string;
  spec: HistoricalSeriesSpec;
  /** Optional short text that should remain visible (e.g. LLM rationale). */
  detail?: string;
  children: ReactNode;
};

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end);
  start.setFullYear(start.getFullYear() - 2);
  return { start: ymd(start), end: ymd(end) };
}

function formatMetric(v: number, pct?: boolean): string {
  if (!Number.isFinite(v)) return "—";
  if (pct) return `${(v * 100).toFixed(2)}%`;
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toFixed(0);
  if (abs >= 100) return v.toFixed(1);
  if (abs >= 10) return v.toFixed(2);
  return v.toFixed(4);
}

async function fetchChartRows(apiBase: string, symbol: string, spec: HistoricalSeriesSpec): Promise<ChartRow[]> {
  const sym = symbol.toUpperCase();
  const { start, end } = defaultRange();

  if (spec.source === "metric_points_daily") {
    const params = new URLSearchParams({
      symbols: sym,
      period: "daily",
      provider: "yfinance",
      start,
      end,
    });
    const r = await fetch(`${apiBase}/value/metrics/history?${params}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const rows: any[] = j.rows || [];
    return rows
      .map((x) => ({
        ts: String(x.asof_date).slice(0, 10),
        value: x[spec.key] == null ? null : Number(x[spec.key]),
      }))
      .filter((x) => x.ts && x.value != null);
  }

  if (spec.source === "technicals_daily") {
    const params = new URLSearchParams({ symbol: sym, provider: "yfinance", start, end });
    const r = await fetch(`${apiBase}/value/technicals/history?${params}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const rows: any[] = j.rows || [];

    // For EMA/MACD we add useful companion series.
    return rows
      .map((x) => {
        const ts = String(x.asof_date).slice(0, 10);
        const close = x.close == null ? null : Number(x.close);
        const ema = x.ema == null ? null : Number(x.ema);
        const macd = x.macd_line == null ? null : Number(x.macd_line);
        const signal = x.macd_signal == null ? null : Number(x.macd_signal);
        const adx = x.adx == null ? null : Number(x.adx);
        const rvol = x.rvol == null ? null : Number(x.rvol);

        if (spec.key === "ema") {
          return { ts, value: ema, aux1: close, aux1Label: "Close" } satisfies ChartRow;
        }
        if (spec.key === "macd_line") {
          return { ts, value: macd, aux1: signal, aux1Label: "Signal" } satisfies ChartRow;
        }
        if (spec.key === "adx") {
          return { ts, value: adx } satisfies ChartRow;
        }
        if (spec.key === "rvol") {
          return { ts, value: rvol } satisfies ChartRow;
        }
        return { ts, value: (x[spec.key] == null ? null : Number(x[spec.key])) as number | null } satisfies ChartRow;
      })
      .filter((x) => x.ts && x.value != null);
  }

  // analyst_daily
  const params = new URLSearchParams({ symbol: sym, provider: "yfinance", start, end, limit: "2000" });
  const r = await fetch(`${apiBase}/value/analyst/history?${params}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = await r.json();
  const rows: any[] = j.rows || [];
  return rows
    .map((x) => ({
      ts: String(x.asof_date).slice(0, 10),
      value: x.recommendation_mean == null ? null : Number(x.recommendation_mean),
      note: x.recommendation_key ? String(x.recommendation_key) : null,
      aux1: x.target_mean == null ? null : Number(x.target_mean),
      aux1Label: x.target_mean == null ? undefined : "Target mean",
    }))
    .filter((x) => x.ts && x.value != null);
}

export function HistoricalCellPopover({ symbol, apiBase, spec, detail, children }: Props) {
  const sym = symbol.toUpperCase();
  const coarse = useCoarsePointer();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<ChartRow[]>([]);
  const [anchor, setAnchor] = useState({ x: 0, y: 0 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);
  useDeferredOutsideDismiss(open && !coarse, [panelRef, triggerRef], close);

  const subtitle = useMemo(() => {
    if (spec.source === "metric_points_daily") return "Daily metrics (DB)";
    if (spec.source === "technicals_daily") return "Technicals (DB)";
    return "Analyst snapshots (DB)";
  }, [spec.source]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const pts = await fetchChartRows(apiBase, sym, spec);
      setRows(pts);
      if (!pts.length) setError("No history in DB for this column");
    } catch (e: unknown) {
      setRows([]);
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [apiBase, sym, spec]);

  const openFromTrigger = (clientX?: number, clientY?: number) => {
    const r = triggerRef.current?.getBoundingClientRect();
    setAnchor({ x: clientX ?? r?.left ?? 0, y: clientY ?? r?.bottom ?? 0 });
    setOpen(true);
    void load();
  };

  const last = rows.length ? rows[rows.length - 1] : null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="cell-detail-trigger cell-detail-trigger--has-detail"
        aria-expanded={open}
        aria-label={`${sym} ${spec.label} history`}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          if (open) close();
          else openFromTrigger(e.clientX, e.clientY);
        }}
      >
        {children}
      </button>
      <OverlayPanel
        ref={panelRef}
        open={open}
        onClose={close}
        title={`${sym} · ${spec.label}`}
        subtitle={subtitle}
        anchor={anchor}
        backdrop={coarse}
      >
        {detail && <p className="cell-detail-text" style={{ marginBottom: 10 }}>{detail}</p>}
        {loading && <div className="symbol-price-popover-msg">Loading…</div>}
        {error && !loading && <div className="symbol-price-popover-msg">{error}</div>}
        {!loading && !error && rows.length > 0 && (
          <>
            {last?.value != null && (
              <div className="symbol-price-overlay-summary" style={{ marginBottom: 10, fontSize: 13 }}>
                Latest: <strong>{formatMetric(Number(last.value), spec.pct)}</strong>
                {last.note ? <span style={{ color: "#64748b" }}> · {last.note}</span> : null}
              </div>
            )}
            <ResponsiveContainer width="100%" height={coarse ? 240 : 190}>
              <LineChart data={rows} margin={{ top: 4, right: 10, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="ts" tick={{ fontSize: 10 }} minTickGap={36} tickFormatter={(v) => String(v).slice(5)} />
                <YAxis
                  tick={{ fontSize: 10 }}
                  width={56}
                  domain={["auto", "auto"]}
                  tickFormatter={(v) => formatMetric(Number(v), spec.pct)}
                />
                <Tooltip
                  contentStyle={{ fontSize: 12 }}
                  labelFormatter={(l) => String(l)}
                  formatter={(v: number, _name: string, ctx: any) => {
                    const key = String(ctx?.dataKey ?? "");
                    if (key === "aux1") return [formatMetric(Number(v), false), ctx?.payload?.aux1Label ?? "Aux"];
                    if (key === "aux2") return [formatMetric(Number(v), false), ctx?.payload?.aux2Label ?? "Aux"];
                    return [formatMetric(Number(v), spec.pct), spec.label];
                  }}
                />
                <Line type="monotone" dataKey="value" stroke="#2563eb" strokeWidth={2} dot={false} connectNulls />
                {rows.some((r) => r.aux1 != null) && (
                  <Line type="monotone" dataKey="aux1" stroke="#64748b" strokeWidth={1.25} dot={false} connectNulls />
                )}
                {rows.some((r) => r.aux2 != null) && (
                  <Line type="monotone" dataKey="aux2" stroke="#94a3b8" strokeWidth={1.25} dot={false} connectNulls />
                )}
              </LineChart>
            </ResponsiveContainer>
          </>
        )}
      </OverlayPanel>
    </>
  );
}


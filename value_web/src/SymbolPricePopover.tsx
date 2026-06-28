import { useCallback, useEffect, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { OverlayPanel } from "./OverlayPanel";
import { useCoarsePointer } from "./useCoarsePointer";
import { useDeferredOutsideDismiss } from "./useDeferredOutsideDismiss";

type PricePoint = { ts: string; close: number };

type Props = {
  symbol: string;
  apiBase: string;
  /** Rendered ticker label (defaults to symbol). */
  label?: string;
};

const cache = new Map<string, PricePoint[]>();
const inflight = new Map<string, Promise<PricePoint[]>>();

async function fetchStoredPrices(apiBase: string, symbol: string): Promise<PricePoint[]> {
  const key = symbol.toUpperCase();
  const hit = cache.get(key);
  if (hit) return hit;

  let pending = inflight.get(key);
  if (!pending) {
    pending = (async () => {
      const params = new URLSearchParams({ symbol: key, years: "1" });
      const r = await fetch(`${apiBase}/value/price/history/stored?${params}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const rows: PricePoint[] = (j.rows || [])
        .filter((p: { close?: number }) => p.close != null)
        .map((p: { ts: string; close: number }) => ({
          ts: String(p.ts).slice(0, 10),
          close: Number(p.close),
        }));
      cache.set(key, rows);
      return rows;
    })().finally(() => inflight.delete(key));
    inflight.set(key, pending);
  }
  return pending;
}

function formatPrice(v: number): string {
  if (v >= 1000) return v.toFixed(0);
  if (v >= 100) return v.toFixed(1);
  return v.toFixed(2);
}

export function SymbolPricePopover({ symbol, apiBase, label }: Props) {
  const sym = symbol.toUpperCase();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [points, setPoints] = useState<PricePoint[]>([]);
  const [anchor, setAnchor] = useState({ x: 0, y: 0 });
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const coarse = useCoarsePointer();

  const clearHoverTimer = () => {
    if (hoverTimer.current) {
      clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  };

  const clearLeaveTimer = () => {
    if (leaveTimer.current) {
      clearTimeout(leaveTimer.current);
      leaveTimer.current = null;
    }
  };

  const close = useCallback(() => {
    clearHoverTimer();
    clearLeaveTimer();
    setOpen(false);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await fetchStoredPrices(apiBase, sym);
      setPoints(rows);
      if (!rows.length) setError("No price history in DB");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load");
      setPoints([]);
    } finally {
      setLoading(false);
    }
  }, [apiBase, sym]);

  const showPanel = useCallback(
    (clientX: number, clientY: number) => {
      clearLeaveTimer();
      setAnchor({ x: clientX, y: clientY });
      setOpen(true);
      void load();
    },
    [load],
  );

  const scheduleShow = (clientX: number, clientY: number) => {
    clearHoverTimer();
    hoverTimer.current = setTimeout(() => showPanel(clientX, clientY), 280);
  };

  const scheduleHide = () => {
    clearHoverTimer();
    clearLeaveTimer();
    leaveTimer.current = setTimeout(() => setOpen(false), 200);
  };

  useEffect(
    () => () => {
      clearHoverTimer();
      clearLeaveTimer();
    },
    [],
  );

  useDeferredOutsideDismiss(open && !coarse, [panelRef, triggerRef], close);

  const first = points[0]?.close;
  const last = points[points.length - 1]?.close;
  const chgPct =
    first != null && last != null && first > 0 ? ((last / first - 1) * 100).toFixed(1) : null;

  const openFromTrigger = (clientX?: number, clientY?: number) => {
    const r = triggerRef.current?.getBoundingClientRect();
    showPanel(clientX ?? r?.left ?? 0, clientY ?? r?.bottom ?? 0);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="symbol-hover-trigger"
        aria-expanded={open}
        aria-label={`${sym} price history`}
        onMouseEnter={(e) => {
          if (!coarse) scheduleShow(e.clientX, e.clientY);
        }}
        onMouseLeave={() => {
          if (!coarse) scheduleHide();
        }}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          if (open) close();
          else openFromTrigger(e.clientX, e.clientY);
        }}
        onPointerEnter={(e) => {
          if (e.pointerType === "mouse" && !coarse) scheduleShow(e.clientX, e.clientY);
        }}
      >
        {label ?? sym}
      </button>
      <OverlayPanel
        ref={panelRef}
        open={open}
        onClose={close}
        title={sym}
        subtitle="1Y daily (DB)"
        anchor={anchor}
        backdrop={coarse}
        className="symbol-price-overlay"
      >
        <div
          onMouseEnter={!coarse ? clearLeaveTimer : undefined}
          onMouseLeave={!coarse ? scheduleHide : undefined}
        >
          {last != null && !loading && (
            <div className="symbol-price-popover-last symbol-price-overlay-summary">
              {formatPrice(last)}
              {chgPct != null && (
                <span className={Number(chgPct) >= 0 ? "up" : "down"}>
                  {" "}
                  {Number(chgPct) >= 0 ? "+" : ""}
                  {chgPct}%
                </span>
              )}
            </div>
          )}
          {loading && <div className="symbol-price-popover-msg">Loading…</div>}
          {error && !loading && <div className="symbol-price-popover-msg">{error}</div>}
          {!loading && !error && points.length > 0 && (
            <ResponsiveContainer width="100%" height={coarse ? 220 : 160}>
              <LineChart data={points} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis
                  dataKey="ts"
                  tick={{ fontSize: 10 }}
                  minTickGap={40}
                  tickFormatter={(v) => String(v).slice(5)}
                />
                <YAxis
                  tick={{ fontSize: 10 }}
                  width={48}
                  domain={["auto", "auto"]}
                  tickFormatter={(v) => formatPrice(Number(v))}
                />
                <Tooltip
                  formatter={(v: number) => [formatPrice(v), "Close"]}
                  labelFormatter={(l) => String(l)}
                  contentStyle={{ fontSize: 12 }}
                />
                <Line type="monotone" dataKey="close" stroke="#2563eb" strokeWidth={1.5} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </OverlayPanel>
    </>
  );
}

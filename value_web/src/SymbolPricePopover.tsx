import { useCallback, useEffect, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

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
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const anchorRef = useRef<HTMLSpanElement>(null);

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

  const showPopover = useCallback(
    (clientX: number, clientY: number) => {
      clearLeaveTimer();
      const pad = 12;
      const w = 340;
      const h = 220;
      let x = clientX + pad;
      let y = clientY + pad;
      if (typeof window !== "undefined") {
        if (x + w > window.innerWidth - pad) x = clientX - w - pad;
        if (y + h > window.innerHeight - pad) y = Math.max(pad, clientY - h - pad);
      }
      setPos({ x, y });
      setOpen(true);
      void load();
    },
    [load],
  );

  const scheduleShow = (clientX: number, clientY: number) => {
    clearHoverTimer();
    hoverTimer.current = setTimeout(() => showPopover(clientX, clientY), 280);
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

  const first = points[0]?.close;
  const last = points[points.length - 1]?.close;
  const chgPct =
    first != null && last != null && first > 0 ? ((last / first - 1) * 100).toFixed(1) : null;

  return (
    <>
      <span
        ref={anchorRef}
        className="symbol-hover-trigger"
        onMouseEnter={(e) => scheduleShow(e.clientX, e.clientY)}
        onMouseLeave={scheduleHide}
        onFocus={() => {
          const r = anchorRef.current?.getBoundingClientRect();
          if (r) showPopover(r.left, r.bottom);
        }}
        onBlur={scheduleHide}
        tabIndex={0}
        role="button"
        aria-label={`${sym} price history`}
      >
        {label ?? sym}
      </span>
      {open && (
        <div
          className="symbol-price-popover"
          style={{ left: pos.x, top: pos.y }}
          onMouseEnter={clearLeaveTimer}
          onMouseLeave={scheduleHide}
        >
          <div className="symbol-price-popover-header">
            <strong>{sym}</strong>
            <span className="symbol-price-popover-sub">1Y daily (DB)</span>
            {last != null && !loading && (
              <span className="symbol-price-popover-last">
                {formatPrice(last)}
                {chgPct != null && (
                  <span className={Number(chgPct) >= 0 ? "up" : "down"}>
                    {" "}
                    {Number(chgPct) >= 0 ? "+" : ""}
                    {chgPct}%
                  </span>
                )}
              </span>
            )}
          </div>
          {loading && <div className="symbol-price-popover-msg">Loading…</div>}
          {error && !loading && <div className="symbol-price-popover-msg">{error}</div>}
          {!loading && !error && points.length > 0 && (
            <ResponsiveContainer width="100%" height={160}>
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
      )}
    </>
  );
}

import type { JSX } from "react";

/** Distinct hues for multi-series charts (works on light backgrounds). */
export const CHART_SERIES_COLORS = [
  "#2563eb",
  "#dc2626",
  "#059669",
  "#ca8a04",
  "#7c3aed",
  "#ea580c",
  "#0891b2",
  "#db2777",
  "#4f46e5",
  "#65a30d",
];

/** Line dash patterns — `undefined` = solid. Cycles for many series. */
export const CHART_STROKE_DASHARRAYS: (string | undefined)[] = [
  undefined,
  "10 6",
  "4 4",
  "12 4 2 4",
  "6 3 2 3",
  "14 6",
  "3 6",
  "8 4 2 4",
];

/** Baselines / reference series — always dashed, patterns distinct from primary cycles above. */
export const CHART_BASELINE_DASHARRAYS = ["8 5", "3 8", "16 6 4 6", "5 5 1 5", "12 3 12 3"];

function shapeMarker(
  cx: number,
  cy: number,
  fill: string,
  shapeIndex: number,
  r: number,
): JSX.Element {
  const stroke = "rgba(255,255,255,0.85)";
  const sw = 0.6;
  const si = ((shapeIndex % 5) + 5) % 5;
  if (si === 0) {
    return <circle cx={cx} cy={cy} r={r} fill={fill} stroke={stroke} strokeWidth={sw} />;
  }
  if (si === 1) {
    const d = r * 1.35;
    return <rect x={cx - d / 2} y={cy - d / 2} width={d} height={d} fill={fill} stroke={stroke} strokeWidth={sw} rx={1} />;
  }
  if (si === 2) {
    const h = r * 1.5;
    const pts = `${cx},${cy - h} ${cx - h * 0.9},${cy + h * 0.55} ${cx + h * 0.9},${cy + h * 0.55}`;
    return <polygon points={pts} fill={fill} stroke={stroke} strokeWidth={sw} />;
  }
  if (si === 3) {
    const d = r * 1.5;
    return (
      <polygon
        points={`${cx},${cy - d} ${cx + d},${cy} ${cx},${cy + d} ${cx - d},${cy}`}
        fill={fill}
        stroke={stroke}
        strokeWidth={sw}
      />
    );
  }
  const arm = r * 1.1;
  return (
    <g stroke={fill} strokeWidth={2} strokeLinecap="round">
      <line x1={cx - arm} y1={cy - arm} x2={cx + arm} y2={cy + arm} />
      <line x1={cx - arm} y1={cy + arm} x2={cx + arm} y2={cy - arm} />
    </g>
  );
}

/**
 * Sparse markers along a line so overlapping curves stay distinguishable in color-blind / print scenarios.
 * Only renders every `stride` points to avoid clutter.
 */
export function sparseSeriesDot(opts: { fill: string; shapeIndex: number; stride?: number }) {
  const stride = opts.stride ?? 28;
  const { fill, shapeIndex } = opts;
  return (props: { cx?: number; cy?: number; index?: number }) => {
    const { cx = 0, cy = 0, index = 0 } = props;
    if (index % stride !== 0) return <g />;
    return shapeMarker(cx, cy, fill, shapeIndex, 3.2);
  };
}

/** Legend / header key: mini line segment + marker shape (matches chart series styling). */
export function LegendSeriesGlyph({
  color,
  shapeIndex,
  strokeDasharray,
  width = 34,
  height = 14,
}: {
  color: string;
  shapeIndex: number;
  strokeDasharray?: string;
  width?: number;
  height?: number;
}) {
  const mid = height / 2;
  const lineEnd = width - 9;
  return (
    <svg
      width={width}
      height={height}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0 }}
      aria-hidden
    >
      <line
        x1={1}
        y1={mid}
        x2={lineEnd}
        y2={mid}
        stroke={color}
        strokeWidth={2}
        strokeDasharray={strokeDasharray}
        strokeLinecap="round"
      />
      <g transform={`translate(${width - 5}, ${mid})`}>{shapeMarker(0, 0, color, shapeIndex, 3)}</g>
    </svg>
  );
}

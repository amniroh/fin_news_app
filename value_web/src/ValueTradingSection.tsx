import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_SERIES_COLORS } from "./chartSeriesStyles";

export type ValueTradingPillar = {
  rationale?: string;
  score?: number | null;
};

export type ValueTradingAssessment = {
  id?: number;
  symbol?: string;
  produced_ts_utc?: string;
  model?: string | null;
  investment_name?: string | null;
  asset_type?: string | null;
  overall_summary?: string | null;
  total_score?: number | null;
  pillars?: Record<string, ValueTradingPillar>;
  competitive_edge_score?: number | null;
  management_competence_score?: number | null;
  financial_fortress_score?: number | null;
  pricing_power_score?: number | null;
  understandability_score?: number | null;
  valuation_score?: number | null;
};

const PILLAR_LABELS: Record<string, string> = {
  competitive_edge: "Competitive edge (moat)",
  management_competence: "Management competence",
  financial_fortress: "Financial fortress",
  pricing_power: "Pricing power",
  understandability: "Understandability",
  valuation: "Valuation & margin of safety",
};

const PILLAR_ORDER = Object.keys(PILLAR_LABELS);

function pillarScore(assessment: ValueTradingAssessment, key: string): number {
  const fromPillars = assessment.pillars?.[key]?.score;
  if (fromPillars != null && !Number.isNaN(Number(fromPillars))) return Number(fromPillars);
  const col = `${key}_score` as keyof ValueTradingAssessment;
  const v = assessment[col];
  return v != null && !Number.isNaN(Number(v)) ? Number(v) : 0;
}

function scoreColor(score: number): string {
  if (score >= 4) return "#276749";
  if (score >= 3) return "#48bb78";
  if (score >= 2) return "#ecc94b";
  if (score >= 1) return "#ed8936";
  return "#c53030";
}

export function ValueTradingSection({
  latest,
  history,
}: {
  latest: ValueTradingAssessment | null | undefined;
  history: ValueTradingAssessment[];
}) {
  const chartRows = useMemo(() => {
    if (!latest) return [];
    return PILLAR_ORDER.map((key) => ({
      key,
      label: PILLAR_LABELS[key],
      score: pillarScore(latest, key),
    }));
  }, [latest]);

  if (!latest) {
    return (
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16 }}>Value-trading assessment (6-pillar)</h2>
        <p style={{ color: "#718096", fontSize: 14 }}>
          No intrinsic value assessment stored yet. Run{" "}
          <code style={{ fontSize: 12 }}>backend/value_trading_agent_run.py</code> for this symbol or
          the full interesting-stocks universe.
        </p>
      </section>
    );
  }

  const produced = String(latest.produced_ts_utc || "").slice(0, 19).replace("T", " ");
  const total = Number(latest.total_score ?? chartRows.reduce((s, r) => s + r.score, 0));

  return (
    <section style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 16 }}>Value-trading assessment (6-pillar)</h2>
      <p style={{ fontSize: 13, color: "#4a5568", marginTop: 0 }}>
        Produced {produced} UTC
        {latest.model ? ` · model ${latest.model}` : ""}
        {history.length > 1 ? ` · ${history.length} assessments on file` : ""}
      </p>

      <div style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 16 }}>
        <div>
          <strong>{latest.investment_name || latest.symbol}</strong>
          {latest.asset_type ? ` (${latest.asset_type})` : ""}
          {" · "}
          <span style={{ fontWeight: 600, color: scoreColor(total / 6) }}>
            {total}/30
          </span>
        </div>
        {latest.overall_summary ? (
          <p style={{ margin: "8px 0 0", color: "#2d3748" }}>{latest.overall_summary}</p>
        ) : null}
      </div>

      <div style={{ width: "100%", height: 280, marginBottom: 20 }}>
        <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 8px" }}>Pillar scores (0–5)</p>
        <ResponsiveContainer>
          <BarChart data={chartRows} layout="vertical" margin={{ left: 8, right: 16 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis type="number" domain={[0, 5]} ticks={[0, 1, 2, 3, 4, 5]} fontSize={11} />
            <YAxis type="category" dataKey="label" width={200} fontSize={11} />
            <Tooltip formatter={(v: number) => [`${v}/5`, "Score"]} />
            <Bar dataKey="score" fill={CHART_SERIES_COLORS[0]} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div style={{ fontSize: 14 }}>
        <p style={{ fontSize: 13, fontWeight: 500, margin: "0 0 8px" }}>Rationale by pillar</p>
        <ul style={{ paddingLeft: 18, margin: 0 }}>
          {PILLAR_ORDER.map((key) => {
            const block = latest.pillars?.[key];
            const score = pillarScore(latest, key);
            const rationale = block?.rationale || "—";
            return (
              <li key={key} style={{ marginBottom: 12 }}>
                <div style={{ fontWeight: 500 }}>
                  {PILLAR_LABELS[key]}{" "}
                  <span style={{ color: scoreColor(score) }}>({score}/5)</span>
                </div>
                <p style={{ margin: "4px 0 0", color: "#4a5568" }}>{rationale}</p>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}

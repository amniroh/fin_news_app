"""
Plot histograms for price coverage/quality by symbol priority buckets.

Inputs:
- price_coverage_report_*.json (from telegram_agent.price_coverage_report)
- top1000_investments_prioritised.json (symbol -> priority int)

Outputs:
- PNGs under telegram_agent/data/plots/price_coverage_priority/
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _priority_bucket(p: Optional[int]) -> str:
    if p is None:
        return "unprioritized"
    try:
        pi = int(p)
    except Exception:
        return "unprioritized"
    if pi <= 0:
        return "p0"
    if pi == 1:
        return "p1"
    if pi == 2:
        return "p2"
    return "p3plus"


@dataclass(frozen=True)
class MetricSpec:
    key: str
    title: str
    transform: str  # "identity" | "log10_1p"
    x_label: str
    bins: int


METRICS: List[MetricSpec] = [
    MetricSpec(
        key="coverage_between_min_max",
        title="Coverage between min/max",
        transform="identity",
        x_label="coverage (n_rows / expected)",
        bins=60,
    ),
    MetricSpec(
        key="null_close_n",
        title="Null close count",
        transform="log10_1p",
        x_label="log10(1 + null_close_n)",
        bins=60,
    ),
    MetricSpec(
        key="nonpositive_close_n",
        title="Non-positive close count",
        transform="log10_1p",
        x_label="log10(1 + nonpositive_close_n)",
        bins=60,
    ),
    MetricSpec(
        key="null_volume_n",
        title="Null volume count",
        transform="log10_1p",
        x_label="log10(1 + null_volume_n)",
        bins=60,
    ),
    MetricSpec(
        key="gaps_gt_2x_step_n",
        title="Gap count (> 2x step)",
        transform="log10_1p",
        x_label="log10(1 + gaps_gt_2x_step_n)",
        bins=60,
    ),
    MetricSpec(
        key="max_gap_seconds",
        title="Max gap (seconds)",
        transform="log10_1p",
        x_label="log10(1 + max_gap_seconds)",
        bins=60,
    ),
]


def _apply_transform(x: pd.Series, how: str) -> pd.Series:
    if how == "identity":
        return x.astype(float)
    if how == "log10_1p":
        # Keep NaNs; clamp negatives to 0 (shouldn't happen).
        xx = pd.to_numeric(x, errors="coerce")
        xx = xx.where(xx >= 0, 0)
        return np.log10(1.0 + xx.astype(float))
    raise ValueError(f"Unknown transform {how!r}")


def _plot_hist_grid(
    df: pd.DataFrame,
    *,
    timeframe: str,
    metric: MetricSpec,
    out_path: Path,
) -> None:
    buckets = ["p0", "p1", "p2", "p3plus"]
    sub = df[df["timeframe"] == timeframe].copy()
    sub = sub[sub["priority_bucket"].isin(buckets)]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    ax_map = dict(zip(buckets, axes.ravel()))

    for b in buckets:
        ax = ax_map[b]
        s = sub[sub["priority_bucket"] == b][metric.key]
        s2 = _apply_transform(s, metric.transform).dropna()
        if len(s2) == 0:
            ax.set_title(f"{b} (n=0)")
            ax.axis("off")
            continue

        ax.hist(s2.to_numpy(), bins=int(metric.bins), color="#2b6cb0", alpha=0.85, edgecolor="white", linewidth=0.3)
        ax.set_title(f"{b} (n={len(s2)})")
        ax.set_xlabel(metric.x_label)
        ax.set_ylabel("count")
        ax.grid(True, alpha=0.2)

    fig.suptitle(f"{timeframe}: {metric.title} by priority bucket", fontsize=14)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def build_dataframe(report: Dict[str, Any], priorities: Dict[str, Any]) -> pd.DataFrame:
    rows = report.get("rows") or []
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["symbol"] = df["symbol"].astype(str).str.upper()
    pr = {str(k).upper(): v for k, v in (priorities or {}).items()}
    df["priority_raw"] = df["symbol"].map(pr)
    df["priority_bucket"] = df["priority_raw"].map(_priority_bucket)
    return df


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Plot coverage/quality histograms by symbol priority buckets.")
    ap.add_argument(
        "--report",
        required=True,
        help="Path to price_coverage_report_*.json",
    )
    ap.add_argument(
        "--priorities",
        default="telegram_agent/top1000_investments_prioritised.json",
        help="Path to symbol->priority json (default: telegram_agent/top1000_investments_prioritised.json)",
    )
    ap.add_argument(
        "--out-dir",
        default="telegram_agent/data/plots/price_coverage_priority",
        help="Output directory for PNG plots",
    )
    args = ap.parse_args(argv)

    report_p = Path(str(args.report)).expanduser()
    pri_p = Path(str(args.priorities)).expanduser()
    out_dir = Path(str(args.out_dir)).expanduser()

    report = _load_json(report_p)
    priorities = _load_json(pri_p)
    df = build_dataframe(report, priorities)
    if df.empty:
        raise SystemExit("Empty report rows; nothing to plot.")

    stamp = _utc_stamp()
    timeframes = ["1m", "1h", "1d"]
    for tf in timeframes:
        for m in METRICS:
            out_p = out_dir / f"hist_{tf}_{m.key}_{stamp}.png"
            _plot_hist_grid(df, timeframe=tf, metric=m, out_path=out_p)

    # Also save a small summary JSON with counts per bucket/timeframe.
    summary = (
        df[df["priority_bucket"].isin(["p0", "p1", "p2", "p3plus"])]
        .groupby(["timeframe", "priority_bucket"])["symbol"]
        .count()
        .reset_index()
        .rename(columns={"symbol": "n_series"})
    )
    (out_dir / f"summary_counts_{stamp}.json").write_text(
        json.dumps(
            {
                "generated_ts_utc": datetime.now(timezone.utc).isoformat(),
                "report_path": str(report_p),
                "priorities_path": str(pri_p),
                "counts": summary.to_dict(orient="records"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(str(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


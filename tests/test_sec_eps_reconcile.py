"""Offline tests for SEC tag EPS forward-split adjustment using the picked fact's ``filed`` date.

The provider divides the picked tag value by the cumulative split ratio for splits with
ex-date strictly after the fact's ``filed``. We validate the rule by stubbing
``_yf_split_factor_after`` so the test runs without yfinance / network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
for p in (_BACKEND, _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import value_metrics_provider_sec_fundamentals as P


def _make_facts(eps_facts: List[Dict[str, object]]) -> Dict[str, object]:
    return {
        "cik": 1234567,
        "facts": {
            "us-gaap": {
                "EarningsPerShareDiluted": {"units": {"USD/shares": eps_facts}},
            }
        },
    }


def _stub_split_factor(monkeypatch_attr: str, table: Dict[str, float]):
    def fake(symbol: str, asof_date: str) -> float:
        return float(table.get(asof_date, 1.0))

    setattr(P, monkeypatch_attr, fake)


def test_old_filing_pre_split_is_divided_by_post_filing_split_factor():
    P_orig = P._yf_split_factor_after
    try:
        # GOOGL Q1 2017: only ever filed pre-split. Cumulative split factor for splits
        # AFTER filed=2018-04-24 is 20.0 (the 2022-07-15 20:1 split).
        _stub_split_factor("_yf_split_factor_after", {"2018-04-24": 20.0})
        facts = _make_facts(
            [
                {
                    "start": "2017-01-01",
                    "end": "2017-03-31",
                    "val": 7.73,
                    "fy": 2017,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2017-05-02",
                },
                {
                    "start": "2017-01-01",
                    "end": "2017-03-31",
                    "val": 7.73,
                    "fy": 2018,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2018-04-24",
                },
            ]
        )
        row = P._build_row(
            symbol="GOOGL", facts_json=facts, end="2017-03-31", fy=2017, fp="Q1", annual=False
        )
        assert row["eps"] is not None
        assert abs(float(row["eps"]) - 7.73 / 20.0) < 1e-6
        assert row["raw"]["eps_method"] == "eps_tag_split_adjusted_after_filed"
    finally:
        P._yf_split_factor_after = P_orig


def test_post_split_restated_is_picked_and_not_re_divided():
    P_orig = P._yf_split_factor_after
    try:
        # GOOGL Q1 2022: latest filed=2023-04-26 (post-split, val already on new basis 1.23).
        # Split factor AFTER 2023-04-26 is 1.0 → no division.
        _stub_split_factor("_yf_split_factor_after", {"2023-04-26": 1.0})
        facts = _make_facts(
            [
                {
                    "start": "2022-01-01",
                    "end": "2022-03-31",
                    "val": 24.62,
                    "fy": 2022,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2022-04-27",
                },
                {
                    "start": "2022-01-01",
                    "end": "2022-03-31",
                    "val": 1.23,
                    "fy": 2023,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2023-04-26",
                },
            ]
        )
        row = P._build_row(
            symbol="GOOGL", facts_json=facts, end="2022-03-31", fy=2022, fp="Q1", annual=False
        )
        assert abs(float(row["eps"]) - 1.23) < 1e-9
    finally:
        P._yf_split_factor_after = P_orig


def test_no_subsequent_split_means_no_division():
    P_orig = P._yf_split_factor_after
    try:
        _stub_split_factor("_yf_split_factor_after", {})  # always 1.0
        facts = _make_facts(
            [
                {
                    "start": "2025-01-01",
                    "end": "2025-03-31",
                    "val": 1.55,
                    "fy": 2025,
                    "fp": "Q1",
                    "form": "10-Q",
                    "filed": "2025-04-25",
                }
            ]
        )
        row = P._build_row(
            symbol="ANY", facts_json=facts, end="2025-03-31", fy=2025, fp="Q1", annual=False
        )
        assert abs(float(row["eps"]) - 1.55) < 1e-9
        assert row["raw"]["split_factor_after_filed"] == 1.0
    finally:
        P._yf_split_factor_after = P_orig


def test_picker_selects_min_duration_quarter_slice():
    P_orig = P._yf_split_factor_after
    try:
        _stub_split_factor("_yf_split_factor_after", {})
        # Same end, two slices: 181-day YTD ($2.44) vs 90-day quarter ($1.21)
        facts = _make_facts(
            [
                {
                    "start": "2022-01-01",
                    "end": "2022-06-30",
                    "val": 2.44,
                    "fy": 2022,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2023-07-26",
                },
                {
                    "start": "2022-04-01",
                    "end": "2022-06-30",
                    "val": 1.21,
                    "fy": 2022,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2023-07-26",
                },
            ]
        )
        row = P._build_row(
            symbol="GOOGL", facts_json=facts, end="2022-06-30", fy=2022, fp="Q2", annual=False
        )
        assert abs(float(row["eps"]) - 1.21) < 1e-9
    finally:
        P._yf_split_factor_after = P_orig

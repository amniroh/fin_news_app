"""Unit tests for SEC Company Facts duration selection (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
for p in (_BACKEND, _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from value_metrics_provider_sec_fundamentals import pick_flow_fact_same_end


def test_pick_quarter_prefers_narrow_duration_same_end():
    """Apple FY2012 Q2: same ``end`` has both H1 YTD and single-quarter facts."""
    facts = [
        {"end": "2012-03-31", "start": "2011-09-25", "val": 26.17, "filed": "2012-04-25"},
        {"end": "2012-03-31", "start": "2012-01-01", "val": 12.3, "filed": "2012-04-25"},
    ]
    picked = pick_flow_fact_same_end(facts, annual=False)
    assert picked is not None
    assert abs(picked["val"] - 12.3) < 1e-9


def test_pick_fy_prefers_long_duration():
    facts = [
        {"end": "2012-09-29", "start": "2012-07-01", "val": 8.22e9, "fp": "FY", "filed": "2012-10-31"},
        {"end": "2012-09-29", "start": "2011-09-25", "val": 41.733e9, "fp": "FY", "filed": "2012-10-31"},
    ]
    picked = pick_flow_fact_same_end(facts, annual=True)
    assert picked is not None
    assert abs(picked["val"] - 41.733e9) < 1.0


if __name__ == "__main__":
    test_pick_quarter_prefers_narrow_duration_same_end()
    test_pick_fy_prefers_long_duration()
    print("ok")

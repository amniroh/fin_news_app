from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
for p in (_BACKEND, _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from value_metrics_daily_backfill import _ttm_sum_last4


def test_ttm_sum_requires_four_quarters():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    ttm = _ttm_sum_last4(s)
    assert pd.isna(ttm.iloc[0])
    assert pd.isna(ttm.iloc[1])
    assert pd.isna(ttm.iloc[2])
    assert float(ttm.iloc[3]) == 10.0
    assert float(ttm.iloc[4]) == 14.0


"""Offline tests for SEC tag EPS vs split reconciliation heuristics."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
for p in (_BACKEND, _REPO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from value_metrics_provider_sec_fundamentals import _reconcile_tag_eps_with_splits


def test_reconcile_prefers_restated_small_eps_when_scaled_down_is_tiny():
    # Already restated ~$1.40; dividing 20:1 would give ~0.07 (wrong)
    e, reason = _reconcile_tag_eps_with_splits(tag_eps=1.4, split_factor_after_end=20.0)
    assert abs(e - 1.4) < 1e-9
    assert "restated" in reason or "default" in reason or "tag" in reason


def test_reconcile_scales_down_large_presplit_tag():
    e, _reason = _reconcile_tag_eps_with_splits(tag_eps=26.29, split_factor_after_end=20.0)
    assert abs(e - 26.29 / 20.0) < 1e-6


def test_reconcile_no_split_factor_returns_tag():
    e, reason = _reconcile_tag_eps_with_splits(tag_eps=1.23, split_factor_after_end=1.0)
    assert abs(e - 1.23) < 1e-9
    assert reason == "tag_only"

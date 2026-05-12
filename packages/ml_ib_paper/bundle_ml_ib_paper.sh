#!/usr/bin/env bash
# Run from repo root. Produces dist/ml_ib_paper_bundle.tgz for copying to a remote host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${ROOT}/dist"
mkdir -p "${OUT}"
STAMP="$(date +%Y%m%d_%H%M%S)"
NAME="ml_ib_paper_bundle_${STAMP}"
TMP="${OUT}/${NAME}"
rm -rf "${TMP}"
mkdir -p "${TMP}/backend" "${TMP}/packages/ml_ib_paper"

cp "${ROOT}/backend/sp500_return_model.py" "${TMP}/backend/"
cp "${ROOT}/packages/ml_ib_paper/requirements.txt" "${TMP}/packages/ml_ib_paper/"
cp "${ROOT}/packages/ml_ib_paper/paper_rebalance.py" "${TMP}/packages/ml_ib_paper/"
cp "${ROOT}/packages/ml_ib_paper/README.md" "${TMP}/packages/ml_ib_paper/"

# Copy current daily ML artifacts (shared joblib + score-weighted metrics).
ART="${ROOT}/backend/data/sp500_return_models"
if [[ -f "${ART}/sp500_return_model_daily.joblib" ]]; then
  mkdir -p "${TMP}/backend/data/sp500_return_models"
  cp "${ART}/sp500_return_model_daily.joblib" "${TMP}/backend/data/sp500_return_models/"
  cp "${ART}/sp500_return_model_daily_score_weighted_metrics.json" "${TMP}/backend/data/sp500_return_models/" 2>/dev/null || true
fi
# Price cache (optional but recommended — otherwise first run downloads via yfinance).
if [[ -d "${ROOT}/backend/data/sp500_prices" ]]; then
  mkdir -p "${TMP}/backend/data/sp500_prices"
  # Limit tarball size: ship SPY + a subset is not enough for predict_top_n which needs all cached names.
  # Copy all parquets if under ~200MB total; else user should rsync sp500_prices separately.
  cp "${ROOT}/backend/data/sp500_prices/"*.parquet "${TMP}/backend/data/sp500_prices/" 2>/dev/null || true
fi

tar -C "${OUT}" -czf "${OUT}/${NAME}.tgz" "${NAME}"
rm -rf "${TMP}"
echo "Wrote ${OUT}/${NAME}.tgz"

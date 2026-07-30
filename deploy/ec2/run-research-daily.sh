#!/usr/bin/env bash
# DEPRECATED: research runs inside orchestrator-daily.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "run-research-daily.sh is deprecated; forwarding to orchestrator-daily." >&2
exec bash "$REPO_ROOT/deploy/ec2/run-orchestrator-daily.sh"

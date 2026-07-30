#!/usr/bin/env bash
# DEPRECATED: daily fetchers moved into the orchestrator.
# This wrapper keeps old cron/systemd ExecStart paths working.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "run-daily-jobs.sh is deprecated; forwarding to orchestrator-daily." >&2
exec bash "$REPO_ROOT/deploy/ec2/run-orchestrator-daily.sh"

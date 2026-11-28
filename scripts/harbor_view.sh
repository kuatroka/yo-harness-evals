#!/usr/bin/env bash
# The viewer with the launch overlay.
#
#   uv run harbor-yo-overlay                     # jobs/ on port 8080
#   scripts/harbor_view.sh                       # same, with PORT/JOBS_DIR defaults
#   PORT=8100 scripts/harbor_view.sh             # another port
#   JOBS_DIR=/tmp/jobs scripts/harbor_view.sh    # another job folder
#
# Nothing under site-packages is touched: the overlay rebuilds Harbor's static
# bundle into .viewer_static/ on start and mounts its own routes on the app.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

JOBS_DIR="${JOBS_DIR:-jobs}"
PORT="${PORT:-8080}"

exec uv run harbor-yo-overlay --jobs "$JOBS_DIR" --port "$PORT" "$@"

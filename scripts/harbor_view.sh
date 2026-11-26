#!/usr/bin/env bash
# The single entry point for the viewer with the launch overlay.
#
#   scripts/harbor_view.sh                      # jobs/ on port 8080
#   PORT=8100 scripts/harbor_view.sh            # another port
#   JOBS_DIR=/tmp/jobs scripts/harbor_view.sh    # another job folder
#
# Nothing under site-packages is touched: the overlay rebuilds Harbor's static
# bundle into .viewer_static/ on start and mounts its own routes on the app.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

JOBS_DIR="${JOBS_DIR:-jobs}"
PORT="${PORT:-8080}"

exec uv run python -m viewer_overlay.app --jobs "$JOBS_DIR" --port "$PORT" "$@"

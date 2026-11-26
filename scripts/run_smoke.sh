#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HARNESS="${1:-droid}"
EFFORT="${2:-high}"
TASK="${3:-regex-log}"
MODEL_KEY="${4:-}"
DATASET="${DATASET:-$ROOT/datasets/frontier-harness-eval}"
JOBS_DIR="${JOBS_DIR:-$ROOT/jobs}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
ATTEMPTS="${ATTEMPTS:-1}"

case "$HARNESS" in
  droid|amp) ;;
  *) echo "harness must be 'droid' or 'amp'" >&2; exit 2 ;;
esac

case "$EFFORT" in
  high|max) ;;
  *) echo "effort must be 'high' or 'max'" >&2; exit 2 ;;
esac

MODEL_ARGS=()
if [ -n "$MODEL_KEY" ]; then
  case "$MODEL_KEY" in
    deepseek-flash-high) HARBOR_MODEL="devin/deepseek-v4.1-flash-high"; EFFORT="high" ;;
    deepseek-flash-max) HARBOR_MODEL="devin/deepseek-v4.1-flash-max"; EFFORT="max" ;;
    swe-2-max) HARBOR_MODEL="devin/swe-2-max"; EFFORT="max" ;;
    swe-2-flash-max) HARBOR_MODEL="devin/swe-2-flash-max"; EFFORT="max" ;;
    *) echo "unknown model: $MODEL_KEY" >&2; exit 2 ;;
  esac
  MODEL_ARGS+=(--ak "model=$MODEL_KEY")
else
  HARBOR_MODEL="devin/deepseek-v4.1-flash-$EFFORT"
fi

TASK_DIR="$DATASET/$TASK"
if [ ! -d "$TASK_DIR" ]; then
  echo "task directory not found: $TASK_DIR" >&2
  echo "download it with: uv run harbor download frontier-harness-eval/frontier-harness-eval@v1.1.0 --export -o datasets" >&2
  exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH" >&2
  exit 2
fi

droid_host_auth_available() {
  local dir="${DROID_HOST_AUTH_DIR:-$HOME/.factory}"
  if [ -f "$dir/auth.v2.file" ] && [ -f "$dir/auth.v2.key" ]; then
    return 0
  fi
  if [ -f "$dir/auth.v2.loginkeychain" ] && [ -f "$dir/auth.v2.key" ]; then
    return 0
  fi
  return 1
}

if [ "$HARNESS" = "droid" ]; then
  if [ -z "${NINEROUTER_API_KEY:-}" ] && [ -z "${DROID_MODEL_API_KEY:-}" ]; then
    echo "NINEROUTER_API_KEY (or DROID_MODEL_API_KEY) is not set" >&2
    exit 2
  fi
  if [ -z "${FACTORY_API_KEY:-}" ] && ! droid_host_auth_available; then
    echo "Droid auth is unavailable: set FACTORY_API_KEY, or log in to Droid on the host so ~/.factory/auth.v2.file and auth.v2.key exist" >&2
    exit 2
  fi
  AGENT="harnesses.droid:DroidAgent"
else
  if [ -z "${AMP_API_KEY:-}" ]; then
    echo "AMP_API_KEY is not set" >&2
    exit 2
  fi
  AGENT="harnesses.amp:AmpAgent"
fi

JOB_NAME="smoke-${HARNESS}-${MODEL_KEY:-$EFFORT}-${TASK}-$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/$JOB_NAME.log"
mkdir -p "$JOBS_DIR" "$LOG_DIR"

ARGS=(
  run -p "$TASK_DIR"
  -a "$AGENT"
  --ak "effort=$EFFORT"
  "${MODEL_ARGS[@]}"
  -m "$HARBOR_MODEL"
  --skip-model-parsing-and-provider-mapping
  --job-name "$JOB_NAME"
  -o "$JOBS_DIR"
  -k "$ATTEMPTS"
  --n-concurrent 1
  --yes
)

if [ "${FORCE_BUILD:-1}" = "1" ]; then
  ARGS+=(--force-build)
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  ARGS+=(--dry-run)
fi
if [ "${DEBUG:-0}" = "1" ]; then
  ARGS+=(--debug)
fi

echo "harness=$HARNESS effort=$EFFORT model=$HARBOR_MODEL task=$TASK"
echo "job=$JOB_NAME"
echo "log=$LOG_FILE"
set +e
uv run harbor "${ARGS[@]}" 2>&1 | tee "$LOG_FILE"
status=${PIPESTATUS[0]}
set -e
echo "exit=$status"
exit "$status"

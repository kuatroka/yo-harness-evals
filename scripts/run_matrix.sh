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

DATASET="${DATASET:-$ROOT/datasets/frontier-harness-eval}"
JOBS_DIR="${JOBS_DIR:-$ROOT/jobs}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
ATTEMPTS="${ATTEMPTS:-2}"
# Harbor retries only exception types whose policy allows it. The default
# exclusion set already covers AgentTimeoutError and NonZeroAgentExitCodeError,
# so this repairs infrastructure failures without resampling real failures.
RETRIES="${RETRIES:-2}"
CLAUDE_SHIM_PORT="${CLAUDE_SHIM_PORT:-20140}"
CLAUDE_BASE_URL="${CLAUDE_BASE_URL:-http://host.docker.internal:$CLAUDE_SHIM_PORT}"
CLAUDE_SMALL_MODEL="${CLAUDE_SMALL_MODEL:-devin/swe-2-flash-max}"
CONCURRENCY="${CONCURRENCY:-1}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
JOB_PREFIX="${JOB_PREFIX:-fh}"
HARNESSES="${HARNESSES:-droid,amp}"
EFFORTS="${EFFORTS:-high,max}"
MODELS="${MODELS:-}"
TASKS="${TASKS:-regex-log,nginx-request-logging,polyglot-c-py,openssl-selfsigned-cert,sqlite-db-truncate}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH" >&2
  exit 2
fi

if [ ! -d "$DATASET" ]; then
  echo "dataset directory not found: $DATASET" >&2
  echo "download it with: uv run harbor download frontier-harness-eval/frontier-harness-eval@v1.1.0 --export -o datasets" >&2
  exit 2
fi

IFS=',' read -r -a TASK_LIST <<< "$TASKS"
IFS=',' read -r -a HARNESS_LIST <<< "$HARNESSES"
IFS=',' read -r -a EFFORT_LIST <<< "$EFFORTS"
if [ -n "$MODELS" ]; then
  IFS=',' read -r -a MODEL_LIST <<< "$MODELS"
else
  MODEL_LIST=()
fi

for task in "${TASK_LIST[@]}"; do
  if [ ! -d "$DATASET/$task" ]; then
    echo "task directory not found: $DATASET/$task" >&2
    exit 2
  fi
done

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

for harness in "${HARNESS_LIST[@]}"; do
  case "$harness" in
    droid)
      if [ -z "${NINEROUTER_API_KEY:-}" ] && [ -z "${DROID_MODEL_API_KEY:-}" ]; then
        echo "NINEROUTER_API_KEY (or DROID_MODEL_API_KEY) is required for droid" >&2
        exit 2
      fi
      if [ -z "${FACTORY_API_KEY:-}" ] && ! droid_host_auth_available; then
        echo "Droid auth is unavailable: set FACTORY_API_KEY, or log in to Droid on the host so ~/.factory/auth.v2.file and auth.v2.key exist" >&2
        exit 2
      fi
      ;;
    amp)
      if [ -z "${AMP_API_KEY:-}" ]; then
        echo "AMP_API_KEY is required for amp" >&2
        exit 2
      fi
      ;;
    claude)
      if [ -z "${NINEROUTER_API_KEY:-}" ]; then
        echo "NINEROUTER_API_KEY is required for claude (it reaches 9router)" >&2
        exit 2
      fi
      ;;
    codex)
      if [ -z "${NINEROUTER_API_KEY:-}" ]; then
        echo "NINEROUTER_API_KEY is required for codex (it reaches 9router)" >&2
        exit 2
      fi
      ;;
    *)
      echo "unknown harness: $harness" >&2
      exit 2
      ;;
  esac
done

REGISTRY_PY=(.venv/bin/python bench/model_id.py)
if ! "${REGISTRY_PY[@]}" check; then
  echo "model registry is invalid" >&2
  exit 2
fi
KNOWN_EFFORTS=",$("${REGISTRY_PY[@]}" efforts),"
for effort in "${EFFORT_LIST[@]}"; do
  case "$KNOWN_EFFORTS" in
    *",$effort,"*) ;;
    *) echo "unknown effort: $effort" >&2; exit 2 ;;
  esac
done

for model in "${MODEL_LIST[@]}"; do
  # Fails with the registry's own message when the key is missing, so a model
  # that only exists in someone's shell history cannot start a job.
  "${REGISTRY_PY[@]}" "$model" route >/dev/null
done

INCLUDE=()
for task in "${TASK_LIST[@]}"; do
  INCLUDE+=(-i "$task")
done

mkdir -p "$JOBS_DIR" "$LOG_DIR"

claude_shim_ready() {
  curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$CLAUDE_SHIM_PORT/health" 2>/dev/null
}

CLAUDE_SHIM_PID=""
ensure_claude_shim() {
  if claude_shim_ready; then
    return 0
  fi
  python3 "$ROOT/scripts/claude_identity_shim.py" --port "$CLAUDE_SHIM_PORT" --quiet &
  CLAUDE_SHIM_PID=$!
  local attempt
  for attempt in $(seq 1 40); do
    if claude_shim_ready; then
      return 0
    fi
    sleep 0.25
  done
  echo "claude identity shim failed to start on port $CLAUDE_SHIM_PORT" >&2
  return 1
}

claude_direct_token() {
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    printf '%s\n' "$CLAUDE_CODE_OAUTH_TOKEN"
    return 0
  fi
  if ! command -v security >/dev/null 2>&1; then
    echo "no CLAUDE_CODE_OAUTH_TOKEN and no keychain on this host" >&2
    return 1
  fi
  security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
    | "$ROOT/.venv/bin/python" -c 'import json, sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])'
}

stop_claude_shim() {
  if [ -n "$CLAUDE_SHIM_PID" ]; then
    kill "$CLAUDE_SHIM_PID" 2>/dev/null || true
  fi
}
trap stop_claude_shim EXIT

run_job() {
  local harness="$1"
  local model_key="$2"
  local effort="$3"
  local harbor_model
  local job_name
  local model_slug
  local -a agent_args=()
  local -a run_prefix=()
  local direct_anthropic=0

  if [ -n "$model_key" ]; then
    harbor_model="$("${REGISTRY_PY[@]}" "$model_key" route)"
    effort="$("${REGISTRY_PY[@]}" "$model_key" effort)"
    # The id carries slashes. A job folder is a filename, so flatten them once
    # here; every Python caller uses bench.lib.slug for the same rule.
    model_slug="${model_key//\//-}"
    job_name="${JOB_PREFIX}-${harness}-${model_slug}-${RUN_ID}"
  else
    harbor_model="devin/deepseek-v4.1-flash-$effort"
    job_name="${JOB_PREFIX}-${harness}-${effort}-${RUN_ID}"
  fi

  case "$harness" in
    droid)
      agent_args+=(-a "harnesses.droid:DroidAgent")
      ;;
    amp)
      agent_args+=(-a "harnesses.amp:AmpAgent")
      ;;
    codex)
      agent_args+=(
        -a "harbor.agents.installed.codex:Codex"
        --base-url "http://host.docker.internal:20128/v1"
        --ae "OPENAI_API_KEY=$NINEROUTER_API_KEY"
        --ae "OPENAI_BASE_URL=http://host.docker.internal:20128/v1"
        --ak "config=$ROOT/bench/config/codex-base.toml"
      )
      ;;
    claude)
      if [ -n "$model_key" ] && [ "$("${REGISTRY_PY[@]}" "$model_key" provider)" = "anthropic" ]; then
        # Direct Anthropic access. The subscription token from the host keychain
        # goes to the CLI, no shim and no 9Router, and Harbor parses the model id
        # so it resolves the anthropic provider instead of treating it as external.
        local token
        if ! token="$(claude_direct_token)"; then
          return 1
        fi
        run_prefix=(env CLAUDE_FORCE_OAUTH=1 "CLAUDE_CODE_OAUTH_TOKEN=$token")
        direct_anthropic=1
        agent_args+=(
          # Background requests go to Anthropic too, so the small-model name has
          # to be a real Anthropic id, not a 9router route.
          --ae "ANTHROPIC_SMALL_FAST_MODEL=$harbor_model"
          # The devin routes encode effort in the model id; a direct Anthropic id
          # carries none, so the level has to go in as a flag or it runs at default.
          --ak "reasoning_effort=$effort"
          -a "harnesses.claude:ClaudeCodeAgent"
        )
      else
        # Claude Code speaks the Anthropic Messages API, which 9router serves at
        # /v1/messages. Devin rejects three paragraphs of Claude Code's own prompt,
        # so every request goes through the local shim that removes them. Effort is
        # encoded in the devin/swe-2-* model IDs, so no reasoning_effort kwarg is
        # passed. The small model handles background requests that Claude Code
        # would otherwise send to a stock haiku name.
        if ! ensure_claude_shim; then
          return 1
        fi
        run_prefix=(env "ANTHROPIC_API_KEY=$NINEROUTER_API_KEY")
        agent_args+=(
          --base-url "$CLAUDE_BASE_URL"
          --ae "ANTHROPIC_SMALL_FAST_MODEL=$CLAUDE_SMALL_MODEL"
          -a "harnesses.claude:ClaudeCodeAgent"
        )
      fi
      ;;
  esac

  if [ "$harness" != "claude" ]; then
    if [ -n "$model_key" ] && [ "$harness" != "codex" ]; then
      # Codex has no model kwarg; its model comes from -m plus --base-url.
      # Every adapter takes the route; Droid and Amp also record the id.
      agent_args+=(--ak "model=$harbor_model")
      if [ "$harness" = "droid" ] || [ "$harness" = "amp" ]; then
        # These two adapters record the id for the viewer and the Droid label.
        agent_args+=(--ak "model_id=$model_key")
      fi
    fi
    if [ "$harness" = "codex" ]; then
      # Codex only knows reasoning_effort; effort is droid/amp-specific.
      agent_args+=(--ak "reasoning_effort=$effort")
    else
      agent_args+=(--ak "effort=$effort" --ak "reasoning_effort=$effort")
    fi
  fi

  if [ -n "$model_key" ] && ! "${REGISTRY_PY[@]}" "$model_key" supports "$harness" >/dev/null; then
    # The registry says which harnesses reach a model. A pair outside that list
    # used to run anyway on whatever route the harness defaulted to.
    echo "==> skip $harness + $model_key: the registry does not route this harness to it"
    return 0
  fi

  local job_concurrency="$CONCURRENCY"
  if [ -n "$model_key" ]; then
    local model_cap
    model_cap="$("${REGISTRY_PY[@]}" "$model_key" max-concurrency)"
    if [ "$model_cap" -gt 0 ] 2>/dev/null && [ "$job_concurrency" -gt "$model_cap" ]; then
      job_concurrency="$model_cap"
    fi
  fi

  local log_file="$LOG_DIR/$job_name.log"
  local args=(
    run -p "$DATASET"
    "${INCLUDE[@]}"
    "${agent_args[@]}"
    -m "$harbor_model"
    --job-name "$job_name"
    -o "$JOBS_DIR"
    -k "$ATTEMPTS"
    --max-retries "$RETRIES"
    --n-concurrent "$job_concurrency"
    --yes
  )

  if [ "$direct_anthropic" != "1" ]; then
    # Direct Anthropic needs Harbor to resolve the real provider from the model id.
    args+=(--skip-model-parsing-and-provider-mapping)
  fi

  if [ "${FORCE_BUILD:-1}" = "1" ]; then
    args+=(--force-build)
  fi

  if [ "${DRY_RUN:-0}" = "1" ]; then
    args+=(--dry-run)
  fi
  if [ "${DEBUG:-0}" = "1" ]; then
    args+=(--debug)
  fi

  echo
  echo "==> $job_name"
  echo "    tasks: ${TASKS}"
  echo "    attempts: ${ATTEMPTS}  max-retries: ${RETRIES}  concurrency: $job_concurrency"
  echo "    log:   $log_file"
  set +e
  "${run_prefix[@]}" uv run harbor "${args[@]}" 2>&1 | tee "$log_file"
  local status=${PIPESTATUS[0]}
  set -e
  if [ "$harness" = claude ] && [ -d "$JOBS_DIR/$job_name" ]; then
    # Claude encodes effort in the model ID, so its trials land without the
    # kwarg the viewer reads. Record the effort they actually ran at.
    "$ROOT/.venv/bin/python" "$ROOT/scripts/backfill_metadata.py" --root "$JOBS_DIR/$job_name" | tail -1
  fi
  echo "==> $job_name exit=$status"
  return "$status"
}

failures=0
if [ "${#MODEL_LIST[@]}" -gt 0 ]; then
  for harness in "${HARNESS_LIST[@]}"; do
    for model in "${MODEL_LIST[@]}"; do
      if ! run_job "$harness" "$model" ""; then
        failures=$((failures + 1))
      fi
    done
  done
else
  for harness in "${HARNESS_LIST[@]}"; do
    for effort in "${EFFORT_LIST[@]}"; do
      if ! run_job "$harness" "" "$effort"; then
        failures=$((failures + 1))
      fi
    done
  done
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "all jobs completed successfully"
else
  echo "$failures job(s) returned a non-zero exit; inspect logs in $LOG_DIR and $JOBS_DIR"
fi
exit "$failures"

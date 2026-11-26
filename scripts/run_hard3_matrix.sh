#!/usr/bin/env bash
# Three hard tasks across three harnesses and four model routes.
#
# Every harness runs the two SWE-2 routes and the Engy DeepSeek route. Claude
# Code is left out of the Devin Haiku route on purpose: it is the harness that
# already reaches Anthropic models directly, so a Devin-bridge Haiku row there
# would measure the bridge rather than the harness.
#
# Overridable: TASKS, ATTEMPTS, CONCURRENCY, RETRIES, RUN_ID, DRY_RUN. The
# default RUN_ID is shared by all three invocations so the jobs sort together as
# one batch. Timeouts are the task's own and are never rescaled here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TASKS="${TASKS:-path-tracing-reverse,model-extraction-relu-logits,cancel-async-tasks}"
ATTEMPTS="${ATTEMPTS:-2}"
CONCURRENCY="${CONCURRENCY:-3}"
RUN_ID="${RUN_ID:-hard3-$(date +%Y%m%d-%H%M%S)}"
TRIO="9r/dv-bridge/swe-2-max,9r/dv-bridge/swe-2-flash-max,9r/engy/deepseek-4-1-flash-1017-max"
QUAD="$TRIO,9r/dv-bridge/haiku-5-5-max"

echo "tasks:      $TASKS"
echo "attempts:   $ATTEMPTS per task, $CONCURRENCY concurrent"
echo "run id:     $RUN_ID"
echo "droid:      $QUAD"
echo "amp:        $QUAD"
echo "claude:     $TRIO"

TASKS="$TASKS" MODELS="$QUAD" HARNESSES=droid \
  ATTEMPTS="$ATTEMPTS" CONCURRENCY="$CONCURRENCY" RUN_ID="$RUN_ID" \
  bash "$ROOT/scripts/run_matrix.sh"

TASKS="$TASKS" MODELS="$QUAD" HARNESSES=amp \
  ATTEMPTS="$ATTEMPTS" CONCURRENCY="$CONCURRENCY" RUN_ID="$RUN_ID" \
  bash "$ROOT/scripts/run_matrix.sh"

TASKS="$TASKS" MODELS="$TRIO" HARNESSES=claude \
  ATTEMPTS="$ATTEMPTS" CONCURRENCY="$CONCURRENCY" RUN_ID="$RUN_ID" \
  bash "$ROOT/scripts/run_matrix.sh"

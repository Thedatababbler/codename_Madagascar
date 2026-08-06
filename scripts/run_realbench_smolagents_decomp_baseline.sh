#!/usr/bin/env bash
# RealBench AdaMAS: dynamic TaskPlan + public harness + smolagents_code.
# One-shot only — no fast/slow loop adaptation. See docs/realbench_dynamic_taskplan_harness.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS="${ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS:-1}"
export SMOLAGENTS_MODEL="${SMOLAGENTS_MODEL:-gpt-5-mini}"

RUN_ID="${RUN_ID:-rb-smol-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${OUT_ROOT:-outputs/realbench_smolagents_decomp_baseline}"
LOG_DIR="${OUT_ROOT}/${RUN_ID}"
mkdir -p "${LOG_DIR}"

echo "batch_dir=${LOG_DIR}"
echo "mode=dynamic_taskplan_public_harness"
echo "agent_backend=smolagents_code"
echo "slow_loop=off fast_loop=off"

exec uv run python -m orchestra.cli.run_realbench_codex_decomp_baseline \
  --config configs/experiments/realbench_smolagents_decomp_baseline.yaml \
  --agent-backend smolagents_code \
  --output-root "${OUT_ROOT}" \
  --run-id "${RUN_ID}" \
  "$@" \
  2>&1 | tee "${LOG_DIR}/console.log"

#!/usr/bin/env bash
# RealBench AdaMAS: dynamic TaskPlan + public harness graphs + scheduler.
# Codex subgraphs, fresh thread per agent, no fast/slow loop adaptation.
# See docs/realbench_dynamic_taskplan_harness.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export ADAMAS_CODEX_SANDBOX_OVERRIDE="${ADAMAS_CODEX_SANDBOX_OVERRIDE:-full_access}"
# Belt-and-suspenders with .adamas_trusted_harness written into workspaces.
export ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS="${ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS:-1}"
export CODEX_MODEL="${CODEX_MODEL:-gpt-5.4}"

RUN_ID="${RUN_ID:-rb-decomp-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${OUT_ROOT:-outputs/realbench_codex_decomp_baseline}"
LOG_DIR="${OUT_ROOT}/${RUN_ID}"
mkdir -p "${LOG_DIR}"

echo "batch_dir=${LOG_DIR}"
echo "tasks=encore-ecosystem_NodeFlow AlienMajik_SnoopR benbovy_xproj FreddyRodgers_emojichef dkweiss31_floquet"
echo "mode=dynamic_taskplan_public_harness"
echo "slow_loop=off fast_loop=off codex_thread_policy=fresh"

exec uv run python -m orchestra.cli.run_realbench_codex_decomp_baseline \
  --config configs/experiments/realbench_codex_decomp_baseline.yaml \
  --output-root "${OUT_ROOT}" \
  --run-id "${RUN_ID}" \
  "$@" \
  2>&1 | tee "${LOG_DIR}/console.log"

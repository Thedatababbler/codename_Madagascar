#!/usr/bin/env bash
# Controlled decomposition experiment on CodeProjectEval.
#
# Three arms, all replaying the same frozen planner draft so none of them
# carries planner variance:
#   solo    one agent, whole repository, the plan's combined wall clock
#   single  every agent of the plan, one gate at the end
#   multi   the planner's segmentation, a gate and a commit per milestone
# solo says whether the machinery pays for itself at all; single separates the
# effect of gating from the effect of simply adding agents. Repetitions exist
# because a single run's hidden score is noisy — never report one.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

TASK="${1:?usage: run_codeprojecteval_ab.sh <task> [repeats] [arms]}"
REPEATS="${2:-3}"
ARMS="${3:-solo single multi}"
PLANS="outputs/cpe_ab/plans"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

export ADAMAS_CODEX_SANDBOX_OVERRIDE="${ADAMAS_CODEX_SANDBOX_OVERRIDE:-full_access}"
export ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS=1

for arm in ${ARMS}; do
  for seed in $(seq 1 "${REPEATS}"); do
    run_id="ab-${TASK}-${arm}-r${seed}-${STAMP}"
    echo "===== ${run_id} ====="
    uv run python -m orchestra.cli.run_codeprojecteval_decomp \
      --task-id "${TASK}" \
      --plan-file "${PLANS}/${TASK}.${arm}.json" \
      --arm "${arm}" \
      --output-root outputs/cpe_ab \
      --run-id "${run_id}" || echo "run failed: ${run_id}"
    uv run python scripts/eval_codeprojecteval.py "outputs/cpe_ab/${run_id}" \
      --per-test-timeout 5 --timeout 1200 || echo "eval failed: ${run_id}"
  done
done
echo "done ${TASK}"

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
#
# This is a thin wrapper over run_codeprojecteval_sweep.py, which runs the
# matrix concurrently and writes one joined row per trial. Pass CONCURRENCY=1
# for the old serial behaviour.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

TASK="${1:?usage: run_codeprojecteval_ab.sh <task> [repeats] [arms]}"
REPEATS="${2:-3}"
ARMS="${3:-solo single multi}"
CONCURRENCY="${CONCURRENCY:-4}"

export ADAMAS_CODEX_SANDBOX_OVERRIDE="${ADAMAS_CODEX_SANDBOX_OVERRIDE:-full_access}"
export ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS=1

exec uv run python scripts/run_codeprojecteval_sweep.py \
  --tasks "${TASK}" \
  --arms "${ARMS// /,}" \
  --repeats "${REPEATS}" \
  --concurrency "${CONCURRENCY}"

#!/usr/bin/env bash
# Stage 1 LiveCodeBench — SINGLE-agent baselines (B0 + B1)
#
# B0: direct coder (single call)
# B1: same coder + public harness + one self-repair
#
# Usage:
#   ./scripts/run_stage1_single.sh                  # default: smoke (15 tasks)
#   PHASE=bringup ./scripts/run_stage1_single.sh    # 3 tasks
#   PHASE=dev ./scripts/run_stage1_single.sh        # 60 tasks
#   PHASE=heldout ./scripts/run_stage1_single.sh    # 60 held-out
#   FORCE_RERUN=0 ./scripts/run_stage1_single.sh    # resume if possible
#   MOCK_LLM=1 ./scripts/run_stage1_single.sh       # pipeline check only
#   BASELINES="b0" ./scripts/run_stage1_single.sh   # only B0
#
# Env (optional):
#   OPENAI_API_KEY / OPENAI_BASE_URL via .env
#   LCB_DATA_DIR, LCB_REPOSITORY_PATH / LCB_REPO_PATH
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PHASE="${PHASE:-smoke}"
FORCE_RERUN="${FORCE_RERUN:-1}"
MOCK_LLM="${MOCK_LLM:-0}"
BASELINES="${BASELINES:-b0 b1}"

case "$PHASE" in
  bringup|smoke|dev|heldout) ;;
  *)
    echo "ERROR: PHASE must be bringup|smoke|dev|heldout (got: $PHASE)" >&2
    exit 2
    ;;
esac

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a
  # Support both KEY=val and export KEY=val
  # shellcheck disable=SC1091
  source <(sed -E 's/^export[[:space:]]+//' .env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' || true)
  set +a
fi

echo "============================================================"
echo " Stage1 SINGLE baselines"
echo " PHASE=$PHASE  BASELINES=$BASELINES"
echo " FORCE_RERUN=$FORCE_RERUN  MOCK_LLM=$MOCK_LLM"
echo " ROOT=$ROOT"
echo "============================================================"

echo "[1/4] generate phase configs"
uv run python -m orchestra.cli.stage1_experiments generate-configs

echo "[2/4] validate single graphs"
uv run python -m orchestra.cli.validate_graph --graph configs/graphs/b0_direct.yaml
uv run python -m orchestra.cli.validate_graph --graph configs/graphs/b1_single_harness.yaml

latest_run_dir() {
  local baseline="$1"
  local root="outputs/stage1_experiments/${PHASE}/${baseline}"
  if [[ ! -d "$root" ]]; then
    echo "ERROR: missing output root $root" >&2
    exit 1
  fi
  # Prefer newest directory that has run_manifest.json
  local newest=""
  local newest_mtime=0
  local d
  for d in "$root"/*; do
    [[ -d "$d" ]] || continue
    [[ -f "$d/run_manifest.json" ]] || continue
    local mt
    mt="$(stat -c %Y "$d" 2>/dev/null || stat -f %m "$d")"
    if (( mt >= newest_mtime )); then
      newest_mtime="$mt"
      newest="$d"
    fi
  done
  if [[ -z "$newest" ]]; then
    echo "ERROR: no completed runs under $root" >&2
    exit 1
  fi
  printf '%s' "$newest"
}

run_one() {
  local baseline="$1"
  local config="configs/experiments/stage1/${PHASE}_${baseline}.yaml"
  if [[ ! -f "$config" ]]; then
    echo "ERROR: missing config $config" >&2
    exit 1
  fi

  echo "------------------------------------------------------------"
  echo " RUN  phase=$PHASE baseline=$baseline"
  echo " config=$config"
  echo "------------------------------------------------------------"

  local cmd=(uv run python -m orchestra.cli.run --config "$config")
  if [[ "$FORCE_RERUN" == "1" ]]; then
    cmd+=(--force-rerun)
  fi
  if [[ "$MOCK_LLM" == "1" ]]; then
    cmd+=(--mock-llm)
  fi
  "${cmd[@]}"

  local run_dir
  run_dir="$(latest_run_dir "$baseline")"
  echo "run_dir=$run_dir"

  if [[ "$MOCK_LLM" != "1" ]]; then
    echo " EVALUATE  $run_dir"
    uv run python -m orchestra.cli.evaluate --run-dir "$run_dir" || {
      echo "WARNING: evaluate exited non-zero for $baseline (may have incomplete tasks)" >&2
    }
  else
    echo "skip evaluate (MOCK_LLM=1)"
  fi

  echo " SUMMARIZE  $run_dir"
  uv run python -m orchestra.cli.summarize --run-dir "$run_dir"

  mkdir -p "outputs/stage1_experiments/${PHASE}"
  printf '%s\n' "$run_dir" >"outputs/stage1_experiments/${PHASE}/last_${baseline}_run_dir.txt"
  echo "done baseline=$baseline -> $run_dir"
}

echo "[3/4] run + evaluate + summarize"
for baseline in $BASELINES; do
  case "$baseline" in
    b0|b1) ;;
    b2)
      echo "ERROR: B2 is MAS; use scripts/run_stage1_mas.sh instead" >&2
      exit 2
      ;;
    *)
      echo "ERROR: unknown baseline '$baseline' (expected b0 or b1)" >&2
      exit 2
      ;;
  esac
  run_one "$baseline"
done

echo "[4/4] write phase report (available baselines only)"
uv run python -m orchestra.cli.stage1_experiments report --phase "$PHASE" || {
  echo "WARNING: phase report incomplete until MAS/single counterparts exist" >&2
}

echo "============================================================"
echo " SINGLE baselines finished for PHASE=$PHASE"
echo " Outputs under: outputs/stage1_experiments/${PHASE}/"
echo "============================================================"

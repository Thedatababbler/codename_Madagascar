# Stage-2 Pareto Experiment Protocol (M6.2)

## Research question

Given a multi-subtask TaskPlan, can an **opt-in**, configuration-driven
production path run:

```text
TaskPlan → ReadySubtaskScheduler → M5 Slow Loop → M6 Pareto policy
  → M5 transactional activation → realized horizon accounting
  → restart-safe reports
```

without weakening M5 hard safety, without private-label leakage, and with
auditable, deterministic Stage-2 reporting suitable for later real-model
evaluation?

Fixture and smoke results **must not** be interpreted as real-model quality
gains.

## Candidate space

Bounded, deterministic, mutation-only edits over **future unleased** subtasks:

* allowlisted `PendingGraphTemplateEdit` (compile-gated)
* scheduling concurrency alternatives
* serialization groups
* context-budget alternatives
* allowlisted backend assignments
* optional archive-guided replay (declared edit lists only)
* optional two-edit pairs

Never add/delete subtasks or rewire dependencies in M6.2. Required
communication repairs remain hard M5 safety actions and preempt Pareto
preference.

## Online policy versus diagnostic oracle

| Kind | Role |
|------|------|
| Online policy | Estimated objectives + complete frontier + preference selection at Slow Loop time |
| Diagnostic oracle | Per-task realized / post-hoc frontier; labeled `oracle` / diagnostic only |

Oracle results are excluded from online aggregates unless explicitly requested
for diagnostics.

## Objectives and directions

| Objective | Direction |
|-----------|-----------|
| quality | maximize |
| cost | minimize |
| latency | minimize |
| risk | minimize |
| communication_overhead | minimize |

Missing evidence → objective **unavailable** (never invent zeros for
selectability). Partial candidates stay out of the default complete frontier.

## Normalization and reference points

Development-only data may set:

* normalization ranges
* preference reference points
* pricing assumptions
* frozen preference profiles

A frozen calibration artifact is written **before** held-out evaluation.
Held-out reporting refuses mismatched `control_plane_hash` /
`preference_hash` / `objective_hash` / `pricing_version`.

Hypervolume is **not** reported unless a frozen development reference point
is recorded; the current pipeline leaves `hypervolume: null`.

## Preference profiles

`quality_first`, `cost_capped_quality`, `latency_capped_quality`,
`robustness_first`, `balanced_knee`, plus ablations:

* M6 without two-edit candidates
* M6 without archive replay
* scalarized selection without Pareto filtering

All comparable modes share task manifests, model settings, seeds, sandbox,
harness, timeout, and total task-budget definitions.

## Baselines and ablations

1. M5 rule-based Slow Loop (`pareto.enabled=false`)
2. M6 profiles above
3. Ablations listed above

## Dev / held-out separation

Typed split identity is persisted on the run manifest (never inferred from
directory names, CLI flags, or report commands):

```text
split: fixture | development | heldout
```

* `run-fixture` → `split=fixture`
* Development runs → `split=development`
* Held-out runs must explicitly persist `split=heldout`
* `freeze-calibration` consumes development (synthetic fixture-as-dev allowed)
* `report --held-out` consumes held-out runs only; fixture/development are rejected
* A report flag must never relabel a run’s persisted split

Frozen calibration validates every selection-relevant field (hashes, objectives/
directions, normalization for each required objective, preference, pricing,
candidate/graph catalogs, backend/model settings, evaluator identity/version,
benchmark manifest, dataset/split identity, private-data policy). Fail closed on
any mismatch.

* Public/development evidence may inform estimation and selection.
* Held-out results are evaluation-only.
* Hidden/private results never enter generation, estimation, frontier
  construction, or online selection.

## Private-label isolation

Estimator and telemetry admit only `PUBLIC` / `DEVELOPMENT` visibility for
online quality. Hidden/private harness contracts and records are rejected.

## Reporting tables and plots

```text
outputs/stage2_pareto/reports/
  stage2_main_results.csv
  stage2_by_difficulty.csv
  stage2_profile_results.csv
  stage2_decision_summary.csv
  stage2_estimated_vs_realized.csv
  stage2_frontier_points.csv
  stage2_failures.csv
  stage2_summary.json
  stage2_summary.md
  pareto_quality_cost.svg
  pareto_quality_latency.svg
```

Reports are built from run manifests, checkpoints, usage records, delivery
ledgers, Pareto decisions, archives, and `search_traces.jsonl` — never from
console output.

## Cost-per-solved (task-level)

```text
solved_task_count = # root benchmark tasks meeting the public success criterion
cost_per_solved_task = total_attributed_cost / solved_task_count
```

Committed subtasks are not the denominator. If no task is solved, cost-per-solved
is unavailable (not 0 / infinity).

## Deterministic reports

Repeated report generation from identical persisted inputs is byte-identical for
CSV/JSON/Markdown/SVG. Reports use persisted run `started_at` (never wall-clock
`now`). Recovery counts come from persisted recovery events.

## Failure and restart semantics

* Finalize a decision only when activation matches **and** the affected wave is
  behaviorally realized (all affected subtasks terminal under that revision).
* Resume reclaims stale leases via scheduler incarnation ownership.
* Resume must not duplicate decisions, realization records, usage, evaluations,
  or applied revisions.
* Post-activation / mid-wave / post-realization failpoints exercise exact-once.
* Pareto requested but runtime init failure → fail closed.

## Commands

Deterministic fixture validation (no paid API):

```bash
uv run python -m orchestra.cli.stage2_pareto_experiments generate-configs
uv run python -m orchestra.cli.stage2_pareto_experiments validate \
  --config configs/experiments/stage2/m6_balanced_knee.yaml
uv run python -m orchestra.cli.stage2_pareto_experiments dry-run \
  --config configs/experiments/stage2/m6_balanced_knee.yaml
uv run python -m orchestra.cli.stage2_pareto_experiments run-fixture \
  --config configs/experiments/stage2/m6_balanced_knee.yaml
uv run python -m orchestra.cli.stage2_pareto_experiments freeze-calibration \
  --config configs/experiments/stage2/m6_balanced_knee.yaml \
  --run-dir <fixture-run-dir> \
  --output outputs/stage2_pareto/calibration/dev_calibration.json
uv run python -m orchestra.cli.stage2_pareto_experiments report \
  --run-dir <fixture-run-dir>
# Held-out reporting fails closed without a matching frozen calibration:
#   ... report --held-out --calibration <file> --config <matching-config> ...
```

Production opt-in runner (multi-subtask TaskPlan + ReadySubtaskScheduler):

```bash
uv run python -m orchestra.cli.run_m6_orchestra \
  --config configs/experiments/stage2/m6_balanced_knee.yaml \
  --mock-backends
```

### Fixture DAG and concurrency

The Stage-2 fixture uses a fork/join plan (`s1 → {s2,s3} → s4`), not a serial
chain. Effective concurrency is `min(runtime_cap, policy)`. Fixture latency /
cost figures are labeled as fixture estimates, never as real API performance.

### Crash / resume

Failpoints `after_activation_checkpoint`, `after_future_wave_started`, and
`after_realization_persisted` validate exact-once activation and accounting.

Later real-model / held-out runs require explicit authorization, a frozen
calibration artifact, and must not be launched by the Stage-2 CLI defaults.

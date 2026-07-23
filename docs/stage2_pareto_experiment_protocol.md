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

## Failure and restart semantics

* Finalize a decision only when
  `activated_revision_id == active_plan_revision_id`.
* Resume must not duplicate decisions, realization records, or applied
  revisions.
* Post-activation crash resumes from the activated checkpoint.
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
uv run python -m orchestra.cli.stage2_pareto_experiments report \
  --run-dir <fixture-run-dir>
```

Production opt-in runner (multi-subtask TaskPlan + ReadySubtaskScheduler):

```bash
uv run python -m orchestra.cli.run_m6_orchestra \
  --config configs/experiments/stage2/m6_balanced_knee.yaml \
  --mock-llm
```

Later real-model / held-out runs require explicit authorization, a frozen
calibration artifact, and must not be launched by the Stage-2 CLI defaults.

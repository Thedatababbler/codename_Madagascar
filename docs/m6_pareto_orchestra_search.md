# Milestone 6 — Pareto-Guided Runtime Orchestra Search

## 1. Role relative to M4/M5

```text
M4 Fast Loop     — local failed-subtask repair
M5 Slow Loop     — safe future-only global adaptation + transactions
M6 Pareto Search — multi-objective proposal, archive, preference selection
```

M6 does **not** replace the M5 safety path. Feasibility, immutable history,
communication validation, graph materialization, revision staging, atomic
checkpoint activation, and rollback remain authoritative in M5.

M6 decides **which feasible future orchestration candidate** to propose.

## 2. Feasibility boundary

Hard constraints determine feasibility. Infeasible candidates are rejected
before archive insertion (not scored as soft penalties):

* FuturePlanValidator / future-only eligibility
* immutable historical communication
* backend capability / allowlists / real agent-node IDs
* no subtask add/delete / dependency rewiring
* hidden/private evaluator isolation
* context-budget and task-budget feasibility
* conflicting edit pairs (same-node backend, payload remove+upsert, …)

## 3. Complete frontier vs partial archive

```text
complete Pareto frontier
  — feasible, validation_errors == [], all required objectives available,
    non-dominated within the same context and evaluation kind

partial diagnostic archive
  — missing at least one required objective (for example quality without
    public harness history). Never mixed into complete_frontier().

data-collection selection
  — profile_id = data_collection AND allow_partial_objectives = true
  — may select from the partial archive with an explicit audit status

rule-based fallback
  — ParetoConfig.fallback_to_rule_based = true
  — returns FALLBACK_RULE_BASED; Slow Loop may then use M5 rule-based policy

estimated objective
  — candidate-specific forecast for the next decision horizon

realized objective
  — measured after activation using decision-horizon ledger slices

public quality
  — PUBLIC / DEVELOPMENT harness records only

hidden final evaluation
  — HIDDEN / PRIVATE records; rejected from estimator, selector, and online
    realized quality
```

Online selection for default profiles (`quality_first`, `cost_capped_quality`,
`latency_capped_quality`, `robustness_first`, `balanced_knee`) uses **only**
`archive.complete_frontier(context_id, ESTIMATED)`.

## 4. Selection flow

```text
validate candidates
→ estimate objectives (candidate-specific)
→ insert into estimated complete/partial archives
→ complete_frontier(context, ESTIMATED)
→ selector.select(frontier only)
→ return ParetoSelectionProposal (does not mutate live state)
→ M5 prepare_revision_staging + checkpoint commit
→ only then pending_decision / baselines become live
```

Statuses:

```text
selected_complete_frontier
selected_partial_for_data_collection
no_comparable_candidate
fallback_rule_based
```

## 5. Candidate-specific estimation

Estimates are incremental for the candidate’s next decision horizon.

* **Cost** — historical BackendUsageRecord by backend/model (+ verifier/retry
  and control overhead). Never lifetime spent cost.
* **Latency** — deterministic critical-path / concurrency-aware wall estimate,
  not the sum of node times.
* **Risk** — edit-conditioned (backend switch, serialization, context reduce).
* **Quality** — public/development harness history only; otherwise unavailable
  (candidate stays partial; no invented neutral score).
* **Scheduling-only quality inheritance** — when the mutation is concurrency-only
  (cannot alter output-producing agents/prompts/tools/models/memory/graph/
  verification) and a current public quality measurement exists, quality may be
  inherited as an explicit estimate with provenance
  `inherited_quality_neutral_scheduling_change`. Never invent zero/fixture/
  private substitutes.

### Public harness evidence lifecycle (production path)

```text
committed public harness artifact
→ PublicEvaluationRecord (evaluator_id/version, source_artifact_hash, …)
→ task state + public_evaluations.jsonl
→ ParetoObjectiveEstimator
→ complete frontier / selection
```

Fixture injection and production commit hooks share
`orchestra.control.pareto.public_evaluation`. Real production mode fails closed
(`no_comparable_candidate` / unavailable quality) when the public evaluator is
genuinely unavailable.

Every estimate records source (`history`, `configured_profile`,
`declared_budget`, `unavailable`), uncertainty, and evidence counts.

## 6. Realized decision-horizon accounting

`ParetoDecisionRecord` stores activation revision, baseline usage / delivery /
commit / public-evaluation indices, and a selected candidate snapshot.

Realized metrics count only rows after those baselines:

* backend / harness / timeout failures from the usage slice
* delivery / aggregation failures from the delivery slice
* canonical conflicts from the commit slice
* **wall_latency** = `completed_at - started_at` (default latency objective)
* cost unavailable if any horizon call lacks cost
* communication = actual delivered projected tokens

## 7. Transactional activation

`ParetoGlobalCandidatePolicy.select()` returns a projection only. Slow Loop
stamps `activated_revision_id` onto the prepared projected state and commits
via M5. Staging / checkpoint / wall-time failure leaves **no** live pending
decision. Finalization requires:

1. `decision.activated_revision_id == state.active_plan_revision_id`
2. **Behavioral realization**: every `affected_subtask_ids` entry reaches a
   terminal state under that activation (activation alone, wave start alone, or
   scheduler return alone are insufficient). Incomplete waves keep the decision
   `pending`; resume finalizes exactly once. If no eligible future wave exists,
   record `censored_no_eligible_wave` rather than fabricating improvement.

### Scheduler incarnation and stale-lease recovery

```text
run_id + scheduler_incarnation + lease_id + lease_owner_incarnation
```

Each `run_task` starts a new persisted incarnation and reclaims only
noncommitted leases owned by previous inactive incarnations. Committed subtasks
never re-execute. Recovery events are persisted (`recovery_events.json`) and
drive report recovery counts.

## 8. Persistence and search traces

Under `outputs/<run>/pareto/`:

```text
archive_events.jsonl
estimated_archive.json
realized_archive.json
decisions.jsonl
search_traces.jsonl
```

Archives and selected-candidate snapshots survive restart. Traces are emitted
on the real controller path (one row per candidate; realization updates linked).

## 9. Decision context fingerprint

`ParetoDecisionContext` includes repository fingerprint, canonical revision,
parent plan/communication hashes (communication content hash when active hash
is missing), committed-subtask fingerprints, eligible futures, objective and
preference hashes, and pricing version.

Candidate IDs are content-addressed: `pareto-<content-hash-prefix>`.

## 10. Telemetry accounting quality

`WaveCommitter` persists `NodeUsageSnapshot` from `NodeExecutionResult`.

```text
0     = provider reported zero
None  = unavailable
```

Cost quality: `exact` / `derived` / `approximate` / `unavailable`.
Pricing registry: `configs/pricing/backend_models.yaml`.

## 11. Codex / backend-agnostic relationship

Pareto controllers contain no `if backend_id == ...` branches. Codex and
CodeAgent remain selectable via allowlists and remain FRESH-only.

## 12. Current limitations

* Speculative multi-candidate global execution is disabled.
* Quality estimates unavailable without public/development evidence.
* Pricing entries may be null → cost unavailable until configured.
* No genetic algorithm / learned proposer in default M6.
* Critical-path latency estimator is deterministic but simplified.
* Auxiliary objectives are optional to avoid near-total non-dominance.

## 13. Modes

```text
M5 rule-based Slow Loop (default when Pareto disabled)
M6 Pareto-guided Slow Loop (candidate_policy / pareto_state.enabled)
M6 Pareto disabled
M6.1 runtime-correct closure (complete frontier + transactional activation)
```

## 14. Closure smoke

```bash
uv run python -m orchestra.cli.run_m6_smoke \
  --config configs/experiments/m6_pareto_smoke.yaml
```

The smoke loads the YAML, runs TaskExecutionState → Slow Loop → frontier
selection → M5 commit → realized finalization → restart recovery, and asserts
search traces plus archive persistence.

## 15. M6.2 production and Stage-2 experiments

M6.2 adds an opt-in, configuration-driven production path and a Stage-2
experiment/reporting layer. Shared typed loader:
`orchestra.experiments.control_plane` (used by smoke, production, and reports).

```text
TaskPlan → ReadySubtaskScheduler → M5 Slow Loop → M6 Pareto policy
  → M5 transactional activation → realized horizon accounting
  → restart-safe reports
```

```bash
# Production multi-subtask runner (fully API-free backends)
uv run python -m orchestra.cli.run_m6_orchestra \
  --config configs/experiments/stage2/m6_balanced_knee.yaml \
  --mock-backends

# Stage-2 CLI (no paid API by default)
uv run python -m orchestra.cli.stage2_pareto_experiments validate \
  --config configs/experiments/stage2/m6_balanced_knee.yaml
uv run python -m orchestra.cli.stage2_pareto_experiments run-fixture \
  --config configs/experiments/stage2/m6_balanced_knee.yaml
uv run python -m orchestra.cli.stage2_pareto_experiments report \
  --run-dir <fixture-run-dir>
uv run python -m orchestra.cli.stage2_pareto_experiments freeze-calibration \
  --config configs/experiments/stage2/m6_balanced_knee.yaml \
  --run-dir <fixture-run-dir> \
  --output outputs/stage2_pareto/calibration/dev_calibration.json
```

### Runtime concurrency semantics (M6.2)

```text
effective_concurrency = min(runtime_concurrency_cap, policy_concurrency)
```

- `runtime.max_concurrent_subtasks` is the typed scheduler cap (manifest field).
- `TaskSchedulingPolicy.max_concurrent_subtasks` is the active policy.
- No-op concurrency candidates are rejected before consuming a revision:
  `NO_EFFECTIVE_RUNTIME_CHANGE`, `NO_FUTURE_PARALLEL_WAVE`, `RUNTIME_CAP_DOMINATED`.

### Fork/join fixture

Stage-2 fixture DAG is `s1 → {s2,s3} → s4`. It starts at effective concurrency 1,
adapts after `s1`, then runs `s2||s3` as one wave. Evidence is persisted in
`concurrency_evidence.json`. Fixture latency numbers are labeled
`fixture_estimate_not_real_model`.

### API-free backends

`--mock-backends` replaces every configured execution backend with deterministic
local implementations (no OpenAI/Codex/smolagents client init). `--mock-llm` is a
compatibility alias with the same full override semantics.

### Environment resolution

`orchestra.settings.resolve_runtime_settings` merges process env + optional
`.env` + defaults into an explicit mapping **without** mutating `os.environ`.
Synthetic formal samples must not inject `LCB_REPOSITORY_PATH`. Importing library
modules never loads environment files.

Protocol: `docs/stage2_pareto_experiment_protocol.md`.

Fixture/smoke results must not be claimed as real-model quality, cost, or latency
improvement.

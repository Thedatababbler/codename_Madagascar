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

## 3. Objective definitions

Default frontier objectives:

| Objective | Direction |
|-----------|-----------|
| quality | maximize |
| cost / cost_usd | minimize |
| latency / wall_latency_seconds | minimize |
| risk / failure_risk | minimize |

Optional (off by default unless configured): communication_tokens /
orchestration_complexity.

Online quality uses only public/development harness evidence. Hidden tests
are forbidden in the runtime decision path.

## 4. Measured vs estimated

```text
EstimatedCandidateArchive  — guides proposal selection
RealizedOutcomeArchive     — decision-horizon measured/derived outcomes
```

Estimated candidates never dominate realized outcomes in the realized archive.

## 5. Pareto dominance

Candidate A dominates B when A is no worse on all enabled objectives and
strictly better on at least one, respecting maximize/minimize and epsilon
tolerances. Unavailable required objectives exclude a candidate from the
complete frontier (never imputed as 0/∞).

## 6. Context-local archives

Dominance is scoped by `ParetoDecisionContext` (parent plan/communication
hashes, committed prefix, eligible futures, triggers, diagnosis, preference
profile, backend capability hash). Cross-context comparison is forbidden.

## 7. Preference profiles

Profiles: `quality_first`, `cost_capped_quality`, `latency_capped_quality`,
`robustness_first`, `balanced_knee`, `data_collection`.

Selection: filter constraints → frontier → profile score → deterministic
tie-break (fewest edits, communication overhead, content hash).

## 8. Decision horizon

A pending decision records baseline usage/delivery/commit indices. At the
next Slow Loop safe checkpoint, realized objectives are computed from
decision-horizon deltas (not lifetime totals).

## 9. Telemetry accounting quality

`WaveCommitter` persists `NodeUsageSnapshot` from `NodeExecutionResult`.
`collect_usage_from_graph_result` prefers snapshots over metadata.

```text
0     = provider reported zero
None  = unavailable
```

Cost quality:

```text
exact       — provider cost present
derived     — exact tokens + configured model prices
approximate — heuristic
unavailable — missing inputs (never invent prices)
```

Pricing registry: `configs/pricing/backend_models.yaml`.

## 10. Hidden-evaluator isolation

Online objectives and candidate validation reject private/hidden artifact
types. Offline experiment reporting may join hidden results later.

## 11. Search trace export

JSONL under `outputs/<run>/pareto/search_traces.jsonl` for future generator
training. **M6 does not train the orchestra generator.** M6 creates the
search traces required for future training.

## 12. Codex / backend-agnostic relationship

Pareto controllers contain no `if backend_id == ...` branches. Codex and
CodeAgent remain selectable via allowlists and remain FRESH-only.

## 13. Current limitations

* Speculative multi-candidate global execution is disabled.
* Quality estimates may be unavailable without historical evidence.
* Pricing entries may be null → cost unavailable until configured.
* No genetic algorithm / learned proposer in default M6.
* Auxiliary objectives are optional to avoid near-total non-dominance.

## 14. Modes

```text
M5 rule-based Slow Loop (default when Pareto disabled)
M6 Pareto-guided Slow Loop (candidate_policy / pareto_state.enabled)
M6 Pareto disabled
```

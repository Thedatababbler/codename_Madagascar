# AdaMAS Experiment Log

> **Canonical experiment history for this repo.**  
> Agents and humans must **read this file before comparing historical performance**, and **append a new entry after every completed experiment run** (real or mock).  
> Do not delete past entries; supersede with a newer entry and cross-link.

| Field | Convention |
|-------|------------|
| IDs | `EXP-YYYYMMDD-NN` (date of run finish, local or UTC noted) |
| Status | `canonical` (use for comparisons) / `smoke` / `mock` / `superseded` / `incomplete` |
| Paths | Repo-relative from AdaMAS root |

## Standing rules — read before launching any RealBench batch

1. **Never run template milestone segmentation as an experiment.** Template
   splitting cuts by public-tree shape (module count), which is not a
   decomposition hypothesis and answers no question we are asking. Risk-first
   dynamic planning is the default and the only mode worth measuring;
   `decomposition.dynamic_planner: true` and `ADAMAS_REALBENCH_DYNAMIC_PLAN=1`
   are now the defaults, and `planner_enabled()` returns true unless explicitly
   disabled.
2. A run that could not reach the planner writes `PLANNER_FALLBACK` next to its
   `plan.yaml` and logs an error. **Such a task is void** — exclude it from
   every comparison and rerun it, never report its numbers as a decomposition
   result.
3. Before reporting a batch, confirm `milestone_plan_draft.json` exists for every
   task. Its absence means the batch silently ran the fallback.
   (Batches `rb-isolated*` through 2026-08-08 all ran the fallback for this
   reason; their per-task numbers say nothing about decomposition.)
4. **Never edit the builder while a batch is in flight, and never average across
   builders.** A frozen plan is not a frozen system: replaying it under a newer
   subgraph builder changes the prompts the agents receive. Runs now record
   their engine (`topology: template:*` = role pool, `dynamic_milestone_agent_chain`
   = the pre-2026-08-09 builder), and `summarize_codeprojecteval_ab.py` refuses
   to average an arm that mixes them — it keeps the majority engine and prints
   what it excluded.

**Last updated:** 2026-08-08 (UTC) — RealBench workspace isolation, first clean batch

---

### EXP-20260809-01 — Reading the first A/B batch: three measurement traps
- **Status:** canonical (methodology, not a result)
- **Date:** 2026-08-09 (UTC)
- **Why it exists:** the first summary of the 18-run batch reported pass rates
  above 1.0 and a large multi-segment win on bplustree. Neither survived
  inspection. All three causes were in the measurement, not in the system under
  test, and each one flattered or damaged one arm specifically.
  1. **Static test counting.** Denominators counted `def test_*`; pytest
     parametrisation expands one function into many cases (bplustree: 59 counted
     vs 356 collected). Fixed by collecting on the reference implementation.
  2. **Unequal compute.** The planner's per-milestone agent cap was also applied
     when reloading a frozen plan, so the merged single-segment arm — which by
     construction puts every agent in one milestone — silently ran three of its
     four agents on bplustree.
  3. **Timeouts scored as zero.** A run whose hidden suite hit the wall clock
     was averaged in as 0.000. Re-scored with a 5s per-test timeout, the two
     affected bplustree runs came back at 0.475 and 0.480 — not 0. The summary
     now excludes unmeasured runs and prints `scored` alongside `n`.
- **Standing rule:** never average a run the harness did not finish measuring.
  "We have no measurement" and "the code scored zero" are different claims, and
  conflating them manufactured a 0.313 effect that does not exist.

### EXP-20260809-02 — Was the decomposition actually templated?
- **Status:** canonical (diagnosis)
- **Date:** 2026-08-09 (UTC)
- **Question:** every plan looked like the old template split — `implementation`
  then `integration`, always two milestones.
- **Evidence that the split decision is real:** across 18 CodeProjectEval
  repositories the planner returned 1 milestone for 12 and 2 for 6, and the
  choice is uncorrelated with size. It refused to split `djangorestframework-simplejwt`
  (30 modules), `cookiecutter` (18) and `xmnlp` (24), and did split
  `trailscraper` (890 LOC) and `zxcvbn` (1402 LOC). A size- or tree-driven
  template would have done the opposite. Risk rationales and focus paths are
  per-repository.
- **Evidence that the *labels* were degenerate, and why:** `role` was a
  three-value enum, the terminal milestone was forced to `integration`, and the
  prompt forbids read-only milestones — so a two-milestone plan had exactly one
  possible role sequence. The label was derived from position, never chosen.
  Sampling until a multi-milestone plan appeared also selected the n=2 stratum.
- **Fix:** the enum is renamed `gate_level` (it only sets harness strictness),
  and agent roles now come from a ten-role pool with per-role prompts, with the
  subgraph topology chosen from a five-template catalogue. See the 2026-08-09
  CHANGELOG entry.
- **Open question this raises:** all six splits put foundation modules first and
  consumers second. That is a defensible risk seam, but it is not yet
  distinguished from a mechanical topological cut of the import graph. Worth
  testing before claiming the planner understands *where* to split, as opposed
  to *whether* to.

### EXP-20260808-04 — CodeProjectEval: controlled single vs multi segment A/B
- **Status:** running
- **Date:** 2026-08-08 (UTC)
- **Design:** the question is whether *gating* helps, not whether more compute
  helps, so the single-segment arm is **derived from** the multi-segment plan by
  merging its milestones. Both arms run the same agents, in the same order, with
  the same token/step budget (`budget_matched: true` recorded per repository);
  they differ only in whether an acceptance gate and a commit sit between those
  agents. Both arms replay a **frozen** planner draft, so planner sampling
  variance sits outside the comparison. 3 repetitions per arm.
- **Population:** simpy (ceiling 1.00), bplustree (0.85), pyjwt (0.41). These
  are the repositories where the risk-first planner reliably finds a gate.
- **Population note:** voluptuous and tinydb were the preferred candidates
  (ceiling 1.00) but the planner returned a single milestone in 4 of 4 samples
  each, so they cannot supply a multi arm. That refusal is itself evidence that
  the planner does not split for the sake of splitting.
- **Gates the planner named:** simpy — the `Environment`/`Event`/`Process`
  execution contract; bplustree — persistent storage and node contracts; pyjwt —
  the algorithm/JWK contract every consumer imports.
- **Read the results as:** within-repository difference between arms, using
  `pass_rate` (passed / tests pytest collects on the reference implementation).
  Never the absolute number alone — see EXP-20260808-03 for why.
- **Two measurement defects found while reading the first 18 runs, both fixed:**
  1. *Denominator.* The ceiling counted `def test_*` statically, but
     parametrisation expands one function into dozens of cases, so reported
     rates exceeded 1.0 (bplustree: 59 counted vs 356 collected). Denominators
     now come from `pytest --collect-only` on the reference repository, cached
     in `outputs/cpe_collect_cache.json`.
  2. *Unequal compute.* `MAX_AGENTS_PER_MILESTONE=3` bounds what the *planner*
     may propose, but it was also applied when reloading a frozen plan. The
     merged single-segment arm concentrates every agent into one milestone, so
     bplustree's control arm silently ran 3 of its 4 agents. The cap no longer
     applies to frozen plans; the first bplustree single-arm triple is **void**
     and was re-run.
  `pass_rate_reachable` is now reported as a clamped optimistic *bound*, not the
  headline: a module predicted unreachable still runs when the agent happens to
  define the undocumented name, which makes its denominator too small.

### EXP-20260808-03 — CodeProjectEval: first end-to-end task
- **Status:** canonical (single task; pipeline validation, not a result)
- **Date:** 2026-08-08 (UTC)
- **Artifacts:** `outputs/codeprojecteval_decomp/cpe-second/`
- **Run:** bplustree, codex_sdk / gpt-5.4, risk-first planner returned **one**
  milestone. The milestone committed with `frozen=true` after passing the
  dataset's visible `check_tests`; 1,132 lines shipped.
- **Hidden eval: 0.000.** Two of eight held-out modules fail at collection:
  `from bplustree.const import TreeConf, ENDIAN` → `ENDIAN` does not exist.
  **`ENDIAN` appears zero times in PRD.md, UML.md, UML_pyreverse.md and
  architecture_design.md.** The held-out suite is the original project's own
  test suite and imports internal names the specification never states, so part
  of the score is unreachable for any system regardless of decomposition.
- **Reporting rule this implies:** never read an absolute hidden pass rate on
  this dataset in isolation. Compare single vs multi milestone **within the same
  repository** under matched budget, and report `failed` separately from
  `error` — a collection ImportError usually means an unstated internal name,
  while an assertion failure is a real behavioural gap.
- **Planner variance:** bplustree planned 2, 2 and 1 milestones across three
  samples. Whether a repository splits is itself stochastic, so an A/B must
  either freeze one plan and reuse it or average several samples per arm.
- **Bug found and fixed:** the shared tree parser counted indentation in plain
  spaces, but `tree` output here indents with non-breaking spaces, so every
  module collapsed to the top level (`const` instead of `bplustree.const`) and
  the first run's milestone failed its import gate for a harness reason. All 18
  repositories now resolve to the package root declared in `config.json`.

### EXP-20260808-02 — CodeProjectEval: does the dataset contain risk gates?
- **Status:** canonical (planning probe only; no generation run yet)
- **Date:** 2026-08-08 (UTC)
- **Artifacts:** `outputs/cpe_env_probe2.json`, `outputs/cpe_planner_probe.json`,
  `docs/codeprojecteval_integration.md`
- **Why:** RealBench hands the public API over in `public_design/` and ships no
  developer-visible tests, so milestone gates there guard a decision the dataset
  already made and grade it against contracts we invented. CodeProjectEval ships
  visible `check_tests` plus a held-out `unit_tests` suite, so gates can run real
  tests and the held-out suite measures whether gating generalises.
- **Environment:** one venv per repository; **11/18 usable** (reference
  implementation green on both suites). Repository pytest configs must be
  neutralised with `-o addopts=` — they bolt coverage thresholds, mypy and
  pycodestyle onto pytest and judge style, not behaviour.
- **Planner probe (2 independent samples over all 18 repos):** splits
  **bplustree, pyjwt, simpy** in both; voluptuous / flask / trailscraper / zxcvbn
  in one of two; the rest stay single. The named gates are real blast-radius
  decisions — on-disk page format, the algorithm→implementation registry and JWK
  contracts, the `Environment`/`Event`/`Process` protocol — not directory cuts.
- **Consequence for experiment design:** the splitting repositories are the
  population where decomposition should pay off and the stable single-milestone
  ones are controls, with the split decided by the planner rather than by us.

### EXP-20260808-01 — RealBench Workspace Isolation, Clean Batch
- **Status:** void as a decomposition result (ran the template fallback — no
  `milestone_plan_draft.json`); still valid evidence for workspace isolation and
  for the per-task variance argument
- **Date:** 2026-08-08 (UTC)
- **Batch path:** `outputs/realbench_codex_decomp_baseline/rb-isolated5-20260808T040317Z`
- **Online:** **frozen 5/5**, zero `backend_run_failed`; planner split NodeFlow / xproj /
  floquet into 4 milestones each, SnoopR and emojichef into 1
- **Hidden eval:** micro **0.292**, macro **0.182**, repo success **0/5**
  | task | this batch | best prior |
  |------|-----------|-----------|
  | NodeFlow | 0.00 (3 collection errors) | 1.00 |
  | SnoopR | 0.40 | 0.30 |
  | xproj | 0.138 | 0.276 |
  | emojichef | 0.370 | 0.438 |
  | floquet | 0.00 (numpy shape bug) | 0.00 |
- **Interpretation:** isolation removed the failure modes it targeted (SnoopR now ships
  `SnoopR.py`, every task freezes), but the decomposed batch still trails the
  `rb-decomp` template batch (0.398). Per-task variance is larger than the batch gap:
  NodeFlow swung 1.00 → 0.00 between two runs of the same code because it re-exported
  `Integer/Float/IF` from `nodeflow/builtin/__init__.py` in one run and not the other.
  Single-run batches cannot separate 0.29 from 0.40 under that variance.
- **Follow-up applied:** agent prompts now state the package-root re-export convention.
  public_design cannot expose this requirement — its UML lists `__init__` with empty
  exports, and the hidden tests import a name (`IF`) the UML never mentions.

### EXP-20260807-03 — RealBench Workspace Isolation, Rerun 2 (credit-truncated)
- **Status:** incomplete (provider billing cut the batch after task 2)
- **Date:** 2026-08-07 (UTC)
- **Batch path:** `outputs/realbench_codex_decomp_baseline/rb-isolated2-20260807T200105Z`
- **Change under test:** everything AdaMAS invents (milestone brief, acceptance
  criteria, contract JSON, cross-milestone memory, check script) is runner-owned and
  prompt-delivered; agent workspaces hold dataset files only. Plus: terminal milestone
  forced to `integration`; single-file modules no longer demanded as packages.
- **Hidden eval:** micro **0.313**, repo success **1/5**
  - NodeFlow 6/6, repo success (planner produced 4 milestones, 3 committed)
  - SnoopR 5/5 → 0.50, up from 3/7 → 0.30 in every earlier batch; the workspace now
    carries `SnoopR.py` instead of the `SnoopR/` package the old contract forced
  - xproj 4/13/12 — cut off mid-run
  - emojichef / floquet — **no code produced at all**
- **Why incomplete:** the provider returned `403 预扣费额度失败, 用户剩余额度 $0.8958,
  需要预扣费额度 $1.0` for the last three tasks. Scored 0 for lack of budget, not
  for behaviour; the batch is not comparable as a whole.
- **False negative found afterwards:** NodeFlow's terminal gate rejected the repo for
  "missing nodeflow.adapter.abstract.Node" while hidden tests pass 6/6 — UML package
  names are basenames and the tree has two `abstract.py`. Fixed via `export_any`;
  the committed repo now passes the gate. Not yet re-run end to end.

### EXP-20260807-02 — RealBench Workspace Isolation, Rerun 1
- **Status:** superseded by EXP-20260807-03
- **Batch path:** `outputs/realbench_codex_decomp_baseline/rb-isolated-20260807T192536Z`
- **Hidden eval:** micro **0.375**, repo success **1/5** (NodeFlow 6/6 recovered from
  the dynplan regression; xproj blocked by a missing `pyproj` in the harness env,
  emojichef lost to a provider 503)

---

### EXP-20260807-01 — RealBench Risk-First Dynamic Milestone Planner
- **Status:** canonical (selected-5 Codex rerun complete; result is a regression)
- **Date:** 2026-08-07 (UTC)
- **Branch / commit:** `m622-production-evidence-closure` @ `3c542fa` + uncommitted
  dynamic-planner work (`milestone_planner.py`, `subgraph_builder.py`)
- **Benchmark / phase:** same selected-5 RealBench level2 tasks, decomposed by the
  risk-first LLM planner (`ADAMAS_REALBENCH_DYNAMIC_PLAN=1`,
  `ADAMAS_REALBENCH_PLANNER_MODEL=gpt-5.4`), per-milestone acceptance contracts and
  generated agent-chain subgraphs
- **Batch path:** `outputs/realbench_codex_decomp_baseline/rb-dynplan-20260807T065503Z`
- **Planner output:** **5/5 tasks collapsed to one milestone with one agent**; every
  rationale claimed no separable risk gate
- **Online:** frozen 5/5
- **Hidden eval:** micro **0.241**, macro **0.080**, repo success **0/5**
  (report: `.../reports/rb-dynplan-20260807T065503Z_hidden_eval.md`)
- **vs `rb-memory-20260806T224858Z`** (0.331) **and `rb-dynamic-20260805T091100Z`** (0.397):
  worst of the three
- **Confound (important):** compute per task dropped ~4x — 3 usage records / ~300 s
  versus 12 records / ~700 s for previously split tasks. The comparison conflates
  "different decomposition" with "much smaller budget", so this run does **not**
  isolate planner quality.
- **Diagnosed causes:**
  1. Planner prompt over-biased toward a single milestone; the collapse rule
     (non-terminal milestones need `risk_rationale`) removes any weakly-argued split.
  2. Single milestone + single agent shrinks the budget instead of redistributing it.
  3. Acceptance checks pinned symbols at their defining modules
     (`nodeflow.node.abstract.Node`) while hidden tests import package re-exports
     (`from nodeflow import func2node`), so the gate stayed green while the real
     contract was missing.
  4. SnoopR regressed to 0.0 by writing `SnoopR/__init__.py` as
     `from proj_clean.SnoopR import *`; that resolves in the online workspace (which
     ships `proj_clean/`) but not under hidden overlay — a workspace-layout leak the
     public harness cannot catch.
- **Notes:** No replan loop; hidden tests stayed offline-only.

### EXP-20260806-01 — RealBench Adaptive + Milestone Contracts + Workspace Memory
- **Status:** canonical (selected-5 Codex rerun complete)
- **Date:** 2026-08-06→07 (UTC)
- **Branch / commit:** `m622-production-evidence-closure` @ `3c542fa`
- **Benchmark / phase:** same selected-5 RealBench level2 tasks as
  `rb-dynamic-20260805T091100Z`, with adaptive milestone split, design-derived
  public milestone contracts, and workspace shared memory scheme A
  (`ADAMAS_CHANGELOG.md` append on commit + inject into next `MILESTONE.md`)
- **Baseline / graph:**
  - config: `configs/experiments/realbench_codex_decomp_baseline.yaml`
  - runner: `scripts/run_realbench_codex_decomp_baseline.sh`
  - model: `CODEX_MODEL=gpt-5.4`
  - compare-to: `rb-dynamic-20260805T091100Z` (micro ≈ 0.397, repo success 0/5)
- **Batch path:** `outputs/realbench_codex_decomp_baseline/rb-memory-20260806T224858Z`
- **Online:** frozen **5/5** (all milestones committed)
- **Adaptive split:** SnoopR + emojichef → single `implement_repository`;
  NodeFlow / xproj / floquet → 4-milestone DAG
- **Hidden eval:** micro **0.331**, macro **0.201**, repo success **0/5**
  (report: `outputs/realbench_codex_decomp_baseline/reports/rb-memory-20260806T224858Z_hidden_eval.md`)
- **vs prior dynamic (`rb-dynamic-20260805T091100Z`):** micro 0.397→0.331 (−0.066);
  SnoopR 0.40→0.50; xproj 0.345→0.069 (main regression); NodeFlow/floquet still 0
- **Notes:** Changelog artifacts present under canonical/workspaces. Codex
  `thread_policy` still `fresh`. No repo-level success; memory+contracts did not
  close the vanilla gap on this micro set.

### EXP-20260805-01 — RealBench Dynamic TaskPlan + Public Harness Wiring
- **Status:** incomplete (engineering delivery; full 5-task Codex rerun not yet
  recorded in this entry)
- **Date:** 2026-08-05 (UTC)
- **Branch / commit:** `m622-production-evidence-closure` (pending push)
- **Benchmark / phase:** RealBench level2 selected-5 path upgraded from fixed
  A/I/V (`keystone=none`) to dynamic milestone TaskPlan + role-graded public
  `repository_test_harness` graphs executed by `ReadySubtaskScheduler`
- **Baseline / graph:**
  - docs: `docs/realbench_dynamic_taskplan_harness.md`
  - plan builder: `src/orchestra/decomposition/realbench_plan.py`
  - public harness: `src/orchestra/realbench/public_harness.py`
  - graphs: `configs/graphs/codex_realbench_public_{discovery,implementation,integration}.yaml`
  - runner: `src/orchestra/cli/run_realbench_codex_decomp_baseline.py`
- **Notes:** Public harness ≠ hidden RealBench tests. Offline eval remains
  `scripts/eval_realbench_codex_decomp_baseline.py`. Prior fixed-plan run
  `rb-decomp-20260803T142700Z` is superseded as the AdaMAS RealBench protocol
  once a new dynamic-plan batch is completed and logged.

### EXP-20260803-04 — M6.2.2 Final Evidence-Integrity Patch
- **Status:** smoke / mock (fixtures + fully mocked backends only; no paid API,
  no real LCB, no private/held-out inference)
- **Date:** 2026-08-03 (UTC)
- **Branch / commit:** `m622-production-evidence-closure` @ start `b18aadc`
- **Benchmark / phase:** canonical selection hash (v3); Git alias fail-closed;
  commit+usage evidence for realization; checkpoint-authoritative reports;
  long-form estimated-vs-realized; real A/B/C ownership recovery
- **Baseline / graph:** starts from `b18aadc`; no M4/M5/M6 redesign; no new
  objectives/candidate families; no fixed-budget real experiments
- **Notes:** Fixture/mock metrics are **not** real-model evidence. Do not claim
  real quality/latency/cost from mocked runs.

### EXP-20260803-03 — M6.2.2 Production Evidence Closure
- **Status:** smoke / mock (fixtures + fully mocked backends only; no paid API,
  no real LCB, no private/held-out inference)
- **Date:** 2026-08-03 (UTC)
- **Branch / commit:** `m622-production-evidence-closure` @ `b18aadc`
  (prior M6.2.2 cost/held-out/attempt/report/ownership slice)
- **Benchmark / phase:** production realized cost attribution; strict held-out
  selection identity; production attempt/wave/revision evidence; checkpoint-
  authoritative reports; ownership-before-mutation
- **Baseline / graph:** starts from `2fac593`; no M4/M5/M6 redesign; no Codex
  hybrid changes; no new objectives/candidate families
- **Notes:** Fixture/mock metrics are **not** real-model evidence.

### EXP-20260803-02 — M6.2.1 Evidence and Report Closure
- **Status:** smoke / mock (fixtures + fully mocked backends only; no paid API,
  no real LCB, no private/held-out inference)
- **Date:** 2026-08-03 (UTC)
- **Branch / commit:** `agnostic` @ start `175f98d` (local
  `m621-evidence-report-closure`, uncommitted)
- **Benchmark / phase:** development-only fail-closed calibration freeze;
  complete frozen-field held-out validation; canonical checkpoint evidence for
  reports; usage retention/merge; run-level scheduler ownership; exact recovery
  counts; wave-bound realization; non-vacuous private-label isolation
- **Baseline / graph:** starts from `175f98d`; no Codex hybrid changes; no new
  Pareto objectives/candidate types; no protocol broadening
- **Notes:** leave uncommitted until review. Fixture metrics are **not**
  real-model evidence. Fixed-budget real M6 development experiments require
  explicit authorization after this closure is green.

### EXP-20260803-01 — M6.2 Final Correctness Closure
- **Status:** smoke / mock (fixtures + fully mocked backends only; no paid API,
  no real LCB, no private/held-out inference)
- **Date:** 2026-08-03 (UTC)
- **Branch / commit:** `agnostic` @ start `5d0a76f` (local `m62-final-closure`,
  uncommitted)
- **Benchmark / phase:** public harness → PublicEvaluationRecord → production
  Pareto; scheduler incarnation stale-lease reclaim; behavioral realization
  gate; typed `fixture|development|heldout` split; expanded calibration gates;
  task-level cost-per-solved; deterministic reports; env resolution without
  `LCB_REPOSITORY_PATH` pollution
- **Baseline / graph:** starts from `5d0a76f`; no Codex hybrid changes; no new
  Pareto algorithms / candidate types / orchestration features / benchmarks
- **Notes:** leave uncommitted until review. Deterministic fixture results are
  **not** real-model quality/latency/cost evidence. Fixed-budget real M6
  development experiments require explicit authorization after this closure is
  green.

### EXP-20260723-02 — M6.2 Production Path + Stage-2 Pareto Experiments
- **Status:** smoke / mock (fixture; no paid or held-out real-model runs)
- **Date:** 2026-07-23 (UTC)
- **Branch / commit:** `agnostic` @ post-`ccdbfe4` (M6.2 uncommitted local work)
- **Benchmark / phase:** opt-in production runner via ReadySubtaskScheduler;
  typed control-plane config; Stage-2 CLI validate/dry-run/run-fixture/report;
  leakage-free calibration gates; deterministic reports
- **Baseline / graph:** starts from `ccdbfe4`; no Codex hybrid changes; no GA/RL
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke \
    --config configs/experiments/m5_slow_loop_smoke.yaml
  uv run python -m orchestra.cli.run_m6_smoke \
    --config configs/experiments/m6_pareto_smoke.yaml
  uv run python -m orchestra.cli.stage2_pareto_experiments validate \
    --config configs/experiments/stage2/m6_balanced_knee.yaml
  uv run python -m orchestra.cli.stage2_pareto_experiments dry-run \
    --config configs/experiments/stage2/m6_balanced_knee.yaml
  uv run python -m orchestra.cli.stage2_pareto_experiments run-fixture \
    --config configs/experiments/stage2/m6_balanced_knee.yaml
  uv run python -m orchestra.cli.stage2_pareto_experiments report \
    --run-dir <fixture-run-dir>
  ```
- **Results:** M6 reachable from `run_m6_orchestra` / Stage-2 fixture on the
  real scheduler path; M5 hard safety remains authoritative; hidden/private
  labels cannot influence selection; reports are artifact-backed and
  deterministic. **Do not claim real-model quality improvement from fixtures.**
- **Artifacts:** `docs/stage2_pareto_experiment_protocol.md`,
  `configs/experiments/stage2/*.yaml`, `outputs/stage2_pareto/`
- **Notes:** leave uncommitted until review; real M6 dev experiments require
  explicit authorization + frozen calibration

### EXP-20260723-01 — M5 Blocked-Wave Recovery + Transactional Slow Loop History
- **Status:** smoke (CI gate; preserves M6/M6.1; no Codex hybrid changes)
- **Date:** 2026-07-23 (UTC)
- **Branch / commit:** `agnostic` @ post-`b9d3c85` (M5 runtime guarantee repair)
- **Benchmark / phase:** scheduler blocked-wave recovery, exact REQUIRED_RULE_MISSING
  repair, Slow Loop history persistence across checkpoint/restart, post-activation
  transaction boundary, M5/M6 smokes
- **Baseline / graph:** starts from `b9d3c85`; M6/M6.1 preserved; no hybrid Codex work
- **Command:**
  ```bash
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke \
    --config configs/experiments/m5_slow_loop_smoke.yaml
  uv run python -m orchestra.cli.run_m6_smoke \
    --config configs/experiments/m6_pareto_smoke.yaml
  ```
- **Results:** All-READY communication-blocked waves invoke Slow Loop at the
  unleased checkpoint, apply exact DeliveryRule repairs, re-preflight, and
  continue; restart preserves blocked-target eligibility; Slow Loop history is
  merged by `record_id` and survives checkpoint exactly once; post-checkpoint
  failures raise `RevisionAlreadyActivated` and never claim
  `keep_previous_plan`; M5 smoke asserts post-revision delivery; M6 smoke green
  including archive resume rehydration of `GlobalCandidate`.
- **Notes / interpretation:** Docs: `docs/m5_slow_global_adaptation.md` (M6
  boundary updated). M6 remains layered on repaired M5 guarantees.

## Quick reference (canonical Stage 1 LCB-dev)

Same manifest `configs/manifests/lcb_dev.json` (60 tasks), `seed=42`, sandbox `lcb_official`, models via contract defaults (`gpt-5-mini` env defaults unless noted).

| Baseline | Hidden pass@1 | Eval n | Repair success | Avg prompt / completion tok | Run dir |
|----------|---------------|--------|----------------|-----------------------------|--------|
| **B0** Direct | **0.776** | 58/60 | — | 773 / 2590 | `outputs/stage1_experiments/dev/b0/stage1_dev_b0-27452d3b-3dfdeb18` |
| **B1** Single+Harness | **0.898** | 59/60 | **0.667** | 1066 / 3143 | `outputs/stage1_experiments/dev/b1/stage1_dev_b1-27452d3b-b1341b1e` |
| **B2** Fixed MAS (structured_llm) | **0.833** | 60/60 | **0.375** | 4182 / 6509 | `outputs/stage1_experiments/dev/b2/stage1_dev_b2-27452d3b-c7dbe5b2` |

**Paired takeaway (57 tasks with B0∩B1∩B2):** B1 best overall; B2 between B0 and B1; B2 repair much weaker than B1 → supports task decomposition + dual-frequency updates over one-shot Fixed MAS comms graph.

**Graph hashes (content):** B0 `3dfdeb18…`, B1 `b1341b1e…`, B2 structured `c7dbe5b2…`. Post-M2 CodeAgent B2 graph is different — do not compare CodeAgent B2 to this B2 row until a new EXP entry exists.

Phase reports: `outputs/stage1_experiments/reports/stage1_dev_main_results.csv`, `stage1_dev_by_difficulty.csv`, `stage1_dev_task_comparison.csv`.

---

## How to append

Copy the template below after each run (evaluate + summarize when applicable):

```markdown
### EXP-YYYYMMDD-NN — short title
- **Status:** canonical | smoke | mock | incomplete | superseded
- **Date:** YYYY-MM-DD (timezone)
- **Branch / commit:** …
- **Benchmark / phase:** …
- **Baseline / graph:** …
- **Config:** path + key knobs (manifest, model env, sandbox, parallelism)
- **Command:** …
- **Run dir:** …
- **Results:** key metrics (pass@1, infra errors, repair, tokens, latency, by-difficulty)
- **Notes / interpretation:** …
```

---

## Entries (newest first)

### EXP-20260721-01 — M6.1 Runtime-Correct Pareto Closure
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-21 (UTC)
- **Branch / commit:** `agnostic` @ post-`7738d10` (M6.1)
- **Benchmark / phase:** complete-frontier selection, candidate-specific
  estimates, public quality ledger, horizon-local realized metrics,
  transactional M5 activation, archive/decision restart recovery, runtime
  search traces, real pipeline smoke
- **Baseline / graph:** starts from `7738d10`; FRESH unchanged; no learned
  generator / GA / speculative global execution
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  uv run python -m orchestra.cli.run_m6_smoke \
    --config configs/experiments/m6_pareto_smoke.yaml
  uv run pytest -q \
    tests/unit/test_m6_telemetry.py \
    tests/unit/test_m6_dominance.py \
    tests/unit/test_m6_archive.py \
    tests/unit/test_m6_candidate_generation.py \
    tests/unit/test_m6_selector.py \
    tests/unit/test_m6_runtime_closure.py \
    tests/integration/test_m6_pareto_runtime.py \
    tests/integration/test_m6_pareto_recovery.py
  ```
- **Results:** Online selection uses only complete frontiers; dominated /
  partial candidates excluded from default profiles; data-collection and
  rule-based fallback are explicit statuses; realized wall latency is
  decision-local; archives + selected snapshot survive restart; M6 smoke
  loads YAML and completes M5-activated Pareto path; M5 smoke still green;
  ruff green. Unit **304** passed; integration **91** passed / **14** skipped.
- **Notes / interpretation:** Docs: `docs/m6_pareto_orchestra_search.md`.
  Repository-level Pareto experiments may begin once this commit is on the
  shared branch.

### EXP-20260720-03 — M6 Pareto-Guided Orchestra Search
- **Status:** superseded by EXP-20260721-01 (utilities landed; runtime closure
  completed in M6.1)
- **Date:** 2026-07-20 (UTC)
- **Branch / commit:** `agnostic` (M6) `7738d10`
- **Benchmark / phase:** node usage persistence, pricing registry, Pareto
  dominance/archives, preference selection, Slow Loop policy integration
- **Baseline / graph:** builds on M5.4; FRESH unchanged; no learned generator
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  uv run python -m orchestra.cli.run_m6_smoke
  ```
- **Results:** WaveCommitter persists NodeUsageSnapshot; missing tokens stay
  None; cost exact/derived/unavailable via pricing registry; estimated vs
  realized archives context-local; preference profiles deterministic; M5
  transaction path remains authoritative; search traces exportable; no GA /
  generator training.
- **Notes / interpretation:** Docs: `docs/m6_pareto_orchestra_search.md`.
  Runtime-correct selection/activation closed in EXP-20260721-01.

### EXP-20260720-02 — M5.4 Final Closure (evidence identity + usage)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-20 (UTC)
- **Branch / commit:** `agnostic` (M5.4)
- **Benchmark / phase:** typed RuntimeEvidenceEvent, active-block fingerprints,
  real attempt identity, BackendUsageRecord ledger, objective accounting quality
- **Baseline / graph:** closes final M5 evidence-consumption gaps; FRESH
  unchanged; M6 not implemented
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Results:** Unresolved active blocks consumed once via fingerprints;
  backend/harness evidence uses real attempt IDs; repeated-failure thresholds
  work; no-safe watermark survives checkpoint resume; controller transaction
  failures do not consume evidence; usage accounting does not overstate
  exactness; M6 not implemented.
- **Notes / interpretation:** Docs: `docs/m5_slow_global_adaptation.md`
  §§11f–11g. M5 final closure.

### EXP-20260720-01 — M5.3 Immutable History and Observation Watermark Closure
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-20 (UTC)
- **Branch / commit:** `agnostic` (M5.3)
- **Benchmark / phase:** CommunicationPlanDelta, historical communication
  immutability, delivery target eligibility, Slow Loop observation watermarks,
  lifetime vs recent telemetry, evidence dedupe
- **Baseline / graph:** closes M5 immutable-history gaps; FRESH unchanged; M6
  not implemented
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Results:** Frozen targets cannot have payload/rule/aggregation/budget
  rewritten; completed/in-flight targets cannot be redelivered; historical
  ledger retained; Slow Loop triggers use recent/active evidence and
  watermarks; `NO_SAFE_FUTURE_EDIT` consumed once; task budget
  `accounting_quality` declared; M6 not implemented.
- **Notes / interpretation:** Docs: `docs/m5_slow_global_adaptation.md`
  §§11c–11e. M5 correctness-complete for immutable history.

### EXP-20260719-01 — M5.2 Runtime Closure
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-19 (UTC)
- **Branch / commit:** `agnostic` (M5.2)
- **Benchmark / phase:** historical communication validation, exact aggregation,
  real agent-node backend adaptation, task budget tracker, complete trigger wiring
- **Baseline / graph:** closes remaining M5 runtime gaps; FRESH unchanged
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Results:** Active plans keep historical contracts; runtime compile scopes to
  current target; aggregation uses exact-set matching; backend candidates resolve
  real nodes; scheduler passes TaskBudgetTracker snapshots; Slow Loop shares
  authoritative checkpoint store; M6 not implemented.
- **Notes / interpretation:** Docs: `docs/m5_slow_global_adaptation.md`.

### EXP-20260718-02 — M5.1 Final Correctness Fix
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-18 (UTC)
- **Branch / commit:** `agnostic` (M5.1)
- **Benchmark / phase:** final graph paths, crash-safe revision activation,
  FinalDeliveryUnit aggregation budget, required condition=false blocking,
  preflight-before-lease
- **Baseline / graph:** closes remaining M5 correctness gaps; FRESH unchanged
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Results:** Active plans store final (non-staging) graph paths; revision
  promote precedes checkpoint activation; orphan revisions are non-active;
  aggregation budgets use final artifact tokens; required condition=false
  blocks targets; delivery preflight runs before lease; M6 not implemented.
- **Notes / interpretation:** Docs: `docs/m5_slow_global_adaptation.md`.

### EXP-20260718-01 — M5 Final Hardening (correctness close)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-18 (UTC)
- **Branch / commit:** `agnostic` (M5 Final Hardening)
- **Benchmark / phase:** DeliveryRule enforcement, ledger replay, required
  fail-closed, strict projection/aggregation, cycle validation, future graph
  materialization, declared-delta validation, atomic revision/checkpoint
- **Baseline / graph:** closes M5 correctness gaps; FRESH backends unchanged
- **Config:** `SlowLoopConfig(enabled=False)` default preserved for M4 paths
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Run dir:** `outputs/m5_slow_loop_smoke` (smoke)
- **Results:** DeliveryRule gates delivery; ledger resume reloads projected
  artifacts; required payloads/fields block targets; projection asserts
  `final_estimated_tokens <= max_tokens`; aggregation executed; cycles rejected;
  backend/model assignments materialize real graph snapshots; undeclared plan
  deltas rejected; revision/checkpoint commit is atomic with rollback on
  checkpoint failure; M4 suite green; M6 not implemented.
- **Notes / interpretation:** M5 correctness-complete. Docs:
  `docs/m5_slow_global_adaptation.md`.

### EXP-20260717-03 — M5 Slow Global Adaptation (engineering gate)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-17 (UTC)
- **Branch / commit:** `agnostic` (M5)
- **Benchmark / phase:** deterministic Slow Loop + communication delivery
- **Baseline / graph:** future-only plan revision; FRESH backends unchanged
- **Config:** `SlowLoopConfig(enabled=…)` default off for M4 paths; smoke enables
  context-pressure update; `configs/experiments/m5_slow_loop_smoke.yaml`
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  uv run python -m orchestra.cli.run_m5_smoke
  ```
- **Run dir:** `outputs/m5_slow_loop_smoke` (smoke)
- **Results:** CommunicationPlan executed with projection/budget/ledger; Slow Loop
  updates only pending/unleased subtasks; leased/committed immutable; invalid
  revisions fail closed; M4 suite green with Slow Loop default-disabled.
- **Notes / interpretation:** M6 Pareto / re-decomposition / committed rollback
  not implemented. Docs: `docs/m5_slow_global_adaptation.md`.

### EXP-20260717-02 — M4 final concurrency + artifact precedence
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-17 (UTC)
- **Branch / commit:** `agnostic` (M4 concurrency close)
- **Benchmark / phase:** trusted `codex_tiny_repo` + fake backends
- **Baseline / graph:** ReadySubtaskScheduler coordinator-owned canonical commits
- **Config:** staging `commit_staging/`; `WorkspaceCommitRecord`; assembler
  precedence `explicit > implicit > root`; `persist_checkpoints=False` in
  scheduler-owned FastLoop
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  ```
- **Run dir:** n/a
- **Results:** parallel sibling non-conflicting changes both land in canonical;
  same-line conflicts fail closed without marking COMMITTED; uncommitted
  candidate artifacts do not propagate; slot conflicts fail closed.
- **Notes / interpretation:** M4 correctness-complete for concurrency/dataflow.
  M5/M6 still unimplemented.

### EXP-20260717-01 — M4 final hardening (correctness close)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-17 (UTC)
- **Branch / commit:** `agnostic` (M4 hardening)
- **Benchmark / phase:** trusted `codex_tiny_repo` fixture + fake backends
- **Baseline / graph:** FastLoopController + ReadySubtaskScheduler correctness fixes
- **Config:** derived `search_cost`; `WorkspaceChangeSet`; post-apply harness;
  canonical task workspace; `SubtaskInputAssembler`; locked concurrent merge;
  `BackendModelPool`; `FastLoopBudgetTracker`
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  ```
- **Run dir:** n/a (deterministic tests)
- **Results:** closes M4 correctness gaps — no cost double-count; tracked+untracked
  winner commit; canonical harness + rollback; upstream artifact/repo propagation;
  parallel checkpoint safety; failed-node targeting; REJECTED audit; budget gates;
  model pools only. M5/M6 still unimplemented.
- **Notes / interpretation:** See `docs/m4_fast_local_adaptation.md`. Real API smoke
  not required for this engineering gate; prior M3.5/M4 fixture paths retained.

### EXP-20260716-03 — M4 Fast Local Adaptation (M4-A engineering gate)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-16 (UTC)
- **Branch / commit:** `agnostic` (M4 implementation)
- **Benchmark / phase:** trusted `codex_tiny_repo` fixture + fake backends
- **Baseline / graph:** `configs/graphs/codex_single_implementer.yaml` + FastLoopController
- **Config:** `FastLoopBudget(max_candidates≤3)`; FRESH-only session policies
- **Command:**
  ```bash
  uv sync --extra smolagents --extra codex
  uv run ruff check .
  uv run pytest -q tests/unit
  uv run pytest -q tests/integration
  ```
- **Run dir:** n/a (deterministic tests; optional `run_m4_smoke` is manual)
- **Results:** unit + integration green with M4 Fast Loop coverage (diagnosis, edits,
  capability filter, workspace isolation, atomic commit, ReadySubtaskScheduler,
  CodeAgent/Codex FRESH paths, harness env redaction, checkpoint resume, infra retry)
- **Notes / interpretation:** AdaMAS retains orchestration/isolation/harness/commit;
  backends own only per-node inner loops. M4-B Codex RESUME/FORK **not** implemented.
  M5 slow update and M6 Pareto archive **not** implemented. Docs:
  `docs/m4_fast_local_adaptation.md`.

### EXP-20260716-02 — M3.5 final hardening / Pre-M4 gate (engineering)
- **Status:** smoke (CI gate; no new real-API accuracy claim)
- **Date:** 2026-07-16 (UTC)
- **Branch / commit:** `agnostic` (this hardening commit)
- **What closed:**
  - Integration CI root cause: shared-UID `RLIMIT_NPROC=32` starves LCB
    `multiprocessing.Manager` on GitHub Actions (no root→nobody drop). Fix raises
    NPROC floor when privileges are not dropped; `/dev/shm` remount kept.
  - `backend_sessions` → `list[BackendSessionRecord]` (node/attempt scoped)
  - Subtask failure semantics (`HARNESS_FAILED` / `SubtaskFailureReason`)
  - Full contract prompt forwarding via `render_agent_request_messages`
  - Integration tests: harness-failure + session persistence (fake Codex)
- **CI commands:** `ruff` + `pytest tests/unit` + `pytest tests/integration`
- **Real Codex smoke:** previously verified as `EXP-20260716-01` (not re-run)
- **M4:** **not implemented** (no fast loop / fork / resume / multi-subtask)

### EXP-20260716-01 — M3.5 Codex tiny-repo smoke (real AsyncCodex)
- **Status:** smoke
- **Date:** 2026-07-16 (UTC)
- **Branch / commit:** `agnostic` @ `11a5120` (smoke run dir from earlier local work)
- **Benchmark:** fixture `tests/fixtures/codex_tiny_repo` (broken `add` → fixed)
- **Graph / plan / config:**
  - `configs/graphs/codex_single_implementer.yaml` (`codex_sdk`)
  - `configs/plans/codex_tiny_repo_single_subtask.yaml`
  - `configs/experiments/m3_5_codex_smoke.yaml`
  - Model: `CODEX_MODEL=gpt-4o-mini` (via graph `${CODEX_MODEL:-gpt-5.4}`)
  - Auth: `OPENAI_API_KEY` + `login_api_key`; `OPENAI_BASE_URL` → Codex `openai_base_url`
  - Sandbox: YAML `workspace_write`; runtime `ADAMAS_CODEX_SANDBOX_OVERRIDE=full_access`
    (host `bwrap`/userns blocked under `workspace_write`)
- **Command:**
  ```bash
  ADAMAS_CODEX_SANDBOX_OVERRIDE=full_access CODEX_MODEL=gpt-4o-mini \
    uv run --extra codex python -m orchestra.cli.run_codex_smoke \
    --config configs/experiments/m3_5_codex_smoke.yaml \
    --run-id m3_5_real_smoke_20260716172719
  ```
- **Run dir:** `outputs/m3_5_codex_smoke/m3_5_real_smoke_20260716172719`
- **Dependency pin:** `openai-codex==0.1.0b3` / `openai-codex-cli-bin==0.137.0a4`
- **SDK kwargs check:** `thread_start` / `run` use **`approval_mode=`** (YAML still
  `approval_policy: never` → `ApprovalMode.deny_all`)
- **Results:**
  - `frozen=True`, `graph_frozen=True`
  - `harness_passed=True` (AdaMAS `repository_test_harness` pytest)
  - non-empty git diff; `patch_hash=e75062da8c777a6032b540dfbbeafce340f25bea400696f77e1cbe2ce1255b44`
  - `thread_id=019f6bf7-fa9b-7052-b1ba-e1476c7eb522`
  - checkpoint: `workspace_ref` + session id (now stored as
    `BackendSessionRecord` list after EXP-20260716-02 schema change)
  - tokens: prompt `78259` / completion `263`; wall latency `47624` ms
  - summary: `tasks/codex_tiny_repo/codex_smoke_result.json`
- **Notes:** Smoke validates control-plane wiring (TaskPlan → AsyncCodex → git
  artifact → trusted-fixture harness → freeze). Not an accuracy benchmark.
  `repository_test_harness` remains trusted-fixture-only (marker
  `.adamas_trusted_harness`), not a low-privilege worker.

### EXP-20260713-06 — Stage1 LCB-dev B2 Fixed MAS (structured_llm)
- **Status:** canonical
- **Date:** 2026-07-13 (run finished ~evening ET / 2026-07-14 UTC window)
- **Branch / commit:** `agnostic` (pre/post `1dda045`; this run used **structured_llm** B2 hash `c7dbe5b2`, not CodeAgent B2)
- **Benchmark / phase:** LiveCodeBench release_v6 / **dev** (60 tasks)
- **Baseline / graph:** B2_FIXED_MAS / `configs/graphs/b2_fixed_mas.yaml` @ hash `c7dbe5b24f995d60…`
  - Agents: `algorithm_analyst`, `edge_case_analyst`, `solution_coder`, `repair_agent` → all `structured_llm`
- **Config:** `configs/experiments/stage1/dev_b2.yaml`
  - Manifest: `configs/manifests/lcb_dev.json`
  - Sandbox: `lcb_official`, per_test 6s, worker wall 60s
  - Runtime: tasks=4, nodes/task=4, llm=8, sandboxes=2
  - Seed: 42
- **Command:** `PHASE=dev ./scripts/run_stage1_mas.sh` (FORCE_RERUN default 1)
- **Run dir:** `outputs/stage1_experiments/dev/b2/stage1_dev_b2-27452d3b-c7dbe5b2`
- **Results:**
  - evaluated **60/60**, hidden pass@1 **0.8333**, infra_errors **0**, incomplete **0**
  - public_pass_before_repair **0.906**, public_pass_after_repair **0.688**
  - repair_trigger **0.133**, repair_success **0.375**
  - avg prompt **4182**, avg completion **6509**, avg wall **~67s**, concurrency speedup **~1.39**
  - by difficulty: easy **1.0** (20), medium **0.9** (20), hard **0.6** (20)
- **Notes / interpretation:**
  - Quality between B0 and B1; **cost/latency ~2–4× B1** with **worse repair success than B1**.
  - High public-before-repair but weak repair → one-shot Fixed MAS graph hard to locally improve; motivates Task IR decomposition + fast/slow loops.
  - **Not** a CodeAgent-MAS result.

### EXP-20260713-05 — Stage1 LCB-dev B1 Single + public harness + ≤1 repair
- **Status:** canonical
- **Date:** ~2026-07-13
- **Branch / commit:** `agnostic`
- **Benchmark / phase:** LCB release_v6 / **dev** (60)
- **Baseline / graph:** B1_SINGLE_HARNESS / `configs/graphs/b1_single_harness.yaml` @ `b1341b1e…`
- **Config:** `configs/experiments/stage1/dev_b1.yaml` + `lcb_dev.json`, sandbox `lcb_official`, seed 42
- **Command:** `PHASE=dev ./scripts/run_stage1_single.sh` (or equivalent stage1 runner)
- **Run dir:** `outputs/stage1_experiments/dev/b1/stage1_dev_b1-27452d3b-b1341b1e`
- **Results:**
  - evaluated **59/60**, hidden pass@1 **0.8983**, infra **0**
  - incomplete: `arc195_e`
  - repair_trigger **0.15**, repair_success **0.667**
  - public_before **0.876**, public_after **0.815**
  - avg prompt **1066**, completion **3143**, wall **~36s**
  - by difficulty: easy **1.0**, medium **1.0**, hard **~0.684**
- **Notes:** Strongest Stage1-dev baseline so far; harness+single repair explains most of B0→B1 lift (~+12pp on evaluated set).

### EXP-20260713-04 — Stage1 LCB-dev B0 Direct coder
- **Status:** canonical
- **Date:** ~2026-07-13
- **Branch / commit:** `agnostic`
- **Benchmark / phase:** LCB release_v6 / **dev** (60)
- **Baseline / graph:** B0_DIRECT / `configs/graphs/b0_direct.yaml` @ `3dfdeb18…`
- **Config:** `configs/experiments/stage1/dev_b0.yaml` + `lcb_dev.json`, sandbox `lcb_official`, seed 42
- **Command:** `PHASE=dev ./scripts/run_stage1_single.sh`
- **Run dir:** `outputs/stage1_experiments/dev/b0/stage1_dev_b0-27452d3b-3dfdeb18`
- **Results:**
  - evaluated **58/60**, hidden pass@1 **0.7759**, infra **0**
  - incomplete: `abc375_c`, `3681`
  - no repair path
  - avg prompt **773**, completion **2590**, wall **~29s**
  - by difficulty: easy **0.85**, medium **1.0**, hard **0.5**
- **Notes:** Cheapest/fastest; paired vs B1 showed B1 rescues with few/no B0-only regressions on shared eval set.

### EXP-20260713-03 — Stage1 LCB-bringup B0/B1/B2 (pipeline check)
- **Status:** smoke / incomplete eval on B0–B1
- **Date:** ~2026-07-13
- **Benchmark / phase:** LCB / **bringup** (3 tasks, `configs/manifests/lcb_bringup.json`)
- **Configs:** `configs/experiments/stage1/bringup_{b0,b1,b2}.yaml`
- **Run dirs:**
  - B0: `outputs/stage1_experiments/bringup/b0/stage1_bringup_b0-67fc21d6-3dfdeb18` (evaluated_tasks 0 in summary)
  - B1: `.../bringup/b1/stage1_bringup_b1-67fc21d6-b1341b1e` (evaluated_tasks 0)
  - B2: `.../bringup/b2/stage1_bringup_b2-67fc21d6-c7dbe5b2` — hidden pass@1 **0.667** (2/3), repair_success **0**
- **Notes:** Use for wiring only; **do not** rank baselines from bringup alone.

### EXP-20260713-02 — BBEH CodeAgent smoke (canonical 3/3)
- **Status:** canonical (M2 smoke)
- **Date:** ~2026-07-13
- **Benchmark:** BBEH 3-task smoke
- **Graph / config:** `configs/graphs/bbeh_single_codeagent.yaml`, `configs/experiments/bbeh_codeagent_smoke.yaml`
  - Backend: `smolagents_code`, tools `python_math` / `calculator` / `final_answer`
- **Run dir:** `outputs/bbeh_codeagent_smoke/bbeh_smoke_m2_final`
- **Redacted summary:** `outputs/bbeh_codeagent_smoke/reports/latest_redacted_summary.json`
- **Results:** execution_success **3/3**, answer_correct **3/3**
  - answers: boolean `E`, counting `131`, arithmetic `-52`
  - usage (aggregate): prompt ~15.4k, completion ~16.1k, latency ~161s
- **Notes:** Validates CodeAgent vertical slice end-to-end. Earlier hardened attempt (`bbeh_smoke_m2_hardened`) was weaker (1/3 correct) — treat as superseded smoke.

### EXP-20260713-01 — BBEH CodeAgent smoke (hardened attempt, superseded)
- **Status:** superseded
- **Run dir:** `outputs/bbeh_codeagent_smoke/bbeh_smoke_m2_hardened`
- **Results:** execution_success 2/3, answer_correct **1/3** (counting ok; arithmetic extraction polluted; boolean failed)
- **Notes:** Superseded by EXP-20260713-02.

### EXP-20260710-ish — Milestone mock / regression harnesses
- **Status:** mock
- **Examples:**
  - `outputs/m11_mock_regression/stage1_b{0,1,2}_*` — M1.1 mock LLM regression
  - `outputs/m2_lcb_regression/stage1_b{0,1,2}_*` — M2 mock regression
  - `outputs/stage1/m1-smoke-b{0,1,2}` — early smokes
- **Notes:** `--mock-llm` / fixture paths only; not for accuracy claims.

### EXP-20260803 — M6.2 correctness closure (fixture / API-free)
- **Status:** engineering validation (not a real-model experiment)
- **Branch base:** `origin/agnostic` @ `46ba11e`
- **Coverage:** fork/join Stage-2 fixture proving selected concurrency changes a
  future scheduler wave; `--mock-backends` API-free override; calibration freeze
  + held-out fail-closed gate; crash/resume failpoints; formal Codex sample with
  M5 enabled under synthetic ProblemArtifact.
- **Notes:** Do **not** claim real quality/cost/latency gains from fixture numbers.
  Real M6 development / held-out experiments remain unauthorized until explicitly
  requested.

### EXP-pending — Stage1 B2 CodeAgent MAS (not yet run as canonical)
- **Status:** incomplete (code shipped on `agnostic` @ `1dda045`, no canonical LCB-dev numbers yet)
- **Graph:** `configs/graphs/b2_fixed_mas.yaml` now uses `smolagents_code` + `final_answer` (structured archive: `b2_fixed_mas_structured.yaml`)
- **Requirement:** `uv sync --extra smolagents`; script `scripts/run_stage1_mas.sh`
- **Action when done:** replace this stub with a full EXP entry and update Quick reference table (separate column or note backend).

---

## Interpretation ledger (durable)

1. **B1 ≫ B0 on LCB-dev:** public harness + one repair is the dominant cheap win.
2. **B2 Fixed MAS (structured) does not beat B1:** higher tokens/latency, lower repair success — Fixed one-shot multi-agent graph is hard to patch after failure.
3. **Architectural implication:** Task/Subtask IR + fast local / slow future updates are motivated by (2), not contradicted by it.
4. **Backend note:** CodeAgent proven on BBEH smoke; LCB B2 CodeAgent still needs its own EXP before any claim vs structured B2/B1.

---

## Maintenance checklist (for agents)

- [ ] Before answering “vs last time / vs B1 / regression?” → read **Quick reference** + matching EXP entries.
- [ ] After any `run` / `evaluate` / `summarize` / smoke finishes → append EXP entry + refresh Quick reference if canonical.
- [ ] Never overwrite historical metrics; mark `superseded` and point to the new ID.
- [ ] Record graph content hash and backend type (`structured_llm` vs `smolagents_code`) every time.

---

## EXP-20260809-03 — The A/B arms were measured on two different builders

**Status:** `superseded` (supersedes the CodeProjectEval A/B numbers in
EXP-20260809-01/02; superseded in turn by the rerun below)

**What happened.** Asked whether the reported A/B results used the new role pool,
the honest answer turned out to be *almost none of them*. Of 18 runs, 17 were
produced by the pre-role-pool builder (`topology: dynamic_milestone_agent_chain`,
roles as free-text titles such as "Low-level storage contract implementer"). One
— `ab-bplustree-single-r3` — started at 01:47 UTC, after the role pool had landed
in the working tree, and ran with `topology: template:chain` and pool roles
(`contract_author, implementer, integrator, integrator`).

**Why it matters.** A frozen plan controls *what* is planned, not *how the agents
are prompted*. The template builder injects each role's own prompt, so r3 was a
different system, not a third sample of the same one. The bplustree single arm
had been averaging 0.480 / 0.096 / 0.199 across two builders and reporting the
mean as one condition.

**Fixes landed.**
- `summarize_codeprojecteval_ab.py` records each run's engine and refuses to
  average an arm that mixes engines; it scores the majority engine and prints the
  excluded runs. With r3 excluded, bplustree single is n=2 (0.288), which is too
  thin and too variable to carry the +0.183 delta previously reported.
- `merge_to_single_milestone` now retargets the merged milestone at the
  extensible `chain` template and rebinds each agent to a slot read from the
  template itself. Without this, a plan whose first milestone used `solo` would
  have dropped every agent past the first on reload — the same class of bug as
  the agent cap in EXP-20260809-01, and it would have hit the control arm only.

**Decision.** Abandon the legacy-builder comparison rather than backfill it. All
three repositories are being rerun on the role-pool builder, both arms, three
repetitions. Legacy plans are kept at `outputs/cpe_ab/plans_legacy/` and legacy
runs remain on disk for reference only.

**What the new planner chose** (first time templates and pool roles are under
test, not just implemented):

| repo | multi arm | single arm (merged control) |
|---|---|---|
| simpy | `review_then_fix` (contract_author → spec_auditor → implementer) then `gate_then_repair` (implementer → gate_repairer) | `chain` of all 5 |
| bplustree | same shape | `chain` of all 5 |
| pyjwt | 2 milestones, 4 agent turns (needed 3 planner samples; it prefers a single milestone) | `chain` of all 4 |

Both arms carry identical agent counts and token budgets. Note one asymmetry to
report rather than hide: `gate_then_repair` lets the multi arm *skip* its
repairer when the mid-milestone gate passes, so the multi arm may spend strictly
less compute than its matched control. Report realized `agent_turns`, not
budgeted ones.

# Changelog

## 2026-08-10 — Make the fast loop search designs, not retries

The fast loop was described as a Pareto search over a milestone's subgraph and
was neither. Candidates varied prompt feedback, session policy, a budget bump
and a model swap — retry parameters, not designs — and `DeterministicCandidate-
Selector` collapsed them with a weighted sum, which returns exactly one winner
from any input and cannot say that two candidates are incomparable. The Pareto
machinery existed but was reachable only from the slow loop, so at the milestone
level no frontier was ever built.

Sizing the search turned up the reason raising `max_candidates` had bought
nothing: `k` was clamped to 3, `model_override` needs a second model in the pool
and Codex offers one, and the verifier was off by default. The effective
candidate count was **2 regardless of configuration**. New edit types were a
precondition for a wider search, not an extra.

Three atomic edits now cover the archive's `local_agent` / `local_edge`
families. `add_role_agent` instantiates any capability from `configs/roles`,
rather than the one hardcoded structured verifier. `rewire_edge` gates an edge on
an upstream failure or clears a condition. `drop_agent` removes a read-only
agent, and matters more than it looks: with no edit that can make a candidate
*cheaper* than its parent, every point on the cost axis is worse-or-equal and the
frontier degenerates into "everything that passed".

`DesignSearchCandidateGenerator` emits one atomic edit per candidate on top of a
shared repair preamble. The preamble — the failure report, and a fresh session —
is held constant rather than varied, because a repair candidate denied the
evidence of what went wrong is strictly worse than the run it replaces, and
paying to measure that is waste rather than a trade-off. A candidate that wins
now names which single edit won.

`ParetoCandidateSelector` builds the frontier on quality / cost / stability and
records it on `FastLoopState`, so a degenerate search is visible in the summary
instead of being inferred from the winner. Selection prefers gate-passing points
— a cheap failure can legitimately sit on the frontier, but committing it would
freeze unaccepted work — and falls back to the scalar selector when the frontier
yields nothing to commit, so switching this on cannot fail a run that would
otherwise have committed.

Epsilon tolerances are configured, not implicit: quality 0.02 (one unit of the
coarsest weighted harness stage rounds to ~0.01), cost $0.08 (~5% of an observed
candidate; one real pair differed by 0.09%, which has to read as a tie),
stability 0 (integer incident counts). Calibrated for candidates costing $1–2 and
documented as such.

Two hazards found by writing the integration test rather than by reasoning:

* `CostRecord.estimated_cost_usd` is a float defaulting to `0.0`, not an
  optional, so an unpriced candidate arrived at the frontier looking free — and
  free dominates everything. Spending tokens while costing exactly zero is now
  read as missing evidence.
* An inserted agent reuses its anchor's contract, and the compiler requires the
  node to declare a slot carrying the contract's input schema. Mirroring only the
  anchor's output produced a graph that failed to compile.

`tests/integration/test_fast_loop_design_frontier.py` drives the real scheduler
with a backend and harness that give each candidate a different score and cost,
and asserts two mutually non-dominating designs survive. It produces a frontier
of `{feedback-only: 0.5 at $0.53, add_dependency_resolver: 0.9 at $1.86}` with
the equal-scoring-but-dearer budget candidate correctly dominated. Unit tests
alone cannot catch a search whose candidates all land on the same point, which is
the failure mode that matters.

`docs/fast_loop_pareto_protocol.md` records the axes, why `latency` is not one of
them, the provisional definition of stability (a monotone inverse of incident
count — revisit once a frontier exists where it separates candidates), the
epsilon derivations, and the measured search cost: 13.5 min / $1.90 for a run
with no repair, plus 13.3 min / $1.52 per candidate. Candidates are evaluated
serially, so `k` multiplies a run's duration; repeats run concurrently, so
wall-clock is one run's duration. `k=3` costs about 13 minutes more than `k=2`,
not double.

### Standing rules

* Check the frontier is not degenerate before believing a tuning result. A
  frontier that always holds every candidate, or always exactly one, is a
  ranking with extra steps.
* Parallelising candidates inside a run is not worth doing. Total agent-minutes
  is fixed, the provider endpoint is the bottleneck, and running repeats
  concurrently already saturates it.

## 2026-08-10 — Credit the candidate that actually won

The first run with the repair loop on rescued a failed milestone and the
objective record said the main path won it. `milestone_objectives` read
`winner_candidate_id`; the state calls it `selected_candidate_id`, so the
`getattr` default turned every repaired milestone into `"main"`. On a tuning
run that field is the one thing the row exists to say.

The unit test agreed with the bug because its `_FastLoop` double declared
`winner_candidate_id` too — the tests and the code had settled on a field the
real schema never had. The double now matches `FastLoopState`.

The stage breakdown also stopped at the first hop: `best_harness_progress`
returned only a score and a stage name, leaving `MilestoneObjective.stages`
permanently empty. It now returns the same shape as `parse_progress`, because
0.62 says little and "contracts 12/19" says where to look.

## 2026-08-10 — Ceilings that would have ended a tuning run without saying so

Turning the repair loop on for the first time meant reading its budget, and
every ceiling in it was sized for milestones far smaller than these. Backend
calls are counted per agent node, so a candidate on a four-agent milestone
spends four of them, while the runner asked for `candidates * 2` — not enough
to pay for one candidate. The wall clock was worse than a cut-off: the
candidate generator clamps each candidate's timeout to it, so the 600s default
would have tuned under a deadline a third of the one the baseline ran with, and
reported the difference as a result. All three ceilings now come off the plan.

The loop also carried its own price list, at $0.15/$0.60 per million against a
model billed at $2.50/$15.00, ignoring the cache hits that are most of a Codex
session's input. Candidate costs now come off the same table as the run's cost
axis, because two disagreeing answers to "what did this candidate spend" is
worse than one.

`run_codeprojecteval_sweep.py` takes `--config` and an arm named `planner` that
samples a plan rather than replaying a frozen one. A probe looking for a
repository whose gate fails has nothing to hold fixed yet, and its runs are
named apart from A/B runs so no summariser can average the two.

## 2026-08-09 — Follow the graded score all the way down before believing in it

The graded harness score was unit tested at every station and still did not
arrive. A scheduler run with the fast loop off — the configuration every A/B
run uses — shows why: the score reached the artifact and stopped there, because
the objective record only ever read fast-loop candidates, and with the loop off
there are none. The axis built to tell two failures apart was blank on exactly
the runs meant to calibrate the tuning loop.

`tests/integration/test_milestone_score_reaches_objectives.py` now runs the
real scheduler against a harness that emits a graded score, and asserts the
score lands on the attempt and in the objective record. Reverting the recording
line turns it red, so it holds the wiring down rather than restating it.

Following that path also surfaced a live crash. The catch-all branch of
`classify_failure` passed `reason=` alongside a `**base_kwargs` that already
contained `reason`, so every failure whose reason matched none of the named
branches raised `TypeError` from inside the error handler. Unclassified
failures were the one case the catch-all existed for, and it was the one case
that could not run.

## 2026-08-09 — Give the fast loop something to climb

Tuning was going to run inside the fast loop, which retries a single milestone
as variant candidates and picks one. Reading its selector first turned out to be
worth the detour: the ordering was quality → cost → tokens, where quality is
`1.0` for a candidate that passed its gate and `0.0` for one that did not. Every
candidate the fast loop ever sees has failed — that is why it is running — so
the first key was constant and the winner was whichever failure was cheapest.
The loop was tuned to fail economically.

The missing axis was a *graded* harness result. `RepositoryHarnessResultArtifact`
carried a boolean and nothing else, so an attempt that compiled, imported every
module and failed two tests was indistinguishable from one that produced no
importable package at all.

The CodeProjectEval harness now scores its stages — compile, imports, contracts,
tests — as ratios and prints one machine-readable line. The weighted total is
`1.0` exactly when everything the level asks for passed, and stages a run never
reached contribute nothing, so stopping early at a cheap stage ranks below
getting through it and failing later. Two deliberate exceptions: an altered
visible test suite scores zero on tests rather than by ratio, because a loop
that could climb by editing tests would learn to do that; and modules skipped
for an absent third-party dependency leave the denominator rather than counting
against the repository.

The three tuning axes, all known the moment a milestone's gate has run:

* **gate** — a hard partition, not a term in the sum. No amount of cheapness
  promotes a failing candidate above a passing one unless
  `allow_cost_to_outrank_gate` is set, which is off by default.
* **harness score** — the graded result above. This is what separates two
  failures.
* **tokens** — normalised against a reference budget and weighted at 0.05, so a
  candidate must spend a whole reference budget more to give up that much
  score. Buying a pass with tokens is the trade being measured, not one to
  optimise away.

Absent by design is the held-out pass rate. It is not visible at milestone time,
and a loop that could see it would be tuning on the test set.

A harness that reports no score at all — a plain pytest command — is recorded as
having none rather than as scoring zero, so a passing candidate under it cannot
rank below a partially-failing one that happened to run under a harness that
grades itself.

Per-milestone objective rows are written to every run's summary even when the
loop is switched off, so a tuning loop can be calibrated against runs that were
not themselves tuned. The loop itself stays off in the A/B configuration:
repair firing in one arm and not the other would be measured as part of that
arm.

## 2026-08-09 — Run the trial matrix concurrently

The A/B driver was a shell loop, so a three-repository three-arm sweep at n=3
took the better part of a day. Nothing about a trial requires that: each one is
a separate process over its own output directory. What made serial execution
*safe* rather than merely slow was a single piece of shared mutable state — the
collected-test-count cache — which is read-modify-written by every scoring run
and would silently lose entries under concurrency.

`scripts/run_codeprojecteval_sweep.py` runs the matrix with a concurrency limit
(default 4, sized to the model endpoint rather than the host) and writes one
joined row per trial. Previously the run summary, the hidden score and the token
usage lived in three files and every consumer rediscovered how to join them; a
tuning loop cannot afford that. Each row carries pass rate, tokens, cost,
wall clock, planned vs. realised agent turns, gates passed and failed, and a
status that distinguishes an unmeasured run from a scored zero.

`scripts/run_codeprojecteval_ab.sh` is now a thin wrapper over it, so there is
one implementation rather than two that drift. `CONCURRENCY=1` restores serial
behaviour.

The cache moved behind `orchestra.codeprojecteval.shared_cache`, which holds a
`flock` around the read-modify-write and replaces the file atomically. The
expensive computation deliberately runs *outside* the lock: holding it across a
multi-minute pytest collection would serialise every concurrent run behind the
first, which is the opposite of the point. A racing writer may compute the same
value twice; it cannot corrupt the file.

Repeats are capped at 5. Anything larger is more compute than this experiment
has agreed to spend, and a driver that will happily accept `--repeats 10` is how
that gets spent by accident.

## 2026-08-09 — Give the cost axis real prices

`estimated_cost_usd` was null on every record, so the Pareto frontier had no
cost dimension at all. Three separate things were missing, and fixing any one of
them alone would not have produced a number:

* **No prices.** The registry listed `gpt-5-mini` and `gpt-5-codex` with null
  rates and did not mention `gpt-5.4` at all — the model every RealBench and
  CodeProjectEval run actually uses. It now carries OpenAI's public list prices
  ($2.50/M input, $0.25/M cached input, $15/M output for `gpt-5.4`).
* **No model name.** The Codex backend never stamped one into its metadata, so
  the registry lookup could not be attempted even once prices existed.
* **No cached-token count.** The SDK reports `cached_input_tokens` and the
  backend discarded it. This is not a rounding detail: a Codex session re-sends
  its transcript every turn, so most of its input is cache hits billed at a
  tenth of the rate, and ignoring it overstates cost by several times.

Telemetry events now carry `cached_tokens`, `model_name` and `cost_quality`, and
the node is priced where the model name is still in scope rather than leaving it
to whoever reads the log later.

Costs are labelled rather than presented as invoices. A run with no cached-token
figure is marked `upper_bound`, because list-price input is a ceiling for a
cache-heavy session; the historical A/B runs all fall in this category. Two
further gaps are documented in the pricing file: the long-context tier above
272K input tokens cannot be detected from per-session aggregates, and Batch's
50% discount does not apply to interactive runs.

For scale, the pyjwt arms price out at roughly $1.39 (solo), $2.83 (multi) and
$10.81 (single) as upper bounds — the single-segment arm spends four times the
multi-segment one and scored zero.

## 2026-08-09 — A role pool and subgraph templates the planner selects from

Every plan came back looking templated — always `implementation` followed by
`integration` — and it was, but not where it appeared to be. The *split*
decision was genuinely content-driven (12 of 18 CodeProjectEval repositories got
one milestone, and the ones that split were not the large ones). What was
degenerate was the vocabulary: `role` was a three-value enum that selected the
harness level, the terminal milestone was forced to `integration`, and read-only
milestones were forbidden, so a two-milestone plan had exactly one possible role
sequence. The label described nothing and the planner never chose it.

Roles are now real objects with their own prompts, and the topology is chosen
from a catalogue instead of being the same chain every time. Both follow the
fixed-pool-plus-selection design of EvoMAS (arXiv:2605.08769), and the catalogue
is restricted to shapes this runtime can actually execute.

### Added

- `configs/roles/*.yaml`: a pool of ten capability roles, each carrying its own
  prompt, budget defaults and an `edits_repository` flag — `implementer`,
  `contract_author`, `test_driven_implementer`, `integrator`, `gate_repairer`,
  `edge_case_hardener`, `dependency_resolver`, `scope_pruner`, plus the
  read-only `spec_auditor` and `contract_critic`. Loaded by
  `orchestra.roles.pool`; the planner picks a role per slot and an unknown pick
  degrades to the slot's default rather than failing the plan.
- `configs/subgraph_templates/*.yaml`: five topologies loaded by
  `orchestra.roles.templates` — `solo`, `chain`, `gate_then_repair`,
  `review_then_fix`, `parallel_audit`. A template declares slots, edges and
  where the gate sits; the builder compiles it into the runtime graph.
- `gate_then_repair` implements early exit on a passing gate. The acceptance
  harness runs straight after the first agent; its result feeds the repair
  slot's input only on failure, so a milestone that passes first time freezes
  immediately and never spends the second agent's budget. This uses conditional
  edges into a shared input slot, which the scheduler already supported and
  nothing used.
- Templates are validated against the runtime's real constraints: two agents
  that may edit the repository can never share a parallel wave, because a
  milestone's agents share one workspace. Only read-only roles may fan out,
  which is what makes `parallel_audit` safe.

### Changed

- `MilestoneDraft.role` is now `gate_level`, named for the only thing it
  controls — how strict the acceptance gate is. `.role` remains as a read-only
  alias so existing callers and frozen plans keep working.
- Generated agent contracts embed the selected role's prompt, and a read-only
  role is told it reports rather than edits; its node is also exempted from the
  `require_git_diff` check that would otherwise score correct behaviour as a
  failure.
- A fan-in agent receives each upstream report in its own input slot. Sharing
  one slot would have silently delivered whichever edge resolved first.
- A template slot marked `required` no longer synthesises an agent the planner
  did not ask for; it describes the shape the template was designed around, and
  filling it would hand the milestone unplanned budget.

## 2026-08-09 — Honest denominators and equal-compute arms

Two defects that corrupted the first CodeProjectEval A/B batch, both found by a
reported pass rate above 1.0.

### Fixed

- Hidden-eval denominators come from `pytest --collect-only` on the reference
  implementation instead of a static count of `def test_*`. Parametrisation
  expands one function into many cases, so the static count undercounted
  bplustree by 6x and put pass rates above 1.0. Counts are cached per repository
  in `outputs/cpe_collect_cache.json` (`orchestra.codeprojecteval.ceiling.collected_counts`).
- `MAX_AGENTS_PER_MILESTONE` no longer clamps a frozen plan reloaded from disk.
  The cap bounds what a *planner* may propose per milestone; the merged
  single-segment arm of an A/B test concentrates every agent into one milestone
  by construction, so the cap was handing the control arm less compute than the
  arm it is compared against. `parse_plan_payload` takes `max_agents`, and
  `ab.load_draft` raises it to the frozen plan's own widest milestone.
- `pass_rate_reachable` is clamped to 1.0 and reported as an optimistic bound
  rather than the headline metric: a module predicted unreachable still runs
  when the agent happens to define the undocumented symbol.

## 2026-08-08 — CodeProjectEval: milestone gates that run real tests

RealBench cannot answer whether milestone gating works: it hands the public API
over in `public_design/` and ships no developer-visible tests, so a gate guards a
decision the dataset already made and grades it against contracts we invented.
CodeProjectEval ships a visible `check_tests` suite for development and holds back
a non-overlapping `unit_tests` suite for scoring.

### Added

- `orchestra.codeprojecteval`: dataset adapter (`dataset.py`), runner-owned
  acceptance harness (`harness.py`) and planning brief (`planning.py`). The
  workspace holds design documents, `requirements.txt` and the visible
  `check_tests`; the reference implementation and the held-out suite stay out, and
  AdaMAS-invented content remains runner-owned and prompt-delivered as on RealBench.
- Harness levels: discovery = packages exist + `compileall`; implementation =
  + imports of every module in `directory_tree.txt` + contracts; integration =
  + the dataset's own `check_tests`. The gate runs under the repository's own
  virtualenv so third-party imports resolve the way they will during scoring.
- `scripts/probe_codeprojecteval_env.py` provisions one venv per repository and
  records which repositories are green on the reference implementation (11/18).
- `scripts/probe_codeprojecteval_planner.py` reports how the risk-first planner
  segments each repository before any generation budget is spent.
- `orchestra.cli.run_codeprojecteval_decomp` + `configs/experiments/
  codeprojecteval_decomp.yaml`: milestone execution through `ReadySubtaskScheduler`.
  There is no template fallback on this dataset — a run whose planner is
  unavailable fails instead of silently measuring a shape-based split.
- `scripts/eval_codeprojecteval.py` scores the committed repository on the
  held-out suite. Both suites are re-overlaid from the dataset, so a rewritten
  visible suite cannot survive into scoring.
- The visible suite is hashed into the harness manifest and verified before it
  runs: editing or deleting `check_tests` fails the milestone outright.

### Fixed

- `parse_expected_modules` counted indentation in plain spaces, but `tree`
  output indents with non-breaking spaces depending on locale, collapsing every
  child to the top level (`const` instead of `bplustree.const`) and failing
  import gates for a harness reason rather than an agent one.
- CodeProjectEval trees rooted at the distribution name rather than the package
  (`djangorestframework-simplejwt/` holding `rest_framework_simplejwt`) now take
  their root from `config.json`.

### Changed

- `milestone_planner` takes a `PlanningBrief` (documents + parsed modules +
  exports) instead of reaching into RealBench's `public_design/` layout;
  `build_planner_prompt` keeps its behaviour through `realbench_brief`.
- `build_plan_from_draft` accepts a `harness_binder`, so a dataset with real
  developer-visible tests gates on those rather than on contracts derived from a
  design document.
- Test runs neutralise repository pytest configuration with `-o addopts=`: these
  repositories bolt coverage thresholds, mypy and pycodestyle onto pytest, which
  judges style rather than whether the milestone works.

## 2026-08-07 — RealBench: AdaMAS scaffolding leaves the agent workspace

Everything AdaMAS invents — milestone briefs, contract JSON, the public check
script, the cross-milestone changelog — used to sit in the repository the agent
edits. That leaked twice: the hidden evaluation overlays workspace files into the
private fixture, and agents optimized against our scaffolding instead of the
task (one run shipped `SnoopR/__init__.py` doing `from proj_clean.SnoopR import *`,
green online and broken everywhere else).

The workspace now contains dataset content only (`TASK.md`, `REQUIREMENTS.md`,
`README.md`, `public_design/`, empty scaffold from `tree.txt`). Every AdaMAS
artifact lives under the run directory and reaches the agent as prompt text.

### Changed

- `realbench/public_harness.py`: `materialize_public_harness(workspace,
  harness_dir=...)` writes the check script and manifest outside the repository;
  the script takes `--manifest` / `--contracts` / `--root` and runs against its
  `cwd`, so forked subtask workspaces and commit staging copies share one
  runner-owned asset. Dropped the generated `tests_public/` package and the
  in-workspace trusted marker.
- `realbench/milestone_contracts.py`: contracts are frozen at plan time into
  `<run_dir>/harness/<milestone_id>.contracts.json` and returned with their path;
  no JSON or pytest module is written into the workspace.
- `ir/nodes.py`, `ir/graph.py`, `executors/agent.py`: new optional
  `AgentNodeSpec.prompt_prelude`, excluded from the graph content hash when
  unset, rendered as a user message right after the system prompt.
- `ready_scheduler`: instead of writing `MILESTONE.md` and re-materializing
  contracts into the fork, it injects the milestone brief plus prior-milestone
  memory into the loaded graph's agent nodes. Commits append to
  `<run_dir>/adamas_memory/ADAMAS_CHANGELOG.md` (no workspace commit, so the
  canonical revision no longer moves for bookkeeping).
- `realbench/workspace_memory.py` is now run-directory scoped and prompt-only.
- `decomposition/realbench_plan.py`: both plan builders bind milestones to
  runner-owned harness assets. The template fallback copies its static role graph
  to `<run_dir>/generated/graphs/<milestone_id>.yaml` with an absolute harness
  command; briefs and objectives no longer reference workspace files.
- Agent prompts (generated contracts and the checked-in milestone contracts)
  describe the acceptance check instead of pointing at a script, and forbid
  shipping files the public tree does not describe.
- `tools/repository_tools.py`: `run_public_check` invokes the runner-owned script
  via `ADAMAS_PUBLIC_CHECK_SCRIPT` / `ADAMAS_PUBLIC_CHECK_MANIFEST`; AdaMAS
  filenames stay reserved so an agent cannot fabricate one.

### Fixed

- Deterministic contracts treated every top-level module as a package, so a
  single-file module like `SnoopR.py` demanded `SnoopR/__init__.py` and pushed
  agents into a layout the hidden evaluation never sees.
- Planner focus paths keep the dataset's `proj_clean/` root out of prompts.
- The terminal milestone is always graded at `integration`. Every planner draft
  in the first isolated batch chose `implementation` for its single milestone, so
  the freeze gate never required UML-declared symbols to be importable from their
  documented module and repositories committed with missing package exports.
- Planner prompt now asks for symbols pinned at their documented export module
  (package root when the UML says so), not only at the definition site — the
  NodeFlow failure mode where `from nodeflow import Node` was never satisfied.
- `eval_realbench_codex_decomp_baseline.py` skips AdaMAS filenames when
  overlaying, so a regression cannot reach the private fixture.
- UML package names are bare basenames, so a repository with two `abstract.py`
  files had `Node` demanded from both. The export gate now accepts the symbol in
  any candidate module (`export_any` contract check); NodeFlow's committed repo
  passes hidden evaluation 6/6 yet its freeze gate had rejected it on this.
- The public check no longer fails a milestone for a third-party package the
  harness environment lacks (`pyproj` blocked every xproj commit). Missing
  dependencies the repository does not own are reported as skipped; a missing
  or broken repository module is still a hard failure.

- A dropped response stream consumed the milestone's backend-call budget, so one
  provider blip cascaded into `max_total_backend_calls exhausted` for every later
  milestone. `codex_sdk` now retries transport failures (stream disconnect,
  connection reset, 502/503/504) with backoff — 3 attempts by default,
  `ADAMAS_CODEX_TRANSIENT_RETRIES` to change. Quota, auth and model-behaviour
  failures are still returned immediately.

### Changed

- Risk-first dynamic planning is the default (`planner_enabled()` is true unless
  `ADAMAS_REALBENCH_DYNAMIC_PLAN=0`; the experiment config sets
  `dynamic_planner: true`). Template segmentation cuts by tree shape rather than
  risk, so it is not a mode: it survives only as the fail-closed fallback, and a
  run that reaches it now logs an error and writes `PLANNER_FALLBACK` beside its
  `plan.yaml` so its numbers cannot be mistaken for a decomposition result.

### Notes

- Agent prompts state the package-root re-export convention. public_design cannot
  express it (NodeFlow's UML lists `__init__` with empty exports while the hidden
  tests import `IF` from `nodeflow.builtin`), and the same code re-exported those
  names in one run but not the next — a 1.00 → 0.00 swing on that task alone.
- The harness gate (`.adamas_trusted_harness`) is opened by the runner via
  `ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS=1`; the executed script is no longer
  agent-writable, which is stricter than the previous in-repo copy.

## 2026-08-07 — RealBench: risk-first dynamic milestones with generated subgraphs

Replaces template-shaped decomposition (single milestone vs. directory-sliced
`implement_*` stages) with milestones derived from the task's own risk
structure. A milestone now only exists to fence a decision whose failure would
invalidate downstream work, and it ships both its acceptance harness and its own
runtime subgraph.

### Added

- `realbench/milestone_planner.py` — risk-first planner. Prompts for blast-radius
  gates, forbids directory/package splits and read-only milestones, caps at 4
  milestones, and collapses to a single milestone when no non-terminal milestone
  can justify itself with a `risk_rationale`. `parse_plan_payload` is a pure
  validator: slugified ids, dependencies restricted to already-declared
  milestones (acyclic by construction), clamped token/step/timeout budgets, and
  check sanitization limited to
  `import` / `export` / `callable_or_class` / `module_file_exists`.
  Gated by `ADAMAS_REALBENCH_DYNAMIC_PLAN=1` or
  `decomposition.dynamic_planner: true`; any failure falls back to the
  deterministic public_design plan.
- `realbench/subgraph_builder.py` — one generated subgraph per milestone:
  `agent_1 → … → agent_n → repository_tests → freeze_change`. Agents run in
  sequence over one workspace and only the terminal change reaches the gate.
  Each agent gets a generated `AgentContract` recording role, prompts,
  `max_tokens`, `max_steps` and timeout; the graph metadata carries an
  `agent_roster` with `prompt_sha256` / `prompt_chars` as evidence. Contracts and
  graphs land under `<run_dir>/generated/`, seeded with the base contract catalog
  so baseline graphs keep resolving.
- `decomposition/realbench_plan.py`: `build_plan_from_draft` compiles a planner
  draft into the existing TaskPlan shape (shared `_assemble_plan` handoff wiring),
  with per-milestone acceptance and roster in subtask metadata.

### Changed

- `milestone_contracts.materialize_milestone_contracts` accepts planner
  `extra_checks` / `acceptance_criteria` / `corner_cases`, merging checks by
  target with a union of `required_levels` instead of appending duplicates.
- `ready_scheduler` forwards a milestone's acceptance into contract
  materialization, so `MILESTONE.md` and `adamas_milestone_contracts.json` state
  the same criteria and corner cases the harness enforces.
- RealBench runner returns the effective contracts directory alongside the plan,
  writes `milestone_plan_draft.json`, and records `decomposition_source` plus
  per-milestone agent rosters in `run_config.json`.
- Docs: `docs/realbench_dynamic_taskplan_harness.md` documents both decomposition
  sources, the acceptance harness, and the generated-subgraph contract.

### Notes

- Hidden RealBench tests remain offline-only; planner inputs are public design
  artifacts only.
- No replan loop: milestones are planned once, before execution.
- Agents within a milestone are sequential; parallel agents on one workspace are
  intentionally out of scope.

## 2026-08-06 — RealBench: adaptive milestones + contracts + workspace memory

Close three gaps between AdaMAS RealBench decomposition and vanilla long-session Codex:

1. unnecessary milestone splits on simple repos
2. weak online public harness (compile/import only)
3. fresh-thread milestones with no cross-step shared memory

### Added

- Adaptive TaskPlan in `realbench_plan.py`: simple repos → single
  `implement_repository`; complex trees keep discovery / implementation* /
  integration. Override with `decomposition.force_split: true`.
- Deterministic milestone public contracts from `public_design`
  (`milestone_contracts.py`): writes `adamas_milestone_contracts.json` +
  `tests_public/test_milestone_contracts.py`; optional LLM enrichment via
  `ADAMAS_REALBENCH_LLM_CONTRACTS=1` (whitelist checks, fail-closed).
- Public harness runs milestone contracts by level (`public_harness.py`).
- Workspace shared memory scheme A (`workspace_memory.py`): append
  `ADAMAS_CHANGELOG.md` after successful canonical commit; inject excerpt into
  next `MILESTONE.md` on fork (`ready_scheduler.py`). Smolagents tools may read
  the changelog but cannot overwrite it.

### Changed

- Codex / smolagents RealBench milestone contracts and experiment YAMLs
  (`min_subtasks: 1`).
- Docs: `docs/realbench_dynamic_taskplan_harness.md` (adaptive split, contracts,
  changelog memory).
- Unit tests for plan shape and backend graph selection.

### Notes

- Codex `thread_policy` remains `fresh`; shared memory is workspace changelog,
  not thread resume.
- Hidden RealBench eval stays offline-only.

## 2026-07-16 — Milestone 3.5: Codex second-backend vertical slice

### Added

- Optional extra `codex` pinning `openai-codex==0.1.0b3` (bundled CLI
  `openai-codex-cli-bin==0.137.0a4`).
- `AgentSessionPolicy` / `BackendSessionRef`; M3.5 implements `FRESH` only.
- `SharedSubtaskGitWorkspaceManager` and `RunContext.workspace_ref` plumbing.
- `CodexSDKBackend` + `CodexSDKBackendConfig` (`codex_sdk`).
- `RepositoryChangeArtifact` / `RepositoryHarnessResultArtifact` and
  `repository_test_harness` via harness registry.
- Tiny fixture repo, TaskPlan/graph/experiment configs, and
  `orchestra.cli.run_codex_smoke`.
- Design note: `docs/m3_5_codex_backend.md`.

## 2026-07-13 — Milestone 3: Task/Subtask IR

### Added

- `communication/` stubs: `CommunicationPlan`, payload/aggregation schemas.
- `decomposition/`: `TaskPlan`, `SubtaskSpec`, validator, deterministic
  single-subtask fallback, `TaskDecomposer` (disabled by default).
- `control/`: `TaskExecutionState`, `SubtaskStatus`, single-subtask
  compatibility runner over `NativeAsyncRuntime`.
- `runtime/task_checkpoint.py` for `task_execution.json` (alongside graph
  `checkpoint.json`).
- Opt-in CLI: `python -m orchestra.cli.run_task_plan`.
- Unit/integration tests covering DAG rejection, fallback, resume skip, and
  single-subtask parity with direct graph execution.

## 2026-07-13 — LCB B2 Fixed MAS uses CodeAgent backends

### Changed

- `configs/graphs/b2_fixed_mas.yaml` agent nodes now use `smolagents_code`
  with `final_answer` tools (topology unchanged).
- Legacy structured-llm B2 graph kept as `b2_fixed_mas_structured.yaml` for
  mock/integration tests.
- `orchestra.cli.run` registers `build_default_backend_registry` and injects
  fixture CodeAgent responses under `--mock-llm`.
- CodeAgent worker passes contract `instructions` and JSON-serializes dict/list
  `final_answer` payloads.
- Stage1 MAS script requires the `smolagents` extra.

## 2026-07-13 — CodeAgent hardening (CI, offline integration, exception map)

### Added

- CI installs `uv sync --extra smolagents`.
- Deterministic offline CodeAgent integration test that spawns the real worker
  with a scripted fixture model (no mocked worker result payload).
- `map_exception_to_status` for smolagents typed exceptions
  (model / parse / tool / infra / max-steps).
- Desensitized BBEH summaries: `bbeh_summary_redacted.{json,md}`.

### Changed

- `SmolagentsCodeBackend` keeps the full `final_output` and delegates answer
  extraction to the OutputContract parser.
- README clarifies the CodeAgent worker is crash isolation, not a security
  sandbox.

## 2026-07-10 — Milestone 2: smolagents CodeAgent vertical slice

### Added

- Optional dependency group `smolagents` pinned to `smolagents[openai]==1.26.0`.
- `SmolagentsCodeBackend` with spawn-isolated worker process, wall timeout, and
  process-group cleanup.
- `SmolagentsModelFactory`, ToolRegistry, and first-batch BBEH tools
  (`python_math`, `calculator`, `final_answer`).
- `BBEHTaskAdapter` + `orchestra.cli.run_bbeh` smoke path.
- Graph/experiment configs: `bbeh_single_codeagent`, `bbeh_codeagent_smoke`.
- `FinalAnswerArtifact` and `final_answer` parser.

### Changed

- Graph compiler validates tool allowlists and includes `smolagents_code` when
  the optional dependency is installed.
- `load_graph` expands `${ENV}` placeholders (for CodeAgent model names).

## 2026-07-10 — Milestone 1.1: backend execution boundaries

### Added

- Backend trace propagation into `NodeExecutionResult` and runtime telemetry
  (`backend_run_started` / `backend_step` / `backend_run_completed|failed`).
- Narrow `BackendExecutionContext` (no RunContext / stores / semaphores).
- Capability-based graph compile validation and pre-run backend healthchecks.
- Typed backend exceptions and `SmolagentsCodeBackendConfig` schema
  (forbids `managed_agents`).

### Changed

- `AgentNodeExecutor` preserves node `max_steps` and `tools` without hardcoding
  single-step structured-only behavior.

## 2026-07-10 — Milestone 1: AgentBackend abstraction

### Added

- `orchestra.backends` package with `AgentBackend` protocol, capabilities,
  registry, and `StructuredLLMBackend`.
- Legacy agent graph nodes without `backend` now default to `structured_llm`.
- Graph content hashes remain stable for default structured_llm backends.

### Changed

- `AgentNodeExecutor` now delegates through `AgentBackendRegistry` instead of
  calling the LLM client directly.
- Graph compiler validates that declared agent backends are registered.

## 2026-07-10 — Private request isolation and evaluation status

### Added

- Worker deletes `request.json` immediately after parsing and runs checker code from
  an `execution/` subdirectory so generated code cannot read hidden tests.
- `FinalEvaluationStatus` with `passed`, `wrong_answer`, `code_timeout`, and
  `infra_error` outcomes.
- One automatic retry for private-final worker wall timeouts before marking
  `infra_error`.
- `repair_eligible` harness field and graph routing so missing public tests skip
  repair and freeze the initial code.
- Tests for request deletion, infra-timeout classification, and no-harness repair
  skipping.

### Changed

- `evaluate` CLI excludes `infra_error` results from pass@1 and reports them
  separately.

## 2026-07-10 — Private-final worker and timeout hardening

### Added

- `FinalLCBWorker` and `PRIVATE_FINAL` worker mode for freeze-gated private evaluation.
- Split sandbox timeout configuration:
  - `per_test_timeout_seconds`
  - `worker_grace_seconds`
  - `max_worker_wall_seconds`
- `compute_worker_wall_timeout()` to align outer process-group timeout with the
  official checker budget.
- `function_name` on `AgentVisibleLCBTask` for consistent functional-task routing.
- `harness_available` on `PublicHarnessResultArtifact`; empty public tests no
  longer auto-pass.
- Dedicated CI integration job with pinned LiveCodeBench checkout via
  `LCB_REPOSITORY_PATH`.
- Tests for private-final isolation, multi-test wall timeout, compile-only
  syntax failures (`return`), and no-public-test harness behavior.

### Changed

- `FinalLCBEvaluator` now delegates to `FinalLCBWorker` instead of calling
  `check_correctness` in the main process.
- Public and private workers share the same low-privilege process runner:
  environment redaction, `nobody` drop, RLIMITs, and process-group wall timeout.
- Syntax checks now use `compile(..., "exec")` instead of `ast.parse()`.
- Environment redaction now matches sensitive suffixes (`_KEY`, `_TOKEN`,
  `_SECRET`, `_PASSWORD`) instead of substring matches such as `TOKEN`.

### Security note

The official LiveCodeBench reliability guard is not a complete security
sandbox. The worker backend is intended for controlled research environments
and relies on process isolation, privilege dropping, environment redaction,
resource limits, and timeout termination. Docker should be preferred when
stronger isolation is required.

## 2026-07-10 — Official LiveCodeBench worker backend

### Added

- `OfficialLCBSandbox`, the default development code-execution backend.
- `lcb_worker`, an independent process that invokes the pinned
  `lcb_runner.evaluation.compute_code_generation_metrics.check_correctness`.
- Strict public-test-only worker request schema using `AgentVisibleLCBTask`.
- Worker environment redaction for names containing `KEY`, `TOKEN`, `SECRET`,
  or `PASSWORD`.
- Linux resource limits for address space, processes, open files, and file size.
- Whole-process-group wall-clock timeout and fail-closed worker startup.
- Root-to-`nobody` UID/GID drop before generated code is evaluated.
- Single-thread OpenBLAS/OMP settings for predictable memory use.
- Structured tests for correct, wrong, syntax-error, runtime-error, and timeout
  outcomes, plus environment, resource-limit, private-isolation, and freeze-gate
  tests.

### Changed

- `SandboxBackend` is now the canonical backend abstraction.
- Supported backend values are `lcb_official`, `docker`, and `mock`.
- B0/B1/B2 experiment configs now default to `lcb_official` with:
  - 10-second wall timeout;
  - one evaluator process;
  - 2 GB memory;
  - 32 processes;
  - 128 open files;
  - 16 MB maximum file size.
- Docker remains available as an optional stronger-isolation backend.
- The runtime refuses automatic fallback to an ordinary subprocess.
- `mock` is restricted to explicit `--mock-llm` test runs.

### Security note

The official LiveCodeBench reliability guard is not a complete security
sandbox. The worker backend is intended for controlled research environments
and relies on process isolation, privilege dropping, environment redaction,
resource limits, and timeout termination. Docker should be preferred when
stronger isolation is required.

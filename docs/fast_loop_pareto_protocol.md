# Fast-Loop Pareto Search Protocol

Companion to `docs/stage2_pareto_experiment_protocol.md`, which covers the slow
loop (M6.2, global orchestration search). This document covers the **fast loop**:
Pareto search over the *subgraph design of a single milestone*, decided
immediately after that milestone's acceptance gate reports.

The division of labour is the one in
`AdaMAS_Final_Backend_Agnostic_Dual_Frequency_Implementation.md` §15–§17: the
fast loop searches locally and often, inside one milestone, on evidence that
milestone just produced; the slow loop tunes the whole flow rarely and cheaply.

## What was already true, and what was not

The fast loop shipped as a **retry mechanism**, not a search. Two things made it
so, and both are corrected here:

* **Candidates varied in retry parameters, not in design.** The generated edits
  were prompt feedback, session policy, a budget bump, and a model swap. Only
  `add_verifier_node` changed topology, and it was off by default.
* **Selection was a weighted scalar.** `DeterministicCandidateSelector` ranked on
  a weighted sum, so no frontier was ever constructed at the milestone level. The
  Pareto machinery (`orchestra/control/pareto/`) existed but was reachable only
  from the slow loop.

A third fact, found while sizing the search: with the shipped generator the
effective candidate count was **2, not the configured maximum**. `k` was clamped
to 3, `model_override` needs a second model in the pool (Codex offers one), and
the verifier was disabled — so raising `max_candidates` alone bought nothing.
New edit types are a precondition for a wider search, not an optional extra.

## Objectives

Three axes, per §19 of the design document.

| Objective | Direction | Source |
|-----------|-----------|--------|
| quality | maximize | graded acceptance-harness score of the candidate's own run |
| cost | minimize | attributed USD from the candidate's usage records |
| stability | maximize | execution health, *not* task correctness |

`latency` is deliberately **not** an axis. Wall-clock at this granularity is
dominated by provider queueing, so it reads as noise on the frontier, and cost
already carries the "spent more" signal that latency was standing in for.

Missing evidence makes an objective **unavailable**, and a candidate missing any
configured objective stays out of the complete frontier (`dominance.dominates`
returns `False` on either side). Unknown quality never becomes a value of zero.

### Stability — provisional definition

Recorded as provisional at the user's request; revisit once we have seen a
frontier where stability actually separates candidates.

Stability is a monotone inverse of the count of `StabilityIncident`s raised
during the candidate's execution — config validation, backend initialisation,
action-parse, tool-execution, output-contract, timeout, and infrastructure-retry
events. It measures whether the machinery ran, which is exactly what §20 asks
for, and it is independent of whether the produced code was correct (that is
quality's job).

The known weakness: a candidate that fails fast and cleanly scores *well* on
stability. That is defensible here — a clean failure is genuinely healthier than
a crash — but it means stability must never be read as a proxy for progress.

## Search space — atomic edits

One candidate differs from the parent by **one** atomic edit. Every candidate
also carries a shared repair preamble (the diagnosis as prompt feedback, plus a
fresh session), because a repair candidate denied the failure evidence is
strictly worse than the run it replaces and would waste a paid execution. The
preamble is held constant across candidates, so it is not a variable of the
search; the atomic edit is.

Already implemented: `prompt_feedback`, `session_policy`, `budget_adjustment`,
`model_override`, `add_verifier_node`.

Added for design search, the `local_agent` / `local_edge` families of §15:

| Edit | Effect on the frontier |
|------|------------------------|
| `add_role_agent` | inserts a role-pool agent; buys quality, costs a turn |
| `drop_agent` | removes a non-editing agent; cheaper, possibly worse |
| `rewire_edge` | gates an edge on failure, or serialises a pair; cheaper on the happy path |

`drop_agent` matters more than it looks: without an edit that can *reduce* cost,
every candidate is at least as expensive as the parent and the cost axis cannot
produce a trade-off, which is how a frontier degenerates into "everything that
passed".

Safety invariants hold as they do for templates: `drop_agent` refuses the last
editing agent and any harness node; parallel placement is refused for editing
agents that would share a workspace; harness nodes stay `public`.

## Epsilon tolerances

`dominance.dominates` takes absolute per-objective tolerances. Two candidates
inside epsilon on every axis are mutually non-dominating and both stay on the
frontier, which is the intended behaviour: a difference smaller than the
measurement's own resolution must not decide anything.

| Objective | Epsilon | Why this size |
|-----------|---------|---------------|
| quality | 0.02 | one unit of the coarsest weighted harness stage rounds to ~0.01; below 0.02 the score cannot tell candidates apart |
| cost | 0.08 USD | ~5% of an observed candidate ($1.52 mean on imapclient). A real run pair differed by 0.09%, which must read as a tie |
| stability | 0.0 | integer incident counts; one more incident is a real difference |

These are calibrated to candidates costing $1–2. They live in the experiment
config, not in code, and must be re-derived for a materially different scale.

## Search cost

Candidates are evaluated **serially** inside a run, so the candidate count
multiplies a run's duration. Measured on imapclient (`gpt-5.4`, Codex backend):
a run with no repair is 13.5 min / $1.90, and each candidate adds 13.3 min /
$1.52. The model reproduces the observed `k=2` point (predicted 40 min / $4.93
against measured 40.1 min / $5.08).

| k | one run | n=3 | n=5 |
|---|---------|-----|-----|
| 2 | 40 min, $4.93 | $15 | $25 |
| 3 | 53 min, $6.45 | $19 | $32 |
| 4 | 67 min, $7.96 | $24 | $40 |

Repeats run concurrently, so wall-clock is one run's duration, not the sum;
raising `k` from 2 to 3 costs about 13 minutes of wall-clock, not double.

Parallelising candidates *within* a run is not worth doing. Total agent-minutes
is fixed and the provider endpoint is the bottleneck; running repeats
concurrently already saturates it, so moving the concurrency inward buys little
and adds in-process SQLite contention on the Codex store.

Every candidate re-runs its milestone from the last committed snapshot rather
than from the failed workspace. That is what makes a candidate a *design*
measurement instead of a patch on a broken state, and it is where the 13.3 min
goes. Starting from the failed workspace would be far cheaper and would answer a
different question.

Operational consequences:

* `k=3` for the first real comparison. Under atomic edits three candidates give a
  frontier of up to three points, enough to show whether the frontier is
  informative or degenerate. Widen to 4 only if it is informative.
* `--run-timeout` must rise from 90 min; `k=4` reaches 67 min plus planning.
* The already-paid `k=2` arm on imapclient serves as the control for the
  comparison, with the several-day gap between arms noted in the log.

## Reachability of a search at all

A milestone whose gate passes on the first attempt never enters the fast loop, so
a tuning experiment needs a milestone that fails its gate and whose graded score
has room to move. On the first real tuning run every repair candidate reached a
graded score of 1.0, leaving the selector to decide on cost alone. That is a
degenerate frontier, and it is the outcome this protocol has to be able to
detect and report rather than paper over.

Measured at `k=3` (EXP-20260810-02): the frontier is degenerate, and reproducibly
so. All three candidates scored 1.0 on the gate in all three seeds, leaving cost as
the only live axis and the frontier at one point. The cost model above was
confirmed — 49 min and $6.55 per run against a predicted 53 min and $6.45.

So the binding constraint on the fast loop is not the search space, which now
produces genuinely distinct designs, but the **acceptance gate's ceiling**: a gate
that every repair candidate can max out cannot rank designs, no matter how good the
search over them is. Widening `k` to 4 buys nothing until the gate can separate
candidates.

The gate's 1.0 does not predict held-out quality. Measured offline in
EXP-20260810-03: nine candidates all scored 1.0, and their held-out pass rates
spanned 0.307–0.375. An objective built on a saturating gate measures agreement
with the gate, not task quality.

**The held-out suite is not available as a fix.** Scoring against it — including
against its reachable ceiling — is test leakage: it is the evaluation metric, and
selecting designs on it would void every pass rate we report. It may be used for
evaluation and for offline diagnosis, never inside a selector, an objective, or a
prompt.

The leak-free candidate — scoring the visible `check_tests` as a graded, non-gating
stage at `implementation` level — was measured and does not work either: all three
imapclient candidates pass 9/9 visible tests. No CodeProjectEval task has a visible
suite with both the resolution (~20 units, given epsilon 0.02) and the coverage for
a visible gain to transfer; the best is flask at 27 visible against 375 held out.

Creating a signal with headroom therefore means adding one: richer behavioural
contracts derived from the PRD and UML, or tests authored test-first by a dedicated
role before the implementer runs. Both are design work and neither is a tuning
parameter.

### The authored-suite axis

The second was built: a `test_author` role writes an executable suite from the
design documents under `spec_tests/` before any implementation exists, the
`test_first` template runs it ahead of the builder, and the harness scores the
milestone on the fraction of that suite which passes. It is graded and never
gating, so an unsatisfiable authored test cannot deadlock a milestone.

Two properties of it matter to this protocol specifically:

* **The suite is frozen once per milestone**, in the runner-owned harness
  directory, and every candidate is scored against the copy the first attempt
  wrote. A frontier built from candidates that each authored their own tests would
  be comparing scores from different yardsticks, which is not a frontier.
* **Tests that pass against the repository as shipped are excluded.** Without that,
  a suite of vacuous assertions restores exactly the saturation this section
  documents.

Quality on this axis moves 0.8 against 0.96 for implementations passing 10 and 18
of 20 authored tests, so the 0.02 epsilon is now well inside the axis's resolution
rather than larger than its whole range.

One caution before trusting it for selection: the suite is authored from prose by a
model, so a wrong test makes correct code look broken. That is bias, not noise, and
epsilon does not absorb it. On the first run of a task, compare the authored score
against the held-out rate offline — the same replay-the-patches method as
EXP-20260810-03 — and only then let the axis drive a selection.

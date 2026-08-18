# Fast-Loop Playbook Search — Design

Companion to two documents. `docs/fast_loop_pareto_protocol.md` defines the
frontier, the objectives and the epsilon tolerances, and assumes candidates are
*atomic edits* drawn from a fixed order. `docs/topology_and_edit.md` describes how
a milestone graph is produced and which layer may change its shape. This one
proposes replacing the fixed order with **playbooks keyed on a diagnosed failure
class**: a change chosen because something was measured about *why* the milestone
failed, rather than because it sat early in a hardcoded list.

Nothing here is implemented. Status: design, agreed in outline on 2026-08-17.
The selection half of the loop — frontier, epsilon, `require_gate_pass`, the
incumbent — is unchanged and out of scope.

## What is actually wrong with the search today

Not the frontier and not the objectives. Two things upstream of them:

**The diagnosis does not diagnose.** `diagnose_subtask_failure` is a lookup on
the `SubtaskFailureReason` enum. Each branch returns a hardcoded
`recommended_edit_types` list, and the only evidence that travels with it is
`furthest_stage` plus `failure_message` truncated to 1200 characters. The
per-test failure list is computed (`behaviour_failures`) and reaches the
selector on `CandidateRecord`, but it has never been in any prompt.

**The generator is diagnosis-independent at the budget we run.**
`DesignSearchCandidateGenerator` builds a fixed draft order — feedback-only, add
a role, drop a read-only agent, budget bump, model swap, verifier — and the only
part the diagnosis influences is which role `_ROLE_FOR_STAGE[furthest_stage]`
names. `codeprojecteval_official_search.yaml` sets `fast_loop_candidates: 2`, so
**every paid search so far has generated exactly the first two drafts**:
feedback-only, and add-one-role. The remaining four have never been built. The
search space is wide on paper and two points wide in practice.

A third, smaller: `SubtaskFailureReason.INFRA` returns
`recommended_edit_types=[]` with `infrastructure_related=True`, and both
generators return `[]` on their first line for that. A `SIGXCPU` from a resource
ceiling and a provider outage are the same class today, so the resource case is
never searched at all.

## The shape of the change

Keep the three-step skeleton — diagnose, draft, compile-and-validate — and
replace the first two.

1. **Diagnosis becomes an LLM classification** into `budget`, `functional` or
   `design`, plus an anchor node. It emits a class and evidence, never edits.
2. **Drafting becomes a playbook table** `failure_class -> ordered playbooks`,
   where a playbook is a *sequence* of edits written down in code.

The third step is untouched: every playbook still goes through
`apply_local_edits`, so the DAG check, terminal reachability, the `max_steps`
cap of 64 and the milestone's wall-clock ceiling all still apply, and a playbook
that cannot compile is recorded as `INVALID_GRAPH_EDIT` rather than run.

### Why the LLM may not write the edits

Because the search has to stay enumerable. If the classifier proposes edits it
can name a `role_id` that does not exist, a node that is not in the graph, or a
combination no template can host, and every such candidate costs a real
execution to discover. Restricting it to a label and an anchor keeps the reachable
design space exactly as auditable as it is today, while letting the *choice* among
playbooks depend on evidence.

### What atomic edits bought, and what replaces it

`DesignSearchCandidateGenerator`'s docstring argues for one edit per candidate on
attribution grounds: a candidate bundling three edits that wins tells you nothing
about which edit helped. That argument is sound and a playbook gives it up.

Two things make the trade acceptable. At `k=2` there is no attribution today
either — the two points are "feedback" and "feedback plus one role", so the
comparison already measures a bundle against a preamble. And attribution can be
kept at playbook granularity: `playbook_id` and the full edit list go into
`CandidateRecord.metadata`, win rates aggregate per playbook across runs, and the
feedback-only anchor stays in every draft list as point zero. Without that anchor
there is no way to say what a playbook bought, and it must not be dropped to make
room for a third playbook.

## Diagnosis

New module, `orchestra/control/fast_loop/llm_diagnosis.py`. Input is deliberately
narrow:

* the failure reason and the exit signal name (`SIGXCPU` and friends);
* the harness stage breakdown — stage, passed, total, failing test identities;
* `behaviour_failures` plus the tail of the pytest output;
* a subgraph summary: nodes, roles, models, `max_steps`, `timeout_seconds`;
* remaining milestone budget.

Output is a fixed JSON object: `failure_class`, `confidence`, `target_node_id`,
`rationale`, `evidence`.

Four properties are not negotiable.

**No held-out suite, ever.** Not the dataset's private suite, not
`proj_with_test`, not the visible `check_tests` beyond what the gate already
reports. The diagnoser sees what the gate saw and nothing else. Every pass rate
we report depends on this, and it is the one guardrail whose violation
retroactively voids finished arms rather than just the current one.

**Replayable.** `temperature=0`, and the full prompt and response land in
`fast_loop/diagnosis/<subtask>_<attempt>.json` beside the candidate records.

**Accounted.** There is a hole to avoid copying here: `plan_milestones` is a bare
`client.chat.completions.create` that writes no `BackendUsageRecord`, and no
`accounting_source` value names the planner, so planner spend is currently on no
run's ledger. The diagnosis call must write
`accounting_source="fast_loop_diagnosis"`. A search whose own decision-making is
unpriced cannot be compared against an arm that does not search.

**Fails closed.** Unavailable model, unparseable JSON, a class outside the enum,
or confidence below `min_confidence` all fall back to the existing lookup
diagnosis. Diagnosis failure must not fail the milestone.

### Class boundaries

The boundaries go in the prompt, because they are the whole content of the
classification.

| Class | The milestone looks like | Evidence |
|-------|--------------------------|----------|
| `budget` | the agent did not finish, or finished by abandoning work: timeout, steps exhausted, truncated output, `TODO`/`NotImplementedError` left behind, imports resolve but bodies are empty | exit signal, steps at cap, `furthest_stage` in `compile`/`imports` |
| `functional` | the agent finished and the structure is complete, but the behaviour is wrong | `furthest_stage` reached `tests`/`spec_tests` with a non-empty failure list |
| `design` | failures spread across unrelated areas, or concentrate where this milestone was not meant to be responsible, or the same playbook fails to move them twice | breadth of the failure set, repeat-attempt history |

`design` is the class the search has least ability to act on, and the one this
document spends the most space on, because acting on it is the point of a
topology search.

## Playbooks

New module, `orchestra/control/fast_loop/playbooks.py`, mapping a class to an
ordered list of `(playbook_id, reason, list[LocalEdit])`. The feedback-only
anchor is always index zero and does not count as a playbook.

Two playbooks are worth writing down before the topology ones because they are
cheap and independent of the gaps below.

* `pb_failures_to_agent` — put the per-test failure list into the prompt of
  whichever agent the diagnosis anchors on. Costs nothing structural and closes
  the "computed but never used" gap on `behaviour_failures`.
* `pb_budget_steps_time` — `max_steps` and `timeout_seconds` increases, sized in
  two variants (small: +2 / +30s; large: +8 / +300s), the large one paired with a
  prompt instruction to reach a minimum passing implementation first.

**Token limits are deliberately absent from the budget playbook.**
`ModelSpec.max_tokens` (default 4096) and `RoleSpec.max_tokens` (default 8192)
exist on the node, so the edit would be trivial — but only
`backends/smolagents_model.py` and `backends/structured_llm.py` read the field.
The Codex backend ignores it, and CodeProjectEval and RealBench both run Codex,
so the edit would be a no-op dressed as a change. Revisit if a
`structured_llm` arm is ever tuned.

## Topology playbooks, per template

### Which templates are worth designing for

Measured across every frozen plan in `outputs/*/plans/*.json` on 2026-08-17:

| Template | Milestones |
|----------|------------|
| `test_first` | 54 |
| `solo` | 31 |
| `gate_then_repair` | 5 |
| `review_then_fix` | 3 |
| `chain` | 3 |
| `parallel_audit` | **0** |

`test_first` and `solo` are 89% of the population and `parallel_audit` has never
been chosen by the planner. Playbooks are designed in that order; anything
written for `parallel_audit` first would be dead code.

### How a topology playbook is written

Since shape changes go through the plan layer, a topology playbook is a **target
template plus a slot assignment**, not a list of edits:

```
pb_tf_diagnose_before_repair:
    template: test_first_diagnosed      # new template file
    slots: {test_author: test_author, builder: <keep>, critic: behaviour_critic,
            repairer: gate_repairer}
```

Slots the playbook does not name keep the role the plan gave them, so a playbook
never silently downgrades an agent the planner chose deliberately.

### One structural fact that bounds all of it

Agents in a milestone share one workspace, and `validate_against_pool` refuses a
template that fans out to two repository-editing roles. So a debate in the usual
sense — two implementers each writing a version, then a merge — **cannot exist
inside a milestone**, in either layer. What can exist is several read-only
reviewers in parallel and one editing agent closing over their reports. A judge
position is reachable; a debater that writes code is not.

The read-only pool is also small: of eleven roles in `configs/roles`, only
`spec_auditor` (reads the design documents) and `contract_critic` (reads frozen
contracts) have `edits_repository: false`, and `parallel_audit` already spends
both. A third angle requires a new role.

### `test_first` — 54 milestones

`test_author -> builder -> [early gate] -> repairer (conditional)`. The builder
cannot see the suite by design, so on failure it does not know what it got wrong;
the repairer sees the gate report, but gets one attempt and receives a raw report
rather than a diagnosis.

| Priority | Playbook | Layer | Action |
|----------|----------|-------|--------|
| 1 | `pb_tf_failures_to_repairer` | edit | per-test failure list into the repairer's prompt |
| 2 | `pb_tf_diagnose_before_repair` | plan | a `test_first` variant with a read-only `behaviour_critic` slot in the failure branch, `runs_if_gate_failed: true`, so it costs nothing when the gate passes |
| 3 | `pb_tf_second_repairer` | plan | a second `gate_repairer` slot behind the same condition — two repair rounds |
| 4 | `pb_tf_builder_budget` | edit | steps and wall-clock on the builder; only when the class is `budget` |

Hard invariant: no playbook may put `spec_tests` within the builder's reach.
Recompiling regenerates the custody wiring, so a variant template that drops the
`suite_custody` input or restores a change edge from author to builder would undo
it. Assert it, do not document it.

### `solo` — 31 milestones

`author -> harness`. No second opinion and no second attempt.

The plan layer changes what is reachable here. No *edit* can insert an early
acceptance gate — `AddVerifierNodeEdit` adds a structured verifier, not a harness
— so from inside the edit layer a repairer added to `solo` runs unconditionally
and blind. Recompiling from a different template does create the probe, so
`solo -> gate_then_repair` is a legal playbook and the conditional repair position
is reachable after all.

| Priority | Playbook | Layer | Action |
|----------|----------|-------|--------|
| 1 | `pb_solo_to_gate_repair` | plan | recompile as `gate_then_repair`, author role kept, `repairer: gate_repairer` — a repair pass that costs nothing when the gate passes |
| 2 | `pb_solo_to_review_fix` | plan | recompile as `review_then_fix` with `behaviour_critic` reviewing |
| 3 | `pb_solo_specialist` | plan | `chain`, second slot stage-directed: `imports` -> `dependency_resolver`, `tests` -> `edge_case_hardener` |
| 4 | `pb_solo_budget` | edit | steps and wall-clock on the author |

Biasing the planner toward `gate_then_repair` over `solo` remains worth doing, but
it is now a separate improvement rather than the only route: the planner change
affects future plans, playbook 1 reaches the 31 `solo` milestones already frozen.

### `gate_then_repair` — 5 milestones

Structurally the repair half of `test_first`. Playbooks 2–4 of `test_first`
transfer unchanged, with `author` in place of `builder`.

### `review_then_fix` — 3 milestones

`author -> reviewer -> fixer`. One review angle, and a fixer acting on a single
opinion.

| Priority | Playbook | Layer | Action |
|----------|----------|-------|--------|
| 1 | `pb_rtf_swap_angle` | plan | same template, reviewer slot refilled from `contract_critic` / `spec_auditor` / `behaviour_critic`; same cost, different angle |
| 2 | `pb_rtf_second_angle` | plan | recompile as `parallel_audit` — this is the only form "add a third party" can take inside a milestone, and at the plan layer it is one slot |
| 3 | `pb_rtf_drop_reviewer` | plan | recompile as `chain` without the reviewer, budget to the fixer — the only action with a direction on the cost axis |

### `chain` — 3 milestones

`first -> second -> third?`, serial handoff with nobody re-reading upstream
output, and a `third` slot that is `required: false` and often uninstantiated.
Playbooks: fill the third slot with a stage-directed role; recompile as
`review_then_fix` to get a reader between the writers; or drop to a shorter chain.

### `parallel_audit` — 0 milestones

Deferred until the planner chooses it. When it does, the two designs are a
parallel `behaviour_critic` as a third angle, and a read-only judge slot *before*
the fixer that reconciles three reports and ranks conflicting recommendations,
leaving the fixer to execute a ruling rather than arbitrate one. The judge needs a
template file that declares the slot; see gap 2 below.

## Where the topology playbooks are expressed

Shape-changing playbooks go through the **plan layer** — a different
`template_id` with slots refilled from the role pool, recompiled by
`build_milestone_graph` — not through `apply_local_edits`. The rationale, the
costs, and the invariants a shape change must preserve are in
`docs/topology_and_edit.md`; only the consequences for this design are repeated
here.

It removes two gaps from the critical path. Parallel placement is unimplemented
in the edit layer (`_add_role_agent` refuses it unconditionally), and no edit can
splice onto a single named edge — but `review_then_fix -> parallel_audit` and
"judge before the fixer" are slot assignments at the plan layer, so neither edit
type is needed to reach them.

It also avoids a live edit-layer defect. `_add_role_agent` re-sources *every*
outgoing edge of its anchor to the inserted node, so inserting a read-only role
after the builder makes an empty diff the artifact the gate scores. That is the
quality axis measuring the reviewer's non-change instead of the builder's work,
and nothing raises. Read `docs/topology_and_edit.md` before adding any edit-layer
topology action.

Parameter playbooks — prompt, budget, model, session — stay in the edit layer,
where they are safe and already work.

## Blocking gaps that remain

### 1. The read-only pool has no unused angle

`spec_auditor` and `contract_critic` are both spent by `parallel_audit`, and both
read documents rather than evidence. The proposed addition is
`behaviour_critic`: read the gate's `spec_tests` failure list and report which
assertion's *meaning* the implementer misread. It is the only angle no existing
role covers — one reads the design documents, one reads the frozen contracts, and
nobody reads what the tests observed.

### 2. No template hosts a judge

`parallel_audit`'s fixer arbitrates and edits in one slot. A judge position — a
read-only slot that reconciles several reports and ranks them, leaving the fixer
to execute a ruling — needs a template that declares it. Adding one template file
is cheaper than adding an edit type, but it is still new plan-layer surface, and
the planner will not choose it until the prompt catalogue describes when to.

### 3. Candidate attribution has no vocabulary yet

`CandidateRecord` records `edits` and `graph_hash`. A plan-layer candidate differs
from its parent by a template pair and a slot assignment, neither of which has a
field. Without one, per-playbook win rates cannot be aggregated across runs and
the playbook granularity argued for above does not actually exist.

## The validator that has to exist either way

Whichever layer produces a shape, one class of failure is silent: a conditional
edge whose source does not emit the field the condition reads. The candidate runs,
a stage does not, and the frontier records the result as though the design had
been evaluated. `EdgeCondition.evaluate` returns `False` for a missing field, so
there is nothing to catch.

Assert, after producing any candidate graph, that every conditional edge's source
node emits `condition.source_field`, and reject with `INVALID_GRAPH_EDIT`
otherwise. It belongs beside the rest of the invariant list in
`docs/topology_and_edit.md`, checked in both layers.

## Budget

`k` becomes an explicit hyperparameter with a default of **3** — the anchor plus
two playbooks — and is recorded in the run manifest's `tuning` block alongside
`design_search`, `selection_rule` and `epsilon`, and in the arm's line in
`EXPERIMENT_LOG.md`.

`k=2` is not enough under playbooks: the anchor takes one slot, leaving a single
playbook with nothing to be compared against but the preamble. Using the cost
model in `docs/fast_loop_pareto_protocol.md` — 13.3 min and $1.52 per candidate —
`k=3` is about 53 min and $6.45 per run against $4.93 at `k=2`, and roughly 13
minutes of extra wall-clock rather than double, since repeats run concurrently.

## Configuration

Under `experiment.tuning`:

```yaml
    playbook_search: true
    fast_loop_candidates: 3
    diagnosis:
      mode: llm            # llm | deterministic
      min_confidence: 0.5
      model: gpt-5.4
```

`playbook_search` and `design_search` are mutually exclusive and setting both is
an error: a run must not be ambiguous about which generator produced its
candidates.

## Experiment discipline

Playbook search changes what candidates exist, so its results are not comparable
with `outputs/cpe_official` and must not share a table with them. New arm, new
`output_root`, its own `EXPERIMENT_LOG.md` entry.

Validate on one task before spending on a sweep, checking three things that are
all invisible in a pass rate: that a playbook was generated and executed, that
the diagnosis artefact on disk is legible and its class is defensible, and — on
`test_first` — that the repairer still ran.

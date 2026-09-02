# Fast-Loop Playbook Search — Design

Companion to two documents. `docs/fast_loop_pareto_protocol.md` defines the
frontier, the objectives and the epsilon tolerances, and assumes candidates are
*atomic edits* drawn from a fixed order. `docs/topology_and_edit.md` describes how
a milestone graph is produced and which layer may change its shape. This one
proposes replacing the fixed order with **playbooks keyed on a diagnosed failure
class**: a change chosen because something was measured about *why* the milestone
failed, rather than because it sat early in a hardcoded list.

Status (2026-09-01): the table, the generator, the LLM diagnoser and the
experiment wiring are in place and have been paid for end to end (EXP-20260831-01).
Four search modes exist and are mutually exclusive under `experiment.tuning`:
`playbook_search`, `design_search`, `anchor_search` (the control arm: k copies of
the anchor design, resampled) and `persistence_search` (two anchor probes, then
the table diagnosed from the failures that survived every sample). The default
controller path is still the lookup plus the atomic-edit generator. The
selection half of the loop — frontier, epsilon, `require_gate_pass`, the
incumbent — is unchanged, but see "Noise floor" below: its `epsilon.quality`
is half a test at the suite sizes measured, and the same-design noise is one.

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

Module `orchestra/control/fast_loop/llm_diagnosis.py`. Input is deliberately
narrow:

* the failure reason and the exit signal name (`SIGXCPU` and friends);
* the harness stage breakdown — stage, passed, total, failing test identities;
* `behaviour_failures` plus the tail of the pytest output;
* a subgraph summary: nodes, roles, models, `max_steps`, `timeout_seconds`;
* remaining milestone budget.

Output is a fixed JSON object: `failure_class`, `confidence`, `target_node_id`,
`recommended_role`, `recommended_reviewer`, `rationale`, `evidence`. The two role
fields are ids from the fixed pool — an editing role and a read-only one — and a
playbook slot marked `role_from_diagnosis` / `reviewer_from_diagnosis` takes them
in place of its constant. The space stays enumerable: an id outside the pool, or
of the wrong kind, is dropped rather than trusted, and a role the target slot
does not accept is ignored at binding rather than sent to a recompile that
would reject the whole candidate.

Every search settles the pair, not only the persistence arm: the failure
path and the plain quality path run the same chain — rules, the model for the
residue when `diagnosis.mode: llm`, then a default pair by search reason
(`gate_repairer` + `behaviour_critic` for a failed gate, whose evidence is a
gate report the pool has a role written for; `implementer` + `spec_auditor` for
a quality search, whose residue is almost always a misread of the documented
behaviour). Persistence defers the chain to phase two, where the evidence is
the persistent set.

Three things stand between the model and the candidate. **Rules first**: when
every persistent failure falls in one category (imports stage; all public-surface
names; all corner-case names) the role is chosen deterministically and the model
is not consulted. **Consistency**: `budget` claimed for a run that reached the
behavioural tests with no exit signal is refused. **A named default last**:
`implementer`, recorded as `role_source: default`, never silence. Every
diagnosis-driven candidate carries a ledger of which persistent failures it
actually fixed; aggregated per (class, role) that decides whether a
recommendation keeps its slot. The model proposes, the harness disposes.

Until 2026-09-01 the diagnoser ran only on the failure path, and every paid run
had been a quality search, so it had never run at all; its class also merely
filtered rows, and `functional` and `design` share a row group. It now runs on
the quality path inside `persistence_search`, on the persistent set.

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

Module `orchestra/control/fast_loop/playbooks.py` maps
`(failure_class, template_id)` to an ordered list of playbooks. A playbook is
either an edit-layer recipe (bound to a slot at generation time) or a
`TemplateSwitch`. The feedback-only anchor is always index zero of
`PlaybookCandidateGenerator` and does not count as a playbook.

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

## Quality search has its own table

A quality search starts from a gate that already passed. The failure table's
first `test_first` row is `pb_tf_failures_to_repairer`, and that repairer is
marked `runs_if_gate_failed`. Sending it into a quality search is a candidate
that compiles, runs, and does nothing. So quality has `QUALITY_CATALOG`, keyed
on template only — there is no failure class to key on — and the two catalogues
share no `playbook_id`.

The quality table is cheapest-first. Each row has to change what a writer sees
or who writes. After a plan-layer switch the named leaks are rebound onto the
*new* graph, because the parent builder is not the person who will run.

### `test_first` (quality)

| Priority | Playbook | Layer | Action |
|----------|----------|-------|--------|
| 1 | `pb_tf_q_improve_after_gate` | plan | recompile as `test_first_improve`; names land on `improver`, whose role is `role_from_diagnosis` (default `edge_case_hardener`) |
| 2 | `pb_tf_q_diagnose_then_improve` | plan | recompile as `test_first_quality_diagnosed`; names land on `critic` and `improver`; both roles from the diagnosis |

`k=3` is the anchor plus both rows. Both templates are `planner_selectable:
false`, and both now carry the early gate **after the improver** with an optional
`repairer` behind it: a probe there freezes the improver's change on a pass and
hands a failure — a dropped contract symbol, a broken import — to a conditional
repair, which is the safety net `test_first` has and the improve shapes lacked.
Without it the improve candidate scored zero twice on the same two omitted
symbols (EXP-20260831-01), symbols the builder had left out in every candidate
and the parent shape's repairer had quietly restored each time.

`pb_tf_q_failures_to_builder` — named leaks into the parent builder's prompt —
was row 1 and was removed after losing to the anchor three times out of three
(−9.4pp, −31pp, and once identical to the incumbent). It differed from the anchor
by exactly the named list, so those are controlled measurements of that
addition. `test_first` blinds the builder to the suite on purpose, and the edit
re-runs it FRESH: the names re-roll the whole substrate biased toward a handful
of tests, they do not repair anything.

The named list the harness returns is often a subset of the failures. The
quality prompt therefore says how many tests ran, how many passed, and that
the list is incomplete when the named count is smaller than `total - passed`.
Names are shortened to `Class::test` so the prompt is readable.

### Already on an improve shape

Do not switch `test_first_improve` to itself. The rows are: names on the
improver; more budget on the improver; then a critic in front of that
improver (`test_first_quality_diagnosed`).

A plan-layer candidate used to leave `edits` empty. Quality switches that
carry names now record those prompt edits on the candidate; `plan_recompile`
is still what describes the shape change.

Recompiled contracts land in the run's contracts directory. Both the
compiler registry *and* the agent executor's copy have to be updated —
updating only the compiler lets the candidate compile and then die at
runtime with a `KeyError` on the new `contract_id`.

## Topology playbooks, per template

### The suite slot is on every template now

Only `test_first` authored a suite, and only an authored suite gives a behaviour
axis that is not saturated at 1.0 — which is why the frozen plans were rewritten
to it (`scripts/rewrite_plans_to_test_first.py`), and why the census below is
89% one template. Custody keys on the `test_author` *role*, not the template, so
since 2026-09-01 every planner-selectable template opens with an optional
`test_author` slot, the planner prompt says to always assign it, and `solo` is
withdrawn from the catalogue (`planner_selectable: false`; the file stays for
frozen-plan replays and the `pb_solo_*` recompiles). The slot is optional so a
search recompile never synthesises a suite author mid-search — that would author
a fresh suite and grade the candidate against different tests than its
incumbent.

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

### Shape rows are pairs from the diagnosis, told the names

Until 2026-09-01 the failure table's ten plan-layer rows named their roles as
constants and carried no evidence: `pb_tf_diagnose_before_repair` always seated
`behaviour_critic` and never told it a test name, `pb_solo_to_gate_repair` always
seated `gate_repairer`, and the two rows that did look at evidence keyed a role
on `furthest_stage` alone, which sent every test-stage failure to the corner-case
hardener. A shape change was "add a person", never "seat the person the failure
calls for".

Every plan-layer row now takes its writer from `recommended_role` and its
read-only slot from `recommended_reviewer` (the row's constants remain as the
fallback when nothing chose), and carries `include_failure_list` with
`feedback_slots` naming the new graph's slots, so the critic that was added to
read evidence is handed it. A plan-layer row applies even when the gate named no
test — a compile failure names none, and that is when a shape with a dependency
resolver is the right move; the list binds when it exists. `stage_slot` is no
longer used by any row: the stage feeds the rule floor instead.

The *choice of shape* is evidence-driven too, within the same closed space.
The diagnoser's output gained `recommended_shape`: an id it must pick from the
plan-layer rows reachable from the current template — `shape_options` renders
that menu into its prompt, next to the milestone's title and objective, the
current subgraph, the stage results and the playbooks already spent here — or
leave empty. It never proposes a topology; it chooses among prepared
recompilations, each keeping the milestone's objective, acceptance and
harness, and an id outside the menu is discarded. The generator then reorders
the fixed menu, never adding to it: a row already tried on this milestone goes
to the back, a row recompiling into the diagnosed shape goes to the front, and
under a `design` class the shape rows as a group outrank the edit rows — which
is the first time that class does anything, `_FUNCTIONAL` having merged it
into functional for eligibility. Once a shape row is chosen, the role
machinery seats its writer and reviewer as above, so a full recommendation is
three picks from three menus: shape, writer, reviewer.

Every row carrying a shape change or evidence now declares an `intent` — the
metric it exists to move — checked against the ledger at aggregation time, so
the table-evolution ring admits and removes rows on the same contract. And the
repair/fix/improve slots of every template were widened to accept the general
editing set (`dependency_resolver`, `edge_case_hardener`, `gate_repairer`,
`implementer`, `integrator`): a slot's allow-list used to veto the diagnosis
silently — `review_then_fix.fixer` refused the dependency resolver — and the
diagnosis, not the slot, should decide who sits in a repair position.

The quality table lost its two budget rows. A quality search starts from a gate
that passed, the Codex backend reports `step_count=1` for every run, and the only
exit-signal detector reads failure text a passing run does not have; nothing
could ever trigger them but their position in the list.

### How a topology playbook is written

Since shape changes go through the plan layer, a topology playbook is a **target
template plus a slot assignment**, not a list of edits:

```
pb_tf_diagnose_before_repair:
    template: test_first_diagnosed      # exists, planner_selectable: false
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
| 2 | `pb_tf_diagnose_before_repair` | plan | recompile as `test_first_diagnosed` — a read-only `behaviour_critic` between the failing gate and the repairer, `runs_if_gate_failed: true`, so it costs nothing when the gate passes |
| 3 | `pb_tf_second_repairer` | plan | recompile as `test_first_double_repair` — two `gate_repairer` slots behind the same failing gate |
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

## Blocking gaps

### 1. The read-only pool has no unused angle — closed

`spec_auditor` and `contract_critic` are both spent by `parallel_audit`, and both
read documents rather than evidence. `behaviour_critic` is the angle neither
covers: it reads what the code does when it runs — the acceptance gate's failing
test *names*, or the documented examples worked through by hand — and reports
which reading of the requirement the implementer took. It is read-only, and
`review_then_fix`'s reviewer slot accepts it, which is what makes
`pb_solo_to_review_fix` and `pb_rtf_swap_angle` reachable.

The prompt is explicit that the suite is not in the repository and that
reconstructing it from the names is out of bounds, because a fix aimed at a test
name satisfies the name and nothing else.

### 2. No template hosts a read-only slot behind a gate — closed for `test_first`

The concern was that a new template widens what the *planner* may choose, so a
run comparing search against no search would be measuring two changes at once.
Templates now carry `planner_selectable`, and `catalog_lines` — the only thing
that renders the catalogue into the planner prompt — skips the ones set to
`false`. A shape can therefore exist for a playbook to recompile into while
staying invisible to planning, and offering it to the planner later is a
one-line change made deliberately rather than as a side effect.

`configs/subgraph_templates/test_first_diagnosed.yaml` is the first such shape
and is what playbook 2 recompiles into: `test_author -> builder -> critic ->
repairer`, early gate after `builder`, with both `critic` and `repairer` marked
`runs_if_gate_failed`, so a milestone that passes first time pays for neither.
The critic is declared before the repairer because the compiler freezes the last
agent it instantiates, and a read-only agent in that position would hand the
acceptance harness an empty diff to grade; `_require_an_editing_terminal` refuses
any assignment that ends that way, naming the slot.

Still open for `parallel_audit`: a judge position — a read-only slot that
reconciles several reports and ranks them, leaving the fixer to execute a ruling.
It needs its own template, and it stays deferred because the planner has never
chosen `parallel_audit`, so nothing would recompile from it.

### 3. Candidate attribution has no vocabulary — closed

`LocalCandidate` and `CandidateRecord` carry `playbook_id`, and `plan_recompile`
records the template pair, the full slot assignment and the slots that actually
moved. `edits` is empty for a bare plan-layer candidate; a quality switch that
then binds the named leaks records those prompt edits as well.

## The validator that has to exist either way — closed

Whichever layer produces a shape, one class of failure is silent: a conditional
edge whose source does not emit the field the condition reads. The candidate runs,
a stage does not, and the frontier records the result as though the design had
been evaluated. `EdgeCondition.evaluate` returns `False` for a missing field, so
there is nothing to catch.

`orchestra.ir.graph_invariants` states this and eight others, and both producers
now check them: `build_milestone_graph` raises, because a violation there is a
compiler defect and not something a caller can recover from, and
`apply_local_edits` rejects with `LocalEditError`, which the generators already
turn into `INVALID_GRAPH_EDIT`.

## Three timescales, and who may change what

| ring | scale | mutates | state |
|---|---|---|---|
| fast loop | one milestone, minutes | candidate instances: which rows are eligible, who fills their slots, what evidence binds, resample vs diagnose | running |
| M5 slow loop | one task, between checkpoints | future, unleased milestones' plans (never the committed past) | built, `enabled: false` in these arms |
| table evolution | across runs, offline | the tables, templates, roles and rules themselves | manual today |

The fast loop adapts the *instance* and never the *policy*: the tables, the
role pool and the rule floor are read-only to it, and everything it learns
leaves as records — candidate results, the persistence ledger, notes. Three
measured reasons it must stay that way: single-run evidence is noise (the
same-design floor is one test); a table that mutates mid-experiment breaks the
rule that a run is never ambiguous about which table produced its candidates,
so ledgers stop aggregating; and the table plus the pool are the whole
enumerable design space, which is what keeps every candidate auditable.

Table evolution is the ring the human has been playing by hand — removing
`pb_tf_q_failures_to_builder` after three controlled losses, rewiring the
roles, gating the improve shapes — and the plan is to mechanise exactly that
gesture, not to free-associate over history. Its evidence unit is the paired,
within-run, persistent-fix ledger record — not "which candidate won", which at
n=1 per design is mostly the resampling lottery (a summariser reading raw
winners would conclude the anchor is the best playbook, which is true and
vacuous). Staged:

* **P0 — the eyes.** An aggregation script over every run's
  `task_execution.json`: per (playbook_id, role, failure_class), the paired
  delta against the same run's anchor, persistent failures fixed, regressions
  introduced, cost. No new spend; 27 records exist today.
* **P1 — entry and exit rules.** A row earns a k-slot with N paired
  persistent-fix wins over the anchor and loses it the same way, turning the
  manual deletion into policy.
* **P2 — the summariser.** An LLM reads failure cases, ledgers, winning diffs
  and external motifs, and proposes new rows or templates as *hypotheses*: each
  declares an `intent` (which metric should move), enters
  `planner_selectable: false`, and must pass P1's gate to keep its slot.
  EvoMAS's evolved pool entries (`/root/projects/EvoMAS/mas_pools/*/`) enter
  here as design motifs — parallel read-only review, judge positions, vote
  aggregation over reports — filtered by the one-workspace constraint that
  forbids parallel writers; their scores on other benchmarks are priors, not
  evidence.

Intent is verified twice: structurally at bind time (the role seated, the
names delivered — unit-tested), and in outcome by checking the declared metric
against the ledger, so a row that never does what it was written to do is
removed by the same evidence that admitted it.

## Persistence diagnosis

One attempt's failure list mixes tests the code gets wrong with tests this sample
was unlucky on. Best-of-n resampling harvests the second kind and cannot touch
the first: in the anchor arm four independent samples of one design failed the
same five tests every time and flipped on two, and the whole +8.7pp of best-of-3
was those two. The intersection over samples separates them.

`persistence_search` spends the same `k`: `persistence_probe_samples` (default 2)
anchor resamples, intersect with the incumbent, then the remaining slots on the
table diagnosed from the persistent set only. Only samples whose gate passed
count. An empty intersection declines phase two — every failure flipped, so
resampling was the right tool and the probes already were it. In
EXP-20260831-01 the persistent set stabilised at the third sample both times.

## Noise floor and epsilon

Three same-design samples in one search scored 16, 17 and 18 of 23: the noise
floor is **one test**, σ ≈ 0.04. `epsilon.quality` is 0.02, half a test, so a
one-test lead is treated as a real quality gap and can be paid for at any cost.
Recommended: 0.05, so a one-test difference falls to the cost axis and two are
needed to win on quality. Not yet applied; n=3 is a crude estimate and every
anchor or persistence run adds free samples.

Cross-run behaviour scores are **not comparable**: the suite is authored per run,
so the yardstick moves (M1 first passes: 0.875, 0.829, 0.970, 0.857 across four
runs of one plan). Only within-run comparisons, which share a suite, are clean.

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

`playbook_search`, `design_search`, `anchor_search` and `persistence_search`
(with `persistence_probe_samples`) are mutually exclusive and setting more than
one is an error: a run must not be ambiguous about which generator produced its
candidates. Each arm has its own experiment yaml and `output_root`.

## Experiment discipline

Playbook search changes what candidates exist, so its results are not comparable
with `outputs/cpe_official` and must not share a table with them. New arm, new
`output_root`, its own `EXPERIMENT_LOG.md` entry.

Validate on one task before spending on a sweep, checking three things that are
all invisible in a pass rate: that a playbook was generated and executed, that
the diagnosis artefact on disk is legible and its class is defensible, and — on
`test_first` — that the repairer still ran.

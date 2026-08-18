# Topology and Edit

Reference for how a milestone's multi-agent graph comes into existence, and what
may change it afterwards. Three layers, and most confusion about the search comes
from conflating them:

| Layer | Who decides | What it produces |
|-------|-------------|------------------|
| **Plan** | the planner LLM, constrained to two catalogues | a template id and one role per slot |
| **Compile** | `build_milestone_graph`, deterministic | a runnable `OrchestraGraph` plus generated contracts |
| **Edit** | the fast loop, per candidate | a mutated `OrchestraGraph`, *not* re-derived from the plan |

The edit layer does not go back through the plan layer. That single fact explains
most of the sharp edges below.

## Plan layer — a template, then roles from the pool

The planner is not allowed to design a topology. It receives two fixed
catalogues and picks from them.

**Templates** live in `configs/subgraph_templates/*.yaml` and are rendered into
the prompt by `catalog_lines`:

| Template | Shape | Early gate |
|----------|-------|------------|
| `solo` | `author` | — |
| `chain` | `first -> second -> third?` (extensible) | — |
| `gate_then_repair` | `author -> repairer?` | after `author` |
| `review_then_fix` | `author -> reviewer -> fixer` | — |
| `parallel_audit` | `author -> {spec_review ‖ contract_review} -> fixer` | — |
| `test_first` | `test_author -> builder -> repairer?` | after `builder` |

**Roles** live in `configs/roles/*.yaml`. Eleven of them, each carrying a prompt,
`edits_repository`, and default `max_tokens` / `max_steps` / `timeout_seconds`.
Only two are read-only: `spec_auditor` (reads the design documents) and
`contract_critic` (reads frozen contracts). Everything else writes.

The planner returns, per milestone, a `template_id` and an `agents` list of
`{slot, role, mandate}`, optionally overriding `focus_paths`, `max_tokens`,
`max_steps`, `timeout_seconds`. The prompt states outright that the template
fixes how many agents run and how they are wired.

Three guardrails make the output safe to compile without validating it as a plan:

* `_resolve_template` — an unrecognised `template_id` falls back to `solo` when at
  most one agent was proposed, and to `chain` otherwise.
* `_agent_for_slot` — a `role` that is not in the pool, **or that the slot's
  `allowed_roles` does not accept**, is replaced by the slot's `default_role`. An
  invented role cannot enter a graph.
* Budgets pass through `_clamp_int` / `_clamp_float` against
  `MAX_TOKENS_RANGE`, `MAX_STEPS_RANGE`, `TIMEOUT_RANGE`, defaulting to the
  role's own values.

### Required slots are not backfilled

`_parse_agents` instantiates only the slots the planner actually named:

```python
if by_slot:
    chosen = [slot for slot in slots if slot.slot_id in by_slot]
else:
    chosen = slots[: max(len(items), 1)]
```

A `required: true` slot the planner omitted stays empty, on the stated ground
that synthesising an agent would hand the milestone budget nobody asked for. So a
plan naming `parallel_audit` with one `author` entry compiles to a one-agent
chain, not an audit shape. When reading a plan, the agent list is authoritative
and `template_id` alone does not tell you what ran.

## Compile layer — what the graph actually looks like

`build_milestone_graph` takes the shape from the template and the occupants from
the plan. Beyond the agents it always adds machinery that no template mentions,
and which every topology change has to keep intact.

* **Agent nodes** are `agent_{index+1}_{role_id}`. Each gets its own generated
  `AgentContract` written to `configs/contracts` under
  `contract_id_for(milestone_id, role_id)`, carrying the role prompt, the
  milestone mandate and the acceptance paragraph. Every agent — read-only
  included — declares `output_slots: {repository_change: RepositoryChangeArtifact}`.
  A read-only role differs only in `require_git_diff: false`, because a reviewer
  produces no diff and a backend demanding one would score correct behaviour as
  failure.
* **Fan-in gets distinct slot names**: `upstream_change`, `upstream_change_2`, …
  One shared slot would resolve to the first active edge only, and a fan-in agent
  would silently see one of its two upstream reports.
* **Custody**, when a `test_author` slot exists and the harness command carries
  `--spec-tests`: a harness node `authored_suite_custody` sits between the author
  and every consumer, and consumers wait on its report via a `suite_custody`
  input rather than on the author's change artifact — whose patch would otherwise
  quote the whole suite into the implementer's prompt.
* **The early gate**, when the template declares `early_gate_after` *and* some
  bound slot is `runs_if_gate_failed`: a probe harness `repository_tests_probe`
  scores the gating agent, and the conditional slot receives a `gate_report`
  input over an edge **from the probe**, gated on `passed is_false`. That input is
  the only one that cannot resolve when the probe passed, which is what keeps the
  repairer out of the run on the happy path.
* **The terminal gate and freeze**: `repository_tests` scores the last bound
  agent, and a `freeze_change` transform emits `final_change`, the graph's
  `final_output_slot`. `tests_pass_to_freeze` and `probe_pass_to_freeze` are
  gated on `passed is_true`.

### Metadata

The payload carries `template_id`, `topology` (`template:<id>`), `milestone_id`,
`gate_level`, `agent_backend`, `risk_rationale` and `agent_roster`. The roster has
one entry per agent with `contract_id`, `role`, `slot`, `role_id`, `title`,
`contract_path`, the prompt hash and the three budgets.

One mapping trap: the roster's `node_id` field holds `agent.role_id`, while the
graph node is `agent_{index+1}_{role_id}`. Anything joining roster to graph has
to apply that prefix.

## Edit layer — what a candidate is

A candidate is a **mutation of the compiled graph**. `apply_local_edits` clones
the graph, applies edits in order, appends each to `metadata["edit_lineage"]`,
sets `parent_graph_hash`, then validates — via `GraphCompiler.compile` when a
compiler is supplied, otherwise `_validate_dag` alone.

Nine edit types exist; `TOPOLOGY_EDIT_TYPES` names the four that change shape.

| Edit | Scope |
|------|-------|
| `prompt_feedback`, `session_policy`, `budget_adjustment`, `model_override`, `tool_policy` | one node's parameters |
| `add_role_agent`, `drop_agent`, `add_verifier_node`, `rewire_edge` | shape |

Node-level safety is real: `tool_policy` refuses `hidden_tests`,
`private_evaluator` and friends; `drop_agent` refuses any role that edits the
repository and refuses to leave the milestone without an editing agent;
`budget_adjustment` respects a `max_steps` cap of 64 and the milestone's
wall-clock ceiling.

### What the edit layer does not do

* **It never re-validates against the template.** No check confirms the mutated
  graph is still an instance of any template, because the template is not
  consulted.
* **`metadata["template_id"]` is left untouched**, so an edited graph keeps
  claiming the template it no longer matches. This is worse than the information
  being absent: downstream readers believe it.
* **`validate_against_pool` does not run.** The rule that two repository-editing
  agents may not share a wave is enforced at *template load* time; in the edit
  layer it survives only as two hand-written lines inside `_add_role_agent`.
* **A candidate's inserted agent is not the same object as a planned one.**
  `_add_role_agent` reuses the anchor's `contract_id` and appends `role.prompt` to
  `prompt_prelude`, taking the role's three budgets. A planned slot gets a
  generated contract of its own. Same role id, two different fidelities.

## Known breakages in the edit layer

### `add_role_agent` inherits everything the anchor produced

It re-sources **every** outgoing edge of the anchor to the inserted node:

```python
edges = [
    e.model_copy(update={..., "source_node": node_id})
    if e.source_node == edit.after_node_id
    else e
    for e in graph.edges
]
```

On `test_first` the builder has three outgoing edges — to the probe, to
`freeze_change`, and the `upstream_change` link to the repairer. All three move,
so the inserted node becomes what the early gate scores and what gets frozen. If
the inserted role is read-only, the graded artifact is now an empty diff: the
quality axis is measuring the reviewer's non-existent change instead of the
builder's work.

The repairer's `gate_report` edge is *not* affected, because its source is the
probe harness rather than the builder — the conditional branch still gates
correctly. The damage is to what the gate reads, not to whether the repairer
runs.

The same applies at the tail: anchoring on the terminal agent moves
`agent_to_tests` and `agent_to_freeze`, so a read-only insertion there empties
the terminal gate's input too.

### Parallel placement is unimplemented

`_add_role_agent` raises `"add_role_agent supports serial placement only"`
unconditionally, before the read-only check above it can matter. No edit can
produce a fan-out.

### `rewire_edge` cannot reorder

Its docstring claims a `serialize` intent; the schema carries only `condition`
and `clear_condition`. It can gate or ungate an existing edge and nothing else.

### No edit can insert a harness

`add_verifier_node` inserts a structured verifier, not an acceptance harness.
There is no way to create an early gate, so `solo` cannot be lifted into the
`gate_then_repair` shape from inside the fast loop. Adding a repairer to `solo`
yields an unconditional second pass that cannot see a gate report.

## Decision: topology changes go through the plan layer

Taken 2026-08-17. Shape-changing search is expressed as **a different
`template_id` with slots refilled from the role pool, recompiled through
`build_milestone_graph`**. The edit layer keeps the parameter-level edits —
prompt, budget, model, session, tools.

What this buys:

* A candidate is always a legal template instance, so `validate_against_pool` and
  the template's own invariants apply for free instead of being reimplemented.
* Inserted agents get generated contracts, like planned ones, so a candidate's
  reviewer is the same object a plan would have produced.
* The shapes we actually want are one line each. `review_then_fix ->
  parallel_audit` is "change the id, fill one more slot" — it needs neither
  parallel placement nor an edge-splicing edit, which removes two of the four
  gaps above from the critical path.
* `metadata["template_id"]` stays true.

What it costs, and must be handled:

* A candidate is no longer a small delta on its parent; the milestone recompiles.
  Cost stays comparable because every candidate already re-runs from the last
  committed snapshot, but graph lineage becomes template-to-template rather than
  an edit list, and `CandidateRecord` has to record the template pair and the slot
  assignment for attribution to survive.
* The frozen authored suite must stay the same suite. Recompiling regenerates the
  custody wiring, and a candidate scored against a different `spec_tests` is not
  on the frontier's yardstick — see `docs/fast_loop_pareto_protocol.md` on why
  the suite is frozen once per milestone.
* Contracts are written to `configs/contracts` at compile time, so per-candidate
  compilation needs contract ids that do not collide across candidates.
* The fast loop currently receives an `OrchestraGraph` and no `MilestoneDraft`.
  It does not need the full draft — `metadata` already carries `template_id` and
  `agent_roster`, which is enough to refill slots — but the mandate text lives in
  the generated contracts, so those have to be read or the roster's
  `contract_path` followed.

## Invariants any topology change must preserve

Checklist for reviewing a proposed shape change, whichever layer produces it.

1. **The gate scores an editing agent.** Whatever feeds `repository_tests` or
   `repository_tests_probe` must be a repository-editing agent, never a read-only
   one.
2. **`final_change` stays reachable** from `freeze_change`, and `freeze_change`
   keeps both a change input and a gate input.
3. **At most one early probe**, and if one exists, at least one node behind
   `passed is_false`. An early gate with nothing waiting on failure is dead cost.
4. **Custody stays between `test_author` and every consumer**, and no change edge
   carries the author's patch into an implementer. The implementer must not be
   able to read `spec_tests`.
5. **No two repository-editing agents in one wave.** They share a workspace.
6. **Every conditional edge's source actually emits the condition's
   `source_field`.** `EdgeCondition.evaluate` returns `False` for a field it
   cannot find, so a mis-sourced condition disables a branch silently rather than
   erroring.
7. **Read-only agents keep `require_git_diff: false`.**
8. **Harness nodes stay public** and no edit grants `hidden_tests` or
   `private_evaluator` tools.
9. **`metadata["template_id"]` describes the graph** it is attached to.

Items 1 and 6 are the two that current edit-layer code can violate without
raising anything, and they are the reason this document exists.

## Related

* `docs/fast_loop_playbook_search.md` — the diagnosis-keyed playbooks that use
  these shapes, and the per-template playbook tables.
* `docs/fast_loop_pareto_protocol.md` — how candidates are compared once
  produced.

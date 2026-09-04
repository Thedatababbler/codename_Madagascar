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

5. **n is capped at 5.** Ten repetitions per arm is not affordable here, and a
   driver that accepts `--repeats 10` is how it gets spent by accident;
   `run_codeprojecteval_sweep.py` refuses anything larger.

6. **Check the gate has a gradient before paying to tune on it.** A repair loop
   only fires when a milestone gate fails, so on a repository whose gate almost
   always passes a tuning run is a lottery, not an experiment. Count gate
   outcomes in existing batches first; if they are nearly all passes, probe
   cheaper repositories with tuning switched on (a probe whose gate passes costs
   no more than one without it) rather than spending n=5 on the saturated one.

7. **Denominators are pinned, never computed per run, and a rate above 1 is
   refused rather than published.** Held-out suite sizes live in
   `configs/codeprojecteval_suite_sizes.json`; scoring reads the pin, and
   `denominator_faults()` withholds the rate whenever any module's size was
   guessed. Two runs of the same task divided by different numbers for two months
   because the counts were cached under `outputs/` and the per-module fallback to
   static counting was silent (EXP-20260810-04). Re-pin only when the dataset
   changes, and read the diff.

8. **A fast-loop experiment needs a task whose gate can fail, which is a much
   smaller set than "tasks that split".** Four repositories plan two milestones
   (imapclient, pyjwt, simpy, bplustree) but only imapclient fails its gate with
   any regularity; the other three pass first try, so adding them to a tuning arm
   buys zero fast-loop activity at full price. Widening the population requires a
   trigger that fires on a low score, not only on a failed gate — now implemented
   as `tuning.quality_trigger`, and enabled only in
   `configs/experiments/codeprojecteval_tuning_multi.yaml`.

9. **Hidden scoring that times out is unmeasured, never a 0.** The held-out
   session wall is 6 hours and each case may run 30s (`eval_codeprojecteval.py`,
   `run_codeprojecteval_sweep.py`). A session that does not finish leaves
   `pass_rate` as `None` with `status=timeout`. Do not average that as 0.000
   (official Codex bplustree solo on 2026-08-14 did this under the old 20-minute
   / 5s defaults). LCB / RealBench per-test budgets are a different exam.
   The same holds for a scorer the harness kills: the address-space cap is
   8192 MB because pandas maps past 2 GB on import, and a nonzero exit that
   wrote nothing to either stream is `status=timeout` with no rate, not a zero
   (EXP-20260815-01). Records now keep `stderr_tail` as well as `tail`, since a
   conftest that will not import reports itself only on stderr.

10. **Report a quality search separately from a repair search.** Both run the same
   machinery through the same fast loop, but one is recovering a milestone that
   failed and the other is refining one that passed. `FastLoopState.search_reason`
   records which; averaging them together mixes a repair rate with a refinement
   rate and neither number then means anything. A quality search that declines is
   a success, not a failure, and is recorded as one.

11. **Read tuning signals off the stage that can still move, never off a blended
    score.** A committed CodeProjectEval milestone has passed compile, imports and
    contracts outright, and those hold 0.6–0.7 of the graded score's weight, so the
    blend is pinned above 0.9 however little of the code works. Anything comparing
    designs or thresholding on "poor" must read the behavioural stage
    (`spec_tests`, else `tests`): the trigger shipped reading the blend and could
    not fire on any milestone ever recorded (EXP-20260810-05). The same arithmetic
    shrinks a real 0.069 quality gap to 0.021, i.e. inside the quality epsilon, so a
    blended axis also reports genuinely different designs as ties.

12. **A correlation study needs candidates that differ.** The authored-suite probe
    was run against nine candidates whose held-out rates span 0.021 in standard
    deviation, and returned r = −0.204 with p ≈ 0.6 — which is not evidence about
    the axis, it is evidence the population was flat. Before spending on a validation
    run, check the spread of the thing being predicted; if it is inside its own noise,
    the study cannot come back either way and the money buys a number that reads like
    a result.

13. **A new artifact in the workspace needs an exemption from every rule that
    forbids it.** Every editing agent is told to ship only files the design
    documents describe. `spec_tests/` is described by no document, so both builders
    in the first `test_first` run deleted the suite they were supposed to implement
    against, and the run passed every check while recording no behavioural score at
    all (EXP-20260810-06). Whenever a role starts producing something the other
    roles have never seen, re-read what those roles were already told about files
    they did not create — and state the exemption after the rule, not before it.

14. **A missing measurement must not look like a passing one.** The lost suite was
    invisible in the summary: the milestone committed, the gate passed, and the
    behavioural stage was simply absent, which reads identically to a milestone that
    authored nothing. Any optional stage whose absence changes what an experiment can
    conclude has to announce the absence — the gate now prints
    `NOTE no authored suite ...` — and the run summary has to carry it.

15. **A yardstick the candidate can read is not a measurement.** The authored suite
    was left in the workspace so the implementer could treat it as an executable
    spec. Every milestone then scored a perfect behavioural stage — 38/38, 29/29,
    51/51 — while the same code failed most of the held-out suite
    (EXP-20260811-01), which is the saturation the axis was introduced to escape.
    The suite is now taken out of the workspace between the two agents. Whenever a
    metric is computed from something a candidate can see, assume the candidate
    will optimise the metric instead of the goal, and check what the metric looks
    like when it does.

16. **Hiding a file is not hiding it; count every channel that renders it.** With
    the suite deleted from the workspace, four separate paths still carried it into
    agent prompts: the author's change artifact quoted the whole diff, the gate's
    pytest tail quoted the assertions, the score line named every failed test, and
    the custody step's own report gave the absolute path it had moved the suite to.
    Three of the four were found only by writing the assertion "this string appears
    nowhere the agent can read" and running it, rather than by reasoning about the
    design. Anything an agent is not supposed to see needs that assertion at the
    render boundary, not at the filesystem.

17. **RealBench's formal gate is a prerequisite, not a quality signal.** Compile,
    import and UML-export checks pass on a repository that does not work
    (EXP-20260815-02: five search arms committed with `score=null, stages=[]`,
    hidden `proj_with_test` at 0–0.5, quality trigger never fired). The public
    check now grades an authored `spec_tests/` suite the way CodeProjectEval
    does — score, not exit code — and the quality trigger reads that stage.
    A `solo` plan still authors nothing, so `behaviour_score` stays `None` and
    search still will not start. Rewrite frozen drafts with
    `scripts/rewrite_plans_to_test_first.py` before a search arm; do not change
    the planner to force a split just to get a suite. Hidden evaluation stays
    offline.

**Last updated:** 2026-08-17 (UTC) — official gpt-5.4 blind solo on the seven
one-segment CodeProjectEval tasks (EXP-20260817-01)

---

### EXP-20260817-01 — official Codex solo, no search, no gate language
- **Status:** canonical (n=1, seven one-segment tasks)
- **Date:** 2026-08-17 (UTC)
- **Question:** what does one official gpt-5.4 Codex agent score when it is
  handed only the design documents — no authored suite, no hidden-test
  language, no AdaMAS gate description, no search?
- **Artefacts:** `outputs/cpe_official_solo/slice_solo_blind.jsonl`,
  `outputs/cpe_official_solo/ab-*-solo-r1-20260817T151257Z`

The seven tasks never had a `solo` run. Their official three-arm table used
`single` / `nosearch` / `search`, all `test_first`. This batch is the missing
single-agent cell. Endpoint is `api.openai.com`, model `gpt-5.4`. Each trial
is one implementer, 20-minute wall, then hidden `unit_tests`. If the gate
refused to commit, scoring still reads the agent's working tree.

| task | solo (this) | official single | official nosearch | official search | solo $ | wall |
|---|---|---|---|---|---|---|
| tinydb | 0.824 | 0.804 | 0.848 | **0.882** | 0.40 | 2.3 min |
| deprecated | 0.562 | 0.591 | 0.591 | **0.602** | 0.32 | 1.6 min |
| parsel | 0.272 | 0.264 | 0.256 | **0.276** | 0.65 | 3.1 min |
| csvs-to-sqlite | 0.640 | 0.000 (empty) | **0.680** | **0.680** | 0.59 | 3.5 min |
| python-hl7 | **0.530** | 0.000 (empty) | 0.000 (empty) | 0.430 | 0.74 | 3.7 min |
| portalocker | unscored | unscored | unscored | unscored | 0.58 | 4.7 min |
| voluptuous | **0.633** | 0.000 (empty) | 0.000 (empty) | 0.000 (refused) | 0.74 | 3.6 min |

Seven trials, 4.7 minutes wall, **$4.02** on the logged official list price.
portalocker is the same `LockerType` collection abort as the other three arms.

On the four tasks where the gated arms actually committed code, solo lands
inside the same band (tinydb / deprecated / parsel / csvs-to-sqlite). On the
three where the gate left an empty repository, solo is the first real score:
python-hl7 0.530 (above search's 0.430) and voluptuous 0.633 (the search arm
had discarded a 0.696 candidate for failing the gate). n=1; do not treat a
single-task gap as an effect.

---

### EXP-20260815-02 — RealBench, smolagents on xiaoai, three arms
- **Status:** canonical (n=1, five level-2 tasks)
- **Date:** 2026-08-15 (UTC)
- **Question:** on RealBench, with smolagents talking to xiaoai gpt-5.4, does
  single / no-search / search separate?
- **Artefacts:** `outputs/realbench_xiaoai/slice_xiaoai_20260815T091426Z.jsonl`

The planner produced one milestone on all five tasks, so single and no-search
are the same frozen plan run twice. Search was armed (`fast_loop_candidates: 2`,
quality trigger 0.94) but `fast_loop_history` is empty on every search arm:
walls are 0.7–4 minutes, which is one pass, not a candidate loop. The first
attempt at this slice died in 6 seconds on `max_tokens` (xiaoai now wants
`max_completion_tokens` for gpt-5); that batch is void. This table is the
rerun after the factory remap.

| task | single | 不搜索 | 搜索 | 搜了？ |
|---|---|---|---|---|
| AlienMajik_SnoopR | 0.400 | 0.000（没提交） | **0.500** | 否 |
| FreddyRodgers_emojichef | **0.466** | 0.411 | 0.425 | 否 |
| benbovy_xproj | 0.000（没提交） | 0.069 | **0.138** | 否 |
| encore-ecosystem_NodeFlow | 0 | 0 | 0 | 否 |
| dkweiss31_floquet | 0（提交了） | 0（没提交） | 0（提交了） | 否 |

This is not a decomposition result: nothing was split. It is also not a search
result: the loop never ran. The spread between single and no-search is n=1
variance on the same config (SnoopR 0.400 vs an empty commit; emojichef 0.466
vs 0.411).

---

### EXP-20260815-01 — why the official gpt-5.4 zeros were zeros
- **Status:** canonical (re-scoring of an existing batch; no new paid runs)
- **Date:** 2026-08-15 (UTC)
- **Question:** of the seven official Codex / gpt-5.4 tasks, four arms published
  0.000 and three published nothing. How many of those are the code failing?
- **Artefacts:** `outputs/cpe_official/ab-*-r1-20260815T012058Z`,
  `outputs/cpe_official/ab-{tinydb,deprecated,parsel}-*-r1-20260814T211020Z`

Four distinct causes, only two of which are about the code.

| cause | arms | what was published | what is true |
|---|---|---|---|
| scorer killed at the 2 GB address-space cap | csvs-to-sqlite nosearch, search | 0.000 | **0.680** each (17/25) |
| dataset conftest imports a symbol no design document names | portalocker ×3 | unscored | `portalocker.portalocker.LockerType` aborts collection; aliasing it scores 0.619 / 0.556 / 0.635 (single / no-search / search) |
| gate failed, so nothing was committed and the suite met an empty repository | python-hl7 single + nosearch, voluptuous single + nosearch, csvs-to-sqlite single | 0.000 | true zero, but of the run, not of the code |
| fast loop refused to commit a non-valid winner | voluptuous search | run_failed | the discarded candidate scores **0.696** (112/161) |

The memory cap is the serious one: at 2048 MB the interpreter died before pytest
wrote a line, and a nonzero exit with empty stdout parsed as zero passes. It is
indistinguishable in the record from an empty repository, and it silently
punishes exactly the tasks that import a scientific stack. Cap is now 8192 MB
and a silent nonzero exit is unmeasured
(`tests/unit/decomposition/test_codeprojecteval_scoring.py`).

The other three are all-or-nothing shapes rather than measurement faults, and
they cost more than the arithmetic suggests. A milestone contract that reads
`voluptuous.Extra` as "callable or class" fails a sentinel object
(`Extra = _ExtraToken()`) whose behaviour scored 0.679; the gate then blocks the
commit, and the held-out suite scores an empty directory at 0.000. One symbol —
`hl7.Container`, `voluptuous.Extra`, `LockerType` — decides the whole task.
Corrected, search is at least no-search on 5 of the 6 measurable tasks (four
wins, one tie at 0.680) and loses only the one where it refused to commit.

portalocker under the alias is the one place no-search lands *below* single
(0.556 vs 0.619), and the whole gap is one deadlock: its `BoundedSemaphore`
recursed through `utils.py:499 acquire` and never returned, so four cases in
`test_semaphore.py` and `test_timeout_behaviour.py` died on the 30s per-test
cut-off and the module pair took two minutes. Single and search pass the same
twelve cases in 0.29s. Search's own margin over single is one case
(`test_rlock_behaviour.py`), which is noise at n=1.

---

### EXP-20260811-02 — the hidden suite, with search and without
- **Status:** canonical (four paid runs, n=1 per cell)
- **Date:** 2026-08-11 (UTC)
- **Question:** with the authored suite taken out of the workspace, does the
  behavioural axis move at all — and if it does, is Pareto search worth its price?
- **Model:** `gpt-5.3-codex-spark`; not comparable with any gpt-5.4 batch.
- **Design:** same frozen `test_first` plans, same custody, same scoring, in both
  arms. The only difference is `fast_loop_candidates: 3` + quality trigger versus
  `0` and no trigger (`configs/experiments/codeprojecteval_nosearch_multi.yaml`).
- **Artefacts:** `outputs/cpe_tuning_multi/slice_hidden_search_20260811.jsonl`,
  `outputs/cpe_nosearch_multi/slice_nosearch_20260811.jsonl`

| task | arm | held-out | $ | wall | committed | behaviour per milestone |
|---|---|---|---|---|---|---|
| imapclient | suite visible, search on | 0.255 | 4.81 | 22 min | 2/2 | 1.00 |
| imapclient | hidden, no search | 0.210 | 5.44 | 34 min | 1/2 | 0.727, gate failed |
| imapclient | hidden, search on | **0.487** | 17.32 | 70 min | 2/2 | 0.837, 0.549 |
| simpy | suite visible, search on | 0.483 | 16.71 | 66 min | 2/2 | 1.00, 1.00 |
| simpy | hidden, no search | 0.463 | 8.07 | 25 min | 2/2 | 0.667, 0.955 |
| simpy | hidden, search on | 0.564 | 8.17 | 38 min | 2/2 | 0.920, 0.905 |

**The axis moves.** Every behavioural score under custody lands between 0.55 and
0.96, against 1.00 on every milestone of the visible run. More to the point, the
three candidates of imapclient's first milestone scored 0.756, 0.780 and 0.837 — a
spread of 0.081, four times the 0.02 quality epsilon. This is the first frontier on
this dataset whose quality axis can tell candidates apart rather than declaring
them tied and falling through to price.

**Search paid on imapclient and did not run on simpy.** On imapclient it searched
both milestones, committed one that the no-search arm left failing, and took the
held-out rate from 0.210 to 0.487 for $5.44 -> $17.32, i.e. 3.2x the money for
+0.277. On simpy it fired on neither milestone, because both scored 0.92 and 0.90
against a trigger threshold of 0.9. **The simpy rows are therefore two samples of
the same configuration, and the 0.463 -> 0.564 gap between them is n=1 variance,
not an effect of search.** Do not quote it as one.

**No single-agent baseline in this batch.** The solo arm was attempted immediately
afterwards, on the same tasks and the same model, with plans merged from these very
`test_first` plans so the one agent inherits their combined wall clock
(`outputs/cpe_solo_spark/plans/`). Both trials died about ninety seconds in with
`stream disconnected before completion`, and the endpoint then reported
`model_cooldown ... reset 149h` for `gpt-5.3-codex-spark`: these four runs spent the
quota. Nothing about the arm is known to be broken — the graph materialises and
dry-runs correctly, one agent, one gate, no custody. So every number here compares
decompositions with each other, and none of them compares against plain Codex.

**The threshold is now the binding parameter, and 0.9 is in the wrong place.**
It was chosen when behaviour was either 1.0 or absent. Observed behaviour now
spans 0.55 to 0.96, so 0.9 searches almost everything on imapclient and nothing on
simpy — the same knife-edge, in both directions, on the two repositories the
B-stage is built from. Calibrate it against this distribution before spending the
nine-run batch.

---

### EXP-20260811-01 — test_first on imapclient and simpy: the suite survives, the score does not get published
- **Status:** canonical (two paid runs, n=1 each; a precondition check, not a result)
- **Date:** 2026-08-11 (UTC)
- **Question:** before spending the three-task × three-repetition budget, does
  `test_first` run to completion on tasks other than pyjwt?
- **Model:** `gpt-5.3-codex-spark` (gpt-5.4 in cooldown), so **none of these numbers
  are comparable with any gpt-5.4 batch.**
- **Artefacts:** `outputs/cpe_tuning_multi/slice_imapclient_simpy.jsonl`,
  `outputs/cpe_tuning_multi/ab-{imapclient,simpy}-multi.test_first-r1-20260810T231358Z/`

| task | $ | wall | milestones committed | held-out counts | rate | fast loop |
|---|---|---|---|---|---|---|
| imapclient | 4.81 | 22.3 min | 2/2 | passed 68, failed 185, error 2 of 267 | 0.255 | not entered |
| simpy | 16.71 | 66.1 min | 2/2 | passed 72, failed 77 of 149 | 0.483 | milestone 2, 3 candidates |

**The suite now survives, and it saturates.** Both runs graded a `spec_tests` stage
on every milestone — the rule-12 fix holds — and every one of them was perfect:
imapclient 38/38, simpy 29/29 then 51/51. A yardstick the implementer is allowed to
read is a yardstick it optimises to completion, so the behavioural axis is degenerate
in exactly the way the blended score was. The quality trigger cannot fire on it.
**Resolved by taking the suite away.** The asymmetry this needed is now structural:
a custody step runs between the author and the implementer, copies `spec_tests/` to
the frozen path and deletes the workspace copy, and the change edge from author to
implementer is cut so the suite cannot arrive as a quoted patch instead. The
implementer works from the design documents alone and is graded on a suite it never
saw. Nothing about scoring changed — grading always read the frozen copy — so the
axis costs the same and is no longer something a candidate can aim at. The numbers
above stay on record as the saturated baseline the next `test_first` batch is
compared against.

**Neither pass rate was published, and the reason is a pin gap, not a failed run.**
Both tasks list one held-out module that is absent from
`configs/codeprojecteval_suite_sizes.json` — `unit_tests/imapclient_test.py` (a
helper base class) and simpy's `unit_tests/test_version.py`. Modules that collect
zero tests were omitted when the file was pinned, and `denominator_faults()` cannot
tell "omitted because empty" from "guessed", so it withholds the rate under rule 7.
The counts are on disk (0.255 and 0.483 respectively) but must not be quoted as
scores until the pin covers every module the dataset ships. **This blocks the whole
B-stage: nine runs would all come back unscored.** Fix the pin first.

**Fixed, and the rates are now published: imapclient 0.255, simpy 0.483.** The
counter only recorded modules pytest reported at least one case for, so a module
that collects nothing was missing from the pin and indistinguishable from one nobody
had pinned. It now seeds every held-out module the dataset ships at zero, the two
modules are pinned at 0, both totals are unchanged (267 and 149), and the stored
counts were rescored in place as `pass_rate_pinned`. The B-stage is unblocked. Note
that both rates are `gpt-5.3-codex-spark` and are not comparable with any gpt-5.4
number.

**Whether a gate can fail is a property of the model, not of the repository.**
EXP-20260810-04 excluded simpy from fast-loop work because its gate passes first
time — under gpt-5.4. Under spark, simpy's milestone 2 failed its gate on
`check_tests/test_benchmark.py::test_store_sim` (the run's event count is 188 where
the test asserts 191), the loop entered, three candidates ran (feedback, added gate
repairer, raised budget) and the raised-budget candidate committed at behaviour
0.902. Search cost $8.23 of the run's $16.71, of which the selected branch was
$4.40. So "which tasks can exercise the fast loop" has to be re-answered per model,
and the population is wider for weak models than the audit implied.

---

### EXP-20260810-06 — The first test_first run: the suite was deleted before it could be graded
- **Status:** canonical (one paid run, then offline forensics and a fix)
- **Date:** 2026-08-10 (UTC)
- **Question:** with the frozen plans rewritten onto `test_first`, does the
  behavioural axis actually appear, and does the quality trigger fire?
- **Cost:** one pyjwt run on `gpt-5.3-codex-spark`, $5.59, 28 min wall clock.

**The run looked like a success and measured nothing.** Two milestones planned, two
committed, both gates passed, held-out rate 0.578. Neither gate report contains a
`spec_tests` stage, so behaviour was `None` on both milestones, the quality trigger
never fired, and the fast loop never ran. The arm cost full price and bought no
tuning — the same outcome as EXP-20260810-04, arrived at through a new route.

**What the artifacts say.** Both test authors wrote a suite: their
`RepositoryChangeArtifact`s name five and six files under `spec_tests/`. Both
builders ran afterwards in the same workspace, at the same base revision, and their
diffs — computed as `git diff HEAD` plus untracked files, so an untracked
`spec_tests/` would appear — contain nothing but the implementation. The directory
was physically gone by then, and the workspaces on disk still have no `spec_tests/`
while the implementation sits there untracked.

**Nothing in the framework removed it.** The subtask workspace is forked once per
attempt and never reset between agent nodes; `snapshot` is read-only; the only
`reset --hard`/`clean -fdx` in the tree is the fast-loop rollback, which runs on
failure paths this run never took; `upstream_change` is prompt wiring, not a
filesystem operation. Which leaves the builders — and their prompts told them to do
it. Every editing agent receives *"Ship only files docs/directory_tree.txt describes:
no notes, plans, logs, scratch directories"*. No document mentions `spec_tests/`.

**Fix:** in a milestone whose plan contains a `test_author`, the builder and the
repairer are now told the suite is their specification, is read-only evidence, and
must be present when they stop — stated after the shipping rule, so it reads as the
exception to it. The test author gets the one-line version. The gate prints
`NOTE no authored suite at spec_tests; behaviour ungraded` when it was handed a
frozen-suite path and found nothing to freeze, so this cannot recur silently.
- **Standing rules this produced:** rules 12 and 13.

**What this says about the money.** The three-task run (④) was blocked on a
precondition everyone believed was met by rewriting the plans onto `test_first`.
Rewriting the plans was necessary and not sufficient: the template only produces an
axis if the suite survives to the gate. One $5.59 probe answered that; nine runs
would have cost roughly $50–150 and answered it nine times.

---

### EXP-20260810-05 — The authored suite discriminates; the trigger reading it did not
- **Status:** canonical (one paid probe, then offline analysis and a fix)
- **Date:** 2026-08-10 (UTC)
- **Question:** does the authored-suite score rank designs the way the held-out suite
  does, and is the quality trigger ready for a three-task run?
- **Cost:** one `test_author` call on `gpt-5.3-codex-spark` (~3 min); everything else
  offline against candidate patches already on disk.

**The axis is not degenerate, which was the point of building it.** The acceptance
gate scored all nine imapclient candidates at exactly 1.0 (EXP-20260810-03). The
authored suite scores them 0.793–0.862 — 46, 49 or 50 of 58 tests. So the thing
`test_first` was added to fix is fixed: designs are now separable at milestone time
without touching the held-out suite.

**Whether it ranks them *correctly* is unanswerable on this population, and the
study was underpowered before it started.** r = −0.204, n = 9, p ≈ 0.6. The
held-out rates it was asked to predict have a standard deviation of 0.021 around a
mean of 0.339; the per-test breakdown says the same thing from the other side — 43
of 58 authored tests pass for every candidate and 7 fail for every candidate, so
only 8 discriminate, and no single one of them separates the held-out means by more
than 0.03. Both suites agree these nine candidates are the same work. Nothing about
the axis's direction can be concluded, and the answer was not available at this
price.
- **Standing rule this produced:** rule 11.

**The paid finding was a bug the probe was not looking for.** Checking why the
threshold was set at 0.8 showed that every committed milestone on record scores
exactly **1.0** on the graded harness score — not the 0.307–0.375 the config comment
claimed, which were held-out pass rates the loop cannot see. Structural stages hold
0.6–0.7 of the weight and all pass, so:

| level | non-behavioural weight | blended score at behaviour 0.79–0.86 |
| --- | --- | --- |
| implementation | 0.60 | 0.917 – 0.945 |
| integration | 0.70 | 0.938 – 0.959 |

At `min_score: 0.8` the trigger could not fire on any milestone ever recorded, and no
blended threshold both fires on bad milestones and spares good ones, because the band
is 0.02 wide. `tuning.quality_trigger` — the whole mechanism for widening the
population past imapclient — was inert on every repository it was written for.
- **Fix:** quality comparison and the trigger both read the behavioural stage;
  `min_score` recalibrated to 0.9 on that axis. Standing rule 10.
- **Second-order consequence:** the epsilon analysis in
  `docs/fast_loop_pareto_protocol.md` was derived against the blend, so
  `quality: 0.02` was tighter than it looked — a 0.069 behavioural gap arrived at the
  selector as 0.021 and counted as a tie. On the behavioural axis 0.02 now means what
  the document says it means: about one authored test in 58.

**A precondition for the three-task run that is not yet met.** Behaviour only has
somewhere to move if the milestone authored a suite. Without one the behavioural
stage is the dataset's visible `check_tests`, which the recorded runs pass 9/9, so
behaviour reads 1.0 and the trigger stays silent — the same inertness by a different
route. Only `test_first` authors a suite, and no frozen plan selects it: the template
postdates them, and the planner is instructed to prefer the cheapest template that
addresses the milestone's risk. Either the frozen plans are rewritten onto
`test_first` (mechanical — every builder role in all four plans is already in that
template's allowed set) or the arm is replanned and the planner may still decline.

---

### EXP-20260810-04 — Which tasks can exercise the fast loop, and a scoring fix that had decayed
- **Status:** canonical (methodology and audit, not a result)
- **Date:** 2026-08-10 (UTC)
- **Question:** the tuning work so far used one repository. Which others can join?

**Two milestones is necessary and nowhere near sufficient.** Across all recorded
CodeProjectEval runs, the risk-first planner returns two milestones for
**imapclient, pyjwt, simpy and bplustree**, and one for **voluptuous** and
**deprecated** (per standing rule 1, that refusal is respected, never overridden).
But the fast loop is invoked only in the gate-failure branch of
`ReadySubtaskScheduler` — a milestone that commits returns before the loop is
reached — and the other three tasks pass their gates on the first attempt, at
held-out rates of 0.78 (simpy), 0.75 (pyjwt) and 0.34 (bplustree) against
imapclient's 0.14. So imapclient was not a convenience sample; it was the only task
where the loop fires at all.

**The authored-suite axis does not change this by itself.** The `test_first` suite
added earlier today makes candidates *rankable* where the gate was degenerate, but
it is graded and non-gating by design, so it cannot make the search *fire*.
Widening the population needs a quality trigger — enter the loop when the gate
passed but the score is low — which is a scheduler change, not a config change.

**Cost is not the obstacle.** pyjwt and simpy run ~11 min against imapclient's
32-40, so a three-task tuning population is cheaper than the single-task one was.

**The denominator defect that would have inflated every task added.** See the
2026-08-10 CHANGELOG entry for the mechanism and the corrected table. What belongs
in the log is why it survived: EXP-20260809-01 recorded this exact problem as
"fixed by collecting on the reference implementation", and that fix was real — but
the counts were cached in the untracked `outputs/` tree and `analyze_ceiling` fell
back to static counting per module without recording it, so the fix held only while
the cache did. Two months of pyjwt evaluations (23 of 25) published a
`pass_rate_reachable` above 1 and nobody was stopped by it.
- **Standing rule this produced:** rule 7. A guard that only fires on an impossible
  *symptom* is not a guard. bplustree was caught because 2.54 > 1; a task whose
  undercount left `passed < total` would still be inflating quietly today, so the
  fault is now raised on the guessed denominator itself.
- **Correction to the record:** bplustree multi was published at 1.125 ± 1.353 and
  is 0.337 ± 0.251 once pinned — most of that headline variance was the divisor
  moving, not the system. pyjwt and simpy shift by ~0.025 in both arms. imapclient
  is unchanged, so EXP-20260810-01 through -03 stand as written.

### EXP-20260810-01 — The repair loop, run for real: 0.000 → 0.163 on imapclient
- **Status:** canonical
- **Date:** 2026-08-10 (UTC)
- **Population:** imapclient, one frozen 2-milestone plan replayed by both arms
  (`review_then_fix` 3 agents, then `gate_then_repair` 2 agents), n=5 per arm,
  arms launched concurrently so endpoint conditions are shared.
- **Arms:** repair loop on (`fast_loop_candidates: 2`) vs off (`0`). Nothing
  else differs — same plan, same budgets, same builder.
- **Artefacts:** `outputs/cpe_tuning/{on,off}_trials.jsonl`,
  `configs/experiments/codeprojecteval_tuning.yaml`.

| arm | hidden pass | milestone gates passed | $/run | wall clock |
|-----|-------------|------------------------|-------|------------|
| repair on  | **0.163 ± 0.082** | 10/10 | $5.08 | 40 min |
| repair off | **0.000 ± 0.000** | 0/10 (5/5 runs died at milestone 1) | $1.90 | 13 min |

**Choosing the repository mattered more than the tuning did.** The first attempt
was going to be pyjwt, where the loop would have fired roughly once in ten runs:
across the previous sweep's 15 gate results, 14 read `passed=True, score=1.0`.
That gate is saturated — it reports a perfect score on work that fails a quarter
of the held-out suite, because it is built from 10 visible tests against 290
hidden ones. Tuning on a rare event with n=5 buys nothing. Three single probe
runs on untried repositories (deprecated $0.21, voluptuous $1.34, imapclient
$5.33) found one whose gate fails reliably; the probes carried tuning switched
on, which costs nothing extra on the runs where the gate passes.

**What the loop bought.** On imapclient, milestone 1's gate fails every time,
and without repair the run stops there: milestone 2 never starts and the
repository scores zero. Five for five. With repair, both candidates fixed it
every time and all ten milestones committed. The loop costs 2.7x and 3x the wall
clock; against a floor of zero that is not a close call.

**The graded score did its job on the main path and never got to do it in the
loop.** Every failed main attempt scored 0.983 with `contracts 34/36` — the same
two symbols missing on all five runs, `imapclient.response_types.Quota` and
`MailboxQuotaRoots`. As a bare pass/fail that failure is indistinguishable from
producing nothing; as 34/36 it names the two lines to look at. But inside the
loop the axis never separated anything: all ten candidates scored exactly 1.0,
so the tie fell to tokens every time.

**Tokens decided all five, sometimes on noise.** Run 4's candidates differed by
1,837 tokens out of 2.07M — 0.09%. The selector is deterministic, so this is
harmless in the sense that both candidates had passed, but it means that when
several candidates pass, we choose among them on an axis uncorrelated with the
held-out outcome. The spread in final scores (0.094 to 0.315) is consistent with
that choice being close to a coin flip. **Reporting rule:** do not present the
token tie-break as a quality decision. It is a cost decision taken among
candidates the gate could not distinguish.

**Open, and not answered here:** whether the graded score helps when candidates
genuinely differ. It needs a milestone where repair sometimes fails, and
imapclient's repair succeeded 10/10.

---

### EXP-20260809-05 — Infrastructure for tuning: parallelism, cost, and a gradient
- **Status:** canonical (infrastructure, no experiment run)
- **Date:** 2026-08-09 (UTC)
- **Why it exists:** the readiness audit for Pareto fast-loop tuning found four
  blockers. Three are now fixed and the fourth is characterised. No benchmark
  was run for this entry; every claim below is from unit tests, dry runs, or
  re-reading existing batches.

**1. Serial execution.** The A/B driver was a shell loop; a three-repository
three-arm sweep at n=3 took most of a day. Trials are independent processes, so
the only thing serial execution protected was the collected-count cache, which
is read-modify-written by every scoring run. That cache now holds a `flock` and
replaces the file atomically, with the expensive collection outside the lock.
`run_codeprojecteval_sweep.py` runs the matrix at a configurable concurrency
(default 4, sized to the model endpoint) and writes one joined row per trial:
pass rate, tokens, cost, wall clock, planned vs. realised turns, gates passed
and failed, and a status distinguishing an unmeasured run from a scored zero.

**2. No cost axis.** `estimated_cost_usd` was null everywhere for three
independent reasons — no price listed for `gpt-5.4`, no model name stamped by
the Codex backend, and a discarded `cached_input_tokens` figure. All three are
fixed. Priced against the existing pyjwt batch as upper bounds: solo $1.39,
multi $2.83, single $10.81. **These are ceilings, not invoices** — a Codex
session re-sends its transcript each turn, so most of its input is cache hits
billed at a tenth of list price, and the historical runs have no cache figure to
subtract. Runs from here on record one and will be labelled `derived`.

**3. No gradient inside a milestone.** The fast loop's selector ordered
candidates by quality → cost → tokens, where quality is the 0/1 gate. Every
candidate it sees has failed, so that key was constant and the cheapest failure
won. The CodeProjectEval harness now grades its stages as ratios (verified
monotone: 0.43 → 0.57 → 1.00 as a three-module package lands), and the selector
optimises gate → graded score → tokens, with the gate as a hard partition.

Per-milestone objective rows are written on **every** run, including untuned
ones, so a tuning loop can be calibrated against existing history rather than
needing fresh runs first.

**4. Noise — not fixed, and it constrains the design.** Within-arm standard
deviations still exceed the between-arm deltas at n=3 (pyjwt single: SD 0.361).
With n capped at 5, a single-repository A/B will not separate arms that differ
by less than roughly a third of the scale. Tuning should therefore be read as
*within-run* milestone selection, where the comparison is between candidates on
the same milestone under the same conditions, and not as a claim about arms.

- **Ceilings on tuning that remain true:** `max_tokens` and `max_steps` are read
  by the openai-compatible and smolagents paths respectively and are inert under
  Codex. `timeout_seconds` is the only budget knob that binds, which is why the
  solo arm inherits summed wall clock rather than a single agent's.
- **Not run:** no experiment accompanies this entry, by instruction. The two
  in-flight solo batches (pyjwt 3/3, simpy 3/3) completed before the code
  changes and are preserved; bplustree solo was interrupted mid-run and its
  partial directory was deleted rather than scored.

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

---

## EXP-20260809-04 — Single vs multi-segment on the role-pool builder

**Status:** `canonical` for mechanism, `underpowered` for effect size
**Batches:** `outputs/cpe_ab/ab-{simpy,bplustree,pyjwt}-{single,multi}-r{1,2,3}-20260809T*`
**Plans:** `outputs/cpe_ab/plans/` (legacy plans preserved at `plans_legacy/`)
**Summary:** `outputs/cpe_ab/ab_summary_role_pool.json`
(`summarize_codeprojecteval_ab.py --engine role_pool`)

First A/B where both arms ran on the role pool and subgraph templates. Arms are
matched on agent count and `max_steps`; see the budget caveat below.

| repo | single | multi | turns single/multi | delta |
|---|---|---|---|---|
| simpy | 0.805 (n=3) | 0.732 (n=3) | 5 / 4 | −0.074 |
| bplustree | 0.034 (n=2) | 0.307 (n=2) | 5 / 4.5 | +0.274 |
| pyjwt | 0.510 (n=3) | 0.771 (n=3) | 4 / 2 | +0.261 |

**What the delta is actually made of.** Not better code — fewer total losses.
Per-run pyjwt single was 0.000 / 0.769 / 0.762 against multi's 0.741 / 0.827 /
0.745. Drop the zero and the arms tie. That zero is the whole mechanism: four
agents ran to completion, the single terminal gate failed, and because a change
only freezes behind a passing gate, `committed: []` and the canonical repo held
**zero lines** of `jwt/`. The other two single runs committed ~1877 lines. Across
nine scored role-pool single runs, two committed nothing and one timed out under
the hidden suite.

Multi-segment arms freeze per milestone, so a failure in the last segment cannot
erase the first. **Segmentation buys the floor, not the ceiling** — where the
single arm commits at all, it matches or beats multi (simpy 0.805 vs 0.732).

**Early exit is real and cheap.** Across 12 gated milestones the mid-milestone
probe passed 10 times and the repairer was never instantiated; it failed twice
(bplustree multi r1, r3), and the repairer fixed r3 through the terminal gate but
not r1. pyjwt multi spent 2 of its 4 budgeted agent turns and **899k tokens
against the single arm's 3.65M — 24.7%** — while scoring higher. The gap exceeds
the turn ratio because each link of a chain re-reads the accumulated context.

**Budget caveat — `max_tokens` does not bind Codex.** `codex_sdk.py` never reads
it; only `openai_compatible_async.py` puts it on the request. The 8192 in every
graph and roster is inert metadata under the Codex backend. Measured: 35 of 74
agent nodes exceeded it (median 8030, max 24643). So "budget-matched" in this
experiment means *agent count and `max_steps`*, never tokens. Left as-is by
decision; do not read `max_tokens` from a run manifest as a spend limit. What
does bind is `timeout_seconds` (1200s author, 1500s repairer) and `max_steps`.

**Why n is small.** bplustree lost one run per arm to a hidden-suite timeout
(generated code with an unbounded loop), leaving n=2. One earlier single run was
excluded by the condition guard for replaying a legacy four-agent plan under the
new builder. Treat the two positive deltas as consistent with the floor
mechanism, not as measured effect sizes.
## EXP-20260811-03 — One agent vs two milestones vs two milestones plus search

**Status:** `canonical` for mechanism, `underpowered` for effect size (n=1 per
cell, except simpy solo n=2)
**Batches:** `outputs/cpe_54_{solo,nosearch,search}/ab-*-20260811T19*`
**Plans:** `outputs/cpe_54_*/plans/`; the solo plans are
`merge_to_single_agent` applied to the very `test_first` plans the other two arms
run, so the one agent inherits their combined wall clock (7800s / 7500s).
**Model:** `gpt-5.4` throughout. `gpt-5.3-codex-spark` went into a 149-hour
provider cooldown after EXP-20260811-02 spent its quota, so this batch is
internally comparable but not comparable with the spark numbers above.

| repo | one agent | two milestones | two milestones + search |
|---|---|---|---|
| imapclient | 0.000 ($0.45, 6m) | 0.090 ($1.95, 24m) | 0.288 ($6.77, 65m) |
| simpy | 0.738 / 0.799 ($0.4, 6-15m) | 0.799 ($1.37, 17m) | 0.805 ($1.25, 18m) |

**The two repositories say opposite things, and the difference is whether the
model can do the job alone.** On simpy every arm lands between 0.738 and 0.805,
and the spread between the two solo repeats (0.061) is wider than any gap
between arms. Decomposition bought nothing there. On imapclient the ordering is
clean and large: one agent 0.000, decomposition 0.090, decomposition plus search
0.288. Read together these are one finding, not two: **structure pays where the
task is beyond the model's one-pass reach, and is dead weight where it is not.**
Any benchmark that averages these two tasks reports a number belonging to
neither.

**Where the solo zero comes from.** The single agent stopped after 6.4 minutes
of a 130-minute budget and declared itself done. It was not empty work: 40 files,
148KB of patch, compile clean, all 17 modules importing, 45/47 milestone
contracts, harness score 0.591. It failed the terminal gate, and because a change
only freezes behind a passing gate, nothing committed and the hidden suite scored
an empty repository. Applying that discarded patch by hand scores **0.180**
(48/267). So the honest reading of the imapclient column is 0.180 vs 0.090 vs
0.288: the single agent's *code* beat the two-milestone arm's code, and only the
commit rule made it a zero. What decomposition reliably buys here is the same
thing EXP-20260809-04 found — the floor, not the ceiling — and what beat both was
search.

**Search fired on one task and not the other, for the documented reason.** On
imapclient the authored suite put the milestones at 0.77-0.86, below the 0.9
quality trigger, so the fast loop evaluated three candidates per milestone
(`cand_add_gate_repairer`, `cand_budget`, `cand_feedback`) and spent 8.65M tokens
against the no-search arm's 2.20M. On simpy the same trigger saw 29/30 and 16/16
and never fired, so that arm is a no-search run with a different name; its +0.007
is noise. The 3.5x cost for 3.2x the score on imapclient is the real trade.

**A measurement flaw inside the fast loop, and it was not the obvious one.**
This milestone's four candidates were scored out of 36, 31, 36 and 31. The suite
was never in doubt — `_freeze_spec_suite` writes once per milestone and every
candidate read the same 36 authored cases. The denominator moved because
`pytest` reports a module it cannot import as **one error rather than as the
cases it holds**, and the harness derived the total from that line. So the
denominator shrank exactly when a candidate's code was broken enough to break an
import, dividing the worst work by the smallest exam and paying a candidate for
losing a whole test file. `discriminating_quality` requires equal
`behaviour_total` to engage, so it silently declined and selection fell back to
comparing raw ratios taken against different denominators.

Fixed the same day: the suite's size is now counted from its source with an AST
walk at grading time, pinned beside the frozen copy, and never allowed to fall —
a collection that finds more than the source shows (a parametrised case) raises
the pin and is remembered. Cases that never ran count as failures. The gate log
now says `SPEC spec_tests 4/7 (vacuous 0 excluded; 3 not collected)` instead of
quietly reporting 4/4. None of the numbers in the table above move: they are the
held-out suite, which was always pinned.

Re-graded offline against the candidate workspaces this run left behind, with no
API calls:

| candidate | as scored | re-graded | cases that never ran |
|---|---|---|---|
| `cand_add_gate_repairer` | 31/36 = 0.861 | 31/36 = 0.861 | 0 |
| first pass (incumbent) | 25/31 = 0.806 | 25/36 = 0.694 | 6 |
| `cand_budget` | 25/31 = 0.806 | 25/36 = 0.694 | 6 |
| `cand_feedback` | 24/31 = 0.774 | 24/36 = 0.667 | 6 |

The winner does not change here and the order is the same, so this run chose
correctly by luck: three of the four were being credited for six cases their code
could not even load, and the margin between best and worst was reported as 0.087
when it was 0.194. The larger repair is that all four now share a denominator, so
`discriminating_quality` — which stands down when they differ — can do the ranking
it was written for instead of falling back to incomparable ratios.

**Infrastructure caveat — the endpoint was shedding streams all evening.** Every
run logged 30-40 `stream disconnected - retrying sampling request` warnings; the
Codex CLI absorbs up to five per request and the backend retries the node up to
six times. Two solo attempts (one on spark, one on 5.4) were killed outright by
it before the third completed, and one imapclient search run lost a candidate's
`contract_author` to a dropped stream, never passed milestone 1, and is voided at
`outputs/cpe_54_search/VOID-infra-*`. **More agents means more exposure**, so a
degraded endpoint biases against exactly the arms with the most nodes. The runs
in the table completed without a node-level infra failure.

## EXP-20260831-01 — Playbook search vs resampling: the table loses to best-of-n, and persistence diagnosis finds what resampling cannot

Five paid runs of the frozen plan `outputs/cpe_tuning/plans/imapclient.multi.test_first.json`
on 2026-08-31 / 09-01, all through a local CLIProxyAPI in front of ChatGPT
subscriptions (Codex OAuth; `qandy1992` Plus for the first two, `zqin30` prolite
after), model `gpt-5.4`. **Channel differs from every earlier entry**, which used
an API key or the xiaoai relay; the derived costs below are token × list price and
are comparable, the quota consumed is not. Runs, in `outputs/cpe_official_*`:

| run | arm | outcome |
|---|---|---|
| `cpe-20260831T062032Z` | playbook k=3 | aborted 11.6 min in: `GraphCompilationError: Missing contract` raised inside candidate *generation*; no usage recorded |
| `cpe-20260831T070623Z` | playbook k=3 | 55 min, $6.46; M1 searched, M2's three candidates dead in 46–151 ms (`CheckpointDriftError`) |
| `cpe-20260831T083458Z` | playbook k=3 | 89 min, $10.31; both milestones searched |
| `cpe-20260831T180037Z` | anchor k=3 | 56 min, ~$6; M1 not triggered, M2 four same-design samples |
| `cpe-20260901T072336Z` | persistence k=3 | 71 min, $8.6; M1 phase two declined, M2 diagnosed |

**Three defects, each found by a run and each masked by a green test suite.**
The generator's validation compile after a plan-layer recompile ran before the
controller registered the new contracts; every generator test passes no compiler,
where `apply_local_edits` skips the compile. Candidate checkpoints were keyed on
`task + candidate` with no milestone, so the second searching milestone found the
first one's records and every candidate died as config drift — invisible before
because M1 had never searched. `milestone_objectives` took the best harness score
over the whole draft list and published it beside the selected candidate's id,
crediting M1 with 0.9375 from a design the selector had discarded when the
committed one scored 0.906. All three fixed with tests that fail without the fix.

**The improve shape scored zero twice on the same two symbols, and it was not the
improver.** `imapclient.response_types.Quota` and `MailboxQuotaRoots` were missing
from the improve candidate in both complete runs. The improver's
`response_types.py` was byte-identical to the builder's: the builder omitted them,
in every candidate. The parent `test_first` has an early gate after the builder
and a repairer behind it, which restored them every time; `test_first_improve`
had neither. Fixed by putting the early gate *after the improver* with an
optional repairer — a pass freezes the improver's change, a failure hands the
exact report to a repair — which reuses the existing wiring unchanged. The naive
alternative, a conditional repairer between builder and improver, races: the
scheduler resolves an input on the first edge carrying a payload and does not
wait for a repairer that may still run.

**No playbook has beaten the anchor.** Across both complete playbook runs, six
playbook appearances, zero wins; the anchor (`concise_feedback` + fresh session)
won every search it did not lose to the incumbent. `pb_tf_q_failures_to_builder`
differed from the anchor by exactly the named-test list and lost 0.8125 vs 0.906,
0.397 vs 0.707, and tied the incumbent once; removed from the table.
`pb_tf_q_improve_after_gate` tied the anchor to four decimals on run-2 M2 at 2.1×
the cost and was correctly discarded on the cost axis.

**Resampling explains most of it.** The anchor arm ran three copies of the anchor
design on M2 (suite of 23): 17, 18, 16 passed, incumbent 16. Best-of-3 gained
+8.7pp; playbook search's best gain was +5.9pp. Same-design σ is **one test**
(0.043), and `epsilon.quality` is 0.02, half a test — the selector has been
treating one-test noise as a real gap. Cross-run scores are worse: the suite is
re-authored per run, so M1's first pass read 0.875, 0.829, 0.970 and 0.857 on one
plan. Only within-run comparisons share a yardstick.

**Persistence: what fails every time is a different population from what
flips.** Intersecting the four M2 samples: five tests failed in all four (fetch
uid/sequence keys, search return type, oauth2 token cache, config default
section, client-from-config), two flipped. The entire best-of-3 gain was the two
flippers; four independent draws never touched the five. Resampling's ceiling on
that milestone is 18/23, and nothing but a change moves it.

**The persistence arm.** Two anchor probes, intersect with the incumbent, then
the table diagnosed from the persistent set with the improver's role chosen from
it. M1: 0 persistent, 4 flaky, phase two declined, best-of-3 committed 0.9655.
M2 (suite of 24): 4 persistent, 2 flaky; the LLM diagnoser — running for the
first time in this project, every earlier run having been a quality search it was
excluded from — returned `functional`, 0.89, `implementer` + `behaviour_critic`,
with a rationale that cited 18/24, the stage results and the four names, and ruled
out budget and public-surface causes; $0.013, 13 s. The phase-two candidate ran
`implementer` as improver and **fixed 2 of the 4 persistent failures**
(client-from-config, oauth2 cache), the two flippers passed, and it still lost:
its re-rolled builder introduced three new `*_requires_server_capability`
failures the incumbent never had. The capability change sits in the builder's
patch and the improver's patch carries it untouched. 18 + 2 + 2 − 3 = 19/24
against the best anchor's 20/24; the ledger says the diagnosis had execution
value, the shape threw it away.

| M2 candidate | passed | persistent fixed | new failures |
|---|---|---|---|
| incumbent | 18/24 | — | — |
| anchor r1 | 19/24 | 0/4 | 0 |
| anchor r2 (committed) | 20/24 | 0/4 | 0 |
| improve, `implementer` from diagnosis | 19/24 | **2/4** | 3 (builder) |

M2 spend: $3.70 here, $3.80 anchor arm, $3.98 playbook arm — the same money, k
unchanged at 3, reallocated.

**What follows.** A candidate that acts on the incumbent's committed workspace
with only the improver, told the persistent set, is the design the data points
at: it cannot lose the 18 the incumbent already passes to a builder re-roll, it
attacks the tests resampling cannot reach, and it costs one agent instead of
three. Then `epsilon.quality` to the measured noise (≈0.05), and the ledger
aggregated per (class, role) before any recommendation is trusted by default.
Template changes made alongside: every planner-selectable template now carries
an optional `test_author` slot (custody keys on the role, so the suite — the only
non-saturated behaviour axis — is reachable from any shape), and `solo` is
withdrawn from the planner catalogue. One run, one ledger entry; 2/4 is an
observation, not a rate.

## EXP-20260902-01 — Persistence arm with evidence-chosen shape and roles: a clean tie, and the rule floor's first misfire

One run of the persistence arm (`cpe-20260902T073231Z`, 76 min, prolite quota
7→10pp) with the full 09-01/09-02 machinery: shape menu, reordering, widened
slots, intents. M1 again had nothing persistent (0/6) and phase two declined —
the second time the mechanism correctly refused to diagnose luck, and the
second time its slot then went unspent, a known gap. M2 split 2 persistent / 5
flaky, and the deterministic floor fired for the first time:
`rule:names:public_surface` seated `integrator` + `contract_critic` with no
model call. The phase-two candidate fixed 1 of 2 persistent failures, the
oauth2 token cache, introduced **zero** regressions — the first playbook
appearance that didn't pay the re-rolled-builder tax — and tied the best
anchor exactly at 25/29, losing the commit on the cost axis.

The rule had no business firing. The suite file was named
`test_client_behaviour_and_public_surface.py`, and `failure_key` carries the
file name, so both persistent tests — config and oauth *semantics*, the same
population the model had classified functional and seated an `implementer`
for the run before — matched the surface tokens through their file name.
Fixed the same day: the rules read the test function name alone. A side
effect worth keeping in mind either way: when the floor fires, the model is
never consulted, so `recommended_shape` stays empty and the table keeps its
own order — rule pre-emption trades shape advice for determinism.

Ledger after six appearances of `pb_tf_q_improve_after_gate`: 3/6 persistent
failures fixed, mean d_best −0.016 (well inside the one-test noise floor),
two ties with the best anchor, and the only two clean failures were the two
since-fixed defects. The row is not losing to resampling any more; it is not
yet beating it. What it still pays for is the builder re-roll, which is the
continuation-candidate argument restated by a third run.

## EXP-20260903-01 — With the slot protected, the continuation boards every search and wins most of them

Three tasks (imapclient, pyjwt, bplustree; frozen test_first plans from
outputs/cpe_54_search), persistence arm, after two changes: a shape pick can
no longer displace a continuation row in a quality search, and a menu row may
be named by its playbook id. 158 minutes, prolite quota 15→22pp.

**Boarding: 4 of 4 triggered searches drafted and ran the continuation; every
incumbent patch replayed cleanly.** The protection did what it was built for.

| search | incumbent | anchors | continuation | outcome |
|---|---|---|---|---|
| imapclient M1 | 0.475 | **0.557**, 0.475 | 0.492 (0/5 persistent, $0.32) | anchor won — 8 flaky vs 5 persistent, resampling's home turf |
| imapclient M2 | 0.696 | 0.652, 0.609 | **0.739** (1/6, $0.39) | committed |
| pyjwt crypto | 0.000 | 0.000, 0.000 | 0.000 (0/4, $0.45) | declined; every candidate zero |
| bplustree M1 | 0.875 | 0.750, 0.813 | **0.938** (2/3, $0.24) | committed |

Across five appearances now (EXP-20260902-02's first win included): **the
continuation has never scored below its incumbent — five of five — while the
anchors fell below theirs in six of ten samples this run.** The ledger after
today:

| playbook | n | beat best anchor | mean d_best | persistent fixed | mean regressions | mean $ |
|---|---|---|---|---|---|---|
| pb_q_continue_improve | 5 | 3/5 | **+0.041** | 4/26 | **0.0** | 0.41 |
| pb_tf_q_improve_after_gate | 7 | 1/6 | −0.013 | 3/8 | 1.17 | 1.48 |
| pb_tf_q_diagnose_then_improve | 1 | 0/1 | −0.031 | 0/3 | 0.0 | 2.08 |
| pb_tf_q_failures_to_builder (removed) | 4 | 0/3 | −0.154 | — | 6.33 | 1.11 |

The only row with a positive paired mean, the only row that has committed
wins (three), no regression ever, at a third of everyone else's price. The
floor mechanism, not luck: it cannot lose what the incumbent already passed.

Two boundary cases worth keeping. imapclient M1 was flaky-dominated (8 of 13
failures flipped) and best-of-2 resampling rightly beat a continuation that
had little persistent material to work with — the allocator lesson is to
spend on resamples when the flaky share is high. pyjwt's crypto milestone
passed its gate with an authored-suite behaviour of exactly zero and nothing
— resample or continuation — moved any of its four persistent failures; when
the base is that far from the yardstick, neither polishing nor re-rolling
inside the same plan helps, and that is the honest case for a design-class
escape hatch left unbuilt so far.

## EXP-20260903-02 — bplustree's 0.05: one undefined constant, and a contract that asked for it in prose

The continuation arm scored bplustree at 0.053 held-out (19/356) while its
authored suite gave milestone 1 a behaviour score of 0.9375. Direct-LLM
baselines on the same task: writer_reviewer 0.626, self_refine 0.483, and
solo/best_of_3/debate at 0.05-0.07. The gap is not a search failure.

**One name.** `unit_tests/test_node.py` opens with
`from bplustree.const import TreeConf, ENDIAN`. The delivered package defines
`TreeConf` and twenty other constants but no `ENDIAN`, so that module dies at
collection, `test_memory.py` with it, and the binary layout the agent invented
(`PAGE_REFERENCE_BYTES = 8` against the reference's 4) fails the rest with
`ValueError: data file is not a...`. 337 of 356 cases descend from that.

**The contract asked for it and did not pin it.** The frozen milestone
contract's first acceptance criterion reads "The package defines TreeConf and
core constants needed by node, entry, serializer, and memory modules". Of the
16 symbols it pinned as checks, all 16 are classes: `callable_or_class` is the
only symbol check the planner reached for, and its name says what it accepts.
`export` exists, does not require callability, and would have pinned a
constant -- no planner has ever emitted one. The documents never name `ENDIAN`
either, so the authored suite could not have tested it: the suite was written
from the same documents, agreed with the implementation's invented layout, and
scored it 0.94 in good faith.

Three changes, none of which can invent the missing name but all of which make
its absence visible:

* `cross_imports`, a new gate stage. Every internal `from <own package> import
  name`, resolved by AST and checked against the imported module. Weighted 0.15
  / 0.10; zero of zero scores as a pass so a single-module milestone is not
  capped. It would not have caught this specific case -- nothing in the package
  imported `ENDIAN` -- and it catches the general shape: a name imported but
  never defined, which is exactly what takes a whole test module out at
  collection time.
* `CONSTANT SURFACE` in the gate log: the module-level ALL_CAPS surface per
  module, reported not scored. A substrate milestone that froze a class and
  published no constants is now visible in the log rather than only in a
  held-out score three hours later.
* The planner prompt now says constants are contract, names `export` as the
  check type for them, and cites this run.

**What none of this fixes.** CPE's held-out suite tests private layout details
the design documents do not specify. That is a ceiling for every method here --
the best baseline on bplustree reached 0.63, not 0.9 -- and it hurts a
decomposed method more, because a milestone boundary is another place for two
modules to disagree about a number nobody wrote down. A single-shot generator
gets that agreement for free.

## EXP-20260903-03 — pyjwt's 0.000 was the gate breaking its own suite

The mirror image of EXP-20260903-02: pyjwt's `crypto_and_jwk_contracts`
recorded a behavioural score of 0.000 (0 of 31) while the task scored 0.769
held-out. Nothing was wrong with the implementation. Run the same frozen suite
against the same frozen workspace with the import path repaired and it reports
**25 passed of 29 collected**.

The suite is authored inside the repository as `spec_tests/`, so its files
share helpers the only way that works there:

    from spec_tests.conftest import load_module

Custody then moves the suite out of the workspace -- correctly, so no candidate
can mark its own exam -- and renames it `<milestone>.spec_tests`. That is not a
legal module name, `spec_tests` is no longer importable, and all four files
die at collection. `--continue-on-collection-errors` keeps the run alive, the
pinned total stays 31, zero cases run, and the milestone is recorded as having
failed every behaviour it was asked about. The `failed_tests` list gave it away
in the end: it held four *file* names, not test ids.

This is not specific to pyjwt. Any authored suite whose files import each other
-- the normal shape once a suite has a conftest helper -- scored zero for its
milestone, and the quality search then spent its budget trying to repair an
implementation that was already mostly right.

Fixed by rebuilding the name rather than moving the suite: the frozen copy
stays exactly where it is, and a scratch directory containing a symlink named
`spec_tests` is put on the import path for the run. Tamper protection is
untouched; the repository stays importable alongside. The same root is given
to the vacuous-baseline run, which had been silently collecting zero cases for
the same reason.

Two false zeros, opposite causes, both found by comparing an authored score to
a held-out one. Neither would have been visible from inside a run.

## EXP-20260904-01 — pyjwt rerun with the suite import repaired: the signal is back, the outcome did not need it

One run, pyjwt only, continuation arm, 44 minutes, prolite 27→28pp.

| milestone | before (EXP-20260903-01) | after |
|---|---|---|
| crypto_and_jwk_contracts first pass | spec_tests **0/31**, four *file* names in `failed_tests` | spec_tests **32/36** = 0.889, proper `file::test` ids |
| token_and_jwks_behaviour first pass | 32/32 | 25/26 = 0.96, no search |
| held-out | 0.769 (226/294) | 0.762 (224/294) |

The gate now grades the suite it froze. `cross_imports` ran on both
milestones (98/98, 109/109) — no dangling internal names on this task, as the
held-out already implied.

The search on M1 fired at 0.889 with 1 persistent failure against 5 flaky
ones — resampling's home turf, as on imapclient M1 — and the anchor that
resampled well (0.972) was committed; the continuation held its floor at the
incumbent's 0.889 and could not move the one persistent case. Correct
behaviour under the corrected signal.

Held-out did not move, and that is the honest reading: M1 was already good
before the fix, the buggy zero only made the search *believe* otherwise, and
M2 — which carries most of the held-out weight — never searched in either run.
What the fix bought is not a score today but the end of a false alarm that
was steering budget on every task whose suite had a conftest helper.

## EXP-20260904-02 — Correction to EXP-20260903-02: ENDIAN explains 32 cases, not 337

EXP-20260903-02 attributed bplustree's 19/356 to the missing `ENDIAN`. That
was wrong by a factor of ten. The held-out suite is 309 `test_tree` cases plus
47 others; `ENDIAN` is imported only by `test_node` (19) and `test_memory`
(13), so it can account for 32. Every implementation in the comparison lacks
`ENDIAN` — all five direct-LLM baselines and AdaMAS — and all of them have
`test_node` blocked. It is not what separates 0.05 from 0.63.

**The 300 are a round-trip bug in AdaMAS's own node layer.**

    unit_tests/test_tree.py::test_insert_split_in_tree_uuid
      tree.insert -> _rebuild_parent_layers -> _get_leaf_nodes
      -> memory.get_node -> Node.from_page_data -> Entry.load
      ValueError: UUID payload must be exactly 16 bytes

The page that `Node` writes is not the page `Entry.load` reads back: the
implementation disagrees with itself, and the disagreement only surfaces once
enough inserts force a split and the tree re-reads its own leaves. Nothing
about the documents is missing here.

**Why no suite caught it.** The held-out matrix inserts up to 1000 keys per
case across page sizes, orders and serializers. AdaMAS's two authored suites
contain no bulk insert at all — no `range(n)` loop of any size — and mention
`split` once and twice; the visible `check_tests` (8/8 for AdaMAS) do not
reach a split either. Both authored suites scored the implementation in good
faith (28/32, 16/16) on the regime they exercised, and the regime the
held-out grades was never entered. This is a *depth* failure of the authored
suite, not a yardstick bug: the test author wrote to the documented behaviour
and stopped short of the structural transitions the design itself describes.

**How the two good baselines escaped — not by design.** `solo` has an
equivalent internal bug (`Node.from_page_data() takes 3 positional arguments
but 4 were given`) and scored 24/356. `self_refine` is that same first pass
plus one revision: its bug happened to show in the visible checks (6/8), the
failure text went back to the model, and the rewrite that fixed the visible
cases also fixed the deep path — 172/356. `writer_reviewer`'s first pass was
internally consistent from the start (8/8 visible) and the review round did
no harm — 223/356. `best_of_3` and `debate` collapsed like `solo`. No baseline
has a mechanism aimed at split-path invariants; two of five were lucky in
where their bugs surfaced.

**What this changes.** The cross_imports stage, the constant-surface log and
the planner-prompt change from -02 stand on their own merits and would not
have moved this number. The lever that would is the test author's mandate:
an authored suite must drive the structure into every transition the design
names — enough inserts to split, enough deletes to merge, overflow, reopen —
because those are exactly the regimes where an implementation can disagree
with itself, and the held-out grades them. The `range(1000)` the held-out
uses is not private knowledge; "a B+ tree splits when a node fills" is on the
first page of the PRD.

## EXP-20260904-04 — Iterating the test author on bplustree: invention to zero in one round, depth in four, and then the bug bites

Six autonomous rounds, one task, one frozen plan, the continuation arm.
Each round changes the `test_author` mandate, re-runs bplustree, and is
audited offline by `scripts/audit_authored_suites.py`: the frozen suites run
against the dataset's reference implementation (a failure there is either an
invented assertion or the reference deviating from its own documents -- the
audit separates the two by checking the test's quoted sentence against the
docs) and against their own milestone's workspace, with depth heuristics.

| round | mandate change | M2 suite on reference | inventions (M1+M2) | M2 largest scenario | M2 on its own code | held-out |
|---|---|---|---|---|---|---|
| v0 | (baseline) | 13/16 | 10 | none | 16/16 | 0.053 |
| v1 | cite the sentence; forbid private layout, catch-all raises, open defaults; reach every named transition | unmeasurable (one top-level import) | 0 | 220 inserts, reopen | 10/12 | 0.096 |
| v2 | import inside each test; root imports need a citation | 8/11 | 0 | 40 inserts | 11/11 | 0.084 |
| v3 | a concrete depth floor; substrate suites fill a node before reading it back | 7/12 | 1 | 120 inserts, order 4 only | 12/12 | 0.053 |
| v4 | the documented configuration matrix; per-test timeouts; gate passes pytest-timeout | 8/10 | 0 | 1205 inserts at order 100, reopen, delete | **7/10** | pending |
| v5 | vary insertion order, read every key back, small entries, delete to merge | 7/14 | 1 | 1500 inserts, 7 orders of insertion, per-key reads | **9/14** | pending |
| v6 | root exports by name only; serializer-typed keys; no unnamed files | pending | | | | |

**Invention was a one-round fix.** Requiring each test to quote the sentence
it derives from, and naming the three recurring inventions, took
document-unsupported assertions from ten to zero. The two "failures" that
remained on the reference in v1 quote the PRD and the architecture verbatim
(`MIN_CACHE_NUM = 5`; strings null-padded to key size): the reference
deviates from its own specification there, and the audit stopped counting
those against the author.

**Depth took four rounds, and the metric had to grow with it.** v1-v3
asked for the transitions the design names and got suites that split a
four-entry node and inserted 40-220 keys at order 4 -- passing their own
implementation every time, so no search ever fired on the tree milestone.
The held-out fails at the documented defaults (order 100, a thousand
inserts) and with 16-byte UUID keys. v4 named that matrix from the documents
and the suite went to 1205 inserts at order 100; v5 added insertion order
and per-key reads and the suite went to 1500 keys in seven orders. Both
failed their own implementation, both fired the search with persistent
failures and no flaky ones, and both persistent sets were the split path:
`ValueError: node does not fit in page`, the same defect class the held-out
had been reporting since EXP-20260904-02. The bulk metric had to learn to
resolve `range(total)` through a module constant before it could see any of
this.

**The reference itself handles the matrix** (1500 keys at order 100 / page
4096 verified directly), so the failures are the implementation's.

**Then the repair side did not close it.** v4: three resamples at 0.70, the
continuation at 0.80, one of three persistent failures fixed (reopen
metadata), committed. v5: resample 0.71 committed, the continuation held its
floor at 0.64 and fixed none of four. The suite now finds the bug the
held-out finds; one specialist pass on the incumbent does not fix a page
layout that is wrong by design. That is a different lever from the one this
entry iterates, and it is where the held-out number now depends.

Held-out for v4-v6 to follow.

**Held-out, v4: 0.126 (45/356)** — the best of the series (v0 0.053, v1
0.096, v2 0.084, v3 0.053), and all of the gain is the one persistent failure
the continuation fixed (reopen metadata). Probing the delivered code with
single held-out cases shows three defects still standing, each a different
one: at order 3 with 4-byte keys, `get()` returns None for a key that was
inserted (split-path data loss); with UUID keys, `UUID payload must be
exactly 16 bytes`; at order 50, the case does not finish inside 60 s.
`test_overflow` passes. The suite that fired the search named none of these
three -- its persistent set was reopen, delete and bulk-iterate at the
documented defaults -- which is the difference between "the suite reaches
the regime" and "the suite names the defect". The three defects also live in
milestone 1's node and entry layout, and a continuation on milestone 2 can
only edit what milestone 2 owns.

**Held-out, v5: 0.121 (43/356).** Two independent runs at the plateau -- v4
0.126 and v5 0.121 -- with the same failure profile: `test_node` blocked on
the missing constant, `test_tree` failing on the split path. The two suites
were the deepest of the series (1205 and 1500 inserts at the documented
order) and both fired the search on persistent failures; the difference
between them and v0-v3 is the reopen fix the v4 continuation landed. The
number is now bounded by what one specialist pass on milestone 2 can repair,
not by what the suite can see.

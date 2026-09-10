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

**Rounds 6 and 7 (v6, v7), authored suites.** Two more rounds of the
mandate, held-out to follow. Both first read `bulk_loop_max = 0` from the
audit; that was the metric's fault, not the suites' -- both hid a
220-250-record scenario behind `record_factory(count)` -- and the resolver
now follows a range name through its helper's call sites (`0e8cc627`).
Corrected: v6 ran 250 records at order 16, v7 220 at order 8, neither at
the documented default of 100 that v4 and v5 had used.

- v6: milestone 1 fired (2 persistent / 6 flaky) and the continuation
  committed at 0.81, fixing 1 of the 2 persistent failures. Milestone 2
  did **not** fire: the suite passed the delivered tree. Depth without the
  default order is depth that misses the split path.
- v7: milestone 1 fired (2 / 7), continuation discarded at 0.78 against an
  anchor resample at 0.87. Milestone 2 fired at 11/18 with **7 persistent,
  0 flaky**, and for the first time the persistent set is the defect the
  held-out probes had found in v4: the six bulk-insert-reopen
  parametrizations (str and uuid keys, ascending / descending / shuffled)
  plus delete. Every candidate, continuation included, scored exactly
  0.611 -- 0 of 7 repaired. Validity 10/18 on the reference, no incidental
  cause, zero inventions, 9 insertion-order scenarios, 3 per-key read-backs.

So the suite has now done both halves of its job in one run -- reached the
regime and named the defect -- and the number did not move, because the
defect is milestone 1's node layout and the milestone 2 continuation edits
only what milestone 2 owns. Depth stability across rounds (order 4 / 16 /
8 in rounds 3 / 6 / 7 against 100 in 4 / 5) is the one thing seven
mandates did not hold; v8 (`7baa8196`) ends the mandate with a checklist
the author reads its own suite against before stopping -- default
configuration past order squared, smallest configuration, every serializer
at its width, three insertion orders with per-key reads -- and round 8 is
the test of whether a checklist holds what prose did not.

**Held-out, v6: 0.261 (93/356)** -- double the plateau (v4 0.126, v5
0.121), and the largest single move in the series. The eval keeps no
per-test list, only the tail, so attribution is partial: `test_node` is
still blocked on the missing constant (19 tests unreachable in every
round), the eleven tail failures are all `test_tree` split-path cases as
before, and the one named test that flips is `test_batch_insert`, which in
v4 failed with `assert 1 == 256` -- the split-path data loss. What is
different about v6 is not its milestone 2 suite (that one never fired) but
its milestone 1: the only round in the series where the milestone 1
continuation committed and fixed a persistent failure in the node layer.
The 48-test gain sits where the defect sits. Read with round 7 -- a
milestone 2 suite that named the same defect and could not move it -- the
two rounds are the same finding from both sides: the lever on bplustree is
a repair that lands in milestone 1, and the fast loop reaches milestone 1
only through milestone 1's own search.

**Round 8 (v8, checklist), first attempt: batch `cpe-20260904T150432Z`,
killed at milestone 2** -- the session hosting it exited at 15:49 and took
the run and the v7 held-out eval with it (both relaunched 2026-09-05). What
survived is the part the round was for. Milestone 1 fired (2 persistent /
8 flaky). The milestone 2 suite was authored and frozen before the kill,
and the checklist held where prose had not: 12 cases, validity 15/19 on
the reference, no incidental cause, **order 100 (the documented default)
and order 4 (the smallest) both present, 10,001 records in the bulk
scenario** -- order squared plus one, read straight off the checklist line
-- 7 insertion-order scenarios, 3 per-key read-backs. Rounds 3, 6 and 7
had run at order 4, 16 and 8; a list the author must tick is what finally
made the documented default appear.

**Round 8 relaunch (`cpe-20260905T071808Z`) failed in two minutes: gpt-5.4
withdrawn upstream.** Every Codex call returned `The 'gpt-5.4' model is
not supported when using Codex with a ChatGPT account`; the proxy's model
catalog logged a Codex-provider change at 20:32 on 2026-09-04, between the
first round 8 attempt (15:04, gpt-5.4, worked) and this one. The proxy
then hid the upstream error behind its failure counter ("no auth
available", twelve consecutive failures) until a restart. The account and
quota are fine; the model is gone. Supported now: gpt-5.4-mini, gpt-5.5,
gpt-5.6-{luna,sol,terra}, gpt-6-astra. Every round v0-v7 ran on gpt-5.4,
so round 8 cannot be a ninth point on that curve; a re-run on a successor
model is a new series and needs its own v0 control. Decision left to the
user; not relaunched.

**Held-out, v7: 0.233 (83/356)** -- and a correction to the v6 reading
above. v7's milestone 2 changed nothing (every candidate 0.611, 0/7
repaired) and its milestone 1 continuation was discarded, yet held-out sits
at v6's level, not v4/v5's. And the table of every round's milestone 1
outcome kills the story that "the gain lands where the milestone 1
continuation committed":

| round | held-out | M1 search | M1 continuation | M2 search |
|---|---|---|---|---|
| v1 | 0.096 | fired, 8 persistent | committed 0.76 | fired, 0 persistent |
| v2 | 0.084 | fired, 4 persistent | committed 0.81 | -- |
| v3 | 0.053 | fired, 6 persistent | committed 0.86 | -- |
| v4 | 0.126 | fired, 0 persistent | -- | fired, 3; continuation committed 0.80 |
| v5 | 0.121 | fired, 0 persistent | -- | fired, 4; continuation discarded |
| v6 | 0.261 | fired, 2 persistent | committed 0.81 | did not fire |
| v7 | 0.233 | fired, 2 persistent | discarded 0.78 (anchor resample committed) | fired, 7; nothing moved |

v1-v3 had milestone 1 continuation commits too and stayed at or under
0.10. What v6 and v7 share and nothing before them has is a milestone 1
whose persistent set is two, down from four to eight -- i.e. a milestone 1
suite that let fewer node-layer defects through -- and v6 is the first
round whose mandate names the three inventions. Whether v6's mandate
changed the milestone 1 suite (validity, depth) is checkable offline; the
audit is running. Until it lands the honest statement is: three levels,
v1-v3 <= 0.10, v4-v5 ~ 0.12, v6-v7 ~ 0.25, n = 2 or 3 each, the step
coinciding with the v6 mandate and with a cleaner milestone 1, and the
mechanism not yet attributed. The earlier sentence "the 48-test gain sits
where the defect sits" over-read one round.

**M1 suite audit, v1-v7 (offline):** validity 32/34, 22/27, 26/28, 34/37,
13/17, 18/21, 22/23; cases 34, 21, 28, 25, 17, 21, 21; bulk 0, 0, 0, 60,
64, 10, 0; split-or-merge 13-16 throughout; zero unsupported tests in every
round. No metric steps at v6. The milestone 1 suite is not what changed, so
the v6/v7 level is not the mandate's doing on milestone 1 either. What is
left is the delivered milestone 1 code itself -- the node layer that the
held-out `test_tree` split cases exercise -- varying from run to run, which
the "2 persistent" reading in v6/v7 would reflect as a property of the
code, not of the suite. Checkable offline: the held-out `test_node.py` is
blocked only by the undocumented `ENDIAN`; run it against each round's
delivered code with that one constant shimmed (analysis only, nothing
enters a workspace or a prompt) and the node-layer quality per round is
measured directly.

**Node-layer probe on the delivered (canonical) repo, v1-v7:** held-out
`test_node` + `test_entry` + `test_serializer` (33 cases) with `ENDIAN`
shimmed, analysis only: 16, 16, 16, 17, 20, 17, 18 passed, the same
failure set every round (`repr`, `__slots__`, `smallest_biggest`,
`get_node_from_page_data`, datetime serializer). Flat. So the v6/v7 level
is not a node-layer change either -- neither the milestone 1 suite nor the
milestone 1 code moved at v6. Whatever changed lives in `test_tree`, the
~300 cases that carry the score, and needs a per-test diff between one
round at each level (v4 vs v6, `test_tree` alone, JUnit output) to name
the behaviour; launched offline.

## EXP-20260905-01 -- new series on gpt-5.5 (gpt-5.4 withdrawn): v8 and a v0 control

Same pipeline, same frozen plan, same continuation arm; `CODEX_MODEL`
gpt-5.5 (the nearest tier the ChatGPT-account Codex channel still
offers). Not comparable with EXP-20260904-04's numbers; the control is the
v0 `test_author` loaded from a frozen copy of the role pool via
`ORCHESTRA_ROLE_POOL_DIR=outputs/roles_v0` (`021f0556`), so the two runs
differ in exactly one file.

**v8 on gpt-5.5, batch `cpe-20260905T073405Z`** (07:34-08:54, ~3 points
of the 5-hour window against ~10 per round on gpt-5.4). Both milestones
fired and both persistent sets were **empty** -- milestone 1: 0 persistent
/ 8 flaky, samples 0.857 / 0.943 / 0.914; milestone 2: 0 persistent / 5
flaky, samples 0.000 / 0.688 / 0.688. With nothing persistent no
continuation was armed, so this run is anchor resampling only. The
milestone 2 suite kept the checklist on the second model: order 100 (the
documented default) present alongside 16 and 8. Held-out, suite audit and
the v0 control launched.

**v8 on gpt-5.5, suite audit.** Milestone 2: 11 cases (16 with
parametrization), 15/16 pass on the delivered code, **10/16 on the
reference**, zero unsupported by the documents, no incidental cause; depth
360 records at order 100 (the checklist's "more than order squared" was
not met on this model -- the first gpt-5.4 attempt wrote 10,001), six
insertion-order scenarios, no per-key read-backs. Milestone 1: 27 cases,
31/35 on the reference. The six milestone 2 cases the reference fails were
run on the reference one by one, and five of them are the reference
disagreeing with the documents, not the author erring:

- `delete` -- PRD line 18 and UML line 14 promise it; the reference
  `BPlusTree` has no `delete` at all.
- default configuration past order squared -- the reference's own
  `Node.dump()` asserts a full leaf fits the page, and at its documented
  defaults (order 100, 8-byte keys, 32-byte values, 4096-byte pages) it does
  not; the reference cannot run its own documented default.
- overflow value across reopen -- the reference returns `None` for it.
- `bplustree.Serializer` at the package root -- the reference does not
  export it; the documents list it.
- the two `batch_insert` parametrizations -- the same `dump()` assertion.

So "validity on reference" is not a suite-quality number on this task; the
split the audit already makes (`doc_backed_but_reference_disagrees` vs
`unsupported_by_docs`) is the one to read, and here the second list is
empty. The delivered code passing 15/16 means it follows the documents
where the reference does not -- behaviour the held-out, which tests the
reference, cannot reward. That is a ceiling on this task that no amount of
test authoring moves: the held-out contains no `delete`, no default-order
bulk case, and the reference's overflow bug is the held-out's expected
behaviour.

**Per-test `test_tree` diff, v4 vs v6 (canonical repos, JUnit, analysis
only).** 308 cases: v4 31 passed, v6 87. 59 flip up, 3 down; **55 of the
59 are parametrizations of `test_insert_split_in_tree`** that in v4 died
with `get()` returning `None` after a split (and `key already exists`),
spread across every order (3/4/50), page size, key size and both
serializers -- a partial fix of the split path in `tree.py`, which is
milestone 2's own file. The node layer did not move (probe above), so the
earlier framing "the defect lives in milestone 1's node layout and
milestone 2 cannot reach it" was wrong for this gain: it lived in
milestone 2 and milestone 2's resample delivered it. What still fails in
both (215 split cases) is three different defects:

| residue | cases | layer |
|---|---|---|
| per-test timeout (>30 s) | 105 | performance -- page scans on insert, either layer |
| `cannot fit 'int' into an offset-sized integer` | 48 | key width in the serializer / entry layout -- milestone 1 |
| `get()` returns `None` after split | 64 | the split path -- milestone 2, the same defect half-fixed |

So the honest map: the v4->v6 step is a milestone 2 fix; of the residue,
about a fifth is milestone 1's, a fifth is milestone 2's, and half is a
performance wall that no test in the authored suites reaches (the
authored bulk scenarios run inside 120 s on the delivered code). The
cross-milestone routing argument (option A) still applies to the 48, not
to the whole residue; the performance wall is a different lever entirely.

**v0 control on gpt-5.5, batch `cpe-20260905T085707Z`** (08:57-09:59,
~2 points of the window; `ORCHESTRA_ROLE_POOL_DIR=outputs/roles_v0`, the
original `test_author`, every other file identical to the v8 run). Both
milestones fired, both persistent sets empty -- milestone 1: 0 / 8 flaky,
samples 0.676 / 0.721 / 0.956; milestone 2: 0 / 4 flaky, samples 0.842 /
0.789 / **1.000**. The milestone 2 suite runs at order 4 only (16
constructions, none at the documented default), and one candidate passes
it outright -- the v0 signature from EXP-20260904-04 reproduced on the
second model: a suite the code can fully satisfy without touching the
split path. Held-out and audit launched; the pair (v0, v8) on gpt-5.5 is
the first comparable two-point series on this model.

**v0 vs v8 suite audits on gpt-5.5, milestone 2.** v0: 17 cases, 16/19
on the delivered code, **0/1 on the reference** -- one collection error:
the suite does `from bplustree import Serializer` at module top, the
reference exports no `Serializer` at the root, and the whole file fails
to import; 256-record bulk at order 4 only, no insertion-order scenarios,
no per-key read-backs. v8: 11 cases, 15/16 delivered, 10/16 reference
(five of six reference disagreements being the reference's, above), 360
records at order 100, six insertion orders. Both suites make the same
documented claim about the root export; v8 makes it inside one test and
loses one case, v0 makes it at import time and loses the suite. That is
the v2 rule ("import inside each test") doing on the second model exactly
what it was written for, and the v4/v8 depth rules putting the documented
default order into the suite where v0 never leaves order 4. What the
mandate could not do on either model is make the search fire on a
persistent failure: both gpt-5.5 runs found every failure flaky.

**Held-out, v0 control on gpt-5.5: 0.890 (317/356), the whole suite in
124 s.** `test_node` still blocked on `ENDIAN` (19 cases); 20 failures,
all in `test_tree`. Against every number in this log for bplustree -- the
gpt-5.4 series topping out at 0.261, the direct-LLM baselines at 0.626 --
this is a different regime: gpt-5.5 with the *original* shallow
`test_author` delivers an implementation that passes nearly all of the
held-out. The v8 held-out from the same model started an hour earlier and
is still running, which on this suite means per-test timeouts, i.e. the
v8 run's delivered code is slow where v0's is fast. Leak check on the
delivered package against the reference recorded below.
Leak check: the v0 delivered package shares no identical file with the
reference (8 files, 1450 lines vs the reference's 1509; 2269 differing
lines in a recursive diff) and neither does v8's (1537 lines, 2366
differing); the two delivered packages differ from each other by 1575
lines. Three independently written implementations; 0.890 is the agent's.
Speed probe on the two gpt-5.5 packages, six held-out `test_tree` cases
(batch insert, create-and-load, iteration, uuid split, length hint) under
the eval's 30 s per-test cap: both pass the same four and fail the same
two (`test_length_hint_tree`, `test_batch_insert_no_in_order`); **v0 in
1.1 s, v8 in 33.5 s**. So the v8 package is not hung and not wrong on
this subset -- it is about thirty times slower, and on a suite of 308
split cases many of which run thousands of inserts, the 30 s cap turns
"slow" into "failed". That is the mechanism behind the still-running v8
held-out: the same per-test timeout wall that took 105 of the 215 residual
split cases on gpt-5.4 (above), reached here from the other side by a
correct-but-slow implementation.

**Held-out, v8 on gpt-5.5: 0.464 (165/356), 1:23:49 of wall clock.** The
tail is all `test_insert_split_in_tree`; at 30 s per case the run time
says roughly 170 cases hit the cap. The first comparable pair on this
model:

| gpt-5.5, same plan, same arm | v0 (original test_author) | v8 (checklist) |
|---|---|---|
| held-out | **0.890** (317/356), 124 s | 0.464 (165/356), 5030 s |
| milestone 2 suite on delivered | 16/19 | 15/16 |
| milestone 2 suite on reference | 0/1 (import error) | 10/16 (5 of 6 the reference's) |
| milestone 2 samples | 0.842 / 0.789 / 1.000 | 0.000 / 0.688 / 0.688 |
| persistent failures | 0 | 0 |
| delivered code, 6-case probe | 4/6 in 1.1 s | 4/6 in 33.5 s |

Reading, in order of confidence:

1. **The model, not the suite, set the level.** gpt-5.5 with the shallow
   v0 suite delivers 0.890 on a task where gpt-5.4 never passed 0.261 and
   the direct-LLM baselines topped at 0.626. Every EXP-20260904-04
   conclusion about "what test_author can and cannot do" was measured
   inside a regime the model change has left.
2. **On the stronger model the deeper suite cost 0.43, n = 1 each.** The
   mechanism is not wrong code -- the v8 package passes the same probe
   cases as v0's -- but a package thirty times slower, which the
   held-out's per-test cap scores as failure. What the v8 suite asks for
   and v0's does not: order 100 with hundreds of records, three insertion
   orders, delete with merging, overflow across reopen, all under a 120 s
   test timeout the suite itself sets. The candidate that satisfied that
   suite (0.688) is a heavier implementation than the one that satisfied
   v0's (1.000). One pair cannot separate "the mandate steers toward
   slow" from "the resample drew a slow one"; it can say the direction of
   the effect on this model is not the direction it had on gpt-5.4.
3. **The reference-versus-document gap is now a live cost, not a
   ceiling.** Five of the v8 suite's six reference disagreements are
   behaviours the documents promise and the reference lacks; on gpt-5.4
   the code never got far enough for that to matter, on gpt-5.5 the suite
   spends the milestone's budget on `delete`, overflow-across-reopen and
   default-order fit, none of which the held-out can score.

What this does to the plan: the test_author loop's product on gpt-5.4 --
no inventions, in-test imports, depth by checklist -- is intact as
suite-quality work (the v0 suite still cannot be collected on the
reference). But "stronger suite, higher held-out" is not a monotone
relation on this task, and before any further mandate change the next
measurement is the cheap one: v0 and v8 once more each on gpt-5.5, to
find out whether 0.890 / 0.464 is a level or a draw. Cost: ~3 points of
the window per run.

**Repeats on gpt-5.5 (2026-09-06): v8 `cpe-20260906T070812Z`, v0
`cpe-20260906T080421Z`, run back to back, ~8 points of the window for all
four gpt-5.5 runs together.**

| gpt-5.5 | run 1 | run 2 |
|---|---|---|
| v0 (original test_author) | 0.890 (317/356), 124 s | 0.899 (320/356), 132 s |
| v8 (checklist) | 0.464 (165/356), 5030 s | **0.902** (321/356), 121 s |

0.464 was the draw, not the level. On gpt-5.5 the level is ~0.90 for
both suites (three of four runs within 0.012 of each other), and the one
low run is a slow package the per-test cap failed. The suite does not
set the level on this model; it sets a tail risk -- one of two v8 draws
was thirty times slower -- and n = 2 cannot say whether that risk is the
suite's or the dice's.

What the v8 repeat's search did is the more useful record. It is the
first gpt-5.5 run with a persistent failure, and the failure was
`test_default_configuration_splits_more_than_order_squared_and_reopens`
-- the checklist's own line, and the behaviour the reference itself cannot
run (its `dump()` asserts a full leaf fits the page at order 100). The
continuation armed on it scored 0.294 with ten regressions and was
discarded; the anchor resample at 0.941 committed and scored 0.902
held-out. So the mandate manufactured a persistent failure the held-out
does not reward, the repair machinery correctly refused a fix that broke
ten other things, and the round ended on resampling -- the machinery
working as designed on a target that was wrong. The v0 repeat, by
contrast, never fired at all (suite passed the delivered code outright at
order 4) and scored the same.

Closing the gpt-5.5 series at four runs: the model set the level; the
test_author work holds as suite quality (the v0 suite still cannot be
collected on the reference); on this task and this model the extra depth
buys no held-out and, through the document-versus-reference gap,
occasionally costs some. Next lever, if bplustree stays in the set, is
not the suite: it is a scale-and-time requirement so a slow-but-correct
package is caught before the held-out's cap catches it.

## EXP-20260906-01 -- gpt-5.5 on imapclient and pyjwt: v9 (scale is a time budget) vs v0 control

`test_author` v9 (`54c23772`) turns bulk scenarios into speed tests (20 s,
sized to the documents' ordinary use, never raised; everything else 60 s).
Four runs back to back, ~12 points of the window; each held-out started
as its run ended. Same frozen plans as the gpt-5.4 series.

| gpt-5.5 | v0 (original) | v9 (checklist + time budget) | search |
|---|---|---|---|
| imapclient | 0.371 (99/267) | 0.356 (95/267) | fired both; v9 9 persistent, continuation 0/9 discarded; v0 5 persistent |
| pyjwt | 0.755 (222/294; one held-out module errors) | **0.827** (243/294) | v9 did not fire; v0 fired at M1, 0 persistent |

Both held-outs run in seconds on both packages -- no timeout wall here;
the v9 budget line had nothing to catch on these tasks.

**imapclient is half ceiling.** Per-test on the canonical packages
(staged like the eval): v9 160 failures, v0 156. Of those, **86 / 79 are
white-box** -- the held-out patches `MockIMAP4._get_response`, calls
`_proc_folder_list`, expects a module-level `select`, asserts "mock
called once": tests of the reference's private dispatch that an
independently written client cannot satisfy whatever it does. The other
74 / 77 are behaviour: `ProtocolError` not raised by
`parse_fetch_response`, return shapes (`('OK', [b'Success'])` where
`b'Success'` is expected), `list_folders` error handling -- many small
gaps, no single defect. So on this task ~0.33 of the score is
unreachable by construction, ~0.29 is behaviour, ~0.37 passes. The v9
suite's nine persistent failures (fetch parsing into documented types,
modified-UTF-7 folder names, idle flow, search criteria normalisation,
silent-flag returns) are the behaviour half, named correctly; the
continuation fixed none and was discarded. Same shape as bplustree v7:
the suite points, the one-pass repair does not land.

**pyjwt: v9 ahead by 0.07, n = 1.** Failures are diverse and small on
both packages (deprecation warnings not emitted, JWK dict key order and
`key_ops`, "Expected a string value" type checks, `PyJWKClient` cache
counts); no dominant signature. v0's package also breaks one held-out
module's collection (`test_utils`), which v9's does not -- the in-test
import discipline again, on the third task.

Three tasks on gpt-5.5 now: bplustree ~0.90 either suite, imapclient
~0.36 either suite with half of it a white-box ceiling, pyjwt 0.76 -> 0.83
with the deeper suite. The suite is not the level-setter on any of them;
where it moves the number it does so through validity (collection) and
through what the search can name, and the repair side lands none of the
named persistent failures on any task or model so far.

## EXP-20260907-01 -- repair evidence: the continuation can run what it is asked to fix

`repair_evidence.py` (`51666867`, `9a64b79d`): a continuation gets, beside
its repository, the persistent tests cut out of the frozen suite, their
output on the replayed incumbent, and the command. Validation on gpt-5.5,
same plans and arm as before:

| task | persistent | continuation `pfix/ptot` | before this change | continuation score vs best anchor | outcome |
|---|---|---|---|---|---|
| imapclient | 3 (fetch dict keyed by id, idle flow, quota dataclasses) | **3/3**, 0 regressions | 0/9 (2026-09-06) | 0.737 vs 0.789 | discarded |
| bplustree M1 | 1 (`iter_slice` empty and final partial slice) | **1/1**, 0 regressions | 0/1, 0/7 (v8b, v7) | 0.756 vs 0.956 | discarded |

The mechanism does what it was built for: every named failure fixed,
nothing regressed, on the first try, on both tasks -- against zero of
seventeen across the previous three attempts. Both were still discarded,
and the reason is the base, not the repair: the continuation is armed on
the **first-pass** incumbent's change (0.579 and 0.733), fixes exactly its
persistent set (+3 and +1 tests), and is then compared on the whole suite
against an anchor resample that happened to draw better (0.789, 0.956).
A repair that starts from the best sample instead would have committed
in both rows. Held-out: imapclient 0.390 (anchor's package; the
continuation's was never delivered), bplustree 0.449.

**Validation rerun (`cpe-20260907T041858Z`) died at its first Codex call:
gpt-5.5 now answers `model_not_found` for this account** (the proxy's
catalog still lists it; gpt-5.6-terra, gpt-5.6-sol and gpt-6-astra answer;
gpt-5.4-mini has left the provider entirely). Second forced model change in
three days. The best-sample arming (`bab153c3`) is therefore unvalidated
on a live run; its unit tests pass. Not relaunched on another model
without a decision.

**Terra draws for the best-sample arming (`bab153c3`), 2026-09-07.** Model
channel moved again (gpt-5.5 withdrawn for the account; gpt-5.6-terra
needs a 0.149 Codex binary, `CODEX_BIN`, `9a64b79d`-era commits). Three
draws on gpt-5.6-terra, none produced a continuation to validate:

| run | search | persistent | held-out |
|---|---|---|---|
| imapclient `0907T054811Z` | did not fire (both milestones >= 0.9 first try) | -- | 0.378 |
| imapclient `0907T060635Z` | did not fire | -- | 0.374 |
| bplustree `0907T062706Z` | M2 fired: samples 0.412 / **0.000** / 0.412 | **0 of 10** | 0.657 |

The bplustree row is the finding: ten failures, none persistent, because
one probe collapsed to 0.000 at a gate stage with no failure list, and an
empty list in the intersection empties the persistent set. The gpt-5.5 v8
run had the same shape (0.000 / 0.688 / 0.688, "0 persistent"). A sample
that ran no tests is not evidence that a test passed.

**bplustree on terra with the collapsed-probe fix, `cpe-20260907T072100Z`
(07:21-09:30 incl. held-out) -- the repair line closes.**

| | |
|---|---|
| milestone 2 search | fired; three samples all 0.467; **8 persistent / 0 flaky** (the collapsed-probe fix, `059c7254`, is what made the set exist) |
| persistent set | batch_insert per serializer (3 parametrizations), default-order persistence past order squared, delete with order after reopen, insertion orders searchable after reopen |
| continuation | evidence staged; **6/8 fixed, 0 regressions**; 0.867 vs best anchor 0.467, d_best **+0.40**; **committed** |
| held-out | **0.699** (249/356) against 0.657 for the previous terra draw with no continuation |

Every piece is now observed working in one run: the search names the
persistent set, the continuation gets the tests and their output beside
its repository, repairs six of the eight without breaking anything,
beats the anchor on the whole suite, commits, and the delivered package
scores higher on the held-out. The best-sample arming (`bab153c3`) was
not exercised -- all three samples tied, so the incumbent was the base by
design -- and was not needed for the commit.

Paired, within-model record of the repair side, before and after
`repair_evidence.py`: 0/17 fixed across three continuations (bplustree
0/7, 0/1; imapclient 0/9), then 3/3, 1/1 (both discarded on the
first-pass base) and 6/8 committed. That column is the deliverable of
this line; it does not depend on which model the channel happens to
serve.

## EXP-20260908-02 -- NL2Repo-Bench, Medium bucket: integration and task selection

NL2Repo-Bench (arXiv 2512.12730; 104 tasks, one `start.md` per task,
upstream pytest suite as the exam) ships its suites only inside per-task
Docker images the repository does not distribute, and this host has no
Docker. `scripts/nl2repo_to_cpe.py` reconstructs each task in
CodeProjectEval's on-disk shape: the document split into PRD and
architecture docs, the environment pinned from the document (Python
version and dependency block, in its several formats), the upstream
project cloned and the release tag chosen whose collected count matches
the document's (`test_case_count.txt`), that tag's source as the
reference and its tests as `unit_tests`, a smoke `check_tests`, and a
per-task environment. AdaMAS changes: `CPE_DATASET_ROOT` /
`CPE_ENV_ROOT` overrides and `src/` on `PYTHONPATH` (harness and eval).

**Planner scan, all 46 Medium tasks (gpt-5.5, max 4 milestones):** 19
two-milestone, 27 one-milestone, none deeper. **Feasibility gate on the
19** (minus plac and autojump, flat layouts): nine reconstruct cleanly --
count matches the document to within a few cases and the reference
passes its own suite -- aiofiles (211/211), emoji (102/102),
python-dotenv (209/209), python-pathspec (119/119), tablib (173/172),
tenacity (124/124), ftfy (346/336, 330 pass), python-jose (3.5.0,
470/458, 454 pass), voluptuous (149/152, 148 pass). flask-restful is
kept as approximate (0.3.10, 322/362, 267 pass). Dropped: databases
(external DB servers), pytorch-grad-cam (torch), binaryalert (Python 3.7
AWS project), flasky (an app), pylama (tests need installed entry
points), stamina and gitingest (no tag reproduces the count). Manifest
and frozen plans: `configs/datasets/nl2repo_medium.json`,
`configs/datasets/nl2repo_medium_plans/`. Pilot run on `nl2_aiofiles`
in progress; bugs found so far: task-id validation against the wrong
root, yaml-pinned dataset root, tree/dependency/tag parsing (fixed).

**Pilot, nl2_aiofiles on gpt-5.5, third attempt `cpe-20260908T111133Z`
(11:11-13:27): held-out 0.919 (194/211, 7 failed, 2 errors, 8 skipped).**
Both milestones committed; the LLM diagnosis ran (two 200s). The
pipeline runs end to end on an NL2Repo task. Bugs the three attempts
found, all fixed and committed:

| # | bug | fix |
|---|---|---|
| 1 | runner validated task ids against the CPE root; yaml pins `dataset_root` | pass `--dataset-root`; `CPE_DATASET_ROOT`/`CPE_ENV_ROOT` overrides (`5d1017f3`) |
| 2 | expected modules derived as `src/aiofiles.aiofiles.base`; imports stage failed every candidate, a 100%-spec package scored `harness_failed` | src-layout derivation + package-namespace filter (`0e86a9d9`, `8020bdec`) |
| 3 | imports/contracts stages import in-process without `src/` on `sys.path` | (`beafe70b`); tests stage, eval and ceiling collection likewise (`5e82a061`, `48d241ea`) |
| 4 | diagnosis model pinned to gpt-5.4 in all four arm yamls -- every LLM diagnosis since 09-04 was 400 `model_not_found`, silently the rule floor | unpinned (`4c39b0ca`); found via `~/.cli-proxy-api/logs/error-*.log` |
| 5 | held-out eval passes `--timeout` flags the nl2 envs lacked -> unscored | pytest-timeout in every task env; converter installs it (`abd1ac45`) |
| 6 | eval scores only tasks in the pinned suite-size table | ten nl2 tasks pinned (`2c9ab02d`, `48d241ea`) |
| 7 | a recompiled-plan candidate with **no gate result** was VALID and got committed over a graded 1.0 | no gate result => HARNESS_FAILED (`60858ee4`); their templates run no gate node -- open |
| 8 | (CPE chain, cookiecutter) failure search with every candidate failed raised at commit and killed the task | milestone fails on the record (`bebf8f26`) |

Held-out numbers on NL2Repo are "upstream suite at the count-matched
tag", not the paper's image; aiofiles' 9 non-passes include the root
`os.access` artifact and two aiohttp-server tests.

## EXP-20260908-01 -- full CodeProjectEval pass on gpt-5.5 (in progress; chain findings so far)

18 tasks, continuation arm, v9 test_author, all repair-side fixes; frozen
plans for four tasks, planner drafts (saved) for the rest. Runner bugs
and dataset ceilings the chain exposed, all fixed or recorded:

- **cookiecutter**: failure search with every candidate failed raised at
  commit and killed the task (`bebf8f26`). Rerun pending.
- **python-hl7 M2**: change set recorded the deletion of a `__pycache__`
  .pyc; the post-apply check found it regenerated and refused the whole
  milestone (`canonical_merge_conflict`). Caches now filtered from change
  sets and excluded from every workspace index (`9ee4ad76`). Rerun pending.
- **rsa**: infra failure (upstream outage at 17:25) marked done by the
  chain because the runner exited 0; unmarked, rerun pending.
- **flask**: the CPE flask env's pytest 9.1.1 cannot collect the reference
  suite (`_pytest.monkeypatch.notset`); pinned pytest<8, re-pinned suite
  size 432->490 (`494520df`). Scored 0.639.
- **Undocumented-name ceilings** (held-out imports a name no document
  mentions): parsel `LXML_SUPPORTS_HUGE_TREE` (162/250 cases unreachable,
  reachable 68/88), portalocker `LockerType` in conftest (63/63
  unreachable), bplustree `ENDIAN` (19).
- Chain stopped before tinydb at 17:41: upstream ChatGPT backend down
  (Cloudflare 522), surfaced by the proxy as "credentials cooling down".
  Resumes automatically when the probe answers.

Held-out so far: bplustree 0.89, imapclient 0.356, pyjwt 0.813, simpy
0.765, csvs-to-sqlite 0.72, deprecated 0.585, simplejwt 0.565, flask
0.639, parsel 0.272 (ceiling), portalocker unscoreable (ceiling),
python-hl7 0.53 (M1 only; M2 refused), rsa -- (infra). Continuations:
5 armed, 3 fixed-and-committed (bplustree M1 1/1, imapclient M2 6/6,
pyjwt M2 5/5), pyjwt M1 0/14 (suite-wide collapse), csvs-to-sqlite 0/2
(LLM-chosen gate_repairer).

## EXP-20260908-01 (closed 2026-09-09 00:27) -- full CodeProjectEval pass on gpt-5.5: AdaMAS vs five direct-LLM baselines

18 tasks, one model (gpt-5.5), one scoring path (scripts/eval_codeprojecteval.py
with today's env fixes and re-pins). AdaMAS: continuation arm, v9 test_author,
every repair-side fix of 2026-09-07/08; frozen plans for four tasks, planner
drafts (saved under outputs/cpe_full55/plans/) for the rest. Baselines:
/root/projects/mas-baselines/runner.py, BASELINE_MODEL=gpt-5.5, 207 calls.

| task | AdaMAS | search | persist | continuation (score, ✓committed) | pfix/ptot | milestones | solo | best_of_3 | self_refine | writer_rev | debate | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bplustree | 0.890 | quality/quality | 1/1 | 0.98✓ | 1/1 | CC | 0.118 | 0.082 | 0.082 | 0.890 | 0.087 | held-out imports undocumented ENDIAN (19 cases) |
| imapclient | 0.356 | none/quality | 6 | 1.00✓ | 6/6 | CC | 0.333 | 0.318 | 0.390 | 0.337 | 0.318 |  |
| pyjwt | 0.813 | quality/quality | 14/5 | 0.00✗/1.00✓ | 0/14/5/5 | CC | 0.639 | 0.735 | 0.735 | 0.748 | 0.711 |  |
| simpy | 0.765 | none/none | — | — | — | CC | 0.698 | 0.732 | 0.745 | 0.732 | 0.765 |  |
| cookiecutter | 0.29† | failure | — | — | — | F | 0.27* | 0.24* | 0.26* | 0.25* | 0.25* | 2 repository/ modules blocked; pin partly static |
| csvs-to-sqlite | 0.720 | quality | 2 | — | — | C | 0.000 | 0.000 | 0.640 | 0.560 | 0.600 |  |
| deprecated | 0.585 | none | — | — | — | C | 0.585 | 0.585 | 0.574 | 0.591 | 0.614 |  |
| djangorestframework-simplejwt | 0.565 | failure/none | — | — | — | CC | 0.031 | 0.031 | 0.026 | 0.031 | 0.000 |  |
| flask | 0.639 | failure | — | — | — | C | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | conftest imports undocumented flask.globals.app_ctx (env pytest<8 pinned) |
| parsel | 0.272 | none | — | — | — | C | 0.084 | 0.096 | 0.096 | 0.088 | 0.096 | test_selector imports undocumented LXML_SUPPORTS_HUGE_TREE (162/250 unreachable) |
| portalocker | 0.00* | none/quality | 0 | — | — | CC | 0.00* | 0.00* | 0.00* | 0.00* | 0.00* | conftest imports undocumented LockerType (63/63 unreachable) |
| python-hl7 | 0.530 | quality | 3 | 1.00✓ | 3/3 | C | 0.340 | 0.470 | 0.480 | 0.340 | 0.440 |  |
| rsa | 0.700 | none | — | — | — | C | 0.700 | 0.710 | 0.700 | 0.710 | 0.710 |  |
| tinydb | 0.750 | failure/none | — | — | — | CC | 0.000 | 0.873 | 0.868 | 0.877 | 0.873 |  |
| trailscraper | 0.60* | none/failure | — | — | — | CF | 0.56* | 0.65* | 0.57* | 0.41* | 0.56* | 15 modules counted statically (boto not importable in env) |
| voluptuous | 0.547 | quality | 3 | — | — | C | 0.509 | 0.522 | 0.534 | 0.478 | 0.460 |  |
| xmnlp | — | none | — | — | — | C | — | — | — | — | — | reference needs tensorflow + downloaded model weights: unscoreable |
| zxcvbn | 0.65* | quality | 4 | 1.00✓ | 4/4 | C | 0.35* | 0.52* | 0.52* | 0.55* | 0.35* | pin partly static |

* = raw passed/pinned where the strict eval declined to score (static pin or blocked module); † = ungated agent workspace (no milestone committed); C/F = milestone committed/failed

**Aggregates.** Over the 13 rows the strict eval scores for everyone:
AdaMAS 0.626, writer_reviewer 0.491, self_refine 0.452, debate 0.436,
best_of_3 0.396, solo 0.311. Over the 17 rows with any number (raw
fractions included): 0.569 / 0.447 / 0.425 / 0.402 / 0.386 / 0.307. AdaMAS
is at or above every baseline on 12 of 17 scored rows; below on rsa
(0.700 vs 0.710, noise), simpy (0.765 = debate), tinydb (0.750 vs ~0.87)
and trailscraper (0.60 vs 0.65 best_of_3; its milestone 2 failed the
gate) and one baseline on imapclient (self_refine 0.390 vs 0.356).

**Repair side, whole pass.** Six continuations were armed; they fixed
**19 of 33** named persistent failures with zero regressions and five
committed (bplustree M1 1/1, imapclient M2 6/6, pyjwt M2 5/5, python-hl7
3/3, zxcvbn 4/4); the one that did not (pyjwt M1, 0/14) faced a suite
that collapsed to 0.00 on every sample. Quality searches with a
persistent set that armed no continuation: csvs-to-sqlite (LLM diagnosis
chose gate_repairer, 0/2) and voluptuous (3 persistent, table row
declined). Two milestones failed the gate cleanly under the new guard
(cookiecutter; trailscraper M2).

**Ceilings shared by every method** (held-out imports a name no
document mentions, or the environment cannot run the reference):
bplustree ENDIAN, parsel LXML_SUPPORTS_HUGE_TREE, portalocker LockerType,
flask flask.globals.app_ctx (AdaMAS's package happened to define it;
no baseline did), xmnlp (tensorflow + model weights), and static pins on
cookiecutter/trailscraper/zxcvbn. flask's env needed pytest<8 and xmnlp's
never had its own requirements installed.

**Runner bugs this pass exposed and fixed:** failure-search commit raise
(`bebf8f26`), __pycache__ in change sets (`9ee4ad76`), no-gate-result
candidates VALID (`60858ee4`), diagnosis model pinned in yamls
(`4c39b0ca`), infra-failed-but-done chain marker (rerun by hand).
Upstream outage 17:41-17:59 stopped the chain twice; a supervisor now
relaunches after three stable probes.

**Why AdaMAS did not lead on six CPE rows (per-test diff of the delivered
package against the best baseline's, same held-out, same env).**

| task | gap (tests) | AdaMAS-only failures | cause | class |
|---|---|---|---|---|
| tinydb | 0.75 vs 0.877 (-26) | 23, of which 15 are `LRUCache` lacking `__len__`/`__contains__`/`__iter__`; 4 "did not raise RuntimeError"; 3 NaN->int | the document says `LRUCache(abc.MutableMapping, Generic[K, V])`; AdaMAS built `class LRUCache(Generic[K, V])`. M1 failed its first gate, the failure-search retry passed at spec 0.87 and **froze** the defect; the M1 suite named LRUCache three times but never exercised the mapping protocol; M2's suite passed, so no search reopened M1 | **architectural**: frozen milestone + authored-suite coverage; the cross-milestone limit already recorded |
| trailscraper | 0.60 vs 0.65 (-4) | 5 (action-list ordering); baseline-only 1 | M2 was refused by the gate's `check_tests` stage -- the dataset's checks import the withheld `unit_tests` package and the env lacks `pkg_resources`; the reference fails them too. The gate honoured unrunnable checks; the baseline shipped ungated | **architectural (gate policy vs. broken dataset checks)**, not code |
| imapclient | 0.356 vs 0.390 self_refine (-9) | 19 vs 10 the other way; mock-signature and literal-parsing details | net 9 tests inside a suite that is half white-box; the continuation had fixed 6/6 of its own persistent set | noise / ceiling |
| deprecated | 0.585 vs 0.614 (-5) | 5, all the wording "deprecated classmethod" vs "class method" in sphinx output | the wording appears in no document; the baseline matched the real library's | undocumented string; incidental |
| rsa | 0.700 vs 0.710 (-1) | 1 (negative-integer encryption error) | one test | noise |
| simpy | 0.765 = debate | 2 vs 2 (error-message regexes) | tie | noise |

Two of six are structural and both are known levers: the frozen-milestone
limit (a defect committed at M1 is never revisited unless M1's own
search fires) and gate strictness on dataset checks that cannot run in a
workspace. The other four are inside the noise of a white-box-heavy or
undocumented-wording suite. On the three no-search rows (deprecated, rsa,
simpy) AdaMAS was a single pass and scored as one.

## EXP-20260909-01 -- NL2Repo Medium core set on gpt-5.5 (paused: weekly usage limit)

Nine core tasks, continuation arm, v9 test_author then v0 control; five
direct-LLM baselines on the same nine (done 03:11, 207 calls). At 09:18
the account's weekly limit hit (`usage_limit_reached`, plan prolite,
resets 2026-09-15 04:35 UTC) for every main model; only
gpt-5.3-codex-spark answers. Chain supervisor keeps probing and resumes
when the account answers.

| task | AdaMAS v9 | solo | best_of_3 | self_refine | writer_rev | debate | note |
|---|---|---|---|---|---|---|---|
| aiofiles | rerun pending | 0 | 0 | 0 | 0.910 | 0 | v9 run refused at contracts by the src-layout `module_file_exists` bug (`4352ecd3`); four baselines die at import (eager sys.stdin wrap) |
| emoji | 0.353 | 0.382 | 0.382 | 0.353 | 0.402 | 0.363 | data ceiling: unicode table not derivable from the documents |
| python-dotenv | **0.823** | 0.694 | 0.799 | 0.804 | 0.766 | 0.775 | |
| python-pathspec | 0.756 | 0.681 | 0.622 | 0.765 | 0.714 | 0.773 | |
| tablib | 0.526 | 0.567 | 0.549 | 0.509 | 0.578 | 0.549 | |
| tenacity | 0.847 | 0.863 | 0.831 | 0.871 | timeout | 0.847 | |
| ftfy | pending | 0 | 0.737 | 0 | 0 | 0 | four baselines: missing data file |
| python-jose | pending | 0.579 | 0.404 | 0.279 | 0.425 | 0.257 | |
| voluptuous | pending | 0.534 | 0.561 | 0.561 | 0.547 | 0 | |

v0 control: not started. Bug found this pass: `module_file_exists`
contract path ignored `src/` (every src-layout candidate failed the
contracts stage at spec 1.0), fixed and verified on the refused
candidate.

**Why AdaMAS did not lead on the NL2Repo rows scored so far (per-test
diff against the best baseline, same held-out, same env; run records).**

| task | AdaMAS vs best | A-only / B-only / both fail | what the run did | cause | class |
|---|---|---|---|---|---|
| emoji | 0.353 vs 0.402 wr (-5) | 6 / 1 / **60** | M2 quality search, 1 persistent, continuation 1/1 -> authored 1.00, committed | 60 shared failures are the unicode table (`KeyError: ':lion:'`); the six A-only are skin-tone ZWJ sequences and alias edge cases | data ceiling; noise on top |
| python-pathspec | 0.756 vs 0.773 debate (-2) | 13 / 11 / 16 | M1 quality search, 4 persistent, LLM chose `pb_q_failures_to_agent`: 1/4, committed at 0.70 | A-only failures assert the reference's exact regex text (`(?P<ps_d>/)` named group) -- representation the documents never state | white-box; noise |
| tablib | 0.526 vs 0.578 wr (-9) | 26 / 17 / 56 | M2 first pass failed the gate -> **failure search**; `cand_feedback` recovered the gate and was **committed at authored 0.62**; no further search | the failure-search path ends at the first gate pass: a milestone that recovers the gate with 38% of its own suite failing gets no quality/persistence phase and no continuation. A-only failures (HeadersNeeded, getter kwargs, RST databook, Dataset equality) are exactly the unrepaired 38% | **architectural** (search-mode design) |
| tenacity | 0.847 vs 0.871 sr (-3) | 7 / 4 / 12 | M1: every sample **0.00** on the authored suite -- the suite imports `tomllib` at module top and the task env is Python 3.10; persistent set = the file itself; `pb_q_failures_to_agent` 0/1; first pass committed at 0.00. M2 failure search -> 1.00 committed | the authored suite collected nothing on the task's own interpreter and custody did not catch it (second occurrence: pyjwt M1 on CPE); the search ran blind. The -3 on held-out is noise (log-format strings, stats dict keys) | **architectural** (custody accepts an uncollectable suite) + noise |
| python-dotenv | **0.823** vs 0.804 (+4) | 9 / 13 / 28 | no search; both milestones passed first try | lead as a single pass | -- |
| aiofiles | rerun pending | -- | every candidate refused at contracts: `module_file_exists` ignored `src/` | fixed `4352ecd3` | bug, fixed |

Pattern across CPE and NL2Repo so far: the rows AdaMAS loses are never
the repair machinery failing on what it was aimed at (continuations are
27/38 fixed, 0 regressions across both sets); they are (1) milestones the
search never reaches -- a frozen M1 defect (tinydb), a gate-recovery
committed without a quality phase (tablib), a suite that collapsed before
the search started (tenacity, pyjwt) -- and (2) held-out content no
document states (undocumented names, exact representations, data
tables), which caps every method alike. Two design fixes follow from (1):
continue into the quality search after a failure search recovers the gate
below the quality threshold; and refuse at custody any suite that
collects zero cases on the task interpreter. A third, smaller signal: the
LLM-chosen `pb_q_failures_to_agent` row is 1/7 across both sets where the
default continuation is 27/38.

### EXP-20260909-01 addendum (2026-09-10 03:20) -- the account reset at ~20:00 09-09; the v9 arm is complete

The supervisor resumed on its own. Remaining v9 rows (held-out, same env,
same suite as the baselines):

| task | AdaMAS v9 | best baseline | milestones | note |
|---|---|---|---|---|
| aiofiles (rerun) | **0.929** | 0.910 writer_rev | M1 0.909 first pass, M2 failure search -> 1.00 | the `src/` contracts bug fixed; four baselines die at import |
| ftfy | **0.861** | 0.737 best_of_3 | M1 0.875, M2 `cand_feedback_r2` 0.914 | four baselines: missing data file |
| python-jose | 0.106 | 0.579 solo | M1 0.857 committed; **M2 never committed** | routing bug, see below; the rejected M2 candidate scores **0.693** on held-out |
| voluptuous | 0.0 | 0.561 best_of_3 / self_refine | M1 suite collapsed (0.00, gate passed); M2 0.952 committed | `voluptuous/util.py` shipped as a 9-line stub; the held-out suite is one module and its import of `Capitalize` zeroes all 148 cases |

**python-jose (0.106): a misrouted repair, not a model failure.** M2's
first pass passed all 22 authored behaviours but failed the gate at the
contracts stage: 15 names the architecture document lists (`jwt._validate_exp`
... `jwe._decrypt_and_auth`, `jwk.get_key`) were absent. The diagnosis LLM
classified it functional with an empty `target_node_id`, so the fallback
`_primary_failed_node` chose the last failing *agent in node_status
order*, which was the **test author**; the "missing symbol" feedback was
attached to `agent_1_test_author_test_author` (candidate_result
`edits[0].feedback`), the implementer never saw it, `cand_feedback` failed
the same 15 checks, and the failure search stopped ("no candidate passed
the gate"). The final repository is M1's stubs (`... is not implemented in
this milestone`, 94+73+57+52 held-out cases). The rejected candidate,
scored in a scratch copy with the held-out suite: 331/478 = 0.693, which
would have led the row. Fix `b42e86a9` (bestn): the fallback now orders
failing agents topologically. A second, policy-level question stays open:
with no incumbent, a gate-failing candidate at behaviour 1.0 whose only
defect is missing private helpers is discarded in favour of nothing.

**voluptuous (0.0): our miss, twice.** The documents name `Capitalize`,
`Lower`, `Upper`, `Title`, `Strip` (architecture_design.md 1350, 1916-1940);
the agent shipped `util.py` re-exporting two validators and none of the
string ones; the M2 authored suite (21 cases) tested none of them, so the
internal 0.952 saw nothing wrong. M1's authored suite scored 0.00 with the
gate passed (the file itself is the one failed unit): the suite does
`import tomllib` and the task interpreter is Python 3.10 -- the same
collapse as tenacity M1, the third occurrence of custody accepting a
suite that collects nothing (pyjwt M1, tenacity M1). The held-out being a single module turns one missing
import into a 148-case zero; the baselines that scored 0.53-0.56 shipped
the string validators.

v9 arm, all nine: aiofiles 0.929, emoji 0.353, dotenv 0.823, pathspec
0.756, tablib 0.526, tenacity 0.847, ftfy 0.861, python-jose 0.106,
voluptuous 0.0 -> mean **0.578**; per-method baseline means on the same
nine (timeouts and import deaths as 0): best_of_3 0.543, writer_rev
0.482, solo 0.478, self_refine 0.460, debate 0.396; the per-row best
baseline (an oracle over five methods) averages 0.691. Without the two
rows above AdaMAS averages 0.728 on the remaining seven against 0.725 for
the oracle. v0 control: running.

## EXP-20260910-01 -- bestn v2 (targeted resample, early stop, symbol ownership, re-author): first pass on the same three tasks

Branch bestn, gpt-5.5, `codeprojecteval_official_bestn.yaml` (node_resample_n 3).
v2 = `da993f77`; routing fix `b42e86a9`; hop `2cf4e169`; re-author
mechanics `41b03d6e`. Same three tasks as the v1 trial.

| task | path taken | v2 event | internal | held-out (v1 trial / v9 run) |
|---|---|---|---|---|
| bplustree | M1 0.96 first pass; **M2 failure search**, `cand_feedback` recovered at 0.941, one probe failed the gate -> 1 voting sample, phase two declined | none | 0.96 / 0.941 | **0.893** (0.86 / 0.699) |
| nl2_aiofiles | M1 quality search, persistent 0 flaky 2 (declined); **M2 failure search**, `cand_feedback` recovered at 0.846, committed at once | none | 1.00 / 0.846 | **0.929** (0.929 / 0.929) |
| nl2_python-pathspec | M1 1.0 first pass; M2 first pass **0.0 with the gate passed**, probes 0.0 and 0.0, persistent 3/3 | author verdict -> re-author, 3 samples, chosen a 0.0 suite, not committed | 1.0 / 0.0 | 0.698 (0.65 / 0.756) |

**What blocked v2 twice: the failure-search path.** Both milestones that
mattered on bplustree and aiofiles failed the gate on the first pass; the
failure search recovered it with `cand_feedback` and committed at once,
with 1 and 2 authored cases still failing. That path has no probes, no
persistent set, and therefore no blamed node -- the tablib pattern of
EXP-20260909-01 seen from the other side. Fix `2cf4e169`
(`persistence_after_recovery`, bestn yaml only): a recovered winner below
1.0 becomes the incumbent of a persistence search on its own graph, with a
second candidate window; continuations and the node resample start from
its patch. bplustree and aiofiles are queued again on it.

**pathspec: the author verdict fired, and everything behind it was wrong.**
The M2 frozen suite collects 10 cases, 7 of them vacuous (they pass on the
milestone base: M1 had already delivered most of the matching behaviour),
so the harness grades 3, and all three fail in every sample -- the
regex-text assertion (`'^src/(?:/.*)?$'`), a bytes/escape case, and a
"documented bulk example". Every sample at 0.0 is the v2 rule for a
suite-level cause, so the author was re-run with the condemned suite's
evidence. Then: (1) the verdict text said "collects nothing" -- false;
(2) the three re-author samples all wrote to one shared `.reauthor` dir;
(3) the chooser ranked by collected size and picked a suite with 14
collected, 13 vacuous, 1 gradable, failing -- score 0.0, no better than
the condemned one; (4) the persistence ledger credited 3/3 fixed because
the old names no longer exist. The winner (0.0) did not beat the
incumbent (0.0) and nothing was committed; held-out 0.698 is the same M1
as before. All four fixed in `41b03d6e`: one dir per sample, chosen dir
replaces the frozen suite, selection requires 0 < score < 1 and ranks by
gradable = collected - vacuous, re-author records carry no ledger, the
verdict says what it saw. pathspec is queued again after aiofiles.

Cost of the first pass: bplustree 43 min, aiofiles 103 min, pathspec 75
min; no run used more calls than the v1 trial.

## EXP-20260909-02 -- best-of-N at the blamed node (bestn branch, v1): first trial

`fdbb435f` on branch `bestn`; arm `codeprojecteval_official_bestn.yaml`
(continuation arm + `node_resample_n: 3`), gpt-5.5. Design in
`docs/node_resample_design.md`.

**bplustree `cpe-20260909T203648Z` (20:36-21:39).** M1 passed first try.
M2 quality search: incumbent 0.333 on a 21-case v9 suite, **14
persistent / 0 flaky** (bulk insert-reopen per serializer ×9, default
order past order² ×3, datetime bulk, delete-many). Blame v1: every
traceback lands in `bplustree/node.py`, last written by the M2
implementer -> blamed `agent_2_author_implementer` (first editing node,
14/14). Prefix = the test author's cumulative patch (spec_tests removed);
custody's result injected into `suite_custody`. Three fresh implementer
samples on that prefix: all three passed the gate, all three scored
0.333 with the **identical** 14 failures -- agreement 1.00, chosen sample
0, 0/14 fixed, tied the incumbent, not committed. Explainability record
at `tasks/rb_bplustree/fast_loop/node_resample/<milestone>/cand_node_resample.json`
and `node_resample_records.jsonl`.

Reading: the mechanism did what it was built for, and the answer it
returned is itself the finding -- perfect agreement with zero gain means
these 14 failures are not sampling variance at this node; three
independent implementers converge on the same page-fit/split behaviour
because `node.py` is milestone 1's design (the page-size/order relation
the reference itself cannot satisfy at its documented default). The lever
is the target (contracts / M1), not this node. The record makes that
legible without a held-out. Cost: three implementer sessions + three
gates, against three full subgraphs for an anchor resample.
Follow-up for v2: stop after two identical samples (agreement 1.0 with no
gain) and spend the remaining budget elsewhere.

**Trial 2/3 -- nl2_aiofiles `cpe-20260909T225142Z`, nl2_python-pathspec
`cpe-20260910T001641Z`.** Chain done 02:12; four resample events over the
three tasks, every one recorded under `fast_loop/node_resample/`.

| task / milestone | persistent | blame (v1) | samples (gate, score, #fail) | agreement | resample vs incumbent | held-out |
|---|---|---|---|---|---|---|
| bplustree M2 | 14/0 | implementer (first), 14/14 via `node.py` | 3× pass, 0.333, 14 / 0.333, 14 / 0.333, 14 | **1.00** | tie, not committed, 0/14 | 0.357 (slow-package draw; prior 0.89/0.90/0.90/0.46) |
| aiofiles M1 | 19/0, every sample 0.0 | **author** -> declined (suite-level cause) | -- | -- | LLM row `failures_to_agent` 0/19 | **0.929** (best aiofiles yet) |
| pathspec M1 | 1/0 | contract_author (only editing node; no frame parsed) | 0.6, 2 / 0.6, 2 / 0.8, 1 | 0.83 | tie 0.8, not committed, 0/1 | 0.740 (prior 0.756) |
| pathspec M2 | 1/1 | implementer (first; no frame parsed) | 0.667, 1 / 0.333, 2 / 0.667, 1 | 0.83 | tie with the best base 0.667, discarded, 0/1 | |

Scorecard: **0 of 16 persistent failures fixed** by resampling, against
27/38 for the evidence-fed continuation on the same kind of set. The
mechanism is sound (prefix rebuilt, suffix graph injected, samples gated,
consensus picked, records written) and it is a good *diagnostic*: agreement
1.00 with no gain (bplustree) says the failure is deterministic at that
node -- the target's, not the node's; agreement 0.83 with the best sample
only tying (pathspec) says one of three draws is worse and none is better.
As a *repair* it is weak, and the reason is structural: the resampled node
runs blind -- same prompt, same inputs, no failure names, no evidence --
so it reproduces the same behaviour. The continuation wins because the
repairer is told what failed and can run it.

v2, in order: (1) **targeted resample** -- inject the persistent failures
and the repair evidence into the blamed node's prompt, so the N samples
are N attempts at the known failures rather than N re-rolls; (2) stop
after two identical samples (agreement 1.0, no gain); (3) the author
verdict should trigger re-authoring, not a decline; (4) traceback frames
came back empty on pathspec (parser), so ownership fell to the last
writer; fix the parser and log frames per failure. The blame rule itself
was never wrong in this trial: first editing node three times (correct
for a first-pass defect), author once (correct, confirmed by the 0.929).

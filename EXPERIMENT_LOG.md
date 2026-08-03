# AdaMAS Experiment Log

> **Canonical experiment history for this repo.**  
> Agents and humans must **read this file before comparing historical performance**, and **append a new entry after every completed experiment run** (real or mock).  
> Do not delete past entries; supersede with a newer entry and cross-link.

| Field | Convention |
|-------|------------|
| IDs | `EXP-YYYYMMDD-NN` (date of run finish, local or UTC noted) |
| Status | `canonical` (use for comparisons) / `smoke` / `mock` / `superseded` / `incomplete` |
| Paths | Repo-relative from AdaMAS root |

**Last updated:** 2026-07-23 (UTC) — M6.2 production + Stage-2 experiment closure

---

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

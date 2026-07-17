# AdaMAS Experiment Log

> **Canonical experiment history for this repo.**  
> Agents and humans must **read this file before comparing historical performance**, and **append a new entry after every completed experiment run** (real or mock).  
> Do not delete past entries; supersede with a newer entry and cross-link.

| Field | Convention |
|-------|------------|
| IDs | `EXP-YYYYMMDD-NN` (date of run finish, local or UTC noted) |
| Status | `canonical` (use for comparisons) / `smoke` / `mock` / `superseded` / `incomplete` |
| Paths | Repo-relative from AdaMAS root |

**Last updated:** 2026-07-17 (UTC) — M4 final hardening (correctness close)

---

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

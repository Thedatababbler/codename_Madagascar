"""Backend-agnostic Fast Loop controller (M4-A: FRESH candidates only)."""

from __future__ import annotations

import asyncio
import sys
import shutil

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestra.backends.base import ArtifactRef
from orchestra.backends.capabilities import BackendCapabilities
from orchestra.backends.catalog import capabilities_for
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.backend_usage import append_usage_records
from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.fast_loop.budget import (
    FastLoopBudgetTracker,
    add_costs,
    remaining_budget,
    spent_from_state,
)
from orchestra.control.fast_loop.candidate_generator import (
    DesignSearchCandidateGenerator,
    LocalCandidateGenerator,
    RuleBasedLocalCandidateGenerator,
)
from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.llm_diagnosis import DiagnosisConfig, refine_diagnosis
from orchestra.control.fast_loop.pareto import ParetoSelectionConfig
from orchestra.control.fast_loop.plan_candidates import register_new_contracts
from orchestra.control.fast_loop.playbook_generator import PlaybookCandidateGenerator
from orchestra.control.fast_loop.quality_trigger import quality_search_diagnosis
from orchestra.roles.pool import default_role_pool
from orchestra.control.fast_loop.repair_evidence import build_repair_evidence
from orchestra.control.fast_loop import node_resample as nr
from orchestra.control.fast_loop.persistence import (
    apply_role_floor,
    default_role,
    persistence_ledger,
    persistent_failures,
)
from orchestra.control.fast_loop.schemas import (
    BackendModelPool,
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopState,
    LocalCandidate,
    StabilityIncident,
)
from orchestra.control.fast_loop.selector import (
    CandidateSelector,
    DeterministicCandidateSelector,
    ParetoCandidateSelector,
)
from orchestra.control.fast_loop.workspace import (
    CandidateWorkspaceError,
    GitCandidateWorkspaceManager,
)
from orchestra.control.task_state import (
    BackendSessionRecord,
    LocalUpdateRecord,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.harness.command_runner import run_authoritative_harness_command
from orchestra.harness.progress import (
    behaviour_failures,
    behaviour_score,
    behaviour_total,
    best_harness_progress,
)
from orchestra.ir.artifacts import ArtifactBundle, ArtifactSourceRef
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.runtime.backend import RunContext
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.state import GraphExecutionResult
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import HarnessStageResult
from orchestra.storage.artifacts import ArtifactStore
from orchestra.workspaces.base import WorkspaceRef

logger = logging.getLogger(__name__)


def candidate_task_id(task_id: str, subtask_id: str, candidate_id: str) -> str:
    """Checkpoint identity for one candidate run.

    The subtask belongs in the key. Two milestones of one task draw candidate
    ids from the same table, so "task + candidate" collides the moment a second
    milestone searches: the checkpoint store finds the first milestone's record
    under that name and rejects the run as config drift, which kills every
    candidate of the second search in milliseconds and leaves that milestone
    falling back to its incumbent with nothing to compare against.
    """
    return f"{task_id}__{subtask_id}__candidate__{candidate_id}"


class FastLoopController:
    """Diagnose → generate ≤K candidates → isolate → evaluate → select → commit."""

    def __init__(
        self,
        *,
        runtime: NativeAsyncRuntime,
        artifact_store: ArtifactStore,
        task_checkpoint_store: TaskCheckpointStore,
        contracts_dir: str = "configs/contracts",
        workspace_manager: GitCandidateWorkspaceManager | None = None,
        generator: LocalCandidateGenerator | None = None,
        selector: CandidateSelector | None = None,
        budget: FastLoopBudget | None = None,
        capabilities: Mapping[str, BackendCapabilities] | None = None,
        model_pools: Mapping[str, BackendModelPool] | None = None,
        pareto: ParetoSelectionConfig | None = None,
        design_search: bool = False,
        playbook_search: bool = False,
        anchor_search: bool = False,
        persistence_search: bool = False,
        persistence_probe_samples: int = 2,
        node_resample_n: int = 0,
        persistence_after_recovery: bool = False,
        diagnosis_config: DiagnosisConfig | None = None,
        clock=None,
        persist_checkpoints: bool = True,
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.workspace_manager = workspace_manager or GitCandidateWorkspaceManager()
        self.compiler = build_compiler(contracts_dir)
        # Design search and Pareto selection travel together: one atomic edit per
        # candidate is what makes a frontier readable, and a frontier is what makes
        # varying the design worth paying for. Enabling one without the other
        # produces either an unreadable frontier or a search with nothing to
        # search over. Playbook search is a different generator on the same
        # selector; setting both would leave a run ambiguous about which table
        # produced its candidates.
        modes = (playbook_search, design_search, anchor_search, persistence_search)
        if sum(map(bool, modes)) > 1:
            raise ValueError(
                "playbook_search, design_search, anchor_search and persistence_search "
                "are mutually exclusive"
            )
        self.persistence_search = bool(persistence_search)
        self.persistence_probe_samples = max(1, int(persistence_probe_samples))
        # best-of-N at the blamed node from a frozen prefix (docs/node_resample_design.md); 0 = off
        self.node_resample_n = max(0, int(node_resample_n))
        self.persistence_after_recovery = bool(persistence_after_recovery)
        # graph results of executed candidates, by (subtask, candidate): the
        # recovery hop needs the winner's node outputs to blame a node
        self._results: dict[tuple[str, str], Any] = {}
        self.probe_generator = None
        if persistence_search:
            # Two phases on one budget. Phase one resamples the anchor design
            # `persistence_probe_samples` times and intersects the failure lists
            # with the incumbent's: what fails every time is the defect, what
            # flips is luck. Phase two spends the remaining slots on the table,
            # diagnosed from that persistent set and with the specialist chosen
            # from it, judged by the same Pareto selector as everything else.
            self.probe_generator = PlaybookCandidateGenerator(
                compiler=self.compiler,
                contracts_dir=contracts_dir,
                anchor_repeat=True,
            )
            self.generator = generator or PlaybookCandidateGenerator(
                compiler=self.compiler,
                contracts_dir=contracts_dir,
            )
            self.selector = selector or ParetoCandidateSelector(pareto)
        elif anchor_search:
            # The playbook table's control arm: identical anchors resampled,
            # judged by the same Pareto selector, so the only variable against
            # a playbook run is where the candidates came from.
            self.generator = generator or PlaybookCandidateGenerator(
                compiler=self.compiler,
                contracts_dir=contracts_dir,
                anchor_repeat=True,
            )
            self.selector = selector or ParetoCandidateSelector(pareto)
        elif playbook_search:
            self.generator = generator or PlaybookCandidateGenerator(
                compiler=self.compiler,
                contracts_dir=contracts_dir,
            )
            self.selector = selector or ParetoCandidateSelector(pareto)
        elif design_search:
            self.generator = generator or DesignSearchCandidateGenerator(
                compiler=self.compiler,
                model_pools=model_pools,
            )
            self.selector = selector or ParetoCandidateSelector(pareto)
        else:
            self.generator = generator or RuleBasedLocalCandidateGenerator(
                compiler=self.compiler,
                model_pools=model_pools,
            )
            self.selector = selector or DeterministicCandidateSelector()
        self.budget = budget or FastLoopBudget()
        self.budget_tracker = FastLoopBudgetTracker(self.budget, clock=clock)
        self.diagnosis_config = diagnosis_config or DiagnosisConfig()
        self.capabilities = dict(capabilities or {})
        # When False, scheduler coordinator owns shared task checkpoints.
        self.persist_checkpoints = persist_checkpoints

    async def _save_checkpoint(self, state: TaskExecutionState) -> None:
        if self.persist_checkpoints:
            await self.task_checkpoint_store.save(state)

    def _caps_for_graph(self, graph: OrchestraGraph) -> dict[str, BackendCapabilities]:
        caps = dict(self.capabilities)
        for node in graph.nodes:
            if getattr(node, "node_kind", None) and node.node_kind.value == "agent":
                backend_id = str(node.resolved_backend().type)
                if backend_id not in caps:
                    known = capabilities_for(backend_id)
                    if known is not None:
                        caps[backend_id] = known
        return caps

    def _harness_command(self, graph: OrchestraGraph) -> tuple[list[str], float]:
        for node in graph.nodes:
            if node.node_kind is NodeKind.HARNESS:
                command = list(getattr(node, "command", None) or ["python", "-m", "pytest", "-q"])
                timeout = float(getattr(node, "timeout_seconds", None) or 60.0)
                return command, timeout
        return ["python", "-m", "pytest", "-q"], 60.0

    def _record_from_local(
        self,
        cand: LocalCandidate,
        *,
        attempt_id: int,
    ) -> CandidateRecord:
        status = (
            CandidateStatus.REJECTED
            if cand.compatibility_rejected
            else CandidateStatus.PENDING
        )
        return CandidateRecord(
            candidate_id=cand.candidate_id,
            attempt_id=attempt_id,
            graph_hash=cand.graph.content_hash,
            parent_graph_hash=cand.parent_graph_hash,
            edits=list(cand.edits),
            playbook_id=cand.playbook_id,
            plan_recompile=cand.plan_recompile,
            status=status,
            session_policy=cand.session_policy,
            rejection_reason=cand.rejection_reason,
            rejection_message=cand.rejection_message,
            failure_message=cand.rejection_message,
            metadata={
                "generation_reason": cand.generation_reason,
                "graph": cand.graph.model_dump(mode="json"),
                **(
                    {"continue_from_incumbent": True}
                    if cand.continue_from_incumbent
                    else {}
                ),
            },
        )

    async def _run_pending_candidates(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        context: RunContext,
        initial_artifacts: Any,
        base_ws: Any,
    ) -> None:
        """Compile and execute every PENDING record, in order, within budget."""
        for record in fl_state.candidates:
            if record.status is not CandidateStatus.PENDING:
                continue

            ok, reason, code = self.budget_tracker.can_start_candidate(fl_state)
            if not ok:
                fl_state.exhausted = True
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = code or CandidateRejectionReason.BUDGET_EXCEEDED
                record.rejection_message = reason
                record.failure_message = reason
                # Reject remaining pending without launching.
                for other in fl_state.candidates:
                    if other.status is CandidateStatus.PENDING and other is not record:
                        other.status = CandidateStatus.REJECTED
                        other.rejection_reason = CandidateRejectionReason.BUDGET_EXCEEDED
                        other.rejection_message = reason
                state.state_version += 1
                await self._save_checkpoint(state)
                break

            graph_payload = record.metadata.get("graph")
            if not graph_payload:
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = CandidateRejectionReason.MISSING_GRAPH
                record.rejection_message = "missing candidate graph payload"
                continue
            candidate_graph = OrchestraGraph.model_validate(graph_payload)
            local_candidate = LocalCandidate(
                candidate_id=record.candidate_id,
                parent_graph_hash=record.parent_graph_hash,
                edits=list(record.edits),
                graph=candidate_graph,
                session_policy=record.session_policy,
                generation_reason=str(record.metadata.get("generation_reason") or ""),
                playbook_id=record.playbook_id,
                plan_recompile=record.plan_recompile,
            )
            caps = self._caps_for_graph(candidate_graph)
            compat = validate_candidate_against_capabilities(local_candidate, caps)
            if not compat.compatible:
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = (
                    compat.rejection_reason or CandidateRejectionReason.OTHER
                )
                record.rejection_message = compat.reason
                record.failure_message = compat.reason
                state.state_version += 1
                await self._save_checkpoint(state)
                continue

            await self._evaluate_candidate(
                state=state,
                fl_state=fl_state,
                record=record,
                candidate_graph=candidate_graph,
                context=context,
                initial_artifacts=initial_artifacts,
                base_ws=base_ws,
            )
            state.state_version += 1
            await self._save_checkpoint(state)



    async def _persistence_phase_two(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        sub: SubtaskState,
        subtask_id: str,
        base_graph: OrchestraGraph,
        incumbent: CandidateRecord,
        context: RunContext,
        initial_artifacts: Any,
        base_ws: Any,
        incumbent_artifact: str | None = None,
        graph_result: Any = None,
        slots: int | None = None,
    ) -> None:
        """Intersect the probe samples, diagnose the persistent set, spend the rest.

        Idempotent across a resume: phase-two records are recognisable by
        their metadata, and a phase two that declined leaves a note.
        """
        if any(c.metadata.get("persistence_phase") == 2 for c in fl_state.candidates):
            self._score_persistence(fl_state)
            return
        if any(str(n).startswith("persistence:") for n in fl_state.notes):
            return

        probes = [c for c in fl_state.candidates if c.metadata.get("persistence_phase") == 1]
        summary = persistent_failures([incumbent, *probes])
        fl_state.persistence = summary.to_dict()
        fl_state.notes.append(
            f"persistence: samples={summary.samples} "
            f"persistent={len(summary.persistent)} flaky={len(summary.flaky)}"
        )
        if summary.samples < 2 or not summary.persistent:
            # Every failure flipped at least once: the defect is luck, and the
            # probes already are the right tool for that. Nothing to diagnose.
            fl_state.notes.append("persistence: no persistent failures; phase two declined")
            state.state_version += 1
            await self._save_checkpoint(state)
            return

        diagnosis = fl_state.diagnosis.model_copy(
            update={
                "behaviour_failures": list(summary.persistent),
                "persistence_samples": summary.samples,
            }
        )
        diagnosis = self._settle_roles(
            diagnosis,
            graph=base_graph,
            sub=sub,
            state=state,
            fl_state=fl_state,
            context=context,
            subtask_id=subtask_id,
            search_reason="quality",
        )
        fl_state.diagnosis = diagnosis

        remaining = slots if slots else max(1, self.budget.max_candidates - len(probes))
        caps = self._caps_for_graph(base_graph)
        generated = self.generator.generate(
            graph=base_graph,
            diagnosis=diagnosis,
            # +1 because index zero of the draft list is always the anchor,
            # which phase one has already sampled.
            budget=self.budget.model_copy(update={"max_candidates": remaining + 1}),
            capabilities=caps,
            search_reason="quality",
            history=self._playbook_history(fl_state),
        )
        picked = [c for c in generated if c.playbook_id][:remaining]
        resample_record = None
        if self.node_resample_n > 0 and graph_result is not None and summary.persistent:
            resample_record = await self._arm_node_resample(
                state=state, fl_state=fl_state, base_graph=base_graph, incumbent=incumbent,
                probes=probes, summary=summary, graph_result=graph_result, context=context, base_ws=base_ws,
            )
            if resample_record is not None:
                picked = picked[: max(0, remaining - 1)]
        if not picked and resample_record is None:
            fl_state.notes.append("persistence: table produced no candidate; phase two declined")
            state.state_version += 1
            await self._save_checkpoint(state)
            return
        for cand in picked:
            record = self._record_from_local(cand, attempt_id=fl_state.base_attempt_id + 1)
            record.metadata.update(
                {
                    "persistence_phase": 2,
                    "persistent_failures": list(summary.persistent),
                    "recommended_role": diagnosis.recommended_role,
                    "recommended_reviewer": diagnosis.recommended_reviewer,
                    "role_source": diagnosis.role_source,
                    "diagnosis_source": diagnosis.diagnosis_source,
                    "failure_class": diagnosis.failure_class,
                }
            )
            fl_state.candidates.append(record)
        if resample_record is not None:
            fl_state.candidates.append(resample_record)
        base = self._best_base(incumbent, probes)
        if base is not incumbent and base.patch:
            for record in fl_state.candidates:
                if record.metadata.get("persistence_phase") == 2 and record.metadata.get(
                    "continue_from_incumbent"
                ):
                    record.metadata["continue_from_candidate"] = base.candidate_id
                    record.metadata["base_behaviour_score"] = base.behaviour_score
            fl_state.notes.append(
                f"persistence: continuation armed on {base.candidate_id} "
                f"({base.behaviour_score:.3f}) rather than the first pass "
                f"({(incumbent.behaviour_score or 0.0):.3f})"
            )
        self._arm_continuations(fl_state, incumbent_artifact)
        state.state_version += 1
        await self._save_checkpoint(state)

        await self._run_pending_candidates(
            state=state,
            fl_state=fl_state,
            context=context,
            initial_artifacts=initial_artifacts,
            base_ws=base_ws,
        )
        self._score_persistence(fl_state)

    async def _persistence_after_recovery(
        self, *, state, fl_state, sub, subtask_id, context, initial_artifacts, base_ws
    ) -> None:
        """A failure search that recovered the gate below 1.0 continues as a quality search.

        The failure search used to end at the first gate pass (tablib
        committed at 0.62 with no persistence phase, EXP-20260909-01). Here
        the recovered winner becomes the incumbent of a persistence search
        on its own graph: probes resample it, the persistent set is
        diagnosed, and continuations / the node resample start from its
        patch. A second candidate window of the same size pays for it.
        Idempotent on resume: probes already present, or a persistence
        note, mean the hop has run.
        """
        if self.probe_generator is None or base_ws is None:
            return
        if any(c.metadata.get("persistence_phase") for c in fl_state.candidates):
            return
        if any(str(n).startswith("persistence:") for n in fl_state.notes):
            return
        recovered = self.selector.select(fl_state.candidates, self.budget)
        if recovered is None or recovered.status is not CandidateStatus.VALID:
            return
        score = recovered.behaviour_score
        if score is None or score <= 0.0 or score >= 1.0 or not recovered.patch:
            return
        payload = recovered.metadata.get("graph")
        if not payload:
            return
        rgraph = OrchestraGraph.model_validate(payload)
        spent = len([c for c in fl_state.candidates if c.status is not CandidateStatus.REJECTED])
        probes_n = min(self.persistence_probe_samples, self.budget.max_candidates)
        slots = max(1, self.budget.max_candidates - probes_n)
        saved = (self.budget, self.budget_tracker)
        self.budget = self.budget.model_copy(update={
            "max_candidates": self.budget.max_candidates + spent,
            "max_attempts_per_subtask": self.budget.max_attempts_per_subtask + spent,
        })
        self.budget_tracker = FastLoopBudgetTracker(self.budget, clock=saved[1]._clock)
        try:
            fl_state.notes.append(
                f"recovery: gate recovered by {recovered.candidate_id} at {score:.3f}; "
                f"persistence search continues from it ({probes_n} probes, {slots} slot(s))"
            )
            recovered.metadata["recovered_incumbent"] = True
            diagnosis = quality_search_diagnosis(recovered, rgraph)
            generated = self.probe_generator.generate(
                graph=rgraph, diagnosis=diagnosis,
                budget=self.budget.model_copy(update={"max_candidates": probes_n}),
                capabilities=self._caps_for_graph(rgraph), search_reason="quality",
            )
            taken = {c.candidate_id for c in fl_state.candidates}
            for i, cand in enumerate(generated, 1):
                cid = cand.candidate_id
                while cid in taken:
                    cid = f"{cand.candidate_id}_p{i}"
                    i += 1
                taken.add(cid)
                record = self._record_from_local(
                    cand.model_copy(update={"candidate_id": cid}), attempt_id=fl_state.base_attempt_id + 1
                )
                record.metadata["persistence_phase"] = 1
                record.metadata["probe_of"] = recovered.candidate_id
                fl_state.candidates.append(record)
            state.state_version += 1
            await self._save_checkpoint(state)
            await self._run_pending_candidates(
                state=state, fl_state=fl_state, context=context,
                initial_artifacts=initial_artifacts, base_ws=base_ws,
            )
            await self._persistence_phase_two(
                state=state, fl_state=fl_state, sub=sub, subtask_id=subtask_id,
                base_graph=rgraph, incumbent=recovered, context=context,
                initial_artifacts=initial_artifacts, base_ws=base_ws,
                incumbent_artifact=f"candidate:{recovered.candidate_id}",
                graph_result=self._results.get((subtask_id, recovered.candidate_id)),
                slots=slots,
            )
        finally:
            self.budget, self.budget_tracker = saved

    @staticmethod
    def _score_persistence(fl_state: FastLoopState) -> None:
        """Write the ledger: what each phase-two candidate did about the persistent set."""
        for record in fl_state.candidates:
            if record.metadata.get("persistence_phase") != 2:
                continue
            if "persistence_ledger" in record.metadata:
                continue
            if record.behaviour_score is None:
                continue
            record.metadata["persistence_ledger"] = persistence_ledger(
                record, list(record.metadata.get("persistent_failures") or [])
            )

    async def _replay_incumbent(
        self, record: CandidateRecord, cand_ws: Any, fl_state: FastLoopState | None = None
    ) -> bool:
        artifact_id = record.metadata.get("incumbent_change_artifact")
        patch = None
        base_id = record.metadata.get("continue_from_candidate")
        if base_id and fl_state is not None:
            base = next((c for c in fl_state.candidates if c.candidate_id == base_id), None)
            if base is not None and base.patch:
                patch = base.patch
                artifact_id = f"candidate:{base_id}"
        if patch is None and artifact_id and str(artifact_id).startswith("candidate:") and fl_state is not None:
            base = next((c for c in fl_state.candidates if c.candidate_id == str(artifact_id)[len("candidate:"):]), None)
            if base is not None and base.patch:
                patch = base.patch
        if patch is None and artifact_id:
            try:
                artifact = await self.artifact_store.get(str(artifact_id))
                patch = (getattr(artifact, "payload", None) or {}).get("patch")
            except Exception as exc:  # noqa: BLE001 -- reject, never run on the wrong base
                record.rejection_message = f"incumbent artifact unavailable: {exc}"
        if not patch:
            record.status = CandidateStatus.REJECTED
            record.rejection_reason = CandidateRejectionReason.OTHER
            record.rejection_message = (
                record.rejection_message
                or "incumbent change artifact carries no patch"
            )
            return False
        try:
            await self.workspace_manager.apply_patch(cand_ws, str(patch))
        except Exception as exc:  # noqa: BLE001
            record.status = CandidateStatus.REJECTED
            record.rejection_reason = CandidateRejectionReason.OTHER
            record.rejection_message = f"incumbent patch replay failed: {exc}"
            return False
        record.metadata["continued_on"] = str(artifact_id)
        return True

    async def _stage_repair_evidence(self, record: CandidateRecord, cand_ws: Any) -> None:
        """The persistent tests, their output, and how to run them -- beside the repo.

        Only for continuations, and only the persistent set: the rest of the
        exam stays sealed and grading reads the frozen copy. Failing to stage
        is the old behaviour, so it never rejects the candidate.
        """
        failures = list(record.metadata.get("persistent_failures") or [])
        if not failures or not cand_ws:
            return
        path = await asyncio.to_thread(
            build_repair_evidence, Path(cand_ws.path), failures
        )
        record.metadata["repair_evidence"] = str(path) if path else ""

    @staticmethod
    def _best_base(
        incumbent: CandidateRecord, probes: list[CandidateRecord]
    ) -> CandidateRecord:
        """The sample a continuation should start from: the best one that passed.

        The persistent set is the intersection over every sample, so the best
        sample has those failures too and nothing else the repair could lose.
        Starting from the first pass instead put a continuation that fixed
        3/3 and 1/1 persistent failures with no regressions below an anchor
        resample on the whole-suite score, and it was discarded both times
        (EXP-20260907-01). Ties go to the incumbent.
        """
        best = incumbent
        for probe in probes:
            if probe.status not in (CandidateStatus.VALID, CandidateStatus.COMMITTED):
                continue
            if probe.behaviour_score is None or not probe.patch:
                continue
            if (best.behaviour_score or 0.0) < probe.behaviour_score:
                best = probe
        return best

    @staticmethod
    def _final_change_artifact(graph_result: Any) -> str | None:
        """The incumbent's frozen repository change, if the first pass has one."""
        state = getattr(graph_result, "state", None)
        outputs = getattr(state, "node_outputs", None) or {}
        freeze = outputs.get("freeze_change") or {}
        artifact_id = freeze.get("final_change")
        if artifact_id:
            return str(artifact_id)
        # No freeze output (gate path differences): fall back to the last
        # agent-produced repository change in the run.
        last = None
        for node_outputs in outputs.values():
            if node_outputs.get("repository_change"):
                last = node_outputs["repository_change"]
        return str(last) if last else None

    @staticmethod
    def _arm_continuations(
        fl_state: FastLoopState, incumbent_artifact: str | None
    ) -> None:
        """Point continuation records at the incumbent's change, or reject them.

        A continuation without the incumbent's patch would silently run on the
        bare milestone base -- a worse anchor wearing the continuation's name --
        so a missing artifact rejects the record instead of executing it.
        """
        for record in fl_state.candidates:
            if not record.metadata.get("continue_from_incumbent"):
                continue
            if record.status is not CandidateStatus.PENDING:
                continue
            if "incumbent_change_artifact" in record.metadata:
                continue
            if incumbent_artifact:
                record.metadata["incumbent_change_artifact"] = incumbent_artifact
            else:
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = CandidateRejectionReason.OTHER
                record.rejection_message = (
                    "continuation candidate has no incumbent change artifact"
                )

    @staticmethod
    def _playbook_history(fl_state: FastLoopState) -> list[str]:
        """Rows already spent on this milestone, in order; the menu pushes them back."""
        seen: list[str] = []
        for record in fl_state.candidates:
            if record.playbook_id and record.playbook_id not in seen:
                seen.append(record.playbook_id)
        return seen

    def _settle_roles(
        self,
        diagnosis: FailureDiagnosis,
        *,
        graph: OrchestraGraph,
        sub: SubtaskState,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        context: RunContext,
        subtask_id: str,
        search_reason: str,
    ) -> FailureDiagnosis:
        """Rules first, the model only for the residue, a named pair last.

        Rules pre-empt the model entirely, so a role they can name is never
        second-guessed by a prompt; the model is asked only when the evidence
        is one no rule covers, and its class refinement rides along on that
        call. Whatever is still empty afterwards gets the search's default
        pair. ``role_source`` records which of the three chose.
        """
        pool = getattr(self.generator, "role_pool", None) or default_role_pool()
        diagnosis = apply_role_floor(diagnosis, pool)
        if not diagnosis.recommended_role and self.diagnosis_config.mode == "llm":
            diagnosis = self._refine_lookup(
                lookup=diagnosis,
                graph=graph,
                subtask_state=sub,
                state=state,
                fl_state=fl_state,
                context=context,
                subtask_id=subtask_id,
                search_reason=search_reason,
            )
        return default_role(diagnosis, pool, search_reason)

    def _refine_lookup(
        self,
        *,
        lookup: FailureDiagnosis,
        graph: OrchestraGraph,
        subtask_state: SubtaskState,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        context: RunContext,
        subtask_id: str,
        search_reason: str = "failure",
    ) -> FailureDiagnosis:
        """Replace the lookup class when the LLM is confident; otherwise keep it."""
        history = [
            record.playbook_id
            for record in fl_state.candidates
            if record.playbook_id
        ]
        diagnosis, call = refine_diagnosis(
            lookup=lookup,
            graph=graph,
            subtask_state=subtask_state,
            config=self.diagnosis_config,
            remaining=remaining_budget(self.budget, fl_state),
            playbook_history=history,
            artifact_dir=Path(context.run_dir) / "fast_loop" / "diagnosis",
            task_id=state.task_id,
            subtask_id=subtask_id,
            attempt_id=len(subtask_state.attempts),
            search_reason=search_reason,
        )
        if call.usage is not None:
            state.backend_usage_records = append_usage_records(
                state.backend_usage_records, [call.usage]
            )
            fl_state.control_plane_cost = add_costs(
                fl_state.control_plane_cost,
                CostRecord(
                    prompt_tokens=int(call.usage.prompt_tokens or 0),
                    completion_tokens=int(call.usage.completion_tokens or 0),
                    estimated_cost_usd=float(call.usage.estimated_cost_usd or 0.0),
                    backend_calls=1,
                ),
            )
        return diagnosis

    async def run(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        source_repo: str | None = None,
        graph: OrchestraGraph | None = None,
        graph_result: GraphExecutionResult | None = None,
        initial_execution_cost: CostRecord | None = None,
        base_workspace: WorkspaceRef | None = None,
        incumbent: CandidateRecord | None = None,
    ) -> TaskExecutionState:
        sub = state.subtasks[subtask_id]
        # An incumbent means this is a quality search: the milestone passed its
        # gate and is being searched anyway because it scored poorly. The usual
        # early-out is exactly wrong there, since a passing milestone is the
        # premise rather than a reason to stop.
        if sub.status is SubtaskStatus.COMMITTED and incumbent is None:
            return state

        base_graph = graph or load_graph(sub.spec.local_graph_template)
        diagnosis = (
            quality_search_diagnosis(incumbent, base_graph)
            if incumbent is not None
            else diagnose_subtask_failure(
                subtask_state=sub,
                graph=base_graph,
                graph_result=graph_result,
            )
        )

        fl_state = state.fast_loop_states.get(subtask_id)
        if fl_state is None:
            fl_state = FastLoopState(
                subtask_id=subtask_id,
                base_attempt_id=len(sub.attempts),
                base_graph_hash=base_graph.content_hash,
                diagnosis=diagnosis,
                initial_execution_cost=initial_execution_cost or CostRecord(),
            )
            state.fast_loop_states[subtask_id] = fl_state
        else:
            for cand in fl_state.candidates:
                if cand.status is CandidateStatus.RUNNING:
                    cand.status = CandidateStatus.PENDING
                    cand.failure_message = "recovered from interrupted candidate run"
            if fl_state.selected_candidate_id:
                winner = next(
                    (
                        c
                        for c in fl_state.candidates
                        if c.candidate_id == fl_state.selected_candidate_id
                    ),
                    None,
                )
                if winner and winner.status is CandidateStatus.COMMITTED:
                    return state

        if incumbent is not None:
            fl_state.search_reason = "quality"
            if not any(
                c.candidate_id == incumbent.candidate_id for c in fl_state.candidates
            ):
                fl_state.candidates.append(incumbent)
        # Every search settles who the next candidate should be, not only the
        # persistence arm: rules where they are unambiguous, the model for the
        # residue, a named pair last. Persistence defers this to phase two,
        # where the evidence is the persistent set instead of one sample.
        if not (self.persistence_search and incumbent is not None):
            diagnosis = self._settle_roles(
                diagnosis,
                graph=base_graph,
                sub=sub,
                state=state,
                fl_state=fl_state,
                context=context,
                subtask_id=subtask_id,
                search_reason="quality" if incumbent is not None else "failure",
            )
            fl_state.diagnosis = diagnosis

        self.budget_tracker.mark_started(fl_state)

        if diagnosis.infrastructure_related:
            await self._infra_retry_once(
                state=state,
                fl_state=fl_state,
                subtask_id=subtask_id,
                base_graph=base_graph,
                context=context,
                initial_artifacts=initial_artifacts,
                source_repo=source_repo,
                base_workspace=base_workspace,
            )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        if not diagnosis.retryable:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        # The incumbent occupies a slot without having been generated, so "have we
        # generated yet" cannot be read off an empty list once it is present.
        searchable = [c for c in fl_state.candidates if not c.metadata.get("incumbent")]
        if not searchable:
            ok, reason, code = self.budget_tracker.can_generate_candidate(fl_state)
            if not ok:
                fl_state.exhausted = True
                if incumbent is not None:
                    self._keep_incumbent(sub, fl_state, incumbent, reason)
                else:
                    sub.status = SubtaskStatus.FAILED
                    sub.failure_message = reason
                state.state_version += 1
                await self._save_checkpoint(state)
                return state
            caps = self._caps_for_graph(base_graph)
            probing = (
                self.persistence_search
                and incumbent is not None
                and self.probe_generator is not None
            )
            if probing:
                probes = min(self.persistence_probe_samples, self.budget.max_candidates)
                generated = self.probe_generator.generate(
                    graph=base_graph,
                    diagnosis=diagnosis,
                    budget=self.budget.model_copy(update={"max_candidates": probes}),
                    capabilities=caps,
                    search_reason="quality",
                )
            else:
                generated = self.generator.generate(
                    graph=base_graph,
                    diagnosis=diagnosis,
                    budget=self.budget,
                    capabilities=caps,
                    search_reason="quality" if incumbent is not None else "failure",
                    history=self._playbook_history(fl_state),
                )
            for cand in generated:
                record = self._record_from_local(
                    cand, attempt_id=fl_state.base_attempt_id + 1
                )
                if probing:
                    record.metadata["persistence_phase"] = 1
                fl_state.candidates.append(record)
            self._arm_continuations(fl_state, self._final_change_artifact(graph_result))
            state.state_version += 1
            await self._save_checkpoint(state)

        if not any(not c.metadata.get("incumbent") for c in fl_state.candidates):
            fl_state.exhausted = True
            if incumbent is not None:
                self._keep_incumbent(
                    sub, fl_state, incumbent, "no candidate designs were generated"
                )
            else:
                sub.status = SubtaskStatus.FAILED
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        base_ws = base_workspace
        repo = source_repo
        if base_ws is None and repo:
            base_ws = await self.workspace_manager.prepare_base_snapshot(
                source_repo=repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            sub.workspace_ref = base_ws.path

        await self._run_pending_candidates(
            state=state,
            fl_state=fl_state,
            context=context,
            initial_artifacts=initial_artifacts,
            base_ws=base_ws,
        )
        if incumbent is None and self.persistence_search and self.persistence_after_recovery:
            await self._persistence_after_recovery(
                state=state, fl_state=fl_state, sub=sub, subtask_id=subtask_id,
                context=context, initial_artifacts=initial_artifacts, base_ws=base_ws,
            )
        if self.persistence_search and incumbent is not None:
            await self._persistence_phase_two(
                state=state,
                fl_state=fl_state,
                sub=sub,
                subtask_id=subtask_id,
                base_graph=base_graph,
                incumbent=incumbent,
                context=context,
                initial_artifacts=initial_artifacts,
                base_ws=base_ws,
                incumbent_artifact=self._final_change_artifact(graph_result),
                graph_result=graph_result,
            )

        winner = self.selector.select(fl_state.candidates, self.budget)
        # Recorded whether or not a winner emerged: a search that ended with an
        # empty frontier is a different failure from one that found points and
        # could not commit any of them, and the two are indistinguishable from the
        # winner alone.
        fl_state.pareto_frontier = list(getattr(self.selector, "last_frontier", []) or [])
        fl_state.selection_rule = str(getattr(self.selector, "last_rule", "") or "scalar")
        if winner is None:
            fl_state.exhausted = True
            if incumbent is not None:
                self._keep_incumbent(
                    sub, fl_state, incumbent, "no candidate was selectable"
                )
            else:
                sub.status = SubtaskStatus.FAILED
                sub.failure_reason = diagnosis.reason
                sub.failure_message = (
                    diagnosis.concise_feedback
                    or "fast loop exhausted without valid winner"
                )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        # The search declined: nothing on the frontier beat the work that was
        # already committed, so the first pass stands and no patch is applied.
        if incumbent is not None and winner.metadata.get("incumbent"):
            self._keep_incumbent(
                sub, fl_state, winner, "no candidate improved on the first pass"
            )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        # Quality never buys its way past safety. `require_gate_pass` makes this
        # the normal outcome of selection, but it is configurable and this is not:
        # replacing a milestone that passed with one that does not is a regression
        # no score can justify, since everything downstream builds on it.
        if incumbent is not None and winner.status not in {
            CandidateStatus.VALID,
            CandidateStatus.COMMITTED,
        }:
            self._keep_incumbent(
                sub,
                fl_state,
                incumbent,
                f"winner {winner.candidate_id} did not pass the gate",
            )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state
        if winner.status not in {CandidateStatus.VALID, CandidateStatus.COMMITTED}:
            # Failure search with nothing to fall back on: the first pass failed
            # the gate and so did every candidate. The selector still ranks
            # executed-but-failed candidates, so the "winner" here is the least
            # bad of them; committing it is refused downstream, and raising
            # there took a whole task down (cookiecutter, 2026-09-08). Fail the
            # milestone on the record instead.
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.HARNESS
            sub.failure_message = (
                f"no candidate passed the gate; best was {winner.candidate_id} "
                f"({winner.status.value})"
            )
            fl_state.notes.append(f"search: {sub.failure_message}")
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        ok, reason, code = self.budget_tracker.can_start_candidate(
            fl_state, reserved_backend_calls=0
        )
        # Wall-time still checked before commit.
        if fl_state.started_monotonic is not None:
            ok2, reason2, code2 = self.budget_tracker.can_start_candidate(fl_state)
            if not ok2 and "wall_time" in (reason2 or ""):
                fl_state.exhausted = True
                winner.status = CandidateStatus.REJECTED
                winner.rejection_reason = code2
                winner.rejection_message = reason2
                sub.status = SubtaskStatus.FAILED
                sub.failure_message = reason2
                state.state_version += 1
                await self._save_checkpoint(state)
                return state

        if base_ws is None:
            await self._commit_non_repo_winner(state, fl_state, winner, subtask_id)
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        await self._commit_winner(
            state=state,
            fl_state=fl_state,
            winner=winner,
            base_ws=base_ws,
            subtask_id=subtask_id,
            base_graph=base_graph,
        )
        state.state_version += 1
        await self._save_checkpoint(state)
        return state

    def _keep_incumbent(
        self,
        sub: SubtaskState,
        fl_state: FastLoopState,
        incumbent: CandidateRecord,
        reason: str,
    ) -> None:
        """End a quality search by keeping the first pass, as a success.

        A quality search runs on a milestone that already passed, so every way the
        search can end without a better design is a no-op, not a failure. Marking it
        failed here would take a repository that works and break it because the
        search it was subjected to found nothing — the one outcome a search for
        improvements must never produce.
        """
        incumbent.status = CandidateStatus.COMMITTED
        fl_state.selected_candidate_id = incumbent.candidate_id
        fl_state.exhausted = True
        sub.status = SubtaskStatus.COMMITTED
        sub.failure_reason = None
        sub.failure_message = None
        if sub.attempts:
            sub.attempts[-1].error = None
        fl_state.notes.append(f"quality search declined: {reason}")

    async def _infra_retry_once(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        subtask_id: str,
        base_graph: OrchestraGraph,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        source_repo: str | None,
        base_workspace: WorkspaceRef | None,
    ) -> None:
        sub = state.subtasks[subtask_id]
        if fl_state.infra_retries_used >= 1:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            return
        ok, reason, code = self.budget_tracker.can_start_candidate(fl_state)
        if not ok:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            sub.failure_message = reason
            return
        fl_state.infra_retries_used += 1
        fl_state.control_plane_cost = CostRecord(
            backend_calls=fl_state.control_plane_cost.backend_calls
        )
        infra_record = CandidateRecord(
            candidate_id="infra_retry_0",
            attempt_id=fl_state.base_attempt_id + 1,
            graph_hash=base_graph.content_hash,
            parent_graph_hash=base_graph.content_hash,
            edits=[],
            status=CandidateStatus.RUNNING,
            failure_reason=SubtaskFailureReason.INFRA,
            stability_incidents=[
                StabilityIncident(
                    kind="infrastructure",
                    message=sub.failure_message or "infra failure",
                )
            ],
            metadata={"infrastructure_related": True, "no_graph_edit": True},
        )
        fl_state.candidates.append(infra_record)

        workspace_ref = (
            (base_workspace.path if base_workspace else None)
            or sub.workspace_ref
            or context.workspace_ref
        )
        if source_repo and not workspace_ref:
            base = await self.workspace_manager.prepare_base_snapshot(
                source_repo=source_repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            workspace_ref = base.path
            sub.workspace_ref = workspace_ref
        run_context = RunContext(
            run_id=f"{context.run_id}:infra_retry",
            task_id=f"{context.task_id}__subtask__{subtask_id}__infra_retry",
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=False,
            subtask_id=subtask_id,
            workspace_ref=workspace_ref,
        )
        compiled = self.compiler.compile(base_graph)
        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
                context=run_context,
            )
        except Exception as exc:  # noqa: BLE001
            infra_record.status = CandidateStatus.BACKEND_FAILED
            infra_record.cost = CostRecord(backend_calls=1)
            infra_record.failure_message = f"{type(exc).__name__}: {exc}"
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INFRA
            sub.failure_message = infra_record.failure_message
            fl_state.exhausted = True
            return
        infra_record.cost = _cost_from_result(result)
        status, reason, message = await classify_subtask_outcome(
            result=result,
            artifact_store=self.artifact_store,
            error=None,
        )
        if status is SubtaskStatus.COMMITTED:
            sub.status = SubtaskStatus.COMMITTED
            sub.failure_reason = None
            sub.failure_message = None
            final_id = result.state.final_output_artifact_id
            sub.final_output_artifact_id = final_id
            state.frozen = all(
                s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
            )
            fl_state.selected_candidate_id = "infra_retry_0"
            fl_state.selected_execution_cost = infra_record.cost
            infra_record.status = CandidateStatus.COMMITTED
            state.mark_ready_from_dependencies()
        else:
            infra_record.status = CandidateStatus.BACKEND_FAILED
            infra_record.failure_message = message
            sub.status = status
            sub.failure_reason = reason
            sub.failure_message = message
            fl_state.exhausted = True

    async def _evaluate_candidate(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        record: CandidateRecord,
        candidate_graph: OrchestraGraph,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        base_ws: WorkspaceRef | None,
    ) -> None:
        subtask_id = fl_state.subtask_id
        if record.metadata.get("node_resample") and base_ws is not None:
            await self._evaluate_node_resample(
                state=state, fl_state=fl_state, record=record, candidate_graph=candidate_graph,
                context=context, initial_artifacts=initial_artifacts, base_ws=base_ws,
            )
            return
        record.status = CandidateStatus.RUNNING
        started = time.perf_counter()
        cand_ws: WorkspaceRef | None = None
        if base_ws is not None:
            cand_ws = await self.workspace_manager.fork_candidate_workspace(
                base=base_ws,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
                candidate_id=record.candidate_id,
            )
            record.workspace_ref = cand_ws
            if record.metadata.get("continue_from_incumbent"):
                # Replay the incumbent's change set so the specialist starts
                # from the state that passed the gate. Any failure to do so
                # rejects the candidate: run on the wrong base and its scores
                # would be an anchor's, filed under the continuation's name.
                if not await self._replay_incumbent(record, cand_ws, fl_state):
                    return
                await self._stage_repair_evidence(record, cand_ws)

        run_context = RunContext(
            run_id=f"{context.run_id}:{record.candidate_id}",
            task_id=candidate_task_id(
                context.task_id, subtask_id, record.candidate_id
            ),
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=False,
            subtask_id=subtask_id,
            workspace_ref=cand_ws.path if cand_ws else context.workspace_ref,
        )
        agent_executor = getattr(self.runtime.executors, "agent", None)
        register_new_contracts(
            compiler=self.compiler,
            graph=candidate_graph,
            contracts_dir=self.contracts_dir,
            executor_contracts=getattr(agent_executor, "contracts", None),
        )
        compiled = self.compiler.compile(candidate_graph)
        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
                context=run_context,
            )
            self._results[(fl_state.subtask_id, record.candidate_id)] = result
        except Exception as exc:  # noqa: BLE001
            from datetime import UTC, datetime

            from orchestra.control.backend_usage import (
                append_usage_records,
                exception_usage_record,
            )

            record.status = CandidateStatus.BACKEND_FAILED
            record.failure_reason = SubtaskFailureReason.INFRA
            record.failure_message = f"{type(exc).__name__}: {exc}"
            record.latency_ms = int((time.perf_counter() - started) * 1000)
            record.cost = CostRecord(backend_calls=1)
            record.stability_incidents.append(
                StabilityIncident(kind="exception", message=record.failure_message)
            )
            finished_at = datetime.now(UTC)
            started_at = datetime.fromtimestamp(
                finished_at.timestamp() - (record.latency_ms or 0) / 1000.0,
                tz=UTC,
            )
            state.backend_usage_records = append_usage_records(
                list(state.backend_usage_records or []),
                [
                    exception_usage_record(
                        task_id=state.task_id,
                        subtask_id=subtask_id,
                        attempt_id=record.attempt_id,
                        candidate_id=record.candidate_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        status="exception",
                        accounting_source="fast_loop_candidate_exception",
                    )
                ],
            )
            return

        record.latency_ms = int((time.perf_counter() - started) * 1000)
        record.backend_sessions = _collect_sessions(
            result=result,
            attempt_id=record.attempt_id,
            candidate_id=record.candidate_id,
        )
        from datetime import UTC, datetime

        from orchestra.control.backend_usage import (
            append_usage_records,
            collect_usage_from_graph_result,
        )

        finished_at = datetime.now(UTC)
        started_at = datetime.fromtimestamp(
            finished_at.timestamp() - (record.latency_ms or 0) / 1000.0,
            tz=UTC,
        )
        state.backend_usage_records = append_usage_records(
            list(state.backend_usage_records or []),
            collect_usage_from_graph_result(
                task_id=state.task_id,
                subtask_id=subtask_id,
                attempt_id=record.attempt_id,
                result=result,
                candidate_id=record.candidate_id,
                started_at=started_at,
                finished_at=finished_at,
                status=str(getattr(record.status, "value", record.status)),
                accounting_source="fast_loop_candidate",
            ),
        )
        # Single source of truth: CandidateRecord.cost only (search_cost is derived).
        record.cost = _cost_from_result(result)

        if cand_ws is not None:
            change_set = await self.workspace_manager.collect_changeset(cand_ws)
            record.change_set = change_set
            patch, changed, digest = await self.workspace_manager.collect_patch(cand_ws)
            record.patch = patch
            record.changed_files = changed
            record.patch_hash = digest or None

        status, reason, message = await classify_subtask_outcome(
            result=result,
            artifact_store=self.artifact_store,
            error=None,
        )
        record.harness_score, stages, record.furthest_stage = (
            await self._harness_progress(result)
        )
        record.behaviour_score = behaviour_score(stages)
        record.behaviour_failures = behaviour_failures(stages)
        record.behaviour_total = behaviour_total(stages)
        if status is SubtaskStatus.COMMITTED and record.harness_score is None:
            # The graph finished but no gate ran for this candidate (seen on
            # recompiled-plan candidates whose harness node sits under a
            # candidate namespace): a candidate nobody graded cannot be VALID,
            # or the selector commits it over a graded 1.0 (nl2_aiofiles pilot).
            record.status = CandidateStatus.HARNESS_FAILED
            record.quality_score = 0.0
            record.failure_reason = SubtaskFailureReason.HARNESS
            record.failure_message = "no gate result recorded for this candidate"
        elif status is SubtaskStatus.COMMITTED:
            record.status = CandidateStatus.VALID
            record.quality_score = 1.0
            record.failure_reason = None
            record.failure_message = None
            if result.state.final_output_artifact_id:
                record.output_artifact_ids = [result.state.final_output_artifact_id]
        elif status is SubtaskStatus.HARNESS_FAILED:
            record.status = CandidateStatus.HARNESS_FAILED
            record.quality_score = 0.0
            record.failure_reason = reason
            record.failure_message = message
        else:
            record.status = CandidateStatus.BACKEND_FAILED
            record.quality_score = 0.0
            record.failure_reason = reason
            record.failure_message = message

        if cand_ws is not None:
            summary_path = Path(cand_ws.path).parent / "candidate_result.json"
            summary_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    async def _arm_node_resample(
        self, *, state, fl_state, base_graph, incumbent, probes, summary, graph_result, context, base_ws
    ) -> CandidateRecord | None:
        """Blame one editing node (v1: file ownership, earliest owner) and arm a
        best-of-N record that reruns the graph from that node on a frozen prefix."""
        outputs = getattr(getattr(graph_result, "state", None), "node_outputs", None) or {}
        changes: dict[str, tuple[str, str]] = {}
        for node_id, outs in outputs.items():
            art_id = (outs or {}).get("repository_change")
            if not art_id:
                continue
            try:
                env = await self.artifact_store.get(str(art_id))
            except Exception:  # noqa: BLE001
                continue
            patch = (getattr(env, "payload", None) or {}).get("patch") or ""
            changes[node_id] = (str(art_id), patch)
        steps = nr.writer_steps(base_graph, changes)
        code_steps = [st for st in steps if not st.is_author]
        if not code_steps:
            fl_state.notes.append("node_resample: no editing node produced a change; declined")
            return None
        frames: dict[str, str] = {}
        evidence_dir = None
        try:
            scratch = await self.workspace_manager.fork_candidate_workspace(
                base=base_ws, run_dir=str(context.run_dir), task_id=state.task_id,
                subtask_id=fl_state.subtask_id, candidate_id="node_resample_blame",
            )
            await self.workspace_manager.apply_patch(scratch, code_steps[-1].patch)
            python = self._task_python(base_graph)
            frames = await asyncio.to_thread(
                nr.traceback_frames, Path(scratch.path), list(summary.persistent), python
            )
            # assertion-only failures have no repository frame: own them by the symbol under test
            sym = await asyncio.to_thread(nr.symbol_frames, Path(scratch.path), list(summary.persistent))
            for k, v in sym.items():
                frames.setdefault(k, v)
            evidence_dir = await asyncio.to_thread(
                build_repair_evidence, Path(scratch.path), list(summary.persistent)
            )
        except Exception as exc:  # noqa: BLE001
            evidence_dir = None
            fl_state.notes.append(f"node_resample: traceback frames unavailable ({exc}); ownership by last writer")
        zero = (incumbent.behaviour_score == 0) and all((p.behaviour_score or 0) == 0 for p in probes)
        blame = nr.blame_v1(list(summary.persistent), frames, steps, suite_collected_zero=zero)
        fl_state.notes.append(f"node_resample: blame={blame.node_id} position={blame.position}: {blame.reason}")
        if blame.position == "author" and blame.node_id:
            # v2: the suite is condemned -> re-author it with the evidence, on a
            # candidate-specific frozen-suite directory; the gate grades on it.
            old_dir = nr.frozen_spec_dir(base_graph)
            if not old_dir:
                fl_state.notes.append("node_resample: re-author declined (no frozen suite dir on the graph)")
                return None
            new_dir = f"{old_dir}.reauthor"
            samples_n = len(probes) + 1
            graph2 = nr.reauthor_graph(base_graph, blame.node_id, new_dir,
                                       nr.condemned_suite_feedback(list(summary.persistent), samples_n))
            cand = LocalCandidate(
                candidate_id="cand_reauthor", parent_graph_hash=base_graph.content_hash, edits=[],
                graph=graph2, session_policy=incumbent.session_policy,
                generation_reason="re-author the condemned suite", playbook_id="pb_q_reauthor",
            )
            record = self._record_from_local(cand, attempt_id=fl_state.base_attempt_id + 1)
            record.metadata.update({
                "persistence_phase": 2, "persistent_failures": list(summary.persistent),
                "node_resample": {"node": blame.node_id, "position": "author", "n": self.node_resample_n,
                                   "reason": blame.reason, "reauthor": True, "reauthor_spec_dir": new_dir,
                                   "frozen_spec_dir": old_dir, "base_behaviour": incumbent.behaviour_score,
                                   "injected": {}, "dropped": [], "prefix_artifact": None,
                                   "steps": [{"node": st.node_id, "files": sorted(st.files)[:40], "author": st.is_author} for st in steps]},
            })
            return record
        if blame.position not in ("first", "last") or not blame.node_id:
            fl_state.notes.append("node_resample: declined (v1 handles first/last editing node only)")
            return None
        try:
            suffix, injected, dropped = nr.suffix_graph(base_graph, blame.node_id, outputs)
        except ValueError as exc:
            fl_state.notes.append(f"node_resample: declined ({exc})")
            return None
        # v2: targeted -- the blamed node is told the persistent failures and gets the evidence
        feedback = nr.targeted_feedback(list(summary.persistent))
        suffix = suffix.model_copy(update={"nodes": [
            n.model_copy(update={"prompt_feedback": ((getattr(n, "prompt_feedback", "") or "") + "\n\n" + feedback).strip()})
            if n.node_id == blame.node_id else n for n in suffix.nodes]})
        idx = [st.node_id for st in code_steps].index(blame.node_id)
        prefix_step = code_steps[idx - 1] if idx > 0 else next((st for st in steps if st.is_author), None)
        cand = LocalCandidate(
            candidate_id="cand_node_resample", parent_graph_hash=base_graph.content_hash, edits=[],
            graph=suffix, session_policy=incumbent.session_policy,
            generation_reason=f"best-of-{self.node_resample_n} at {blame.node_id}", playbook_id="pb_q_node_resample",
        )
        record = self._record_from_local(cand, attempt_id=fl_state.base_attempt_id + 1)
        record.metadata.update({
            "persistence_phase": 2, "persistent_failures": list(summary.persistent),
            "node_resample": {
                "node": blame.node_id, "position": blame.position, "n": self.node_resample_n,
                "reason": blame.reason, "owners": blame.owners, "frames": frames,
                "prefix_artifact": prefix_step.artifact_id if prefix_step else None,
                "injected": injected, "dropped": dropped,
                "evidence_dir": str(evidence_dir) if evidence_dir else None,
                "base_behaviour": incumbent.behaviour_score, "targeted": True,
                "steps": [{"node": st.node_id, "files": sorted(st.files)[:40], "author": st.is_author} for st in steps],
            },
        })
        return record

    def _task_python(self, graph: OrchestraGraph) -> str:
        for node in graph.nodes:
            cmd = getattr(node, "command", None)
            if cmd and isinstance(cmd, (list, tuple)) and str(cmd[0]).endswith("python"):
                return str(cmd[0])
        return sys.executable

    async def _evaluate_node_resample(
        self, *, state, fl_state, record, candidate_graph, context, initial_artifacts, base_ws
    ) -> None:
        """Run the suffix graph N times on the frozen prefix, pick by consensus."""
        from datetime import UTC, datetime
        from orchestra.control.backend_usage import append_usage_records, collect_usage_from_graph_result

        meta = record.metadata["node_resample"]
        subtask_id = fl_state.subtask_id
        record.status = CandidateStatus.RUNNING
        started = time.perf_counter()
        bundle = initial_artifacts.model_copy(deep=True)
        for slot, art_id in (meta.get("injected") or {}).items():
            env = await self.artifact_store.get(str(art_id))
            bundle.slots[slot] = env
            bundle.slot_sources[slot] = ArtifactSourceRef(source="root", producer_subtask_id=subtask_id, artifact_id=str(art_id))
        prefix_patch = None
        if meta.get("prefix_artifact"):
            env = await self.artifact_store.get(str(meta["prefix_artifact"]))
            prefix_patch = (getattr(env, "payload", None) or {}).get("patch")
        agent_executor = getattr(self.runtime.executors, "agent", None)
        register_new_contracts(compiler=self.compiler, graph=candidate_graph, contracts_dir=self.contracts_dir,
                               executor_contracts=getattr(agent_executor, "contracts", None))
        compiled = self.compiler.compile(candidate_graph)
        samples: list[dict] = []
        cost = CostRecord()
        sessions = []
        for i in range(int(meta.get("n") or 1)):
            sid = f"{record.candidate_id}_s{i}"
            ws = await self.workspace_manager.fork_candidate_workspace(
                base=base_ws, run_dir=str(context.run_dir), task_id=state.task_id, subtask_id=subtask_id, candidate_id=sid)
            if prefix_patch:
                await self.workspace_manager.apply_patch(ws, prefix_patch)
            spec_dir = Path(ws.path) / nr.SPEC_DIR
            if spec_dir.is_dir() and not meta.get("reauthor"):
                shutil.rmtree(spec_dir, ignore_errors=True)
            if meta.get("evidence_dir") and Path(meta["evidence_dir"]).is_dir():
                dst = Path(ws.path).parent / "repair_evidence"
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(meta["evidence_dir"], dst)
            run_context = RunContext(
                run_id=f"{context.run_id}:{sid}", task_id=candidate_task_id(context.task_id, subtask_id, sid),
                run_dir=context.run_dir, limits=context.limits, semaphores=context.semaphores,
                contract_hash=context.contract_hash, allow_config_drift=False, subtask_id=subtask_id, workspace_ref=ws.path)
            t0 = time.perf_counter()
            try:
                result = await self.runtime.execute(graph=compiled, initial_artifacts=bundle, context=run_context)
            except Exception as exc:  # noqa: BLE001
                samples.append({"index": i, "ws": ws, "error": f"{type(exc).__name__}: {exc}", "gate": False,
                                "harness": None, "behaviour": None, "failed": set(), "stages": [], "furthest": ""})
                continue
            sessions.extend(_collect_sessions(result=result, attempt_id=record.attempt_id, candidate_id=sid))
            finished_at = datetime.now(UTC)
            started_at = datetime.fromtimestamp(finished_at.timestamp() - (time.perf_counter() - t0), tz=UTC)
            state.backend_usage_records = append_usage_records(
                list(state.backend_usage_records or []),
                collect_usage_from_graph_result(task_id=state.task_id, subtask_id=subtask_id, attempt_id=record.attempt_id,
                                                result=result, candidate_id=sid, started_at=started_at, finished_at=finished_at,
                                                status="node_resample_sample", accounting_source="fast_loop_candidate"))
            c = _cost_from_result(result)
            cost = CostRecord(prompt_tokens=cost.prompt_tokens + c.prompt_tokens, completion_tokens=cost.completion_tokens + c.completion_tokens,
                              estimated_cost_usd=cost.estimated_cost_usd + c.estimated_cost_usd, backend_calls=cost.backend_calls + c.backend_calls)
            status, reason, message = await classify_subtask_outcome(result=result, artifact_store=self.artifact_store, error=None)
            harness, stages, furthest = await self._harness_progress(result)
            samples.append({"index": i, "ws": ws, "result": result, "status": status, "reason": reason, "message": message,
                            "gate": status is SubtaskStatus.COMMITTED and harness is not None, "harness": harness,
                            "behaviour": behaviour_score(stages), "failed": set(behaviour_failures(stages) or []),
                            "total": behaviour_total(stages), "stages": stages, "furthest": furthest})
            stop = nr.should_stop([nr.Sample(s["index"], bool(s["gate"]), s["harness"], s["behaviour"], set(s["failed"])) for s in samples],
                                  meta.get("base_behaviour")) if not meta.get("reauthor") else None
            if stop:
                fl_state.notes.append(f"node_resample[{meta['node']}]: {stop}")
                meta["stopped_early"] = stop
                break
        if meta.get("reauthor"):
            # different suites per sample: pick a gate-passing, discriminating suite (0 < score < 1),
            # largest first, then highest score; never consensus across unrelated test sets
            ok = [s for s in samples if s["gate"] and s["behaviour"] is not None]
            disc = [s for s in ok if 0 < (s["behaviour"] or 0) < 1] or ok
            best = max(disc, key=lambda s: ((s.get("total") or 0), s["behaviour"] or 0), default=None)
            chosen = nr.Sample(best["index"], True, best["harness"], best["behaviour"], set(best["failed"])) if best else None
            agreement, consensus = 0.0, set()
            note = (f"re-author: {len(ok)}/{len(samples)} passed the gate; chosen sample {best['index']} "
                    f"(suite {best.get('total')} cases, score {best['behaviour']:.3f})" if best else "re-author: no sample passed the gate")
        else:
            chosen, agreement, consensus, note = nr.consensus_select(
                [nr.Sample(s["index"], bool(s["gate"]), s["harness"], s["behaviour"], set(s["failed"])) for s in samples])
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        record.cost = cost
        record.backend_sessions = sessions
        meta.update({"samples": [{"index": s["index"], "gate": s["gate"], "harness": s["harness"], "behaviour": s["behaviour"],
                                   "failed": sorted(s["failed"])[:60], "error": s.get("error"), "furthest": s.get("furthest")} for s in samples],
                     "chosen": chosen.index if chosen else None, "agreement": agreement, "consensus_failed": sorted(consensus)[:60], "note": note})
        fl_state.notes.append(f"node_resample[{meta['node']}]: {note}")
        if chosen is None:
            record.status = CandidateStatus.HARNESS_FAILED if any(s.get("result") for s in samples) else CandidateStatus.BACKEND_FAILED
            record.quality_score = 0.0
            record.failure_reason = SubtaskFailureReason.HARNESS if record.status is CandidateStatus.HARNESS_FAILED else SubtaskFailureReason.INFRA
            record.failure_message = note
            self._write_resample_record(state, fl_state, record, meta, None)
            return
        s = samples[chosen.index]
        record.workspace_ref = s["ws"]
        record.change_set = await self.workspace_manager.collect_changeset(s["ws"])
        patch, changed, digest = await self.workspace_manager.collect_patch(s["ws"])
        record.patch, record.changed_files, record.patch_hash = patch, changed, digest or None
        record.harness_score, record.furthest_stage = s["harness"], s["furthest"]
        record.behaviour_score, record.behaviour_failures, record.behaviour_total = s["behaviour"], sorted(s["failed"]), s.get("total")
        record.status = CandidateStatus.VALID
        record.quality_score = 1.0
        record.failure_reason = None
        record.failure_message = None
        fid = s["result"].state.final_output_artifact_id
        if fid:
            record.output_artifact_ids = [fid]
        Path(s["ws"].path).parent.joinpath("candidate_result.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
        self._write_resample_record(state, fl_state, record, meta, s)

    def _write_resample_record(self, state, fl_state, record, meta, chosen) -> None:
        """Explainability record: problem, milestone, contracts, topology, failure, blame, repair, outcome."""
        try:
            ws = record.workspace_ref.path if record.workspace_ref else None
            task_dir = Path(ws).parents[4] if ws else None
            if task_dir is None:
                return
            harness_dir = task_dir.parent.parent / "harness"
            graph = record.metadata.get("graph") or {}
            payload = {
                "problem": {"task_id": state.task_id, "milestone": fl_state.subtask_id},
                "milestone_design": {"plan_draft": str(task_dir.parent.parent / "milestone_plan_draft.json")},
                "contracts": {"path": str(harness_dir / f"{fl_state.subtask_id}.contracts.json")},
                "topology": {"nodes": [{"id": n.get("node_id"), "kind": n.get("node_kind"), "inputs": list((n.get("input_slots") or {}).keys())} for n in graph.get("nodes", [])],
                             "edges": [f"{e.get('source_node')}.{e.get('source_output')} -> {e.get('destination_node')}.{e.get('destination_input')}" for e in graph.get("edges", [])],
                             "dropped_prefix": meta.get("dropped"), "writer_steps": meta.get("steps")},
                "failure": {"persistent": record.metadata.get("persistent_failures"), "frames": meta.get("frames"),
                            "incumbent_behaviour": (fl_state.persistence or {}).get("samples")},
                "diagnosis": {"blamed_node": meta.get("node"), "position": meta.get("position"), "rule": "v1: last writer of the failing file; earliest owner",
                              "reason": meta.get("reason"), "owners": meta.get("owners"),
                              "llm_reflection": getattr(fl_state.diagnosis, "concise_feedback", None)},
                "repair": {"kind": "best_of_n_from_prefix", "n": meta.get("n"), "prefix_artifact": meta.get("prefix_artifact"), "injected": meta.get("injected")},
                "outputs": meta.get("samples"),
                "result": {"chosen": meta.get("chosen"), "agreement": meta.get("agreement"), "consensus_failed": meta.get("consensus_failed"),
                           "behaviour_score": record.behaviour_score, "harness_score": record.harness_score, "status": str(record.status), "note": meta.get("note")},
            }
            nr.write_record(task_dir, fl_state.subtask_id, record.candidate_id, payload)
        except Exception as exc:  # noqa: BLE001
            fl_state.notes.append(f"node_resample: record not written ({exc})")

    async def _harness_progress(

        self, result: GraphExecutionResult
    ) -> tuple[float | None, list[HarnessStageResult], str]:
        """The graded score this candidate's acceptance harness reported.

        Without it two failing candidates are indistinguishable and the selector
        falls back to price, so the fast loop learns to fail cheaply.
        """
        return await best_harness_progress(result, self.artifact_store)

    async def _commit_winner(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        winner: CandidateRecord,
        base_ws: WorkspaceRef,
        subtask_id: str,
        base_graph: OrchestraGraph,
    ) -> None:
        if winner.status is CandidateStatus.COMMITTED:
            return
        if winner.workspace_ref is None:
            raise CandidateWorkspaceError("winner missing workspace_ref")
        if winner.status is not CandidateStatus.VALID:
            raise CandidateWorkspaceError("refusing to commit non-valid winner")
        nrm = (winner.metadata or {}).get("node_resample") or {}
        if nrm.get("reauthor") and nrm.get("reauthor_spec_dir") and nrm.get("frozen_spec_dir"):
            # the re-authored suite becomes the milestone's frozen suite; the condemned one is kept aside
            new_dir, old_dir = Path(nrm["reauthor_spec_dir"]), Path(nrm["frozen_spec_dir"])
            if new_dir.is_dir():
                if old_dir.is_dir():
                    shutil.rmtree(str(old_dir) + ".condemned", ignore_errors=True)
                    old_dir.rename(str(old_dir) + ".condemned")
                shutil.copytree(new_dir, old_dir)
                fl_state.notes.append(f"node_resample: frozen suite replaced by the re-authored one ({new_dir.name})")

        expected_rev = base_ws.base_revision
        change_set = winner.change_set or await self.workspace_manager.collect_changeset(
            winner.workspace_ref
        )

        # PREPARE: apply without finalize, then re-run authoritative harness.
        try:
            await self.workspace_manager.apply_changeset(
                base=base_ws,
                winner=winner.workspace_ref,
                change_set=change_set,
                expected_base_revision=expected_rev,
            )
        except CandidateWorkspaceError as exc:
            winner.status = CandidateStatus.COMMIT_VALIDATION_FAILED
            winner.failure_message = str(exc)
            sub = state.subtasks[subtask_id]
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INFRA
            sub.failure_message = str(exc)
            fl_state.exhausted = True
            return

        command, timeout = self._harness_command(base_graph)
        passed, code, stdout, stderr = await run_authoritative_harness_command(
            cwd=base_ws.path,
            command=command,
            timeout_seconds=timeout,
        )
        if not passed:
            if expected_rev:
                await self.workspace_manager.rollback_to_revision(base_ws, expected_rev)
            winner.status = CandidateStatus.COMMIT_VALIDATION_FAILED
            winner.failure_message = (
                f"post-apply canonical harness failed exit={code}: "
                f"{(stderr or stdout)[-500:]}"
            )
            sub = state.subtasks[subtask_id]
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.HARNESS
            sub.failure_message = winner.failure_message
            fl_state.exhausted = True
            return

        # COMMIT: finalize git commit on canonical/base.
        new_rev = await self.workspace_manager.finalize_git_commit(
            base_ws, f"commit winner from {winner.candidate_id}"
        )
        committed = WorkspaceRef(
            workspace_id=base_ws.workspace_id,
            path=base_ws.path,
            kind=base_ws.kind,
            task_id=base_ws.task_id,
            subtask_id=base_ws.subtask_id,
            base_revision=new_rev,
        )

        sub = state.subtasks[subtask_id]
        sub.status = SubtaskStatus.COMMITTED
        sub.failure_reason = None
        sub.failure_message = None
        sub.workspace_ref = committed.path
        sub.current_graph_hash = winner.graph_hash
        sub.local_revision += 1
        if winner.output_artifact_ids:
            sub.final_output_artifact_id = winner.output_artifact_ids[0]
            sub.committed_artifacts = [
                ArtifactRef(
                    slot="final",
                    artifact_id=winner.output_artifact_ids[0],
                    artifact_type="RepositoryChangeArtifact",
                )
            ]
        sub.backend_sessions.extend(winner.backend_sessions)

        fl_state.selected_candidate_id = winner.candidate_id
        fl_state.selected_execution_cost = winner.cost
        winner.status = CandidateStatus.COMMITTED
        for cand in fl_state.candidates:
            if cand.candidate_id == winner.candidate_id:
                continue
            if cand.status not in {
                CandidateStatus.REJECTED,
                CandidateStatus.COMMIT_VALIDATION_FAILED,
            }:
                cand.status = CandidateStatus.DISCARDED
            if cand.workspace_ref is not None:
                await self.workspace_manager.discard_candidate(cand.workspace_ref)

        state.fast_loop_history.append(
            LocalUpdateRecord(
                record_id=f"{subtask_id}:{winner.candidate_id}",
                subtask_id=subtask_id,
                revision=sub.local_revision,
                summary=f"committed fast-loop winner {winner.candidate_id}",
                metadata={
                    "parent_graph_hash": winner.parent_graph_hash,
                    "graph_hash": winner.graph_hash,
                    "candidate_search_cost": fl_state.search_cost.model_dump(mode="json"),
                    "selected_execution_cost": winner.cost.model_dump(mode="json"),
                    "initial_execution_cost": fl_state.initial_execution_cost.model_dump(
                        mode="json"
                    ),
                    "total_method_cost": fl_state.total_method_cost.model_dump(mode="json"),
                    "edit_types": [e.type for e in winner.edits],
                    "spent_check": spent_from_state(fl_state).model_dump(mode="json"),
                },
            )
        )
        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.mark_ready_from_dependencies()
        logger.info(
            "fast_loop committed winner=%s subtask=%s search_cost=%s selected_cost=%s",
            winner.candidate_id,
            subtask_id,
            fl_state.search_cost.model_dump(),
            winner.cost.model_dump(),
        )

    async def _commit_non_repo_winner(
        self,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        winner: CandidateRecord,
        subtask_id: str,
    ) -> None:
        if winner.status is CandidateStatus.COMMITTED:
            return
        sub = state.subtasks[subtask_id]
        sub.status = SubtaskStatus.COMMITTED
        sub.failure_reason = None
        sub.failure_message = None
        sub.current_graph_hash = winner.graph_hash
        sub.local_revision += 1
        if winner.output_artifact_ids:
            sub.final_output_artifact_id = winner.output_artifact_ids[0]
        fl_state.selected_candidate_id = winner.candidate_id
        fl_state.selected_execution_cost = winner.cost
        winner.status = CandidateStatus.COMMITTED
        for cand in fl_state.candidates:
            if (
                cand.candidate_id != winner.candidate_id
                and cand.status is not CandidateStatus.REJECTED
            ):
                cand.status = CandidateStatus.DISCARDED
        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.mark_ready_from_dependencies()


def _collect_sessions(
    *,
    result: GraphExecutionResult,
    attempt_id: int,
    candidate_id: str,
) -> list[BackendSessionRecord]:
    from orchestra.backends.base import BackendSessionRef

    records: list[BackendSessionRecord] = []
    for node_id, meta in result.state.node_backend_metadata.items():
        raw = meta.get("session_ref")
        if not raw:
            continue
        session_ref = BackendSessionRef.model_validate(raw)
        backend_id = str(meta.get("backend_id") or session_ref.backend_id)
        records.append(
            BackendSessionRecord(
                node_id=node_id,
                backend_id=backend_id,
                attempt_id=attempt_id,
                session_ref=session_ref,
                candidate_id=candidate_id,
            )
        )
    return records


def _cost_from_result(result: GraphExecutionResult) -> CostRecord:
    from orchestra.control.backend_usage import derive_cost_usd

    prompt = 0
    completion = 0
    cached = 0
    calls = 0
    model_name: str | None = None
    for meta in result.state.node_backend_metadata.values():
        usage = meta.get("usage") or {}
        if isinstance(usage, dict):
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            cached += int(usage.get("cached_tokens") or 0)
        model_name = model_name or meta.get("model_name")
        if meta.get("backend_status") or meta.get("session_ref"):
            calls += 1
    if calls == 0:
        calls = 1
    # This used to carry its own price list, and it had drifted to a model an
    # order of magnitude cheaper than the one being run, with no allowance for
    # the cache hits that are most of a Codex session's input. Candidate costs
    # have to come off the same table as the run's cost axis or the two
    # disagree about what a candidate spent.
    estimated, _quality = derive_cost_usd(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        model_name=model_name,
        provider_cost_usd=None,
    )
    return CostRecord(
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost_usd=estimated or 0.0,
        backend_calls=calls,
    )

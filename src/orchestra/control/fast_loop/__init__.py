"""Milestone 4: Backend-agnostic Fast Local Adaptation."""

from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.llm_diagnosis import DiagnosisConfig, refine_diagnosis
from orchestra.control.fast_loop.playbook_generator import PlaybookCandidateGenerator
from orchestra.control.fast_loop.playbooks import (
    QUALITY_CATALOG,
    FailureClass,
    Playbook,
    SearchReason,
    playbooks_for,
)
from orchestra.control.fast_loop.schemas import (
    BackendModelPool,
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopState,
    LocalCandidate,
    LocalEdit,
    PlanRecompile,
    SessionPolicyEdit,
    WorkspaceChangeSet,
)
from orchestra.control.fast_loop.selector import DeterministicCandidateSelector

__all__ = [
    "BackendModelPool",
    "CandidateRecord",
    "CandidateRejectionReason",
    "CandidateStatus",
    "DeterministicCandidateSelector",
    "DiagnosisConfig",
    "FailureDiagnosis",
    "FastLoopBudget",
    "FastLoopController",
    "FailureClass",
    "FastLoopState",
    "LocalCandidate",
    "Playbook",
    "QUALITY_CATALOG",
    "PlaybookCandidateGenerator",
    "SearchReason",
    "LocalEdit",
    "PlanRecompile",
    "SessionPolicyEdit",
    "WorkspaceChangeSet",
    "diagnose_subtask_failure",
    "playbooks_for",
    "refine_diagnosis",
    "validate_candidate_against_capabilities",
]

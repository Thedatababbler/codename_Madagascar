"""Milestone 4 Fast Loop schemas (LocalEdit, candidates, budgets, state)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from orchestra.backends.base import BackendSessionRef
from orchestra.backends.capabilities import SessionPolicy
from orchestra.control.task_state import BackendSessionRecord, SubtaskFailureReason
from orchestra.ir.edges import EdgeCondition
from orchestra.ir.graph import OrchestraGraph
from orchestra.workspaces.base import WorkspaceRef


class PromptFeedbackEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["prompt_feedback"] = "prompt_feedback"
    node_id: str
    feedback: str


class ModelOverrideEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["model_override"] = "model_override"
    node_id: str
    model_name: str


class ToolPolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["tool_policy"] = "tool_policy"
    node_id: str
    add_tools: list[str] = Field(default_factory=list)
    remove_tools: list[str] = Field(default_factory=list)


class BudgetAdjustmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["budget_adjustment"] = "budget_adjustment"
    node_id: str
    max_steps_delta: int = 0
    timeout_seconds_delta: int = 0


class AddVerifierNodeEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["add_verifier_node"] = "add_verifier_node"
    target_node_id: str
    verifier_template_id: str


class SessionPolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["session_policy"] = "session_policy"
    node_id: str
    policy: SessionPolicy
    parent_session_ref: BackendSessionRef | None = None


class AddRoleAgentEdit(BaseModel):
    """Insert a role-pool agent into the milestone's subgraph.

    The `local_agent` family of the design document. Unlike
    ``add_verifier_node``, which can only attach one hardcoded structured
    verifier, this instantiates any capability from ``configs/roles`` and so is
    the edit that lets the search explore designs rather than retries.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["add_role_agent"] = "add_role_agent"
    role_id: str
    after_node_id: str
    # Parallel placement is refused for editing roles: two agents writing the
    # same workspace concurrently is the one topology the templates forbid.
    parallel: bool = False


class DropAgentEdit(BaseModel):
    """Remove a non-editing agent from the milestone's subgraph.

    Without an edit that can make a candidate *cheaper* than its parent, every
    point on the cost axis is worse-or-equal and the frontier collapses into
    "everything that passed". This is the edit that gives cost a direction to
    trade against quality.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["drop_agent"] = "drop_agent"
    node_id: str


class RewireEdgeEdit(BaseModel):
    """Re-gate or re-order an existing edge.

    The `local_edge` family. ``condition`` gates a downstream agent on an
    upstream failure, which is how a repair stage stops costing anything on the
    happy path; ``serialize`` orders two agents that currently run in parallel.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["rewire_edge"] = "rewire_edge"
    edge_id: str
    condition: EdgeCondition | None = None
    clear_condition: bool = False

    @model_validator(mode="after")
    def _one_intent(self) -> RewireEdgeEdit:
        if self.clear_condition and self.condition is not None:
            raise ValueError("rewire_edge cannot both set and clear a condition")
        if not self.clear_condition and self.condition is None:
            raise ValueError("rewire_edge needs a condition to set or clear_condition")
        return self


LocalEdit = Annotated[
    PromptFeedbackEdit
    | ModelOverrideEdit
    | ToolPolicyEdit
    | BudgetAdjustmentEdit
    | AddVerifierNodeEdit
    | SessionPolicyEdit
    | AddRoleAgentEdit
    | DropAgentEdit
    | RewireEdgeEdit,
    Field(discriminator="type"),
]

#: Edits that change the subgraph's shape rather than one node's parameters.
#: The fast loop's search is a design search only to the extent that it can
#: reach these.
TOPOLOGY_EDIT_TYPES = frozenset(
    {"add_verifier_node", "add_role_agent", "drop_agent", "rewire_edge"}
)


class FailureDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: SubtaskFailureReason
    retryable: bool
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    failed_node_ids: list[str] = Field(default_factory=list)
    primary_failed_node_id: str | None = None
    concise_feedback: str
    recommended_edit_types: list[str] = Field(default_factory=list)
    infrastructure_related: bool = False
    # How far the acceptance harness got before failing. Which capability the
    # milestone was missing follows from this, so a design edit can address the
    # stage that actually failed rather than adding an agent at random.
    furthest_stage: str = ""
    # Where a search should attach its edits when nothing failed. A quality search
    # runs on a milestone whose gate passed, so it has no failed node to edit, and
    # naming the anchor explicitly is better than letting the generator fall back
    # to whichever agent happens to be first.
    focus_node_id: str | None = None
    # ``budget`` / ``functional`` / ``design``. Empty means the generator infers
    # from ``reason`` and ``furthest_stage``. Filled in once LLM diagnosis exists;
    # the playbook table already keys on it.
    failure_class: str = ""
    #: Named tests the gate reported as failing. Computed for the selector but
    #: never put in a prompt until a playbook does it; carried here so that
    #: playbook can bind without reaching back into harness artifacts.
    behaviour_failures: list[str] = Field(default_factory=list)


class CostRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    backend_calls: int = 0


class StabilityIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    node_id: str | None = None


class FastLoopBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_candidates: int = 3
    max_total_backend_calls: int = 8
    max_total_cost: float | None = None
    max_total_tokens: int | None = None
    max_wall_time_seconds: int = 600
    max_attempts_per_subtask: int = 4


class BackendModelPool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    allowed_models: list[str] = Field(default_factory=list)
    fallback_order: list[str] = Field(default_factory=list)


class CandidateRejectionReason(StrEnum):
    UNSUPPORTED_SESSION_POLICY = "unsupported_session_policy"
    UNSUPPORTED_MODEL_OVERRIDE = "unsupported_model_override"
    UNSUPPORTED_TOOL_EDIT = "unsupported_tool_edit"
    INVALID_GRAPH_EDIT = "invalid_graph_edit"
    BUDGET_EXCEEDED = "budget_exceeded"
    WORKSPACE_INCOMPATIBLE = "workspace_incompatible"
    MISSING_GRAPH = "missing_graph"
    OTHER = "other"


class RenameRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_path: str
    to_path: str


class WorkspaceChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked_patch: str = ""
    modified_files: list[str] = Field(default_factory=list)
    added_untracked_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[RenameRecord] = Field(default_factory=list)
    file_manifest_hash: str = ""


class PlanRecompile(BaseModel):
    """How a plan-layer candidate differs from the attempt it is compared against.

    An edit-layer candidate is its parent plus a list of edits, so ``edits`` says
    everything about it. A plan-layer candidate is a different template with its
    slots refilled and the milestone recompiled, and no edit describes that.
    Without somewhere to record the pair, a winning candidate carries no account
    of what it changed and win rates cannot be aggregated across runs — which is
    the whole compensation for giving up attribution at edit granularity.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str
    parent_template_id: str = ""
    #: Slot id to the pool role filling it, in the template's own slot order.
    slots: dict[str, str] = Field(default_factory=dict)
    #: Slots whose role differs from the parent's, plus slots the parent did not
    #: have. The delta, as opposed to the assignment it is a delta on.
    changed_slots: list[str] = Field(default_factory=list)
    #: Separates this candidate's generated contracts and graph from the ones it
    #: is compared against, which share a directory for the length of a run.
    contract_namespace: str = ""


class LocalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    parent_graph_hash: str
    edits: list[LocalEdit]
    graph: OrchestraGraph
    session_policy: SessionPolicy = SessionPolicy.FRESH
    generation_reason: str
    #: Which playbook produced this candidate, empty for the anchor and for the
    #: fixed-order generators. Recorded on the candidate rather than derived from
    #: its edits because two playbooks may emit the same edit for different
    #: reasons, and it is the reason whose win rate is worth aggregating.
    playbook_id: str = ""
    plan_recompile: PlanRecompile | None = None
    compatibility_rejected: bool = False
    rejection_reason: CandidateRejectionReason | None = None
    rejection_message: str | None = None


class CandidateCompatibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatible: bool
    reason: str | None = None
    rejection_reason: CandidateRejectionReason | None = None


class CandidateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BACKEND_FAILED = "backend_failed"
    HARNESS_FAILED = "harness_failed"
    VALID = "valid"
    REJECTED = "rejected"
    COMMIT_VALIDATION_FAILED = "commit_validation_failed"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class CandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    attempt_id: int
    graph_hash: str
    parent_graph_hash: str
    edits: list[LocalEdit]
    #: Carried over from the candidate so a frontier read after the fact still
    #: says which playbook each entry came from and, for a plan-layer candidate,
    #: what it recompiled. `edits` is empty for those, so it cannot stand in.
    playbook_id: str = ""
    plan_recompile: PlanRecompile | None = None
    workspace_ref: WorkspaceRef | None = None
    backend_sessions: list[BackendSessionRecord] = Field(default_factory=list)
    output_artifact_ids: list[str] = Field(default_factory=list)
    harness_artifact_id: str | None = None
    status: CandidateStatus = CandidateStatus.PENDING
    quality_score: float | None = None
    # How far the harness got, when it grades itself. quality_score is the gate
    # and is only ever 0.0 or 1.0, so on its own it cannot separate two failed
    # candidates and the selector fell through to picking the cheaper one.
    harness_score: float | None = None
    # The behavioural slice of that score -- the stage measuring whether the code
    # works rather than whether it exists. Carried separately because it is the
    # only part that still moves once the structural stages saturate, and quality
    # comparison reads it in preference to the blend.
    behaviour_score: float | None = None
    #: Which behavioural tests this candidate failed, and how many the stage ran.
    #: Kept so quality can be compared over the tests that *differ* between the
    #: candidates of one search: all candidates in a search are graded against one
    #: frozen suite, and the tests they all pass or all fail contribute a constant.
    behaviour_failures: list[str] = Field(default_factory=list)
    behaviour_total: int | None = None
    #: Quality as the selector compared it, once the constant tests were removed.
    #: Derived from the pool, so it is meaningful only within its own search and is
    #: recorded rather than recomputed to keep a frontier auditable after the fact.
    comparable_quality: float | None = None
    furthest_stage: str = ""
    cost: CostRecord = Field(default_factory=CostRecord)
    stability_incidents: list[StabilityIncident] = Field(default_factory=list)
    latency_ms: int | None = None
    failure_reason: SubtaskFailureReason | None = None
    failure_message: str | None = None
    rejection_reason: CandidateRejectionReason | None = None
    rejection_message: str | None = None
    session_policy: SessionPolicy = SessionPolicy.FRESH
    patch: str = ""
    changed_files: list[str] = Field(default_factory=list)
    patch_hash: str | None = None
    change_set: WorkspaceChangeSet | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def sum_candidate_costs(candidates: list[CandidateRecord]) -> CostRecord:
    """Sum execution costs of candidates that actually ran a backend."""
    total = CostRecord()
    for cand in candidates:
        if cand.status is CandidateStatus.REJECTED and cand.cost.backend_calls == 0:
            continue
        total = CostRecord(
            prompt_tokens=total.prompt_tokens + cand.cost.prompt_tokens,
            completion_tokens=total.completion_tokens + cand.cost.completion_tokens,
            estimated_cost_usd=total.estimated_cost_usd + cand.cost.estimated_cost_usd,
            backend_calls=total.backend_calls + cand.cost.backend_calls,
        )
    return total


class FastLoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    base_attempt_id: int
    base_graph_hash: str
    diagnosis: FailureDiagnosis
    candidates: list[CandidateRecord] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    # Which candidates were mutually non-dominating, and the rule that picked one
    # of them. A frontier that always contains every candidate, or always exactly
    # one, is a degenerate search, and that is only visible if the frontier is
    # written down rather than inferred from the winner.
    pareto_frontier: list[str] = Field(default_factory=list)
    selection_rule: str = ""
    # Why the loop ran at all. A search triggered by a failed gate and one
    # triggered by a passing gate with a poor score have different success
    # conditions, and averaging them would mix a repair rate with a refinement
    # rate. Empty means the historical case, a failed gate.
    search_reason: str = ""
    # Why a quality search ended without adopting a candidate. "Declined" is a
    # legitimate and expected outcome, so it needs to be distinguishable from a
    # search that broke.
    notes: list[str] = Field(default_factory=list)
    exhausted: bool = False
    # Control-plane-only cost (e.g. future LLM generators). Deterministic gen = 0.
    control_plane_cost: CostRecord = Field(default_factory=CostRecord)
    # Cost of the initial subtask attempt that triggered the Fast Loop.
    initial_execution_cost: CostRecord = Field(default_factory=CostRecord)
    selected_execution_cost: CostRecord = Field(default_factory=CostRecord)
    infra_retries_used: int = 0
    started_monotonic: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_derived_cost_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # Derived fields may appear in older checkpoints; never load as storage.
            data.pop("search_cost", None)
            data.pop("total_method_cost", None)
            # Migrate accidental double-count field into control_plane if needed.
            if "control_plane_cost" not in data and "search_cost" in data:
                pass
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def search_cost(self) -> CostRecord:
        """Derived: sum of candidate execution costs (not double-counted)."""
        return sum_candidate_costs(self.candidates)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_method_cost(self) -> CostRecord:
        base = self.initial_execution_cost
        search = self.search_cost
        ctrl = self.control_plane_cost
        return CostRecord(
            prompt_tokens=base.prompt_tokens + search.prompt_tokens + ctrl.prompt_tokens,
            completion_tokens=(
                base.completion_tokens + search.completion_tokens + ctrl.completion_tokens
            ),
            estimated_cost_usd=(
                base.estimated_cost_usd
                + search.estimated_cost_usd
                + ctrl.estimated_cost_usd
            ),
            backend_calls=base.backend_calls + search.backend_calls + ctrl.backend_calls,
        )

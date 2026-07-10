from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class VersionedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION


class PublicExample(BaseModel):
    input: str
    output: str
    testtype: Literal["stdin", "functional"] = "stdin"


class ProblemArtifact(VersionedArtifact):
    """Agent-visible task data. Private tests are deliberately impossible to store."""

    question_id: str
    title: str
    statement: str
    starter_code: str = ""
    difficulty: str
    platform: str
    contest_date: str = ""
    public_examples: list[PublicExample] = Field(default_factory=list)
    function_name: str | None = None


class AlgorithmPlanArtifact(VersionedArtifact):
    problem_summary: str
    algorithm: str
    data_structures: list[str] = Field(default_factory=list)
    correctness_argument: str
    time_complexity: str
    space_complexity: str
    edge_cases: list[str]
    implementation_notes: list[str] = Field(default_factory=list)


class EdgeCaseArtifact(VersionedArtifact):
    input_output_interpretation: str
    edge_cases: list[str]
    overflow_risks: list[str] = Field(default_factory=list)
    indexing_risks: list[str] = Field(default_factory=list)
    interface_concerns: list[str] = Field(default_factory=list)
    likely_failure_modes: list[str] = Field(default_factory=list)


class CombinedPlanArtifact(VersionedArtifact):
    problem_summary: str
    algorithm: str
    correctness_argument: str
    time_complexity: str
    space_complexity: str
    data_structures: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    implementation_risks: list[str] = Field(default_factory=list)


class CodingInputArtifact(VersionedArtifact):
    problem: ProblemArtifact
    plan: AlgorithmPlanArtifact


class CodeArtifact(VersionedArtifact):
    language: Literal["python"] = "python"
    code: str
    source_node: str = ""
    explanation: str | None = None


class VisibleFailureSummary(VersionedArtifact):
    compile_error: str | None = None
    exception_type: str | None = None
    timeout: bool = False
    public_input: str | None = None
    expected_public_output: str | None = None
    actual_public_output: str | None = None
    failed_public_test_index: int | None = None
    errors: list[str] = Field(default_factory=list)


class RepairInputArtifact(VersionedArtifact):
    problem: ProblemArtifact
    plan: AlgorithmPlanArtifact
    previous_code: str
    visible_failure_summary: VisibleFailureSummary


class RepairArtifact(VersionedArtifact):
    diagnosis: str
    changes: list[str]
    revised_code: str


class PublicHarnessResultArtifact(VersionedArtifact):
    harness_available: bool
    repair_eligible: bool
    passed: bool
    pass_ratio: float
    compile_success: bool
    runtime_errors: int
    timeouts: int
    duration_ms: int
    failure_summary: VisibleFailureSummary


class FinalCodeArtifact(VersionedArtifact):
    language: Literal["python"] = "python"
    code: str
    source_artifact_id: str


class FinalAnswerArtifact(VersionedArtifact):
    answer: str
    raw_output: str | None = None
    source_node: str = ""
    extraction_status: Literal["ok", "empty", "malformed"] = "ok"


class ArtifactProvenance(VersionedArtifact):
    artifact_name: str
    produced_by: str
    consumed_by: list[str] = Field(default_factory=list)
    attempt: int = 0

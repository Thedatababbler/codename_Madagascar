from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from orchestra.schemas.artifacts import ProblemArtifact, PublicExample


class LCBTask(BaseModel):
    """Runtime task without private tests."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    question_id: str
    release_version: str
    problem: ProblemArtifact
    metadata: dict = Field(default_factory=dict)


class PrivateTestCase(BaseModel):
    input: str
    output: str
    testtype: str = "stdin"


class PrivateTaskData(BaseModel):
    """Evaluator-only data; never serialize into task artifacts or telemetry."""

    question_id: str
    private_tests: list[PrivateTestCase]
    public_tests: list[PublicExample]
    function_name: str | None = None


class AgentVisibleLCBTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    question_title: str
    question_content: str
    platform: str
    contest_date: datetime
    starter_code: str
    difficulty: str
    public_test_cases: list[PublicExample]
    metadata_public: dict = Field(default_factory=dict)


class HiddenLCBEvaluationRecord(BaseModel):
    question_id: str
    private_test_cases_ref: str

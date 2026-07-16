import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from orchestra.schemas.artifacts import (
    AlgorithmPlanArtifact,
    CodeArtifact,
    CombinedPlanArtifact,
    EdgeCaseArtifact,
    FinalAnswerArtifact,
    FinalCodeArtifact,
    ProblemArtifact,
    PublicHarnessResultArtifact,
    RepairArtifact,
    RepairInputArtifact,
    RepositoryChangeArtifact,
    RepositoryHarnessResultArtifact,
    VisibleFailureSummary,
)

PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        ProblemArtifact,
        AlgorithmPlanArtifact,
        EdgeCaseArtifact,
        CombinedPlanArtifact,
        CodeArtifact,
        PublicHarnessResultArtifact,
        VisibleFailureSummary,
        RepairInputArtifact,
        RepairArtifact,
        FinalCodeArtifact,
        FinalAnswerArtifact,
        RepositoryChangeArtifact,
        RepositoryHarnessResultArtifact,
    )
}


class ArtifactEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    artifact_type: str
    schema_version: str = "1.0"
    producer_node_id: str
    task_id: str
    created_at: datetime
    payload: dict[str, Any]
    parent_artifact_ids: tuple[str, ...] = ()
    content_hash: str

    def validate_payload(self) -> BaseModel:
        if self.artifact_type not in PAYLOAD_SCHEMAS:
            raise ValueError(f"Unknown artifact type: {self.artifact_type}")
        parsed = PAYLOAD_SCHEMAS[self.artifact_type].model_validate(self.payload)
        canonical = json.dumps(
            parsed.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        if expected != self.content_hash:
            raise ValueError(f"Artifact content hash mismatch: {self.artifact_id}")
        return parsed


class ArtifactBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slots: dict[str, ArtifactEnvelope] = Field(default_factory=dict)


def create_artifact(
    payload: BaseModel,
    *,
    producer_node_id: str,
    task_id: str,
    parent_artifact_ids: list[str] | tuple[str, ...] = (),
) -> ArtifactEnvelope:
    data = payload.model_dump(mode="json")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return ArtifactEnvelope(
        artifact_id=f"{task_id}-{producer_node_id}-{uuid4().hex[:12]}",
        artifact_type=type(payload).__name__,
        producer_node_id=producer_node_id,
        task_id=task_id,
        created_at=datetime.now(UTC),
        payload=data,
        parent_artifact_ids=tuple(parent_artifact_ids),
        content_hash=digest,
    )

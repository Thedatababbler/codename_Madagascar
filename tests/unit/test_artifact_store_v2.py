import pytest

from orchestra.ir.artifacts import create_artifact
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.storage.artifacts import FileArtifactStore


@pytest.mark.asyncio
async def test_artifact_round_trip_and_immutability(tmp_path):
    store = FileArtifactStore(tmp_path)
    artifact = create_artifact(
        ProblemArtifact(
            question_id="q",
            title="Q",
            statement="S",
            difficulty="easy",
            platform="synthetic",
        ),
        producer_node_id="input",
        task_id="q",
    )
    await store.put(artifact)
    assert await store.get(artifact.artifact_id) == artifact
    await store.put(artifact)
    changed = create_artifact(
        ProblemArtifact(
            question_id="q",
            title="Changed",
            statement="S",
            difficulty="easy",
            platform="synthetic",
        ),
        producer_node_id="input",
        task_id="q",
    ).model_copy(update={"artifact_id": artifact.artifact_id})
    with pytest.raises(RuntimeError, match="Immutable artifact"):
        await store.put(changed)

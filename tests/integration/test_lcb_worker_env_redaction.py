import pytest


@pytest.mark.asyncio
async def test_worker_does_not_inherit_api_keys(
    monkeypatch, official_sandbox, visible_echo_task
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-worker")
    monkeypatch.setenv("CUSTOM_SECRET", "must-not-reach-worker")
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="print(input())",
        timeout_seconds=10,
    )
    assert result.worker_metadata["sensitive_env_present"] == []

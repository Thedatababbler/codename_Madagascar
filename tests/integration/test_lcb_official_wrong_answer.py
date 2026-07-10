import pytest


@pytest.mark.asyncio
async def test_wrong_answer_is_structured_failure(official_sandbox, visible_echo_task):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="print(0)",
        timeout_seconds=10,
    )
    assert result.compiled
    assert result.passed_count == 0
    assert result.total_count == 1
    assert not result.per_test_visible_results[0].passed

import pytest


@pytest.mark.asyncio
async def test_known_correct_solution_passes(official_sandbox, visible_echo_task):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="print(input())",
        timeout_seconds=10,
    )
    assert result.compiled
    assert result.passed_count == result.total_count == 1
    assert result.runtime_errors == 0
    assert result.timeouts == 0

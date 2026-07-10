import pytest


@pytest.mark.asyncio
async def test_infinite_loop_is_killed_as_process_group(official_sandbox, visible_echo_task):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="while True:\n    pass",
        timeout_seconds=2,
    )
    assert result.passed_count == 0
    assert result.timeouts >= 1

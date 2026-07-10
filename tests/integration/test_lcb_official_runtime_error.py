import pytest


@pytest.mark.asyncio
async def test_runtime_error_does_not_escape_worker(official_sandbox, visible_echo_task):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="raise RuntimeError('boom')",
        timeout_seconds=10,
    )
    assert result.compiled
    assert result.passed_count == 0
    assert result.runtime_errors >= 1
    assert result.per_test_visible_results[0].runtime_error

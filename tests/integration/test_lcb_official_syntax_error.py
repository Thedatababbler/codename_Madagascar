import pytest


@pytest.mark.asyncio
async def test_syntax_error_is_structured(official_sandbox, visible_echo_task):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="def broken(",
        timeout_seconds=10,
    )
    assert not result.compiled
    assert result.passed_count == 0
    assert "line" in (result.stderr_summary or "")


@pytest.mark.asyncio
async def test_ast_valid_but_compile_invalid_is_not_compiled(
    official_sandbox, visible_echo_task
):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="return",
        timeout_seconds=10,
    )
    assert result.compiled is False

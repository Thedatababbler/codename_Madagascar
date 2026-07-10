import pytest


@pytest.mark.asyncio
async def test_worker_observes_requested_resource_limits(
    official_sandbox, visible_echo_task
):
    result = await official_sandbox.evaluate_public(
        task=visible_echo_task,
        code="print(input())",
        timeout_seconds=10,
    )
    limits = result.worker_metadata["resource_limits"]
    assert limits["memory_bytes"] <= 2048 * 1024 * 1024
    assert limits["max_processes"] <= 32
    assert limits["max_open_files"] <= 128
    assert limits["max_file_size_bytes"] <= 16 * 1024 * 1024
    assert result.worker_metadata["num_process_evaluate"] == 1
    assert result.worker_metadata["worker_identity"]["uid"] != 0

from orchestra.sandbox.lcb_official import compute_worker_wall_timeout


def test_worker_wall_timeout_scales_with_test_count():
    assert compute_worker_wall_timeout(
        per_test_seconds=2,
        test_count=3,
        worker_grace_seconds=5,
        max_worker_wall_seconds=60,
    ) == 14


def test_worker_wall_timeout_is_capped():
    assert compute_worker_wall_timeout(
        per_test_seconds=6,
        test_count=100,
        worker_grace_seconds=5,
        max_worker_wall_seconds=60,
    ) == 60

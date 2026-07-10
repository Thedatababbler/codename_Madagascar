from orchestra.schemas.task import PrivateTaskData, PrivateTestCase


def test_public_worker_request_cannot_contain_private_tests(
    official_sandbox, visible_echo_task
):
    hidden = PrivateTaskData(
        question_id="echo",
        public_tests=visible_echo_task.public_test_cases,
        private_tests=[
            PrivateTestCase(
                input="PRIVATE_MARKER_INPUT",
                output="PRIVATE_MARKER_OUTPUT",
            )
        ],
    )
    request = official_sandbox.build_request(
        task=visible_echo_task,
        code="print(input())",
        timeout_seconds=10,
    )
    encoded = request.model_dump_json()
    assert hidden.private_tests[0].input not in encoded
    assert hidden.private_tests[0].output not in encoded
    assert "private_test" not in encoded.lower()

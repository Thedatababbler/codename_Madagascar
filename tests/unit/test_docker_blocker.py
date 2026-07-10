import shutil

import pytest

from orchestra.sandbox.docker import DockerSandbox, DockerUnavailableError


def test_real_sandbox_fails_closed_when_docker_is_missing():
    if shutil.which("docker") is not None:
        pytest.skip("Docker is available on this runner")
    with pytest.raises(DockerUnavailableError, match="unavailable"):
        DockerSandbox()

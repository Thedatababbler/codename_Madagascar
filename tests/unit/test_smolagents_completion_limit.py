"""gpt-5 rejects max_tokens; the factory must send the name the host accepts."""

from orchestra.backends.base import ModelSpec
from orchestra.backends.smolagents_model import completion_limit


def test_gpt5_uses_max_completion_tokens() -> None:
    assert completion_limit(ModelSpec(name="gpt-5.4", max_tokens=8192)) == {
        "max_completion_tokens": 8192
    }


def test_older_chat_models_keep_max_tokens() -> None:
    assert completion_limit(ModelSpec(name="gpt-4.1-mini", max_tokens=2048)) == {
        "max_tokens": 2048
    }

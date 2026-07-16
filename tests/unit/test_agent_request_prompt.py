"""Unit tests for generic AgentRequest prompt rendering."""

from orchestra.backends.base import AgentRequest, ModelSpec, OutputContract
from orchestra.prompts.agent_request import render_agent_request_messages


def _request(**overrides) -> AgentRequest:
    base = {
        "request_id": "r1",
        "task_id": "t1",
        "node_id": "n1",
        "role": "coder",
        "instruction": "fallback instruction",
        "rendered_context": "last-user-only",
        "model": ModelSpec(name="m"),
        "tools": [],
        "max_steps": 1,
        "timeout_seconds": 30.0,
        "output_contract": OutputContract(
            parser_id="repository_change",
            output_schema="RepositoryChangeArtifact",
        ),
        "messages": [],
    }
    base.update(overrides)
    return AgentRequest(**base)


def test_render_includes_system_and_user():
    text = render_agent_request_messages(
        _request(
            messages=[
                {"role": "system", "content": "You are a careful implementer."},
                {"role": "user", "content": "Fix the bug."},
            ]
        )
    )
    assert "[SYSTEM]\nYou are a careful implementer." in text
    assert "[USER]\nFix the bug." in text
    assert "last-user-only" not in text


def test_render_falls_back_to_instruction():
    text = render_agent_request_messages(_request(messages=[], instruction="Do the work"))
    assert text == "[USER]\nDo the work"

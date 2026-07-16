"""Generic AgentRequest → prompt text rendering (backend-agnostic)."""

from __future__ import annotations

from orchestra.backends.base import AgentRequest


def render_agent_request_messages(request: AgentRequest) -> str:
    """Render the full contract message list for backends that take a single string.

    Includes system / role / constraints / user turns from ``request.messages``.
    Falls back to ``instruction`` only when messages are empty.
    """
    sections: list[str] = []
    for message in request.messages:
        role = str(message.get("role", "user")).upper()
        content = str(message.get("content", "")).strip()
        if content:
            sections.append(f"[{role}]\n{content}")
    if not sections and request.instruction:
        sections.append(f"[USER]\n{request.instruction.strip()}")
    return "\n\n".join(sections)

"""CodeAgent capabilities: FRESH only; RESUME/FORK rejected."""

from __future__ import annotations

from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import capabilities_for
from orchestra.backends.smolagents_code import SmolagentsCodeBackend


def test_catalog_and_runtime_codeagent_fresh_only():
    catalog = capabilities_for("smolagents_code")
    assert catalog is not None
    assert catalog.supported_session_policies == frozenset({SessionPolicy.FRESH})
    assert catalog.supports_session_state is False

    runtime = SmolagentsCodeBackend().capabilities
    assert runtime.supported_session_policies == frozenset({SessionPolicy.FRESH})
    assert runtime.supports_policy(SessionPolicy.RESUME) is False
    assert runtime.supports_policy(SessionPolicy.FORK) is False

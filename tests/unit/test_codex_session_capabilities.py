"""Codex M4-A session capabilities: FRESH only until M4-B."""

from __future__ import annotations

import pytest

from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import capabilities_for


def test_codex_catalog_fresh_only_m4a():
    caps = capabilities_for("codex_sdk")
    assert caps is not None
    assert SessionPolicy.FRESH in caps.supported_session_policies
    assert SessionPolicy.RESUME not in caps.supported_session_policies
    assert SessionPolicy.FORK not in caps.supported_session_policies
    assert caps.supports_session_state is True


def test_codex_runtime_capabilities_match_catalog():
    pytest.importorskip("openai_codex")
    from orchestra.backends.codex_sdk import CodexSDKBackend

    runtime = CodexSDKBackend(client_factory=lambda: None).capabilities
    catalog = capabilities_for("codex_sdk")
    assert catalog is not None
    assert runtime.supported_session_policies == catalog.supported_session_policies
    assert runtime.supports_session_state == catalog.supports_session_state

"""Codex session capabilities: FRESH/RESUME/FORK when SDK exposes lifecycle APIs."""

from __future__ import annotations

import pytest

from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import capabilities_for
from orchestra.backends.codex_lifecycle import sdk_supports_resume_fork


def test_codex_catalog_supports_resume_fork_when_sdk_ready():
    caps = capabilities_for("codex_sdk")
    assert caps is not None
    assert SessionPolicy.FRESH in caps.supported_session_policies
    assert caps.supports_session_state is True
    # Catalog declares resume/fork; runtime must match installed SDK.
    assert SessionPolicy.RESUME in caps.supported_session_policies
    assert SessionPolicy.FORK in caps.supported_session_policies
    assert caps.supports_resume is True
    assert caps.supports_fork is True
    assert caps.supports_cross_workspace_resume is True
    assert caps.supports_cross_workspace_fork is True


def test_codex_runtime_capabilities_match_catalog():
    pytest.importorskip("openai_codex")
    from openai_codex import AsyncCodex

    from orchestra.backends.codex_sdk import CodexSDKBackend

    assert sdk_supports_resume_fork(AsyncCodex)
    runtime = CodexSDKBackend(client_factory=lambda: None).capabilities
    catalog = capabilities_for("codex_sdk")
    assert catalog is not None
    assert runtime.supported_session_policies == catalog.supported_session_policies
    assert runtime.supports_session_state == catalog.supports_session_state
    assert runtime.supports_resume == catalog.supports_resume
    assert runtime.supports_fork == catalog.supports_fork

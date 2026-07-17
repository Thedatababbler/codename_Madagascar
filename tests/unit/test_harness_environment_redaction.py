"""Harness env must not receive model/cloud secrets."""

from __future__ import annotations

import os

from orchestra.harness.env_redaction import build_harness_env, is_sensitive_environment_name


def test_sensitive_names_detected():
    assert is_sensitive_environment_name("OPENAI_API_KEY")
    assert is_sensitive_environment_name("ANTHROPIC_API_KEY")
    assert is_sensitive_environment_name("AWS_SECRET_ACCESS_KEY")
    assert is_sensitive_environment_name("MY_CUSTOM_TOKEN")
    assert not is_sensitive_environment_name("PATH")
    assert not is_sensitive_environment_name("PYTHONPATH")


def test_build_harness_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_harness_env()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"
    # Parent process still has secrets.
    assert os.environ.get("OPENAI_API_KEY") == "sk-secret"

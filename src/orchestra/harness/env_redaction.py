"""Secret environment redaction for AdaMAS harness subprocesses."""

from __future__ import annotations

import os
from collections.abc import Mapping

SENSITIVE_EXACT_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HF_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCP_SERVICE_ACCOUNT_KEY",
        "AZURE_CLIENT_SECRET",
        "CODEX_API_KEY",
    }
)

SENSITIVE_SUFFIXES = (
    "_API_KEY",
    "_SECRET",
    "_TOKEN",
    "_PASSWORD",
    "_CREDENTIALS",
)


def is_sensitive_environment_name(name: str) -> bool:
    upper = name.upper()
    if upper in SENSITIVE_EXACT_NAMES:
        return True
    return any(upper.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def build_harness_env(
    base: Mapping[str, str] | None = None,
    *,
    extra_deny: set[str] | None = None,
) -> dict[str, str]:
    """Copy env without model/cloud credentials (trusted-fixture harness only)."""
    source = dict(base if base is not None else os.environ)
    deny = set(extra_deny or ())
    cleaned: dict[str, str] = {}
    for key, value in source.items():
        if key in deny or is_sensitive_environment_name(key):
            continue
        cleaned[key] = value
    # Explicitly ensure common secrets are absent even if naming changes.
    for key in SENSITIVE_EXACT_NAMES:
        cleaned.pop(key, None)
    return cleaned

"""A fixed pool of agent roles that a planner selects from.

Letting the planner invent a role name per milestone produced labels that
carried no information: every plan came back as ``implementer`` followed by
something called ``integration``, because nothing constrained the vocabulary or
attached behaviour to it. Here a role is a first-class object with its own
prompt, so selecting one is a real decision with a real consequence, and two
plans that pick different roles differ in what the agent is actually told.

The pool is fixed and small on purpose (EvoMAS, arXiv:2605.08769): the planner
chooses *which* capabilities a milestone needs and in what order, rather than
authoring capabilities from scratch each time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import yaml

# ORCHESTRA_ROLE_POOL_DIR lets a control run load a frozen copy of the pool
# (e.g. an older test_author) without touching the live directory.
DEFAULT_POOL_DIR = Path(os.environ.get("ORCHESTRA_ROLE_POOL_DIR") or "configs/roles")

MAX_TOKENS_RANGE = (1024, 16384)
MAX_STEPS_RANGE = (1, 24)
TIMEOUT_RANGE = (120.0, 2400.0)


class RolePoolError(ValueError):
    """Raised when a role definition on disk is unusable."""


@dataclass(frozen=True)
class RoleSpec:
    """One capability an agent node can be instantiated with."""

    role_id: str
    title: str
    capability: str
    prompt: str
    edits_repository: bool = True
    max_tokens: int = 8192
    max_steps: int = 12
    timeout_seconds: float = 1200.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "title": self.title,
            "capability": self.capability,
            "edits_repository": self.edits_repository,
            "max_tokens": self.max_tokens,
            "max_steps": self.max_steps,
            "timeout_seconds": self.timeout_seconds,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class RolePool:
    """The set of roles a planner may choose from."""

    roles: dict[str, RoleSpec]

    def __contains__(self, role_id: object) -> bool:
        return role_id in self.roles

    def __iter__(self):  # noqa: ANN204 - iterating a pool yields its roles
        return iter(self.ordered())

    def __len__(self) -> int:
        return len(self.roles)

    def get(self, role_id: str) -> RoleSpec | None:
        return self.roles.get(role_id)

    def require(self, role_id: str) -> RoleSpec:
        role = self.roles.get(role_id)
        if role is None:
            raise RolePoolError(f"unknown role {role_id!r}")
        return role

    def ordered(self) -> list[RoleSpec]:
        """Editing roles first, then read-only ones; stable within each group."""
        return sorted(
            self.roles.values(),
            key=lambda role: (not role.edits_repository, role.role_id),
        )

    def role_for_node_id(self, node_id: str) -> RoleSpec | None:
        """The role a compiled agent node was instantiated with, or ``None``.

        Read off the node id, which the subgraph builder composes as
        ``agent_<n>_<slot>_<role_id>``, because a compiled node carries its
        contract but not its role. A planner may name a slot's ``role_id``
        itself, in which case the suffix does not match and the role is
        genuinely unknown — callers must decide what to assume, and the safe
        assumption is that an unidentified agent edits the repository.
        """
        for role in self.ordered():
            if node_id.endswith(f"_{role.role_id}"):
                return role
        return None

    def catalog_lines(self) -> list[str]:
        """One line per role, for the planner prompt."""
        lines = []
        for role in self.ordered():
            suffix = "" if role.edits_repository else " (read-only; reports, never edits)"
            lines.append(f"- `{role.role_id}`: {role.capability}{suffix}")
        return lines


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def parse_role(payload: Any, *, source: str = "<memory>") -> RoleSpec:
    if not isinstance(payload, dict):
        raise RolePoolError(f"{source}: role definition must be a mapping")
    role_id = str(payload.get("role_id") or "").strip()
    if not role_id:
        raise RolePoolError(f"{source}: role_id is required")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise RolePoolError(f"{source}: role {role_id!r} has no prompt")
    capability = str(payload.get("capability") or "").strip()
    if not capability:
        raise RolePoolError(f"{source}: role {role_id!r} has no capability line")
    budget = payload.get("budget") or {}
    if not isinstance(budget, dict):
        budget = {}
    return RoleSpec(
        role_id=role_id,
        title=str(payload.get("title") or role_id).strip(),
        capability=capability,
        prompt=prompt,
        edits_repository=bool(payload.get("edits_repository", True)),
        max_tokens=_clamp_int(budget.get("max_tokens"), *MAX_TOKENS_RANGE, 8192),
        max_steps=_clamp_int(budget.get("max_steps"), *MAX_STEPS_RANGE, 12),
        timeout_seconds=_clamp_float(
            budget.get("timeout_seconds"), *TIMEOUT_RANGE, 1200.0
        ),
        tags=[str(tag) for tag in (payload.get("tags") or []) if str(tag).strip()],
    )


def load_role_pool(directory: str | Path = DEFAULT_POOL_DIR) -> RolePool:
    """Load every ``*.yaml`` role definition under ``directory``."""
    root = Path(directory)
    if not root.is_dir():
        raise RolePoolError(f"role pool directory not found: {root}")
    roles: dict[str, RoleSpec] = {}
    for path in sorted(root.glob("*.yaml")):
        role = parse_role(
            yaml.safe_load(path.read_text(encoding="utf-8")), source=str(path)
        )
        if role.role_id in roles:
            raise RolePoolError(f"duplicate role_id {role.role_id!r} in {path}")
        roles[role.role_id] = role
    if not roles:
        raise RolePoolError(f"no role definitions found under {root}")
    return RolePool(roles=roles)


@lru_cache(maxsize=4)
def default_role_pool(directory: str | Path = DEFAULT_POOL_DIR) -> RolePool:
    """Process-wide cached pool; role files do not change during a run."""
    return load_role_pool(directory)

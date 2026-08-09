"""Risk-first dynamic milestone planner for RealBench repository tasks.

The planner answers one question: *does this task contain a decision whose
failure would silently invalidate everything built afterwards?* Milestones exist
only to fence such blast-radius risks — never to mirror the directory tree.

Everything here is fail-closed: an unusable LLM answer yields ``None`` and the
caller keeps the deterministic public_design plan.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from orchestra.realbench.public_harness import (
    parse_expected_modules,
    parse_package_exports,
)

Role = Literal["discovery", "implementation", "integration"]

PLAN_ENV_FLAG = "ADAMAS_REALBENCH_DYNAMIC_PLAN"
PLAN_MODEL_ENV = "ADAMAS_REALBENCH_PLANNER_MODEL"
ROLES: frozenset[str] = frozenset({"discovery", "implementation", "integration"})
ALLOWED_CHECK_TYPES = frozenset(
    {"import", "export", "callable_or_class", "module_file_exists"}
)

MAX_MILESTONES = 4
MAX_AGENTS_PER_MILESTONE = 3
MAX_TOKENS_RANGE = (1024, 16384)
MAX_STEPS_RANGE = (1, 24)
TIMEOUT_RANGE = (120.0, 2400.0)


@dataclass(frozen=True)
class AgentDraft:
    """One agent node inside a milestone subgraph."""

    role_id: str
    title: str
    mandate: str
    focus_paths: list[str] = field(default_factory=list)
    max_tokens: int = 8192
    max_steps: int = 12
    timeout_seconds: float = 1200.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MilestoneAcceptance:
    """Acceptance harness attached to a milestone."""

    criteria: list[str] = field(default_factory=list)
    corner_cases: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MilestoneDraft:
    milestone_id: str
    title: str
    objective: str
    risk_rationale: str
    role: Role
    depends_on: list[str] = field(default_factory=list)
    focus_paths: list[str] = field(default_factory=list)
    acceptance: MilestoneAcceptance = field(default_factory=MilestoneAcceptance)
    agents: list[AgentDraft] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True)
class MilestonePlanDraft:
    milestones: list[MilestoneDraft]
    rationale: str
    generator: str = "llm"

    @property
    def split(self) -> bool:
        return len(self.milestones) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "rationale": self.rationale,
            "split": self.split,
            "milestones": [m.to_dict() for m in self.milestones],
        }


class MilestonePlanError(ValueError):
    """Raised when an LLM plan payload cannot be normalized safely."""


def _slug(text: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", str(text).strip().lower()).strip("_")
    return cleaned[:48] or fallback


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


def _string_list(value: Any, *, limit: int, max_chars: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:max_chars])
        if len(out) >= limit:
            break
    return out


def _focus_paths(value: Any, *, limit: int = 12) -> list[str]:
    """Normalize focus paths to repository-relative form.

    ``tree.txt`` is rooted at ``proj_clean/``, which does not exist in the agent
    workspace; a focus path keeping that prefix invites the agent to create the
    directory and hide the real package inside it.
    """
    out: list[str] = []
    for raw in _string_list(value, limit=limit, max_chars=120):
        text = raw.replace("\\", "/").lstrip("./")
        for prefix in ("proj_clean/", "proj_with_test/"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        text = text.strip("/")
        if text and text not in {"proj_clean", "proj_with_test"}:
            out.append(text)
    return out


def sanitize_contract_checks(raw_checks: Any, *, limit: int = 60) -> list[dict[str, Any]]:
    """Keep only machine-checkable, AST-safe public contract checks."""
    if not isinstance(raw_checks, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type") or "").strip()
        if ctype not in ALLOWED_CHECK_TYPES:
            continue
        cleaned: dict[str, Any] = {"type": ctype}
        if ctype == "module_file_exists":
            path = str(item.get("path") or "").replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            cleaned["path"] = path
        else:
            module = str(item.get("module") or "").strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", module):
                continue
            cleaned["module"] = module
            if ctype in {"export", "callable_or_class"}:
                symbol = str(item.get("symbol") or "").strip()
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol):
                    continue
                cleaned["symbol"] = symbol
        levels = item.get("required_levels")
        wanted = [str(x) for x in levels if str(x) in ROLES] if isinstance(levels, list) else []
        cleaned["required_levels"] = wanted or ["integration"]
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _parse_agents(
    raw: Any, *, milestone_id: str, max_agents: int = MAX_AGENTS_PER_MILESTONE
) -> list[AgentDraft]:
    items = raw if isinstance(raw, list) else []
    agents: list[AgentDraft] = []
    used: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        mandate = str(item.get("mandate") or item.get("instruction") or "").strip()
        if not mandate:
            continue
        role_id = _slug(
            item.get("role_id") or item.get("role") or f"agent{index + 1}",
            fallback=f"agent{index + 1}",
        )
        while role_id in used:
            role_id = f"{role_id}_2"
        used.add(role_id)
        agents.append(
            AgentDraft(
                role_id=role_id,
                title=str(item.get("title") or role_id).strip()[:120],
                mandate=mandate[:4000],
                focus_paths=_focus_paths(item.get("focus_paths")),
                max_tokens=_clamp_int(item.get("max_tokens"), *MAX_TOKENS_RANGE, 8192),
                max_steps=_clamp_int(item.get("max_steps"), *MAX_STEPS_RANGE, 12),
                timeout_seconds=_clamp_float(
                    item.get("timeout_seconds"), *TIMEOUT_RANGE, 1200.0
                ),
            )
        )
        if len(agents) >= max_agents:
            break
    if not agents:
        agents.append(
            AgentDraft(
                role_id="implementer",
                title=f"Implementer for {milestone_id}",
                mandate=(
                    "Implement this milestone end to end and keep the public "
                    "acceptance harness green."
                ),
            )
        )
    return agents


def parse_plan_payload(
    payload: Any,
    *,
    max_milestones: int = MAX_MILESTONES,
    max_agents: int = MAX_AGENTS_PER_MILESTONE,
) -> MilestonePlanDraft:
    """Normalize a raw planner payload into a validated milestone DAG.

    ``max_agents`` bounds what a *planner* may propose per milestone. A plan
    that was already validated and frozen -- such as the merged single-milestone
    arm of an A/B test -- carries the agent count of the whole plan, and
    re-clamping it here would silently hand that arm less compute.

    Raises ``MilestonePlanError`` when nothing usable survives validation.
    """
    if isinstance(payload, str):
        match = re.search(r"\{[\s\S]*\}", payload)
        if not match:
            raise MilestonePlanError("planner returned no JSON object")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise MilestonePlanError(f"planner JSON decode failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise MilestonePlanError("planner payload is not an object")

    raw_milestones = payload.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise MilestonePlanError("planner payload has no milestones")

    milestones: list[MilestoneDraft] = []
    known_ids: list[str] = []
    for index, item in enumerate(raw_milestones):
        if not isinstance(item, dict):
            continue
        objective = str(item.get("objective") or "").strip()
        if not objective:
            continue
        milestone_id = _slug(
            item.get("milestone_id") or item.get("id") or f"milestone_{index + 1}",
            fallback=f"milestone_{index + 1}",
        )
        while milestone_id in known_ids:
            milestone_id = f"{milestone_id}_2"
        role = str(item.get("role") or "").strip()
        if role not in ROLES:
            role = "integration" if index == len(raw_milestones) - 1 else "implementation"
        # A milestone may only depend on milestones already declared: keeps the
        # DAG acyclic by construction regardless of what the model emitted.
        depends_on = [
            _slug(dep, fallback="")
            for dep in _string_list(item.get("depends_on"), limit=6, max_chars=64)
        ]
        depends_on = [dep for dep in depends_on if dep in known_ids]
        if not depends_on and known_ids:
            depends_on = [known_ids[-1]]

        acceptance_raw = item.get("acceptance")
        acceptance_raw = acceptance_raw if isinstance(acceptance_raw, dict) else {}
        acceptance = MilestoneAcceptance(
            criteria=_string_list(acceptance_raw.get("criteria"), limit=12),
            corner_cases=_string_list(acceptance_raw.get("corner_cases"), limit=12),
            checks=sanitize_contract_checks(acceptance_raw.get("checks")),
        )

        milestones.append(
            MilestoneDraft(
                milestone_id=milestone_id,
                title=str(item.get("title") or milestone_id).strip()[:120],
                objective=objective[:4000],
                risk_rationale=str(item.get("risk_rationale") or "").strip()[:1000],
                role=role,  # type: ignore[arg-type]
                depends_on=depends_on,
                focus_paths=_focus_paths(item.get("focus_paths")),
                acceptance=acceptance,
                agents=_parse_agents(
                    item.get("agents"),
                    milestone_id=milestone_id,
                    max_agents=max_agents,
                ),
            )
        )
        known_ids.append(milestone_id)
        if len(milestones) >= max_milestones:
            break

    if not milestones:
        raise MilestonePlanError("no milestone survived validation")

    # A split plan is only worth its overhead when the early milestones justify
    # themselves as risk gates; otherwise collapse to the terminal milestone.
    if len(milestones) > 1 and not any(m.risk_rationale for m in milestones[:-1]):
        milestones = [milestones[-1]]

    # The last gate before freezing must grade the full public contract: an
    # implementation-level terminal milestone commits code whose UML-declared
    # symbols were never required to be importable from their documented module.
    if milestones[-1].role != "integration":
        milestones[-1] = replace(milestones[-1], role="integration")

    return MilestonePlanDraft(
        milestones=milestones,
        rationale=str(payload.get("rationale") or "").strip()[:2000],
    )


def _read_text(path: Path, *, limit: int) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


@dataclass(frozen=True)
class PlanningBrief:
    """Dataset-agnostic planning inputs.

    Each dataset describes a repository differently (RealBench ships UML under
    ``public_design/``, CodeProjectEval ships a PRD plus a pyreverse diagram), so
    the planner takes documents rather than reaching into a fixed layout.
    """

    task_id: str
    documents: list[tuple[str, str]] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    exports: dict[str, list[str]] = field(default_factory=dict)
    acceptance_note: str = (
        "Hidden benchmark tests do not exist for you: acceptance may only "
        "reference the public design below."
    )


def realbench_brief(*, task_id: str, workspace: Path) -> PlanningBrief:
    """Planning inputs for a RealBench workspace."""
    ws = Path(workspace)
    tree = _read_text(ws / "public_design" / "tree.txt", limit=6000)
    package_json: dict[str, Any] = {}
    pkg_path = ws / "public_design" / "package.json"
    if pkg_path.is_file():
        try:
            raw = json.loads(pkg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                package_json = raw
        except json.JSONDecodeError:
            package_json = {}
    return PlanningBrief(
        task_id=task_id,
        documents=[
            ("TASK.md", _read_text(ws / "TASK.md", limit=4000)),
            ("REQUIREMENTS.md", _read_text(ws / "REQUIREMENTS.md", limit=8000)),
            ("public_design/tree.txt", tree),
        ],
        modules=parse_expected_modules(tree),
        exports=parse_package_exports(package_json),
    )


def build_planner_prompt(
    *,
    task_id: str,
    workspace: Path,
    agent_backend: str,
    max_milestones: int = MAX_MILESTONES,
) -> str:
    """Render the risk-first decomposition prompt from public inputs only."""
    return render_planner_prompt(
        brief=realbench_brief(task_id=task_id, workspace=Path(workspace)),
        agent_backend=agent_backend,
        max_milestones=max_milestones,
    )


def render_planner_prompt(
    *,
    brief: PlanningBrief,
    agent_backend: str,
    max_milestones: int = MAX_MILESTONES,
) -> str:
    """Render the risk-first decomposition prompt for any dataset's brief."""
    task_id = brief.task_id
    modules = brief.modules
    exports = brief.exports
    design_sections = "\n\n".join(
        f"## {title}\n{text or '(missing)'}" for title, text in brief.documents
    )

    return f"""You plan milestones for an autonomous repository-implementation run.

# The only reason a milestone may exist
A milestone is a *risk gate*. Create one only when some decision would, if made
wrong, silently invalidate the work that comes after it — shared keys/IDs that
must line up across modules, a base class or protocol every module implements,
a serialization/return-shape contract, a cross-module invariant.
If nothing in this task carries that kind of blast radius, return exactly ONE
milestone covering the whole repository. One milestone is the expected answer.

# Forbidden ways to split
- Splitting by directory, package, or file count.
- Milestones that only read, review, document, or "map" the code.
- Milestones whose failure would merely cost some local rework.
- More than {max_milestones} milestones.

# What every milestone must carry
1. `risk_rationale`: what breaks downstream if this milestone is wrong. Leave it
   empty only for a single whole-repository milestone.
2. `acceptance`: how a machine decides the milestone is done.
   - `criteria`: observable statements about behaviour.
   - `corner_cases`: edge inputs/states that must not regress.
   - `checks`: machine-checkable items, types limited to
     `import`, `export`, `callable_or_class`, `module_file_exists`.
     Use `required_levels` from ["discovery","implementation","integration"].
     Prefer checks that pin the risky contract itself (exact symbols that later
     milestones import), not restatements of the file tree.
     Pin a symbol at the module the public design says exports it (a package
     root when the UML lists it there), not only at its definition site: a
     symbol that is importable from its private module but missing from the
     documented one still breaks every consumer.
3. `agents`: 1..{MAX_AGENTS_PER_MILESTONE} agents that will run **in sequence** on
   the same workspace as one subgraph. Each needs `role_id`, `title`, `mandate`
   (its specific instruction), optional `focus_paths`, plus `max_tokens`,
   `max_steps`, `timeout_seconds`. Only add a second agent when it has work the
   first one cannot do; every agent must edit the repository.

Executing backend: `{agent_backend}`. {brief.acceptance_note}

# Task `{task_id}`
{design_sections}

## Parsed modules
{json.dumps(modules[:60], ensure_ascii=False)}

## Parsed UML exports
{json.dumps(exports, ensure_ascii=False)[:4000]}

# Output
Return ONLY a JSON object:
{{
  "rationale": "why this shape (mention why you did or did not split)",
  "milestones": [
    {{
      "milestone_id": "snake_case_id",
      "title": "...",
      "objective": "what to build, concretely",
      "risk_rationale": "what downstream work breaks if this is wrong",
      "role": "discovery|implementation|integration",
      "depends_on": ["earlier_milestone_id"],
      "focus_paths": ["pkg/mod.py"],
      "acceptance": {{
        "criteria": ["..."],
        "corner_cases": ["..."],
        "checks": [
          {{"type": "callable_or_class", "module": "pkg.mod", "symbol": "Name",
            "required_levels": ["implementation", "integration"]}}
        ]
      }},
      "agents": [
        {{"role_id": "implementer", "title": "...", "mandate": "...",
          "focus_paths": ["pkg/"], "max_tokens": 8192, "max_steps": 12,
          "timeout_seconds": 1200}}
      ]
    }}
  ]
}}
"""


def planner_enabled(explicit: bool | None = None) -> bool:
    """Risk-first planning is the default; the template split is not a mode.

    Template segmentation cuts by tree shape, which is not a decomposition
    hypothesis worth measuring. It survives only as the fail-closed fallback when
    the planner itself is unavailable, and set ``ADAMAS_REALBENCH_DYNAMIC_PLAN=0``
    to force it for a debugging session.
    """
    if explicit is not None:
        return bool(explicit)
    raw = os.getenv(PLAN_ENV_FLAG, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def plan_milestones(
    *,
    task_id: str,
    workspace: Path,
    agent_backend: str,
    enable: bool | None = None,
    max_milestones: int = MAX_MILESTONES,
    brief: PlanningBrief | None = None,
) -> MilestonePlanDraft | None:
    """Ask an LLM for a risk-first milestone DAG; ``None`` when unavailable."""
    if not planner_enabled(enable):
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key or not base_url:
        return None

    prompt = render_planner_prompt(
        brief=brief or realbench_brief(task_id=task_id, workspace=Path(workspace)),
        agent_backend=agent_backend,
        max_milestones=max_milestones,
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
        model = (
            os.getenv(PLAN_MODEL_ENV)
            or os.getenv("CODEX_MODEL")
            or os.getenv("SMOLAGENTS_MODEL")
            or "gpt-5.4"
        )
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You decompose repository tasks into risk gates and emit "
                        "only JSON. You prefer one milestone unless a genuine "
                        "blast-radius risk exists."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = (response.choices[0].message.content or "").strip()
        draft = parse_plan_payload(content, max_milestones=max_milestones)
    except Exception:  # noqa: BLE001 — fail closed to the deterministic plan
        return None
    return draft

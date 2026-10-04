"""The author rules document: versioned entries the author reads (spec §3).

Only ``trigger``, ``rule`` and ``example`` of the ``active`` and ``trial``
entries are rendered into the prompt; ``origin``, ``added_in`` and the state
machine are bookkeeping for the evolver and the change log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

RULES_DOC_ROOT = Path("configs/author/rules_doc")
STATES = ("candidate", "trial", "active", "retired")
RENDERED_STATES = ("active", "trial")
DEFAULT_MAX_ACTIVE = 15


class RulesDocError(ValueError):
    """A rules document on disk is unusable."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    state: str
    trigger: str
    rule: str
    example: str = ""
    applies_to: tuple[str, ...] = ()
    origin: str = ""
    added_in: str = ""
    #: how many trials failed so far (second failure retires the entry, §3.2)
    trial_failures: int = 0

    def to_dict(self) -> dict:
        d = {
            "rule_id": self.rule_id,
            "state": self.state,
            "applies_to": list(self.applies_to),
            "trigger": self.trigger,
            "rule": self.rule,
            "example": self.example,
            "origin": self.origin,
            "added_in": self.added_in,
        }
        if self.trial_failures:
            d["trial_failures"] = self.trial_failures
        return d


@dataclass(frozen=True)
class RulesDoc:
    version: str
    rules: tuple[Rule, ...] = field(default_factory=tuple)

    def rendered(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.state in RENDERED_STATES)

    def get(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None


def parse_rule(payload: dict, *, source: str = "<memory>") -> Rule:
    if not isinstance(payload, dict):
        raise RulesDocError(f"{source}: rule entry must be a mapping")
    rid = str(payload.get("rule_id") or "").strip()
    if not rid:
        raise RulesDocError(f"{source}: rule_id is required")
    state = str(payload.get("state") or "candidate").strip()
    if state not in STATES:
        raise RulesDocError(f"{source}: rule {rid} has unknown state {state!r}")
    trigger = str(payload.get("trigger") or "").strip()
    rule = str(payload.get("rule") or "").strip()
    if not trigger or not rule:
        raise RulesDocError(f"{source}: rule {rid} needs both trigger and rule")
    applies = payload.get("applies_to") or []
    if isinstance(applies, str):
        applies = [applies]
    return Rule(
        rule_id=rid,
        state=state,
        trigger=trigger,
        rule=rule,
        example=str(payload.get("example") or "").rstrip(),
        applies_to=tuple(str(a) for a in applies),
        origin=str(payload.get("origin") or ""),
        added_in=str(payload.get("added_in") or ""),
        trial_failures=int(payload.get("trial_failures") or 0),
    )


def load_rules_doc(path: str | Path) -> RulesDoc:
    """Load ``<dir>/rules.yaml`` (or a rules.yaml file) into a RulesDoc."""
    p = Path(path)
    if p.is_dir():
        p = p / "rules.yaml"
    if not p.is_file():
        raise RulesDocError(f"rules document not found: {p}")
    payload = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(payload, list):  # bare list form, as the spec's example shows
        payload = {"version": p.parent.name, "rules": payload}
    rules = tuple(parse_rule(r, source=str(p)) for r in (payload.get("rules") or []))
    ids = [r.rule_id for r in rules]
    if len(ids) != len(set(ids)):
        raise RulesDocError(f"{p}: duplicate rule_id")
    return RulesDoc(version=str(payload.get("version") or p.parent.name), rules=rules)


def write_rules_doc(doc: RulesDoc, directory: str | Path) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "rules.yaml"
    out.write_text(
        yaml.safe_dump({"version": doc.version, "rules": [r.to_dict() for r in doc.rules]},
                       sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def current_version_dir(root: str | Path = RULES_DOC_ROOT) -> Path:
    """``<root>/current`` resolved (a symlink to ``versions/v<N>``)."""
    cur = Path(root) / "current"
    if not cur.exists():
        raise RulesDocError(f"no current rules document under {root}")
    return cur.resolve()


def render_rules(rules: tuple[Rule, ...] | list[Rule]) -> str:
    """The text the author reads: numbered trigger / rule / example blocks."""
    shown = [r for r in rules if r.state in RENDERED_STATES]
    if not shown:
        return ""
    lines = ["## Additional rules for this suite", ""]
    for i, r in enumerate(shown, 1):
        lines.append(f"{i}. When {r.trigger.rstrip('.')}: {r.rule}")
        if r.example:
            lines.append("")
            lines.append("   ```python")
            lines.extend("   " + ln if ln else "" for ln in r.example.splitlines())
            lines.append("   ```")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "DEFAULT_MAX_ACTIVE", "RENDERED_STATES", "RULES_DOC_ROOT", "STATES", "Rule", "RulesDoc",
    "RulesDocError", "current_version_dir", "load_rules_doc", "parse_rule", "render_rules",
    "write_rules_doc",
]

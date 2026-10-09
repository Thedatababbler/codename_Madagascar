"""The judge: one temperature-0 model call per milestone (memory spec §3.1 step 1).

It reads the bank's SKILL.md, the active categories (id, name, definition,
signals only), the domain-tag vocabulary and the milestone's objective,
acceptance and relevant document paragraphs, and returns a fixed JSON object.
The skill text it is shown ends with a ``skill_version`` line; the judge must
echo it, which shows it read the skill to the end. Any parse failure, an
unknown category or tag, more categories than allowed, or a wrong
``skill_version`` raises ``JudgeError`` -- nothing is skipped silently.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestra.memory.store import MemoryView, sha8, sha256_text

Call = Callable[[str, str], str]

SYSTEM = (
    "You are a judge that maps one development milestone onto a fixed category catalogue. "
    "Follow the skill document exactly and answer with one JSON object only."
)


class JudgeError(RuntimeError):
    """The judge's answer could not be used; the run stops."""


@dataclass
class Judgement:
    bank: str
    domain_tags: list[str]
    selected: list[dict[str, str]]
    skill_version: str
    input_sha: str
    raw: str
    prompt: str = field(repr=False, default="")

    @property
    def category_ids(self) -> list[str]:
        return [s["category_id"] for s in self.selected]

    def to_dict(self) -> dict[str, Any]:
        return {"bank": self.bank, "domain_tags": self.domain_tags, "selected": self.selected,
                "skill_version": self.skill_version, "input_sha256": self.input_sha, "raw_output": self.raw}


def skill_block(skill: str) -> tuple[str, str]:
    """(the text the judge sees, its version): the skill body followed by its version line."""
    version = sha8(skill)
    return f"{skill.rstrip()}\n\nskill_version: {version}\n", version


def category_block(categories: list[Mapping[str, Any]]) -> str:
    keep = [{k: c.get(k) for k in ("category_id", "name", "definition", "signals")} for c in categories]
    return json.dumps(keep, ensure_ascii=False, indent=1)


def build_prompt(view: MemoryView, bank: str, *, milestone_text: str, docs_excerpt: str, max_categories: int) -> tuple[str, str]:
    skill, version = skill_block(view.skill(bank))
    cats = view.active_categories(bank)
    prompt = (
        "# Skill\n" + skill + "\n"
        "# Category catalogue (active categories only)\n" + category_block(cats) + "\n\n"
        "# Domain tag vocabulary\n" + json.dumps(view.domain_tags()) + "\n\n"
        "# Milestone\n" + milestone_text.strip() + "\n\n"
        "# Relevant document paragraphs\n" + (docs_excerpt.strip() or "(none found for this milestone's files)") + "\n\n"
        "# Answer\nReturn exactly one JSON object:\n"
        '{"domain_tags": [<tags from the vocabulary>], '
        '"selected": [{"category_id": "<id from the catalogue>", "reason": "<one sentence>", "confidence": "high|medium|low"}], '
        '"skill_version": "<the skill_version line of the skill>"}\n'
        f"Select at most {max_categories} categories; an empty list is a valid answer.\n"
    )
    return prompt, version


def parse(raw: str) -> dict[str, Any]:
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise JudgeError("judge answer contains no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge answer is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JudgeError("judge answer is not a JSON object")
    return data


def judge(view: MemoryView, bank: str, *, milestone_text: str, docs_excerpt: str, max_categories: int = 3,
          call: Call | None = None) -> Judgement:
    prompt, version = build_prompt(view, bank, milestone_text=milestone_text, docs_excerpt=docs_excerpt, max_categories=max_categories)
    if call is None:
        from orchestra.control.evolution.evolver import default_call as call  # temperature 0
    raw = call(SYSTEM, prompt)
    data = parse(raw)
    if str(data.get("skill_version") or "") != version:
        raise JudgeError(f"skill_version {data.get('skill_version')!r} does not match the injected SKILL.md ({version})")
    known = {c["category_id"] for c in view.active_categories(bank)}
    vocab = set(view.domain_tags())
    tags = [str(t) for t in (data.get("domain_tags") or [])]
    bad_tags = [t for t in tags if t not in vocab]
    if bad_tags:
        raise JudgeError(f"domain tags outside the vocabulary: {bad_tags}")
    selected = []
    for s in data.get("selected") or []:
        if not isinstance(s, Mapping):
            raise JudgeError("selected entries must be objects")
        cid = str(s.get("category_id") or "")
        if cid not in known:
            raise JudgeError(f"judge selected a category outside the catalogue: {cid!r}")
        selected.append({"category_id": cid, "reason": str(s.get("reason") or ""), "confidence": str(s.get("confidence") or "")})
    if len(selected) > max_categories:
        raise JudgeError(f"judge selected {len(selected)} categories, at most {max_categories} allowed")
    return Judgement(bank=bank, domain_tags=tags, selected=selected, skill_version=version,
                     input_sha=sha256_text(SYSTEM + "\n" + prompt), raw=raw, prompt=prompt)


__all__ = ["Judgement", "JudgeError", "SYSTEM", "build_prompt", "judge", "parse", "skill_block"]

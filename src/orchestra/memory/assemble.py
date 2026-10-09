"""Assembly (memory spec §3.1 step 3): program only.

Recalled pitfalls go under a fixed heading, each line tagged ``[MEM:<id>]``;
pattern instructions go under the next heading; the prompt ends with the fixed
``MEMORY_ACK`` requirement. ``parse_tags`` / ``parse_ack`` read them back for
the delivery check.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

PITFALL_HEADING = "## 可能的坑（来自过去的经验）"
PATTERN_HEADING = "## 本阶段的额外要求"
RULES_HEADING = "## 出题经验（来自过去的经验）"
ACK_REQUIREMENT = (
    "在最终回复中给出一行 `MEMORY_ACK: <你阅读过的 MEM 标签，逗号分隔>`。"
    " (In your final reply, include exactly one line `MEMORY_ACK: <the MEM tags you read, comma-separated>`, "
    "listing every tag shown in the memory sections above.)"
)
TAG_RE = re.compile(r"\[MEM:([A-Za-z0-9][A-Za-z0-9_\-]*)\]")
ACK_RE = re.compile(r"^\s*[`*]*MEMORY_ACK:\s*(.*?)[`*]*\s*$", re.M)


def tag(entry_id: str) -> str:
    return f"[MEM:{entry_id}]"


def pitfall_line(p: Mapping[str, Any]) -> str:
    return (f"- {tag(p['pitfall_id'])} Symptom: {str(p.get('symptom') or '').strip()} "
            f"Cause: {str(p.get('cause') or '').strip()} Lesson: {str(p.get('lesson') or '').strip()}")


def first_pass_block(pitfalls: Iterable[Mapping[str, Any]], patterns: Iterable[Mapping[str, Any]]) -> str:
    """The implementer's memory section ('' when nothing was recalled)."""
    pits = list(pitfalls)
    pats = [p for p in patterns if ((p.get("action") or {}).get("instruction") or "").strip()]
    parts: list[str] = []
    if pits:
        parts.append(PITFALL_HEADING + "\n" + "\n".join(pitfall_line(p) for p in pits))
    if pats:
        parts.append(PATTERN_HEADING + "\n" + "\n".join(
            f"- {tag(p['pattern_id'])} {str(p['action']['instruction']).strip()}" for p in pats))
    return "\n\n".join(parts)


def rules_block(rules: Iterable[Mapping[str, Any]]) -> str:
    rs = list(rules)
    if not rs:
        return ""
    lines = []
    for r in rs:
        trig = str(r.get("trigger") or "").strip()
        lines.append(f"- {tag(r['rule_id'])} " + (f"When {trig}: " if trig else "") + str(r.get("rule") or "").strip())
    return RULES_HEADING + "\n" + "\n".join(lines)


def repair_block(pattern_id: str, text: str) -> str:
    return PATTERN_HEADING + "\n" + f"- {tag(pattern_id)} {text.strip()}"


def strip_first_pass(messages: list[dict]) -> list[dict]:
    """A candidate's copy of the first run's messages without the first-pass memory (memory spec §2.2).

    The section is the tail of the system message (``AgentDraft.memory_block``)
    and the acknowledgement line closes the contract's user message; a repair
    pattern's own section arrives in a later feedback message and is kept.
    """
    out = [dict(m) for m in messages]
    for i, m in enumerate(out[:2]):
        text = str(m.get("content") or "")
        if i == 0:
            cut = [text.find("\n\n" + h) for h in (PITFALL_HEADING, PATTERN_HEADING)]
            cut = [c for c in cut if c >= 0 and TAG_RE.search(text[c:])]
            if cut:
                text = text[: min(cut)]
        text = text.replace("\n\n" + ACK_REQUIREMENT, "")
        out[i]["content"] = text
    return out


def parse_tags(text: str) -> list[str]:
    seen: list[str] = []
    for t in TAG_RE.findall(text or ""):
        if t not in seen:
            seen.append(t)
    return seen


def parse_ack(text: str) -> list[str] | None:
    """The tags of the last ``MEMORY_ACK:`` line, or None when there is none."""
    lines = ACK_RE.findall(text or "")
    if not lines:
        return None
    raw = lines[-1]
    out = []
    for part in re.split(r"[,\s]+", raw):
        part = part.strip().strip("`*[]")
        if part.startswith("MEM:"):
            part = part[4:]
        if part and part.lower() not in ("none", "n/a"):
            out.append(part)
    return out


__all__ = [
    "ACK_REQUIREMENT", "PATTERN_HEADING", "PITFALL_HEADING", "RULES_HEADING", "TAG_RE", "first_pass_block", "parse_ack", "parse_tags",
    "pitfall_line", "repair_block", "rules_block", "strip_first_pass", "tag",
]
